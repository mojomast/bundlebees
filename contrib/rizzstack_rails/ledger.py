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
    try:
        contracts._scan_raw_fields(value)
    except contracts.PayloadRefusal as exc:
        raise RawIngestionRefusal(str(exc)) from None


def _check_observation(kind, authority, payload):
    # Queue outcomes may be reported by the coordinator. A worker's claim
    # cannot create an owner decision, including in the legacy events view.
    if authority in ('worker_report', 'imported_observation') and (
        (kind == 'acceptance.result' and payload.get('verdict') == 'PASS') or
        (kind in ('intervention.requested', 'intervention.result') and
         payload.get('decision') in ('approved', 'completed'))
    ):
        raise AuthorityRefusal('observation cannot record approval')


def _validate_origin_content(env):
    """Prove these very bytes are admissible; never scrub/re-emit a peer row.

    /1-R02 once used a nested lane and sparse scope. Only that identifiable
    historical shape is grandfathered, and only for representable safe facts.
    A flat /1 must match the frozen contract without any compatibility repair.
    """
    base = {'schema', 'chain_id', 'kind', 'source', 'scope', 'payload', 'authority',
            'occurred_at', 'duration_ms', 'timing_quality', 'redaction_policy',
            *contracts.SERVER_ASSIGNED_KEYS}
    legacy = 'lane' in env
    fields = base | ({'lane'} if legacy else {'dedupe_key', *contracts.LANE_FIELDS})
    if env.keys() != fields:
        raise contracts.PayloadRefusal('unexpected or missing origin fields')
    _scan_raw_fields(env)
    if (privacy.scrub_structure(env) != env or not isinstance(env['source'], dict) or
            privacy.scrub_source(env['source']) != env['source']):
        raise RawIngestionRefusal('origin would require redaction')
    if not isinstance(env['payload'], dict) or not privacy.safe_origin_text(env['payload']):
        raise RawIngestionRefusal('origin contains unprojected free text')
    if env['authority'] not in ('worker_report', 'imported_observation'):
        # This transport has no authenticated mapping for higher roles. Keep
        # verifier/owner data local rather than re-labeling the committed bytes.
        raise AuthorityRefusal('untrusted origin cannot assert elevated authority')
    if not isinstance(env['recorded_at'], str) or not contracts.STAMP_RE.fullmatch(env['recorded_at']):
        raise contracts.PayloadRefusal('invalid origin timestamp')
    if legacy:
        if env['redaction_policy'] != contracts.OLD_POLICY or not isinstance(env['lane'], dict):
            raise contracts.PayloadRefusal('unknown historical envelope')
        lane = dict(env['lane'])
        if not isinstance(lane.pop('orphan', False), bool) or lane.keys() != set(contracts.LANE_FIELDS):
            raise contracts.PayloadRefusal('unknown historical lane fields')
    else:
        contracts.require_id(env['dedupe_key'], 'dedupe_key')
        lane = {k: env[k] for k in contracts.LANE_FIELDS}
    body = contracts.event_body(env['kind'], env['source'], env['scope'], env['payload'],
                                env['authority'], lane, env['occurred_at'], env['duration_ms'],
                                env['timing_quality'], env['redaction_policy'])
    if not legacy and any(value != env[k] for k, value in body.items()):
        raise contracts.PayloadRefusal('origin is not a frozen /1 envelope')
    _check_observation(env['kind'], env['authority'], body['payload'])


def _legacy_fields(envelope, envelope_json, rails_digest, legacy_prev):
    payload = envelope["payload"]
    result = ""
    for key in ("verdict", "outcome", "result", "decision", "review_verdict", "status"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            result = value
            break
    if envelope['kind'] == 'decision.recorded':
        result = payload.get('summary', result)
    if envelope['kind'] == 'acceptance.result' and envelope['authority'] != 'owner_record':
        result = 'UNVERIFIED'
    tokens_in = payload.get('input_tokens', payload.get('tokens_in'))
    tokens_out = payload.get('output_tokens', payload.get('tokens_out'))
    row = {
        "id": envelope["event_id"],
        "ts": envelope["recorded_at"],
        "task_id": envelope["scope"].get("task_id") or "",
        "actor": envelope["authority"],
        "parent": envelope.get('lane', envelope).get("parent_event_id") or "",
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
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._enable_wal()
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
            self._init_identity()

    def _enable_wal(self):
        # Concurrent first opens can both hold read locks before upgrading the
        # journal mode. SQLite may return BUSY immediately to avoid deadlock.
        # Retry this statement alone, before any ledger transaction begins.
        for attempt in range(len(_BUSY_BACKOFF) + 1):
            try:
                self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
                return
            except sqlite3.OperationalError as exc:
                primary = getattr(exc, 'sqlite_errorcode', 0) & 0xff
                if primary not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or attempt == len(_BUSY_BACKOFF):
                    raise
                time.sleep(_BUSY_BACKOFF[attempt])

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
        if not isinstance(authority, str) or (self.allowed_authorities is not None and
                                              authority not in self.allowed_authorities):
            raise AuthorityRefusal('authority outside the authenticated producer mapping')

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
                " AND coalesce(json_extract(envelope_json, '$.lane_id'),"
                " json_extract(envelope_json, '$.lane.lane_id')) = ?"
                " ORDER BY seq DESC LIMIT 1",
                (current,),
            ).fetchone()
            if row is None:
                break
            env = json.loads(row['envelope_json'])
            current = env.get('lane', env).get('parent_lane_id')
            depth += 1
        if current is not None and depth == 64:
            raise ParentCycleRefusal('lane ancestry exceeds verification limit')

    def _append_locked(self, conn, kind, source, scope, payload, authority, dedupe_key,
                       lane, occurred_at, duration_ms, timing_quality, redaction_policy):
        # All producer paths (including atomic batches and receipts) pass the
        # same persistence boundary. Keep the transaction helper signature.
        self._check_authority(authority)
        candidate = [kind, source, scope, payload, authority, dedupe_key, lane, occurred_at,
                     duration_ms, timing_quality, redaction_policy]
        _scan_raw_fields(candidate)
        if len(contracts.canonicalize(candidate)) > contracts.MAX_RECORD:
            raise RawIngestionRefusal('record exceeds byte limit')
        contracts.require_id(dedupe_key, 'dedupe_key')
        if privacy.scrub_text(dedupe_key) != dedupe_key:
            raise RawIngestionRefusal('sensitive dedupe key')
        source = privacy.scrub_source(source)
        scope, payload, lane = privacy.scrub_structure([scope, payload, lane])
        body = contracts.event_body(kind, source, scope, payload, authority, lane,
                                    occurred_at, duration_ms, timing_quality, redaction_policy)
        body.update(chain_id=self.chain_id, dedupe_key=dedupe_key)
        _check_observation(kind, authority, body['payload'])
        candidate_sha = contracts.semantic_digest(body)
        existing = conn.execute(
            'SELECT semantic_sha, envelope_json FROM dedupe_index JOIN event_rows USING(event_id)'
            ' WHERE dedupe_index.chain_id = ? AND dedupe_key = ?', (self.chain_id, dedupe_key),
        ).fetchone()
        if existing is not None:
            stored = json.loads(existing['envelope_json'])
            expected = candidate_sha
            if 'lane' in stored and 'dedupe_key' not in stored:
                # Pre-freeze index committed the sparse producer spelling,
                # without the computed orphan bit. Compare that exact preimage
                # on retry; don't upgrade an immutable row or its index.
                old = dict(schema=contracts.SCHEMA, chain_id=self.chain_id, kind=kind,
                           scope=scope, source=source, payload=payload, authority=authority,
                           lane={**dict.fromkeys(contracts.LANE_FIELDS), **(lane or {})},
                           occurred_at=occurred_at, duration_ms=duration_ms, timing_quality=timing_quality,
                           redaction_policy=contracts.OLD_POLICY if redaction_policy is None else redaction_policy)
                expected = contracts.semantic_digest(old)
            if existing['semantic_sha'] != expected:
                raise IdempotencyConflict('dedupe key already recorded with different semantic bytes')
            return {**{k: stored[k] for k in ('event_id', 'seq', 'hash', 'recorded_at', 'chain_id')},
                    'duplicated': True}
        self._check_target_exists(conn, body['payload'])
        self._check_parent_cycle(conn, body)
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
        envelope = dict(body)
        envelope["seq"] = seq
        envelope["event_id"] = event_id
        envelope["prev_hash"] = prev_hash
        envelope["recorded_at"] = recorded_at
        rails_digest = contracts.rails_hash(prev_hash, envelope)
        envelope["hash"] = rails_digest
        envelope_json = contracts.canonicalize(envelope).decode("ascii")
        if len(envelope_json) > contracts.MAX_RECORD:
            raise RawIngestionRefusal('envelope exceeds byte limit')
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
            imported = 'origin_bytes' in row.keys()
            text = row['origin_bytes' if imported else 'envelope_json']
            try:
                env = contracts.parse_text(text)
                if not isinstance(env, dict) or contracts.canonicalize(env).decode('ascii') != text:
                    return {'ok': False, 'at_seq': expected_seq, 'reason': 'noncanonical'}
            except contracts.RailsRefusal:
                return {'ok': False, 'at_seq': expected_seq, 'reason': 'noncanonical'}
            if row['seq'] != expected_seq or env.get('chain_id') != chain_id or env.get('seq') != expected_seq:
                return {"ok": False, "at_seq": expected_seq, "reason": "ordering"}
            if env.get('event_id') != chain_id + ':' + str(expected_seq) or env['event_id'] != row['event_id']:
                return {"ok": False, "at_seq": expected_seq, "reason": "event_id"}
            if env.get('prev_hash') != expected_prev:
                return {"ok": False, "at_seq": expected_seq, "reason": "prev_hash"}
            without_hash = {k: v for k, v in env.items() if k != "hash"}
            if (env.get('hash') != row['origin_hash' if imported else 'rails_hash'] or
                    env.get('hash') != contracts.rails_hash(expected_prev, without_hash)):
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
        contracts.require_id(chain_id, 'chain_id')
        if self.chain_id == chain_id or privacy.scrub_text(chain_id) != chain_id:
            raise AuthorityRefusal('invalid peer chain identity')
        if not isinstance(pages, (list, tuple)) or not 1 <= len(pages) <= contracts.MAX_RECORDS:
            raise contracts.RailsRefusal('invalid origin page count', code='pages')
        pages = tuple(pages)
        total = 0
        for text in pages:
            if not isinstance(text, str) or len(text) > contracts.MAX_RECORD or not text.isascii():
                raise contracts.RailsRefusal('invalid or oversized origin record', code='origin_bytes')
            total += len(text)
            if total > contracts.MAX_PAGE:
                raise contracts.RailsRefusal('origin page exceeds byte limit', code='pages')

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
            parsed = contracts.parse_text(text)
            if not isinstance(parsed, dict) or contracts.canonicalize(parsed).decode('ascii') != text:
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
            contracts.require_id(parsed['event_id'], 'event_id')
            without_hash = {k: v for k, v in parsed.items() if k != 'hash'}
            if parsed.get('hash') != contracts.rails_hash(parsed.get('prev_hash'), without_hash):
                raise contracts.RailsRefusal('invalid origin hash', code='origin_bytes')
            _validate_origin_content(parsed)
            self._check_authority(parsed['authority'])
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
            if parsed.get("prev_hash") != prev:
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
