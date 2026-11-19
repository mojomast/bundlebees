"""F2 ``require_contract`` enforcement: refuse uncontracted work before dispatch.

Reliability campaign F2 (corrected lease r2; the r1 lesson is inlined here and
in ``docs/contract-enforcement.md``: leases and scope checks speak in exact
file paths, never directories).

Threat model
============

``require_contract`` is durable plugin configuration (``settings.py``) resolved
at the campaign/repository/execution-profile boundary. When it is enabled for a
scope, :func:`pre_dispatch` is the fail-closed gate a dispatcher runs before
any launcher call:

- a brief with no ``acdd/1`` contract is refused with the machine-readable
  code ``contract_required``;
- an attached contract is validated, checked against its frozen hash
  (``contract_not_frozen`` / ``contract_changed``), compared with the brief
  (``stale_revision`` / ``scope_violation`` / ``tests_tampered``), checked for
  provenance against the pinned workflow revision
  (``provenance_unavailable`` / ``provenance_mismatch`` /
  ``provenance_invalid``), and cross-checked for acceptance ids across the
  contract, the feature packet, the brief and any verification evidence
  (``acceptance_mismatch``);
- with the flag off, contract-free work runs in legacy mode and is labelled as
  such — never silently. Off is the shipped default; the only profile that
  turns it on is the documented M2 canary profile, which opts in explicitly in
  its plugin settings.

The gate never trusts the brief to describe the flag: a brief cannot switch
enforcement off, and no decision is cached in session state. Every refusal
message is scrubbed before it can reach a tool envelope.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence

from . import contract as acdd
from . import packets as _packets
from . import settings as _settings
from .pins import WORKFLOW_PIN
from .security import child_env, scrub

__all__ = [
    "CONTRACT_REQUIRED",
    "LEGACY_NOTE",
    "PIN_WORKFLOW_REVISION",
    "ContractRefusal",
    "require_contract_enabled",
    "pre_dispatch",
    "check_provenance",
    "check_acceptance_ids",
    "scaffold",
    "main",
]

#: The precise machine-readable refusal for contract-free work in an
#: enforced scope. (The legacy tool path keeps its own ``contract_missing``.)
CONTRACT_REQUIRED = "contract_required"

#: The label a contract-free dispatch carries when the flag is off.
LEGACY_NOTE = "no ACDD contract; TDD evidence not required"

#: The pinned workflow commit a scaffolded packet seals as its ``workflow_ref``.
PIN_WORKFLOW_REVISION = WORKFLOW_PIN

_PACKET_VERSION = "rizzstack.feature-packet/1"
_KNOWN_CONTRACT_VERSIONS = frozenset({"acdd/1"})
_AC_ID_RE = re.compile(r"AC-[0-9]{3,}")
_HEX_RE = re.compile(r"^[0-9a-f]{40}$")
_FIX_CONTRACT = (
    "Attach a frozen acdd/1 contract under brief.contract; scaffold one with "
    "`python3 -m bundlebees_plugin.contracts scaffold`, review it, freeze it "
    "with `hermes bundlebees contract freeze`, then dispatch."
)
_FIX_PACKET = "Re-issue the packet from the frozen contract; never edit a frozen contract."


def _require_exact_paths(values: Sequence[Any], field: str) -> None:
    """Refuse directory-style lease entries: leases speak in exact file paths.

    The r1 lesson this module inlines: a lease entry like ``docs/`` is not a
    lease. Normalising ``shop/`` to ``shop`` would silently mint an exact path
    the operator never asked for, so scaffolding refuses instead.
    """
    for value in values:
        if isinstance(value, str) and value.strip().endswith("/"):
            _refuse(
                "contract_invalid",
                f"{field} entry {value!r} is a directory-style lease entry",
                "Leases speak in exact repository-relative file paths, never "
                "directories; pass the exact file path instead.",
                field,
            )


class ContractRefusal(Exception):
    """A dispatch refusal with the machine-readable code, error and fix.

    ``code`` is one of ``contract_required``, ``contract_invalid``,
    ``contract_not_frozen``, ``contract_changed``, ``stale_revision``,
    ``scope_violation``, ``tests_tampered``, ``acceptance_mismatch``,
    ``provenance_unavailable``, ``provenance_mismatch`` or
    ``provenance_invalid``. ``error`` and ``fix`` are scrubbed before
    exposure; :meth:`to_dict` is the tool-envelope shape.
    """

    def __init__(self, code: str, error: str, fix: str, field: str | None = None) -> None:
        self.code = str(code)
        self.error = scrub(str(error))
        self.fix = scrub(str(fix))
        self.field = scrub(str(field)) if field is not None else None
        super().__init__(f"{self.code}: {self.error}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "error": self.error, "code": self.code, "fix": self.fix}
        if self.field:
            out["field"] = self.field
        return out


def _refuse(code: str, error: str, fix: str, field: str | None = None) -> NoReturn:
    raise ContractRefusal(code, error, fix, field)


def require_contract_enabled(
    config: Mapping[str, Any],
    *,
    campaign: str | None = None,
    repository: str | None = None,
) -> bool:
    """The durable flag, read only through the settings boundary."""
    return _settings.require_contract(config, campaign=campaign, repository=repository)


# ---------------------------------------------------------------------------
# Provenance: the pinned workflow revision
# ---------------------------------------------------------------------------


#: The pinned checkout's HEAD is cached per process for this many seconds, so
#: dispatch does not run git on every brief. Only a resolved HEAD is cached.
_WORKFLOW_REF_TTL_S = 30.0
#: git rev-parse is local and fast; a hung checkout must not stall dispatch.
_WORKFLOW_REF_TIMEOUT_S = 5
_workflow_ref_cache: dict[str, tuple[float, str]] = {}


def _reset_workflow_ref_cache() -> None:
    _workflow_ref_cache.clear()


def pinned_workflow_ref() -> str:
    """The exact commit of the pinned workflow checkout (the protocol pin)."""
    try:
        source = _packets.workflow_src()
    except _packets.PacketError:
        _refuse(
            "provenance_unavailable",
            "no pinned workflow checkout is configured, so contract provenance cannot be established",
            "Check out the pinned workflow revision and set WORKFLOW_SRC; "
            "enforcement fails closed rather than guessing.",
        )
    key = str(source)
    now = time.monotonic()
    cached = _workflow_ref_cache.get(key)
    if cached is not None and 0.0 <= now - cached[0] < _WORKFLOW_REF_TTL_S:
        return cached[1]
    try:
        done = subprocess.run(
            ["git", "-C", key, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_WORKFLOW_REF_TIMEOUT_S,
            env=child_env({"GIT_TERMINAL_PROMPT": "0"}),
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        _refuse(
            "provenance_unavailable",
            "the pinned workflow checkout did not answer git rev-parse in time",
            "Fix the pinned checkout (WORKFLOW_SRC); do not relax the check.",
        )
    ref = done.stdout.strip().lower()
    if done.returncode != 0 or _HEX_RE.fullmatch(ref) is None:
        _refuse(
            "provenance_unavailable",
            "cannot resolve the pinned workflow revision in the pinned checkout",
            "Fix the pinned checkout (WORKFLOW_SRC); do not relax the check.",
        )
    _workflow_ref_cache[key] = (now, ref)
    return ref


def check_provenance(contract_body: Mapping[str, Any], *, packet: Any = None) -> dict[str, Any]:
    """Check the contract's protocol provenance and the packet's pin.

    The contract must name a known pinned protocol version. When a feature
    packet accompanies the brief, its ``workflow_ref`` must equal the pinned
    workflow checkout's ``HEAD`` exactly — the packet may not reinterpret
    itself under a different protocol — and a packet sealed to another
    contract hash is refused. Unresolvable provenance fails closed.
    """
    version = contract_body.get("contract_version")
    if version not in _KNOWN_CONTRACT_VERSIONS:
        _refuse(
            "contract_invalid",
            f"contract_version {version!r} is not a known pinned protocol version",
            "Use acdd/1; validators refuse versions they do not know.",
            "contract.contract_version",
        )
    out = {"contract_version": str(version), "workflow_ref": ""}
    if packet is None:
        return out
    if not isinstance(packet, Mapping):
        _refuse("provenance_invalid", "the feature packet is not an object",
                "Attach the packet mapping the pinned protocol validates.")
    pinned = packet.get("workflow_ref")
    if not isinstance(pinned, str) or not pinned.strip():
        _refuse(
            "provenance_invalid",
            "the feature packet names no workflow_ref",
            "Set the packet's workflow_ref to the pinned checkout's HEAD.",
            "packet.workflow_ref",
        )
    actual = pinned_workflow_ref()
    if pinned.strip() != actual:
        _refuse(
            "provenance_mismatch",
            f"packet workflow_ref {pinned.strip()!r} is not the pinned revision {actual!r}",
            "Re-pin the packet at the pinned workflow revision; never relax the pin.",
            "packet.workflow_ref",
        )
    review = contract_body.get("review")
    frozen_hash = str(review.get("contract_sha256") or "") if isinstance(review, Mapping) else ""
    sealed = packet.get("contract_sha256")
    if isinstance(sealed, str) and sealed.strip() and sealed.strip() != frozen_hash:
        _refuse(
            "contract_changed",
            "the feature packet is sealed against a different contract",
            _FIX_PACKET,
            "packet.contract_sha256",
        )
    embedded = packet.get("contract")
    if isinstance(embedded, Mapping) and acdd.contract_sha256(embedded) != frozen_hash:
        _refuse(
            "contract_changed",
            "the feature packet carries a different contract body",
            _FIX_PACKET,
            "packet.contract",
        )
    out["workflow_ref"] = actual
    return out


# ---------------------------------------------------------------------------
# Acceptance-id consistency across contract / packet / brief / evidence
# ---------------------------------------------------------------------------


def _contract_ids(contract: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for criterion in contract.get("acceptance_criteria") or []:
        if isinstance(criterion, Mapping) and isinstance(criterion.get("id"), str):
            ids.append(criterion["id"])
    return ids


def _brief_ids(brief: Mapping[str, Any]) -> list[str]:
    acceptance = brief.get("acceptance")
    items: list[Any] = []
    if isinstance(acceptance, Mapping):
        items = list(acceptance.get("criteria") or [])
    elif isinstance(acceptance, list):
        items = list(acceptance)
    found: list[str] = []
    for item in items:
        if isinstance(item, str):
            found.extend(_AC_ID_RE.findall(item))
    return found


def _evidence_id_sets(evidence: Any) -> list[set[str]]:
    records = evidence if isinstance(evidence, list) else [evidence]
    sets: list[set[str]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        criteria = record.get("criteria")
        if isinstance(criteria, Mapping):
            sets.append({str(key) for key in criteria})
        named = record.get("acceptance_ids")
        if isinstance(named, list):
            sets.append({str(item) for item in named if isinstance(item, str)})
    return sets


def check_acceptance_ids(
    contract: Mapping[str, Any],
    *,
    brief: Mapping[str, Any] | None = None,
    packet: Any = None,
    evidence: Any = None,
) -> list[str]:
    """Refuse any acceptance id the contract does not define.

    Every artifact that names acceptance ids — the feature packet's
    ``acceptance_ids``, the brief's acceptance criteria, and verification
    evidence records' ``criteria`` — must draw on the contract's own ids. A
    packet must carry the contract's full id set: a dropped criterion would be
    silently unjudged, an invented one silently unbound.
    """
    ids = _contract_ids(contract)
    known = set(ids)
    if packet is not None and isinstance(packet, Mapping):
        named = packet.get("acceptance_ids")
        if not isinstance(named, list) or not named or not all(isinstance(item, str) and item.strip() for item in named):
            _refuse(
                "acceptance_mismatch",
                "the feature packet names no usable acceptance ids",
                "Set the packet's acceptance_ids to the frozen contract's criterion ids.",
                "packet.acceptance_ids",
            )
        unknown = sorted(set(named) - known)
        missing = sorted(known - set(named))
        if unknown or missing:
            detail = []
            if unknown:
                detail.append("names unknown " + ", ".join(unknown))
            if missing:
                detail.append("drops " + ", ".join(missing))
            _refuse(
                "acceptance_mismatch",
                "packet acceptance_ids do not match the contract: " + "; ".join(detail),
                "Re-issue the packet from the frozen contract's acceptance_criteria.",
                "packet.acceptance_ids",
            )
    if brief is not None:
        for token in _brief_ids(brief):
            if token not in known:
                _refuse(
                    "acceptance_mismatch",
                    f"brief acceptance names {token!r}, which the contract does not define",
                    "Reference only ids the frozen contract defines.",
                    "brief.acceptance",
                )
    for id_set in _evidence_id_sets(evidence):
        unknown = sorted(id_set - known)
        if unknown:
            _refuse(
                "acceptance_mismatch",
                "verification evidence names acceptance id(s) the contract does not define: "
                + ", ".join(unknown),
                "Record evidence against the frozen contract's criterion ids.",
                "evidence.criteria",
            )
    return ids


# ---------------------------------------------------------------------------
# The dispatch gate
# ---------------------------------------------------------------------------


def pre_dispatch(
    brief: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
    packet: Any = None,
    evidence: Any = None,
    campaign: str | None = None,
    repository: str | None = None,
) -> dict[str, Any]:
    """The fail-closed gate a dispatcher runs before any launcher call.

    Returns the ``acdd`` envelope block on success and raises
    :class:`ContractRefusal` otherwise. ``config`` must be the durable
    settings mapping from ``bundlebees_plugin.settings`` (execution-profile or
    Hermes profile file); the brief itself carries no vote.
    """
    body = acdd.contract_from_brief(brief)
    if body is None:
        if require_contract_enabled(config, campaign=campaign, repository=repository):
            _refuse(
                CONTRACT_REQUIRED,
                "no frozen acdd/1 contract is attached to this brief and "
                "require_contract is enabled for this scope",
                _FIX_CONTRACT,
            )
        return {"mode": "legacy", "note": LEGACY_NOTE}
    try:
        validated = acdd.validate_contract(body)
        digest = acdd.check_frozen(body)
        acdd.check_brief_against_contract(brief, validated)
    except acdd.ContractError as exc:
        raise ContractRefusal(exc.code, exc.error, exc.fix, exc.field) from exc
    provenance = check_provenance(validated, packet=packet)
    ids = check_acceptance_ids(validated, brief=brief, packet=packet, evidence=evidence)
    tdd = validated.get("tdd")
    return {
        "mode": "contract",
        "contract_id": validated.get("id"),
        "contract_sha256": digest,
        "tdd_applicable": bool(tdd.get("applicable")) if isinstance(tdd, Mapping) else False,
        "acceptance_ids": ids,
        "provenance": provenance,
    }


# ---------------------------------------------------------------------------
# Scaffold: a valid contract plus a feature packet skeleton
# ---------------------------------------------------------------------------


def scaffold(
    *,
    contract_id: str,
    title: str,
    summary: str,
    behaviors: Sequence[str] = (),
    tests: Sequence[str] = (),
    statements: Sequence[str] = (),
    assigned_files: Sequence[str],
    test_files: Sequence[str] = (),
    read_only: Sequence[str] = (),
    prohibited: Sequence[str] = (),
    source_rev: str,
    feature_id: str | None = None,
    job_id: str | None = None,
    repository: str = "",
    branch: str = "",
) -> dict[str, Any]:
    """Generate a valid ``acdd/1`` contract and a feature packet skeleton.

    The contract validates with :func:`bundlebees_plugin.contract.validate_contract`
    as returned; freezing it is the reviewer's act. The packet is a *skeleton*
    for the pinned ``rizzstack.feature-packet/1`` protocol: its
    ``workflow_ref``, authorization and budget blocks are placeholders the
    operator must complete before the pinned validator would admit it.
    """
    tests = [item for item in tests if isinstance(item, str) and item.strip()]
    if not tests:
        _refuse(
            "scaffold_invalid",
            "a scaffolded contract needs at least one test to anchor a criterion",
            "Pass --test tests/test_x.py::test_y (one acceptance criterion each).",
        )
    _require_exact_paths(assigned_files, "contract.scope.assigned_files")
    behaviors = [item for item in behaviors if isinstance(item, str) and item.strip()]
    statements = [item for item in statements if isinstance(item, str) and item.strip()]
    test_files = [item for item in test_files if isinstance(item, str) and item.strip()]
    _require_exact_paths(test_files, "contract.scope.test_files")
    _require_exact_paths(read_only, "contract.scope.read_only")
    if not test_files:
        test_files = sorted({item.split("::", 1)[0] for item in tests})
    criteria = []
    for index, test_id in enumerate(tests):
        statement = statements[index] if index < len(statements) else f"{title}: proven by {test_id}"
        criteria.append(
            {
                "id": f"AC-{index + 1:03d}",
                "statement": statement,
                "verification": "test",
                "tests": [test_id],
            }
        )
    draft = {
        "contract_version": "acdd/1",
        "id": contract_id,
        "title": title,
        "outcome": {
            "summary": summary,
            "observable_behavior": behaviors or [item["statement"] for item in criteria],
        },
        "acceptance_criteria": criteria,
        "scope": {
            "assigned_files": list(assigned_files),
            "test_files": test_files,
            "read_only": list(read_only),
            "prohibited": list(prohibited),
        },
        "source_rev": source_rev,
        "tdd": {"applicable": True},
        "verification": {"independent": True},
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }
    try:
        contract = acdd.validate_contract(draft)
    except acdd.ContractError as exc:
        raise ContractRefusal(exc.code, exc.error, exc.fix, exc.field) from exc
    ids = _contract_ids(contract)
    feature = feature_id or contract_id
    job = job_id or feature
    packet = {
        "packet_version": _PACKET_VERSION,
        "feature_id": feature,
        "job_id": job,
        "request": {"summary": summary, "provenance": f"scaffold:{contract_id}"},
        "repository": {"identity": repository or "REPOSITORY-IDENTITY", "branch": branch or "BRANCH"},
        "workflow_ref": PIN_WORKFLOW_REVISION,
        "source_policy": {"kind": "exact", "revision": source_rev},
        "authorization": {
            "reference": f"auth:{job}",
            "approved_by": "OWNER-APPROVAL",
            "approved_at": "APPROVED-AT",
            "limits": {"bees": 1, "wall_clock_s": 1800},
        },
        "contract": contract,
        "contract_sha256": "SEAL-WITH-review.contract_sha256-AFTER-FREEZE",
        "acceptance_ids": ids,
        "tests": sorted({test_id for item in criteria for test_id in item["tests"]}),
        "scope": {"lease": list(contract["scope"]["assigned_files"]),
                  "read_only": list(dict.fromkeys([*read_only, *test_files]))},
        "budget": {"bees": 1, "wall_clock_s": 1800},
        "required_evidence": ["red", "green"],
        "prohibited": ["push to origin", "merge", "deploy", "edit approved tests", *prohibited],
        "human_gates": list(contract["authorization"]["human_gates"]),
    }
    return {"contract": contract, "packet": packet}


# ---------------------------------------------------------------------------
# Scaffold command
# ---------------------------------------------------------------------------


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        _refuse("scaffold_exists", f"{path} already exists",
                "Choose another --out directory; scaffolding never overwrites.")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload)


def main(argv: Sequence[str] | None = None) -> int:
    """``python3 -m bundlebees_plugin.contracts scaffold ...``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m bundlebees_plugin.contracts",
        description="Scaffold a valid acdd/1 contract and a feature packet skeleton.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("scaffold", help="write contract.json and packet.json")
    gen.add_argument("--id", dest="contract_id", required=True)
    gen.add_argument("--title", required=True)
    gen.add_argument("--summary", required=True)
    gen.add_argument("--behavior", dest="behaviors", action="append", default=[])
    gen.add_argument("--test", dest="tests", action="append", default=[],
                     help="tests/test_x.py::test_y; one acceptance criterion each")
    gen.add_argument("--statement", dest="statements", action="append", default=[],
                     help="optional criterion statement per --test, in order")
    gen.add_argument("--assigned", dest="assigned_files", action="append", default=[])
    gen.add_argument("--test-file", dest="test_files", action="append", default=[])
    gen.add_argument("--read-only", dest="read_only", action="append", default=[])
    gen.add_argument("--prohibited", action="append", default=[])
    gen.add_argument("--source-rev", dest="source_rev", required=True)
    gen.add_argument("--feature-id", dest="feature_id")
    gen.add_argument("--job-id", dest="job_id")
    gen.add_argument("--repository", default="")
    gen.add_argument("--branch", default="")
    gen.add_argument("--out", default=".")
    args = parser.parse_args(argv)
    if args.command != "scaffold":  # pragma: no cover - argparse enforces this
        parser.error("unknown command")
    if not args.assigned_files:
        parser.error("--assigned is required at least once")
    try:
        built = scaffold(
            contract_id=args.contract_id,
            title=args.title,
            summary=args.summary,
            behaviors=args.behaviors,
            tests=args.tests,
            statements=args.statements,
            assigned_files=args.assigned_files,
            test_files=args.test_files,
            read_only=args.read_only,
            prohibited=args.prohibited,
            source_rev=args.source_rev,
            feature_id=args.feature_id,
            job_id=args.job_id,
            repository=args.repository,
            branch=args.branch,
        )
        out = Path(args.out)
        _write_json(out / "contract.json", built["contract"])
        _write_json(out / "packet.json", built["packet"])
    except ContractRefusal as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True))
        return 1
    except OSError as exc:
        # Refusal-code completeness: every scaffold refusal reaches the caller
        # as the machine-readable envelope, never as a traceback.
        refusal = ContractRefusal(
            "scaffold_invalid",
            f"the --out location cannot be used: {exc}",
            "Choose a writable --out directory; scaffolding never overwrites "
            "or creates partial output.",
        )
        print(json.dumps(refusal.to_dict(), sort_keys=True))
        return 1
    print(f"contract: {out / 'contract.json'}")
    print(f"packet skeleton: {out / 'packet.json'}")
    print(f"contract_sha256 (sealed at freeze): {acdd.contract_sha256(built['contract'])}")
    print(
        "next: review, freeze with `hermes bundlebees contract freeze`, "
        "seal the frozen hash and the pinned workflow revision into the packet, "
        "then dispatch."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
