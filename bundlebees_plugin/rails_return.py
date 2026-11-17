"""Read one pinned coordinator snapshot for an optional RETURN attachment.

The operator supplies the heads separately from the packet/peer/database.
No network, source polling, database initialization or inferred trust anchors.
"""

from collections import defaultdict
from contextlib import closing
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from . import leases
from .return_report import ReturnError

try:
    from rizzstack_rails import contracts as wire, privacy
except ImportError:  # source checkout, before packaging the shared runtime
    from contrib.rizzstack_rails import contracts as wire, privacy


FILE = "rails-evidence.json"
INPUT_SCHEMA = "bundlebees.rails-return-input/1"
MAX_FILE = 128 * 1024
MAX_ROWS = 4096
MAX_BYTES = 8 * 1024 * 1024
CHAIN = re.compile(r"(m-[a-f0-9]{12})-([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})")


def require(ok, reason):
    if not ok:
        raise ReturnError(
            "rails_evidence_refused",
            reason,
            "Check the enrolled anchors, selection and durable evidence. Do not edit a receipt.",
        )


def read_file(path, *, protected=False):
    """Bound before reading; do not follow a symlink or block opening a FIFO."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as file:
            info = os.fstat(file.fileno())
            require(
                stat.S_ISREG(info.st_mode) and info.st_size <= MAX_FILE, "Invalid or oversized evidence file."
            )
            if protected and hasattr(os, "getuid"):
                require(
                    info.st_uid in (0, os.getuid()) and not info.st_mode & 0o022,
                    "The anchor file must be controlled by the operator.",
                )
            data = file.read(MAX_FILE + 1)
            require(len(data) == info.st_size, "Evidence changed while reading.")
            return data
    except OSError:
        raise ReturnError("rails_evidence_refused", "Evidence file missing or unreadable.") from None


def _id(value):
    return isinstance(value, str) and wire.ID_RE.fullmatch(value) and privacy.scrub_text(value) == value


def _input(path, manifest, tasks):
    cfg = wire.parse_text(read_file(path, protected=True))
    require(
        isinstance(cfg, dict)
        and set(cfg)
        == {
            "schema",
            "job_id",
            "task_id",
            "contract_sha256",
            "code_rev",
            "coordinator",
            "workers",
            "evidence",
        }
        and cfg["schema"] == INPUT_SCHEMA,
        "Unknown Rails RETURN input format.",
    )
    require(cfg["job_id"] == manifest["job_id"], "Anchor input names a different job.")
    matches = [t for t in tasks if t["task_id"] == cfg["task_id"] and t["contract_ok"] and t["contract"]]
    require(len(matches) == 1, "Missing or ambiguous frozen task contract.")
    task = matches[0]
    require(
        task["contract_sha256"] == manifest["contract"]["sha256"] == cfg["contract_sha256"],
        "Anchor input names a different contract.",
    )
    rev = cfg["code_rev"]
    require(
        isinstance(rev, str)
        and wire.REV_RE.fullmatch(rev)
        and rev
        in (
            manifest["repository"]["source_rev"],
            manifest["verification"]["commit"],
            manifest["integration"]["commit"],
        ),
        "Revision is not bound to this RETURN.",
    )
    evidence = cfg["evidence"]
    require(isinstance(evidence, dict) and 1 <= len(evidence) <= 100, "Invalid evidence selection.")
    acs = {a["id"] for a in task["contract"]["acceptance_criteria"]}
    for ac, ids in evidence.items():
        require(
            ac in acs and sum(a["id"] == ac for a in manifest["acceptance"]) == 1,
            "Unknown or ambiguous acceptance criterion.",
        )
        require(
            isinstance(ids, list)
            and 1 <= len(ids) <= 500
            and all(_id(i) for i in ids)
            and len(set(ids)) == len(ids),
            "Invalid event references.",
        )
    require(sum(map(len, evidence.values())) <= 500, "Too many event references.")
    require(
        isinstance(cfg["workers"], list) and 1 <= len(cfg["workers"]) <= 8,
        "Supply 1–8 enrolled worker heads.",
    )
    heads = [cfg["coordinator"], *cfg["workers"]]
    names, uuids = set(), set()
    for h in heads:
        require(
            isinstance(h, dict)
            and set(h) == {"chain_id", "seq", "hash"}
            and isinstance(h["chain_id"], str)
            and CHAIN.fullmatch(h["chain_id"])
            and type(h["seq"]) is int
            and 1 <= h["seq"] <= MAX_ROWS
            and isinstance(h["hash"], str)
            and wire.HEX64_RE.fullmatch(h["hash"]),
            "Invalid enrolled head.",
        )
        alias, uid = CHAIN.fullmatch(h["chain_id"]).groups()
        require(alias not in names and uid not in uuids, "Duplicate machine or ledger identity.")
        names.add(alias)
        uuids.add(uid)
    require(sum(h["seq"] for h in heads) <= MAX_ROWS, "Anchored ranges exceed the row budget.")
    return cfg, task, heads


def _read_chain(db, h, local, budget):
    table, column, digest = (
        ("event_rows", "envelope_json", "rails_hash")
        if local
        else ("imported_origin_rows", "origin_bytes", "origin_hash")
    )
    chain = h["chain_id"]
    require(
        db.execute(
            f"SELECT seq,{digest} FROM {table} WHERE chain_id=? ORDER BY seq DESC LIMIT 1", (chain,)
        ).fetchone()
        == (h["seq"], h["hash"]),
        "Ledger head differs from the independent anchor.",
    )
    rows = []
    prev = legacy_prev = wire.GENESIS
    legacy = (", " + ",".join((*wire.LEGACY_CHAIN_KEYS, "hash", "id", "ingest_seq"))) if local else ""
    for seq, eid, stored_hash, raw, *compat in db.execute(
        f"SELECT seq,event_id,{digest},{column}{legacy} FROM {table} WHERE chain_id=? ORDER BY seq LIMIT ?",
        (chain, h["seq"] + 1),
    ):
        require(isinstance(raw, str) and len(raw) <= wire.MAX_RECORD, "Oversized ledger record.")
        budget[0] -= len(raw)
        require(budget[0] >= 0, "Anchored ranges exceed the byte budget.")
        e = wire.parse_text(raw)
        require(
            isinstance(e, dict)
            and wire.canonicalize(e).decode() == raw
            and e["schema"] == wire.SCHEMA
            and type(e["seq"]) is int
            and len(rows) + 1 == seq == e["seq"]
            and e["chain_id"] == chain
            and e["event_id"] == eid == f"{chain}:{seq}"
            and e["hash"] == stored_hash
            and e["prev_hash"] == prev
            and stored_hash == wire.rails_hash(prev, {k: v for k, v in e.items() if k != "hash"}),
            "Origin bytes, index or hash chain disagree.",
        )
        require(
            e["redaction_policy"] == wire.POLICY
            and (local or e["authority"] in ("worker_report", "imported_observation")),
            "Ineligible origin policy or authority.",
        )
        body = wire.event_body(
            e["kind"],
            e["source"],
            e["scope"],
            e["payload"],
            e["authority"],
            {k: e[k] for k in wire.LANE_FIELDS},
            e["occurred_at"],
            e["duration_ms"],
            e["timing_quality"],
            e["redaction_policy"],
        )
        require(
            e.keys() == body.keys() | {"chain_id", "dedupe_key", *wire.SERVER_ASSIGNED_KEYS}
            and all(e[k] == v for k, v in body.items())
            and _id(e["dedupe_key"])
            and isinstance(e["recorded_at"], str)
            and wire.STAMP_RE.fullmatch(e["recorded_at"]),
            "Envelope does not conform to the frozen Rails contract.",
        )
        if local:
            projection = dict(zip(wire.LEGACY_CHAIN_KEYS, compat))
            legacy_hash, legacy_id, ingest = compat[-3:]
            require(
                projection["why"] == raw
                and projection["prev_hash"] == legacy_prev
                and projection["sha"] == stored_hash
                and legacy_id == eid
                and ingest == seq
                and wire.legacy_hash(legacy_prev, projection) == legacy_hash,
                "Coordinator compatibility chain disagrees.",
            )
            legacy_prev = legacy_hash
        rows.append(e)
        prev = stored_hash
    require(len(rows) == h["seq"] and prev == h["hash"], "Incomplete anchored range.")
    return rows


def _snapshot(path, heads):
    require(
        path and not str(path).startswith(("//", "\\\\")), "Configure a local coordinator Rails database."
    )
    file = Path(path).absolute()
    require(
        file.is_file() and not file.is_symlink(), "Coordinator database missing or not a local regular file."
    )
    with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        deadline = time.monotonic() + 5
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4 * wire.MAX_RECORD)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        ident = dict(
            db.execute(
                "SELECT key,value FROM ledger_meta WHERE key IN ('chain_id','machine_alias','ledger_uuid')"
            )
        )
        local = heads[0]["chain_id"]
        require(
            ident["chain_id"] == local == ident["machine_alias"] + "-" + ident["ledger_uuid"],
            "Wrong coordinator identity.",
        )
        budget = [MAX_BYTES]
        local_rows = _read_chain(db, heads[0], True, budget)
        all_rows = list(local_rows)
        checkpoints = []
        for h in heads[1:]:
            chain = h["chain_id"]
            rows = _read_chain(db, h, False, budget)
            require(
                db.execute(
                    "SELECT next_seq,head_hash,state FROM import_cursors WHERE chain_id=?", (chain,)
                ).fetchone()
                == (h["seq"] + 1, h["hash"], "active"),
                "Worker cursor missing, halted or divergent.",
            )
            receipts = db.execute(
                "SELECT through_seq,head_hash,origin_count FROM import_receipts WHERE chain_id=? "
                "ORDER BY receipt_seq LIMIT ?",
                (chain, h["seq"] + 1),
            ).fetchall()
            marks = [
                e
                for e in local_rows
                if e["kind"] == "source.checkpoint"
                and e["source"]["adapter"] == "rails_import"
                and e["source"]["instance"] == chain
            ]
            require(len(receipts) == len(marks), "Import receipts disagree with coordinator checkpoints.")
            start = 0
            for (end, digest, count), mark in zip(receipts, marks):
                require(start < end <= len(rows) and count == end - start, "Invalid imported range.")
                batch = hashlib.sha256(b"".join(wire.canonicalize(e) for e in rows[start:end])).hexdigest()
                p = mark["payload"]
                require(
                    mark["authority"] == "imported_observation"
                    and digest == rows[end - 1]["hash"] == mark["source"]["revision"]
                    and (p["code"], p["range_start"], p["range_end"], p["count"], p["digest"])
                    == ("imported", start + 1, end, count, batch),
                    "Imported bytes do not match the coordinator receipt.",
                )
                start = end
            require(start == len(rows), "Missing import receipt for the enrolled worker head.")
            checkpoints.append(dict(h, source="rails_import"))
            all_rows.extend(rows)
    return all_rows, checkpoints, local_rows[-1]["recorded_at"]


def _bound(event, cfg, task, state, manifest, lease_cache, *, native=False):
    s = event["scope"]
    for field, expected in [
        ("repo", manifest["repository"]["identity"]),
        ("job_id", cfg["job_id"]),
        ("task_id", cfg["task_id"]),
        ("contract_sha256", cfg["contract_sha256"]),
        ("code_rev", cfg["code_rev"]),
    ]:
        require(
            s[field] in (None, expected)
            or (native and field == "task_id" and s[field] == task["contract"]["id"]),
            "Selected event conflicts with job, task, revision or contract.",
        )
    attempt = s["attempt_id"]
    if attempt is not None:
        require(attempt in task["attempts"], "Attempt is not enrolled in the selected task.")
    local = event["chain_id"] == cfg["coordinator"]["chain_id"]
    if local and all(s[k] is not None for k in ("job_id", "task_id", "contract_sha256", "code_rev")):
        return
    if native:
        # R05 signed-record projections use the contract id, not a job id.
        # This record was independently located/revalidated by this RETURN.
        return
    # Native worker envelopes expose attempt only. Resolve against the actual
    # MAC-checked dispatch lease; never infer relationships by event time.
    require(attempt in task["attempts"], "Selected event has no durable job binding.")
    if attempt not in lease_cache:
        lease_cache[attempt] = leases.load_lease(state, attempt)
    lease = lease_cache[attempt]
    brief = lease["brief"]
    require(
        brief.get("job_id") == cfg["job_id"]
        and brief.get("task_id") == cfg["task_id"]
        and brief.get("contract_sha256", task["contract_sha256"]) == cfg["contract_sha256"]
        and lease["source_rev"] == cfg["code_rev"],
        "Attempt lease disagrees with the selected snapshot.",
    )


def _native_ref(e, ac, manifest, state):
    """R05 uses opaque references to actual durable records, not event IDs."""
    if e["source"]["adapter"] != "bundlebees" or e["kind"] != "verification.result":
        return None
    from .rails_bundlebees_source import _alias

    row = next(a for a in manifest["acceptance"] if a["id"] == ac)
    paths = list(row["evidence"])
    if e["payload"]["verdict"] == "EXPECTED_RED" and manifest["red_green"]["red"]:
        paths.append(manifest["red_green"]["red"])
    for path in paths:
        # Only RETURN-resolved paths; never dereference an origin-supplied path.
        rid = _alias("record", path)
        if rid == e["source"]["record_id"] and rid in e["payload"]["evidence_refs"]:
            require(
                hashlib.sha256(read_file(state / path)).hexdigest() == e["source"]["revision"],
                "Referenced native record no longer matches its projected bytes.",
            )
            return rid
    return None


def _document(cfg, task, heads, rows, checks, stamp, manifest, state):
    by_id = {e["event_id"]: e for e in rows}
    chosen = sorted({eid for ids in cfg["evidence"].values() for eid in ids})
    require(all(i in by_id for i in chosen), "Selected event does not resolve in the anchored ranges.")
    require(
        all(any(by_id[i]["chain_id"] == h["chain_id"] for i in chosen) for h in heads[1:]),
        "Enrolled worker has no selected observation.",
    )
    require(wire.STAMP_RE.fullmatch(stamp), "Invalid coordinator snapshot time.")
    local = heads[0]["chain_id"]
    statuses = {a["id"]: a["status"] for a in manifest["acceptance"]}

    # A selection cannot resurrect an earlier PASS by omitting a newer
    # applicable verifier result. A different task/revision is a different run.
    def verifier_key(e):
        s, p = e["scope"], e["payload"]
        return (s["job_id"], s["task_id"], p["contract_sha256"], p["code_rev"], p["ac_id"])

    latest = {
        verifier_key(e): e
        for e in rows
        if e["chain_id"] == local
        and e["kind"] == "verification.result"
        and e["authority"] == "verifier_record"
    }
    links, cache = [], {}
    for ac, ids in sorted(cfg["evidence"].items()):
        for eid in ids:
            e = by_id[eid]
            native = _native_ref(e, ac, manifest, state) if e["chain_id"] == local else None
            _bound(e, cfg, task, state, manifest, cache, native=native is not None)
            if e["kind"] in ("verification.result", "acceptance.result"):
                p = e["payload"]
                require(
                    e["kind"] == "verification.result"
                    and e["chain_id"] == local
                    and e["authority"] == "verifier_record"
                    and p["ac_id"] == ac
                    and p["contract_sha256"] == e["scope"]["contract_sha256"] == cfg["contract_sha256"]
                    and p["code_rev"] == e["scope"]["code_rev"] == cfg["code_rev"],
                    "Unbound or unauthorized verification link.",
                )
                verdict = p["verdict"]
                refs = p["evidence_refs"]
                require(
                    verdict in wire.VERDICTS
                    and isinstance(refs, list)
                    and (refs or verdict != "PASS")
                    and all(
                        (native is not None and ref == native)
                        or (
                            ref in ids
                            and ref != eid
                            and by_id[ref]["kind"] not in ("verification.result", "acceptance.result")
                        )
                        for ref in refs
                    ),
                    "Verifier evidence must resolve to selected supporting events for this criterion.",
                )
                require(
                    verdict != "PASS" or statuses[ac] == "PASS",
                    "Verifier PASS lacks matching native RETURN evidence.",
                )
                require(
                    verdict != "PASS" or latest[verifier_key(e)]["payload"]["verdict"] == "PASS",
                    "A later applicable verifier result supersedes this PASS.",
                )
                require(
                    verdict not in ("FAIL", "BLOCKED") or statuses[ac] != "PASS",
                    "Verifier and RETURN results conflict.",
                )
                links.append(dict(ac_id=ac, event_id=eid, verdict=verdict))
    gaps = defaultdict(list)
    interventions = set()
    for e in rows:
        p, s = e["payload"], e["scope"]
        if (
            e["chain_id"] == local
            and e["kind"] in ("intervention.requested", "intervention.result")
            and (e["authority"] in ("coordinator_record", "owner_record") and s["job_id"] == cfg["job_id"])
        ):
            interventions.add(p["request_id"])
        if e["kind"] in ("source.gap", "source.error") or e["event_id"] in chosen:
            adapter = e["source"]["adapter"]
            require(_id(adapter), "Invalid public source alias.")
            observed = gaps[adapter]
            if e["kind"] in ("source.gap", "source.error"):
                require(
                    isinstance(p["code"], str)
                    and re.fullmatch(r"[A-Za-z0-9_:-]{1,80}", p["code"])
                    and _id(p["source_id"])
                    and len(observed) < 100,
                    "Unsafe or excessive source diagnostics.",
                )
                observed.append({k: p[k] for k in ("code", "source_id", "range_start", "range_end")})
    require(len(gaps) <= 50, "Too many evidence sources.")
    machines, ledgers = [], []
    for h in heads:
        alias, uid = CHAIN.fullmatch(h["chain_id"]).groups()
        machines.append(dict(alias=alias, role="coordinator" if h["chain_id"] == local else "worker"))
        ledgers.append(dict(machine=alias, ledger_uuid=uid))
    return dict(
        schema="rizzstack.rails-evidence/1",
        generated_at=stamp,
        machines=machines,
        ledgers=ledgers,
        origin_heads=heads,
        imported_checkpoints=checks,
        selected_event_ids=chosen,
        source_coverage=[
            dict(adapter=a, status="gap" if g else "partial", gaps=g) for a, g in sorted(gaps.items())
        ],
        code_rev=cfg["code_rev"],
        contract_sha256=cfg["contract_sha256"],
        redaction_policy=wire.POLICY,
        intervention_count=len(interventions),
        verification_links=links,
        receiver_decision="pending",
        limitations=[
            "Coverage is limited to the anchored sources; worker observations are not acceptance.",
            "Interventions count distinct local coordinator/owner requests explicitly scoped to this job.",
            "Snapshot time is the recorded time of the anchored coordinator head.",
        ],
    )


def collect(state, manifest, tasks, *, anchors, database):
    try:
        cfg, task, heads = _input(anchors, manifest, tasks)
        rows, checks, stamp = _snapshot(database, heads)
        doc = _document(cfg, task, heads, rows, checks, stamp, manifest, state)
        return doc, set(cfg["evidence"])
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        wire.RailsRefusal,
        leases.LeaseError,
    ) as exc:
        raise ReturnError(
            "rails_evidence_refused", "Malformed, unavailable or unverifiable Rails evidence."
        ) from exc
