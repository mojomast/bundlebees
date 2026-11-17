"""Rails ledger runtime: append-only SQLite event store (schema rizzstack.rails/1).

One database holds append-only local event_rows (canonical rails envelope plus
legacy vibeline compatibility columns), imported_origin_rows (verbatim origin
bytes) and append-only import receipts; mutable projections (import cursors,
dedupe index) live in the same database and are rebuildable. Local writers
serialize through SQLite (BEGIN IMMEDIATE, busy timeout 5000, WAL,
synchronous=FULL on local disk); commit precedes acknowledgment. Both chains
are independently verifiable. Readers use URI mode=ro plus query_only.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import time
import urllib.parse
import uuid

from rizzstack_rails import contracts, privacy

_BUSY_BACKOFF = (0.05, 0.2, 0.4)

_TARGET_KINDS = ("feedback.recorded", "intervention.requested", "intervention.result")

_SCHEMA_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    (
        "CREATE TABLE IF NOT EXISTS event_rows ("
        " ingest_seq INTEGER PRIMARY KEY,"
        " chain_id TEXT NOT NULL,"
        " seq INTEGER NOT NULL,"
        " event_id TEXT NOT NULL UNIQUE,"
        " envelope_json TEXT NOT NULL,"
        " id TEXT NOT NULL,"
        " ts TEXT NOT NULL,"
        " task_id TEXT NOT NULL,"
        " actor TEXT NOT NULL,"
        " parent TEXT NOT NULL,"
        " kind TEXT NOT NULL,"
        " ac_id TEXT NOT NULL,"
        " result TEXT NOT NULL,"
        " why TEXT NOT NULL,"
        " tokens_in,"
        " tokens_out,"
        " sha TEXT NOT NULL,"
        " prev_hash TEXT NOT NULL,"
        " hash TEXT NOT NULL,"
        " rails_hash TEXT NOT NULL,"
        " UNIQUE(chain_id, seq))"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS event_rows_no_update BEFORE UPDATE ON event_rows"
        " BEGIN SELECT RAISE(ABORT, 'rails: event_rows is append-only'); END"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS event_rows_no_delete BEFORE DELETE ON event_rows"
        " BEGIN SELECT RAISE(ABORT, 'rails: event_rows is append-only'); END"
    ),
    (
        "CREATE TABLE IF NOT EXISTS imported_origin_rows ("
        " chain_id TEXT NOT NULL,"
        " seq INTEGER NOT NULL,"
        " event_id TEXT NOT NULL,"
        " origin_hash TEXT NOT NULL,"
        " origin_bytes TEXT NOT NULL,"
        " PRIMARY KEY(chain_id, seq))"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS imported_origin_no_update BEFORE UPDATE"
        " ON imported_origin_rows"
        " BEGIN SELECT RAISE(ABORT, 'rails: imported_origin_rows is append-only'); END"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS imported_origin_no_delete BEFORE DELETE"
        " ON imported_origin_rows"
        " BEGIN SELECT RAISE(ABORT, 'rails: imported_origin_rows is append-only'); END"
    ),
    (
        "CREATE TABLE IF NOT EXISTS import_receipts ("
        " receipt_seq INTEGER PRIMARY KEY,"
        " chain_id TEXT NOT NULL,"
        " through_seq INTEGER NOT NULL,"
        " head_hash TEXT NOT NULL,"
        " origin_count INTEGER NOT NULL,"
        " imported_at TEXT NOT NULL)"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS import_receipts_no_update BEFORE UPDATE"
        " ON import_receipts"
        " BEGIN SELECT RAISE(ABORT, 'rails: import_receipts is append-only'); END"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS import_receipts_no_delete BEFORE DELETE"
        " ON import_receipts"
        " BEGIN SELECT RAISE(ABORT, 'rails: import_receipts is append-only'); END"
    ),
    (
        "CREATE TABLE IF NOT EXISTS import_cursors ("
        " chain_id TEXT PRIMARY KEY,"
        " next_seq INTEGER NOT NULL,"
        " state TEXT NOT NULL,"
        " head_hash TEXT)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS dedupe_index ("
        " chain_id TEXT NOT NULL,"
        " dedupe_key TEXT NOT NULL,"
        " event_id TEXT NOT NULL,"
        " semantic_sha TEXT NOT NULL,"
        " PRIMARY KEY(chain_id, dedupe_key))"
    ),
    (
        "CREATE VIEW IF NOT EXISTS events AS SELECT id, ts, task_id, actor, parent,"
        " kind, ac_id, result, why, tokens_in, tokens_out, sha, prev_hash, hash"
        " FROM event_rows"
    ),
    (
        "CREATE VIEW IF NOT EXISTS imported_events AS SELECT chain_id, seq, event_id,"
        " origin_hash, origin_bytes FROM imported_origin_rows"
    ),
)

_NETWORK_SHARE_PREFIXES = ("//", "\\\\")


class LedgerUnavailable(Exception):
    pass


class ReadonlyRefusal(contracts.RailsRefusal):
    def __init__(self, message):
        super().__init__(message, code="readonly")


class IdempotencyConflict(contracts.RailsRefusal):
    status_code = 409

    def __init__(self, message):
        super().__init__(message, code="idempotency_conflict")


class AuthorityRefusal(contracts.RailsRefusal):
    def __init__(self, message):
        super().__init__(message, code="authority")


class RawIngestionRefusal(contracts.RailsRefusal):
    def __init__(self, message):
        super().__init__(message, code="raw_ingestion")


class SourceGap(contracts.RailsRefusal):
    def __init__(self, message):
        super().__init__(message, code="source_gap")


class SourceFork(contracts.RailsRefusal):
    def __init__(self, message):
        super().__init__(message, code="source_fork")


class ParentCycleRefusal(contracts.RailsRefusal):
    def __init__(self, message):
        super().__init__(message, code="parent_cycle")


def _scan_raw_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key in contracts.RAW_FIELDS:
                raise RawIngestionRefusal("raw ingestion field %r is refused" % (key,))
            _scan_raw_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _scan_raw_fields(item)


def _legacy_fields(envelope, envelope_json, rails_digest, legacy_prev):
    payload = envelope["payload"]
    result = ""
    for key in ("verdict", "outcome", "result", "decision"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            result = value
            break
    tokens_in = payload.get("tokens_in")
    tokens_out = payload.get("tokens_out")
    row = {
        "id": envelope["event_id"],
        "ts": envelope["recorded_at"],
        "task_id": envelope["scope"].get("task_id") or "",
        "actor": envelope["authority"],
        "parent": envelope["lane"].get("parent_event_id") or "",
        "kind": envelope["kind"],
        "ac_id": payload.get("ac_id") if isinstance(payload.get("ac_id"), str) else "",
        "result": result,
        "why": envelope_json,
        "tokens_in": tokens_in
        if isinstance(tokens_in, int) and not isinstance(tokens_in, bool)
        else "",
        "tokens_out": tokens_out
        if isinstance(tokens_out, int) and not isinstance(tokens_out, bool)
        else "",
        "sha": rails_digest,
        "prev_hash": legacy_prev,
    }
    row["hash"] = contracts.legacy_hash(legacy_prev, row)
    return row


class RailsLedger:
    def __init__(
        self,
        path,
        read_only=False,
        min_free_bytes=0,
        allowed_authorities=None,
        hooks=None,
    ):
        self.path = os.path.abspath(path)
        self.read_only = bool(read_only)
        self.min_free_bytes = int(min_free_bytes)
        self.allowed_authorities = (
            frozenset(allowed_authorities)
            if allowed_authorities is not None
            else None
        )
        self.hooks = dict(hooks) if hooks else {}
        self._healthy = True
        self._last_error = None
        self._reject_network_share()
        if self.read_only:
            if not os.path.exists(self.path):
                raise ReadonlyRefusal("read-only open refused: database missing at %s" % self.path)
            uri = "file:" + urllib.parse.quote(self.path) + "?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True)
            self._conn.row_factory = sqlite3.Row
            self._conn.isolation_level = None
            self._conn.execute("PRAGMA query_only=ON")
            self._require_rails_schema()
            self._load_identity_readonly()
        else:
            self._conn = sqlite3.connect(self.path)
            self._conn.row_factory = sqlite3.Row
            self._conn.isolation_level = None
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
            self._init_identity()

    def _reject_network_share(self):
        if self.path.startswith(_NETWORK_SHARE_PREFIXES):
            raise contracts.RailsRefusal(
                "network-share sqlite is refused: %s" % self.path, code="network_share"
            )

    def _require_rails_schema(self):
        try:
            row = self._conn.execute(
                "SELECT value FROM ledger_meta WHERE key = 'chain_id'"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise ReadonlyRefusal("read-only open refused: not a rails ledger (%s)" % (exc,))
        if row is None:
            raise ReadonlyRefusal("read-only open refused: not a rails ledger")

    def _load_identity_readonly(self):
        rows = self._conn.execute(
            "SELECT key, value FROM ledger_meta WHERE key IN"
            " ('machine_alias','ledger_uuid','chain_id')"
        ).fetchall()
        stored = {r["key"]: r["value"] for r in rows}
        missing = {"machine_alias", "ledger_uuid", "chain_id"} - set(stored)
        if missing:
            raise ReadonlyRefusal(
                "read-only open refused: ledger identity incomplete (%s)" % ",".join(sorted(missing))
            )
        self.machine_alias = stored["machine_alias"]
        self.ledger_uuid = stored["ledger_uuid"]
        self.chain_id = stored["chain_id"]

    def _init_schema(self):
        def work(conn):
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)

        self._write_tx(work, fire_hooks=False)

    def _init_identity(self):
        def work(conn):
            rows = conn.execute(
                "SELECT key, value FROM ledger_meta WHERE key IN"
                " ('machine_alias','ledger_uuid','chain_id')"
            ).fetchall()
            stored = {r["key"]: r["value"] for r in rows}
            if len(stored) == 3:
                self.machine_alias = stored["machine_alias"]
                self.ledger_uuid = stored["ledger_uuid"]
                self.chain_id = stored["chain_id"]
                return "loaded"
            if stored:
                raise contracts.RailsRefusal(
                    "ledger identity is partially initialized", code="identity"
                )
            self.machine_alias = "m-" + uuid.uuid4().hex[:12]
            self.ledger_uuid = str(uuid.uuid4())
            self.chain_id = self.machine_alias + "-" + self.ledger_uuid
            for key, value in (
                ("machine_alias", self.machine_alias),
                ("ledger_uuid", self.ledger_uuid),
                ("chain_id", self.chain_id),
                ("created_at", contracts.utc_now()),
            ):
                conn.execute(
                    "INSERT INTO ledger_meta(key, value) VALUES (?, ?)", (key, value)
                )
            return "created"

        self._write_tx(work, fire_hooks=False)

    def _mark_unhealthy(self, message):
        self._healthy = False
        self._last_error = message

    def _probe_capacity(self):
        if self.min_free_bytes <= 0:
            return
        free = shutil.disk_usage(os.path.dirname(self.path) or os.getcwd()).free
        if free < self.min_free_bytes:
            self._mark_unhealthy(
                "disk capacity below floor: %d free < %d required" % (free, self.min_free_bytes)
            )
            raise LedgerUnavailable(self._last_error)
        self._healthy = True
        self._last_error = None

    def _ensure_writable(self):
        if self.read_only:
            raise ReadonlyRefusal("append refused on read-only reader")

    def _classify_operational(self, exc):
        text = str(exc).lower()
        if "full" in text or "disk i/o" in text:
            self._mark_unhealthy("sqlite storage failure: %s" % (exc,))
            raise LedgerUnavailable(self._last_error)
        raise exc

    def _write_tx(self, work, fire_hooks=True):
        attempt = 0
        while True:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if ("locked" in text or "busy" in text) and attempt < len(_BUSY_BACKOFF):
                    time.sleep(_BUSY_BACKOFF[attempt])
                    attempt += 1
                    continue
                self._classify_operational(exc)
            try:
                result = work(self._conn)
                before = self.hooks.get("before_commit") if fire_hooks else None
                if before is not None:
                    before()
                self._conn.execute("COMMIT")
                after = self.hooks.get("after_commit") if fire_hooks else None
                if after is not None:
                    after()
                return result
            except sqlite3.OperationalError as exc:
                self._safe_rollback()
                text = str(exc).lower()
                if ("locked" in text or "busy" in text) and attempt < len(_BUSY_BACKOFF):
                    time.sleep(_BUSY_BACKOFF[attempt])
                    attempt += 1
                    continue
                self._classify_operational(exc)
            except BaseException:
                self._safe_rollback()
                raise

    def _safe_rollback(self):
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    def _check_authority(self, authority):
        if self.allowed_authorities is not None and authority not in self.allowed_authorities:
            raise AuthorityRefusal(
                "authority %r is outside the authenticated producer mapping" % (authority,)
            )

    def _check_target_exists(self, conn, payload):
        target = payload.get("target_event_id")
        if not isinstance(target, str) or not target:
            return
        row = conn.execute(
            "SELECT 1 FROM event_rows WHERE event_id = ?", (target,)
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT 1 FROM imported_origin_rows WHERE event_id = ?", (target,)
            ).fetchone()
        if row is None:
            raise contracts.PayloadRefusal("unknown target event %r" % (target,), code="target")

    def _check_parent_cycle(self, conn, lane):
        lane_id = lane.get("lane_id")
        parent_lane_id = lane.get("parent_lane_id")
        if not lane_id or not parent_lane_id:
            return
        seen = {lane_id}
        current = parent_lane_id
        depth = 0
        while current is not None and depth < 64:
            if current in seen:
                raise ParentCycleRefusal("lane parent cycle through %r" % (current,))
            seen.add(current)
            row = conn.execute(
                "SELECT envelope_json FROM event_rows WHERE kind IN ('lane.open','lane.spawn')"
                " AND json_extract(envelope_json, '$.lane.lane_id') = ?"
                " ORDER BY seq DESC LIMIT 1",
                (current,),
            ).fetchone()
            if row is None:
                break
            current = json.loads(row["envelope_json"])["lane"].get("parent_lane_id")
            depth += 1

    def _append_locked(self, conn, kind, source, scope, payload, authority, dedupe_key,
                       lane, occurred_at, duration_ms, timing_quality, redaction_policy):
        self._check_target_exists(conn, payload)
        self._check_parent_cycle(conn, lane or {})
        lane_base = {"lane_id": None, "parent_lane_id": None, "parent_event_id": None}
        lane_base.update(lane or {})
        if redaction_policy is None:
            redaction_policy = {"mode": "structured_safe", "path_detail": "omitted"}
        body = {
            "schema": contracts.SCHEMA,
            "chain_id": self.chain_id,
            "kind": kind,
            "source": source,
            "scope": scope,
            "lane": lane_base,
            "occurred_at": occurred_at,
            "duration_ms": duration_ms,
            "timing_quality": timing_quality,
            "authority": authority,
            "payload": payload,
            "redaction_policy": redaction_policy,
        }
        candidate_sha = hashlib.sha256(contracts.canonicalize(body)).hexdigest()
        if dedupe_key is not None:
            existing = conn.execute(
                "SELECT event_id, semantic_sha FROM dedupe_index"
                " WHERE chain_id = ? AND dedupe_key = ?",
                (self.chain_id, dedupe_key),
            ).fetchone()
            if existing is not None:
                if existing["semantic_sha"] != candidate_sha:
                    raise IdempotencyConflict(
                        "dedupe key %r already recorded with different semantic bytes"
                        % (dedupe_key,)
                    )
                stored = json.loads(
                    conn.execute(
                        "SELECT envelope_json FROM event_rows WHERE event_id = ?",
                        (existing["event_id"],),
                    ).fetchone()["envelope_json"]
                )
                return {
                    "event_id": stored["event_id"],
                    "seq": stored["seq"],
                    "hash": stored["hash"],
                    "recorded_at": stored["recorded_at"],
                    "chain_id": self.chain_id,
                    "duplicated": True,
                }
        row = conn.execute(
            "SELECT seq, rails_hash FROM event_rows WHERE chain_id = ?"
            " ORDER BY seq DESC LIMIT 1",
            (self.chain_id,),
        ).fetchone()
        head_seq = row["seq"] if row else 0
        prev_hash = row["rails_hash"] if row else contracts.GENESIS
        seq = head_seq + 1
        event_id = self.chain_id + ":" + str(seq)
        recorded_at = contracts.utc_now()
        lane_full = dict(lane_base)
        orphan = False
        if lane_full.get("parent_event_id"):
            parent_row = conn.execute(
                "SELECT 1 FROM event_rows WHERE event_id = ?", (lane_full["parent_event_id"],)
            ).fetchone()
            if parent_row is None:
                parent_row = conn.execute(
                    "SELECT 1 FROM imported_origin_rows WHERE event_id = ?",
                    (lane_full["parent_event_id"],),
                ).fetchone()
            orphan = parent_row is None
        lane_full["orphan"] = orphan
        envelope = dict(body)
        envelope["seq"] = seq
        envelope["event_id"] = event_id
        envelope["prev_hash"] = prev_hash
        envelope["recorded_at"] = recorded_at
        envelope["lane"] = lane_full
        rails_digest = contracts.rails_hash(prev_hash, envelope)
        envelope["hash"] = rails_digest
        envelope_json = contracts.canonicalize(envelope).decode("ascii")
        legacy_prev = "genesis"
        last = conn.execute(
            "SELECT hash FROM event_rows ORDER BY ingest_seq DESC LIMIT 1"
        ).fetchone()
        if last is not None:
            legacy_prev = last["hash"]
        legacy = _legacy_fields(envelope, envelope_json, rails_digest, legacy_prev)
        ingest_seq = conn.execute(
            "SELECT COALESCE(MAX(ingest_seq), 0) + 1 FROM event_rows"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO event_rows (ingest_seq, chain_id, seq, event_id, envelope_json,"
            " id, ts, task_id, actor, parent, kind, ac_id, result, why, tokens_in,"
            " tokens_out, sha, prev_hash, hash, rails_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ingest_seq,
                self.chain_id,
                seq,
                event_id,
                envelope_json,
                legacy["id"],
                legacy["ts"],
                legacy["task_id"],
                legacy["actor"],
                legacy["parent"],
                legacy["kind"],
                legacy["ac_id"],
                legacy["result"],
                legacy["why"],
                legacy["tokens_in"],
                legacy["tokens_out"],
                legacy["sha"],
                legacy["prev_hash"],
                legacy["hash"],
                rails_digest,
            ),
        )
        if dedupe_key is not None:
            conn.execute(
                "INSERT INTO dedupe_index (chain_id, dedupe_key, event_id, semantic_sha)"
                " VALUES (?, ?, ?, ?)",
                (self.chain_id, dedupe_key, event_id, candidate_sha),
            )
        return {
            "event_id": event_id,
            "seq": seq,
            "hash": rails_digest,
            "recorded_at": recorded_at,
            "chain_id": self.chain_id,
            "duplicated": False,
        }

    def append(self, kind, *, source, scope, payload, authority, dedupe_key,
               lane=None, occurred_at=None, duration_ms=None,
               timing_quality="unknown", redaction_policy=None):
        self._ensure_writable()
        self._probe_capacity()
        self._check_authority(authority)
        _scan_raw_fields(payload)
        source = privacy.scrub_source(source)
        scope = privacy.scrub_structure(scope)
        payload = privacy.scrub_structure(payload)
        lane = privacy.scrub_structure(lane) if lane else None
        contracts.validate_event(
            kind,
            source,
            scope,
            payload,
            authority,
            lane,
            occurred_at,
            duration_ms,
            timing_quality,
            redaction_policy,
        )
        return self._write_tx(
            lambda conn: self._append_locked(
                conn,
                kind,
                source,
                scope,
                payload,
                authority,
                dedupe_key,
                lane,
                occurred_at,
                duration_ms,
                timing_quality,
                redaction_policy,
            )
        )

    def head(self):
        row = self._conn.execute(
            "SELECT chain_id, seq, rails_hash FROM event_rows WHERE chain_id = ?"
            " ORDER BY seq DESC LIMIT 1",
            (self.chain_id,),
        ).fetchone()
        if row is None:
            return {"chain_id": self.chain_id, "seq": 0, "hash": contracts.GENESIS}
        return {"chain_id": row["chain_id"], "seq": row["seq"], "hash": row["rails_hash"]}

    def query(self, sql, params=()):
        stripped = sql.lstrip().lower()
        if not (stripped.startswith("select") or stripped.startswith("pragma") or stripped.startswith("with")):
            raise contracts.RailsRefusal("only read queries are permitted", code="read_path")
        cur = self._conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def health(self):
        return {"healthy": self._healthy, "last_error": self._last_error}

    def _verify_rails_chain(self, rows, chain_id, upto):
        expected_prev = contracts.GENESIS
        expected_seq = 1
        for row in rows:
            if upto is not None and row["seq"] > upto:
                break
            env = json.loads(row["origin_bytes" if "origin_bytes" in row.keys() else "envelope_json"])
            if env["chain_id"] != chain_id or env["seq"] != expected_seq:
                return {"ok": False, "at_seq": expected_seq, "reason": "ordering"}
            if env["event_id"] != chain_id + ":" + str(expected_seq):
                return {"ok": False, "at_seq": expected_seq, "reason": "event_id"}
            if env["prev_hash"] != expected_prev:
                return {"ok": False, "at_seq": expected_seq, "reason": "prev_hash"}
            without_hash = {k: v for k, v in env.items() if k != "hash"}
            if env.get("hash") != contracts.rails_hash(expected_prev, without_hash):
                return {"ok": False, "at_seq": expected_seq, "reason": "hash_mismatch"}
            expected_prev = env["hash"]
            expected_seq += 1
        return {"ok": True, "checked_from": 1, "checked_to": expected_seq - 1, "head_hash": expected_prev}

    def verify_chain(self, chain_id=None, upto=None):
        if chain_id is None or chain_id == self.chain_id:
            rows = self._conn.execute(
                "SELECT * FROM event_rows WHERE chain_id = ? ORDER BY seq",
                (self.chain_id,),
            ).fetchall()
            head = self.head()
            rails_report = self._verify_rails_chain(rows, self.chain_id, upto)
            legacy_ok, legacy_to = self._verify_legacy_chain(upto)
            partial = upto is not None and upto < head["seq"]
            return {
                "chain_id": self.chain_id,
                "ok": rails_report["ok"],
                "legacy_ok": legacy_ok,
                "checked_from": rails_report.get("checked_from", 1),
                "checked_to": rails_report.get("checked_to", 0),
                "head_seq": head["seq"],
                "head_hash": head["hash"],
                "trusted_head_seq": head["seq"],
                "trusted_head_hash": head["hash"],
                "legacy_checked_to": legacy_to,
                "partial": partial,
                "detail": rails_report,
            }
        rows = self._conn.execute(
            "SELECT seq, event_id, origin_hash, origin_bytes FROM imported_origin_rows"
            " WHERE chain_id = ? ORDER BY seq",
            (chain_id,),
        ).fetchall()
        report = self._verify_rails_chain(rows, chain_id, upto)
        head_row = self._conn.execute(
            "SELECT seq, origin_hash FROM imported_origin_rows WHERE chain_id = ?"
            " ORDER BY seq DESC LIMIT 1",
            (chain_id,),
        ).fetchone()
        return {
            "chain_id": chain_id,
            "ok": report["ok"],
            "legacy_ok": None,
            "checked_to": report.get("checked_to", 0),
            "head_seq": head_row["seq"] if head_row else 0,
            "head_hash": head_row["origin_hash"] if head_row else contracts.GENESIS,
            "partial": upto is not None and head_row is not None and upto < head_row["seq"],
            "detail": report,
        }

    def _verify_legacy_chain(self, upto=None):
        rows = self._conn.execute(
            "SELECT ingest_seq, id, ts, task_id, actor, parent, kind, ac_id, result,"
            " why, tokens_in, tokens_out, sha, prev_hash, hash FROM event_rows"
            " ORDER BY ingest_seq"
        ).fetchall()
        expected_prev = contracts.GENESIS
        checked = 0
        for row in rows:
            if upto is not None and row["ingest_seq"] > upto:
                break
            if row["prev_hash"] != expected_prev:
                return False, checked
            computed = contracts.legacy_hash(
                expected_prev, {key: row[key] for key in contracts.LEGACY_CHAIN_KEYS}
            )
            if computed != row["hash"]:
                return False, checked
            expected_prev = row["hash"]
            checked += 1
        return True, checked

    def backup(self, dest_path):
        self._ensure_writable()
        self._probe_capacity()
        dest_abs = os.path.abspath(dest_path)
        if os.path.exists(dest_abs):
            raise contracts.RailsRefusal(
                "backup destination already exists: %s" % dest_abs, code="backup"
            )
        dest = sqlite3.connect(dest_abs)
        try:
            self._conn.execute("PRAGMA wal_checkpoint(FULL)")
            self._conn.backup(dest)
            dest.execute("PRAGMA journal_mode=DELETE")
            dest.commit()
        finally:
            dest.close()
        restored = RailsLedger(dest_abs, read_only=True)
        local = restored.verify_chain()
        results = {"local": local}
        ok = bool(local["ok"] and local["legacy_ok"])
        for row in restored._conn.execute(
            "SELECT DISTINCT chain_id FROM imported_origin_rows"
        ).fetchall():
            report = restored.verify_chain(chain_id=row["chain_id"])
            results[row["chain_id"]] = report
            ok = ok and bool(report["ok"])
        return {"ok": ok, "restored_path": dest_abs, "chains": results}

    def import_origin_page(self, chain_id, pages):
        self._ensure_writable()
        self._probe_capacity()
        if not isinstance(chain_id, str) or not chain_id:
            raise contracts.RailsRefusal("chain_id must be a nonempty string", code="chain_id")
        if not isinstance(pages, (list, tuple)) or not pages:
            raise contracts.RailsRefusal("pages must be a nonempty list", code="pages")

        def work(conn):
            outcome = self._validate_origin_pages(conn, chain_id, pages)
            if outcome is _FORK:
                conn.execute(
                    "INSERT INTO import_cursors(chain_id, next_seq, state, head_hash)"
                    " VALUES (?, 1, 'halted', NULL)"
                    " ON CONFLICT(chain_id) DO UPDATE SET state = 'halted'",
                    (chain_id,),
                )
                return {"halted": True}
            new_rows, next_seq, head_hash = outcome
            for seq, env, text in new_rows:
                conn.execute(
                    "INSERT INTO imported_origin_rows"
                    " (chain_id, seq, event_id, origin_hash, origin_bytes)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (chain_id, seq, env["event_id"], env["hash"], text),
                )
            checkpoint_id = None
            if new_rows:
                first = new_rows[0][0]
                last = new_rows[-1][0]
                digest = hashlib.sha256(
                    "".join(text for _, _, text in new_rows).encode("ascii")
                ).hexdigest()
                receipt = self._append_locked(
                    conn,
                    "source.checkpoint",
                    {
                        "adapter": "rails_import",
                        "instance": chain_id,
                        "record_id": "import:" + chain_id + ":" + str(last),
                        "revision": head_hash,
                    },
                    {"session_id": "rails-import"},
                    {
                        "range": [first, last],
                        "cursor": last,
                        "code": "imported",
                        "count": len(new_rows),
                        "digest": digest,
                    },
                    "imported_observation",
                    "import:" + chain_id + ":" + str(last),
                    None,
                    None,
                    None,
                    "unknown",
                    None,
                )
                checkpoint_id = receipt["event_id"]
                last_receipt = conn.execute(
                    "SELECT COALESCE(MAX(receipt_seq), 0) + 1 FROM import_receipts"
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO import_receipts (receipt_seq, chain_id, through_seq,"
                    " head_hash, origin_count, imported_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (last_receipt, chain_id, last, head_hash, len(new_rows), contracts.utc_now()),
                )
            conn.execute(
                "INSERT INTO import_cursors(chain_id, next_seq, state, head_hash)"
                " VALUES (?, ?, 'active', ?)"
                " ON CONFLICT(chain_id) DO UPDATE SET next_seq = ?, head_hash = ?",
                (chain_id, next_seq, head_hash, next_seq, head_hash),
            )
            return {
                "imported": len(new_rows),
                "through_seq": next_seq - 1,
                "duplicated": not new_rows,
                "checkpoint_event_id": checkpoint_id,
            }

        result = self._write_tx(work)
        if result.get("halted"):
            raise SourceFork("source %s halted: same origin seq with different hash" % chain_id)
        return result

    def _validate_origin_pages(self, conn, chain_id, pages):
        cursor = conn.execute(
            "SELECT next_seq, state, head_hash FROM import_cursors WHERE chain_id = ?",
            (chain_id,),
        ).fetchone()
        if cursor is not None and cursor["state"] == "halted":
            raise SourceFork("source %s is halted after fork" % chain_id)
        next_seq = cursor["next_seq"] if cursor is not None else 1
        prev = cursor["head_hash"] if cursor is not None and cursor["head_hash"] else contracts.GENESIS
        if next_seq == 1:
            prev = contracts.GENESIS
        batch = {}
        new_rows = []
        for text in pages:
            if not isinstance(text, str):
                raise contracts.RailsRefusal("origin entries must be canonical text", code="origin_bytes")
            parsed = json.loads(contracts.canonicalize_text(text).decode("ascii"))
            if contracts.canonicalize(parsed).decode("ascii") != text:
                raise contracts.RailsRefusal("origin bytes are not canonical", code="origin_bytes")
            if parsed.get("schema") != contracts.SCHEMA:
                raise contracts.RailsRefusal("origin schema mismatch", code="origin_bytes")
            if parsed.get("chain_id") != chain_id:
                raise contracts.RailsRefusal("origin chain_id mismatch", code="origin_bytes")
            seq = parsed.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
                raise contracts.RailsRefusal("origin seq must be a positive integer", code="origin_bytes")
            if parsed.get("event_id") != chain_id + ":" + str(seq):
                raise contracts.RailsRefusal("origin event_id mismatch", code="origin_bytes")
            if seq < next_seq:
                known = batch.get(seq)
                if known is None:
                    stored = conn.execute(
                        "SELECT origin_hash FROM imported_origin_rows WHERE chain_id = ? AND seq = ?",
                        (chain_id, seq),
                    ).fetchone()
                    known = stored["origin_hash"] if stored is not None else None
                if known != parsed.get("hash"):
                    return _FORK
                continue
            if seq > next_seq:
                raise SourceGap(
                    "source gap for %s: missing origin seq %d..%d"
                    % (chain_id, next_seq, seq - 1)
                )
            without_hash = {k: v for k, v in parsed.items() if k != "hash"}
            if parsed.get("prev_hash") != prev or parsed.get("hash") != contracts.rails_hash(
                prev, without_hash
            ):
                return _FORK
            batch[seq] = parsed["hash"]
            new_rows.append((seq, parsed, text))
            prev = parsed["hash"]
            next_seq = seq + 1
        return new_rows, next_seq, prev

    def import_cursor(self, chain_id):
        row = self._conn.execute(
            "SELECT next_seq, state, head_hash FROM import_cursors WHERE chain_id = ?",
            (chain_id,),
        ).fetchone()
        if row is None:
            return {"next_seq": 1, "state": "active", "head_hash": None}
        return {"next_seq": row["next_seq"], "state": row["state"], "head_hash": row["head_hash"]}

    def close(self):
        self._conn.close()


_FORK = object()
