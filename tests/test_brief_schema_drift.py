"""RED for Wave B slice B8: bundlebees reports brief schema drift, never refuses it.

Wave B freezes ``rizzstack.brief/1`` in the workflow repository
(``schemas/rizzstack-brief.schema.json``, contract ``wave-b-b2``) and vendors one
standard-library Python validator for it (``wave-b-b3``,
``validators/python/rizzstack_validators.py``). This slice is the bundlebees
consumer: every outgoing brief is ALSO checked against the frozen schema in
**report-only** mode, and a difference is recorded as ``brief_schema_drift``
instead of being refused. Enforcement is a later slice (B6) and is not this one.

The two record shapes are the ones the report-only contract gate already uses
(``tools._report_only_gate`` / ``tools._record_gate_findings``): a block in the
dispatch envelope plus one append-only JSONL line per finding under
``<state_dir>/brief-schema-drift/report-only.jsonl``.

RED now: ``bundlebees_plugin/contract_drift.py``, the vendored validator and the
vendored schema do not exist, so every test below fails on the shipped tree.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "bundlebees_plugin"
SCHEMAS_DIR = PLUGIN / "schemas"
VENDORED_VALIDATOR = PLUGIN / "rizzstack_validators.py"
VENDORED_BRIEF_SCHEMA = SCHEMAS_DIR / "rizzstack-brief.schema.json"
DRIFT_MODULE = PLUGIN / "contract_drift.py"

#: sha256 of workflow ``schemas/rizzstack-brief.schema.json`` at the B3 head
#: (2c10217fd0252cb98ececf1dc59397e013eec7c2). A vendor copy is byte-identical
#: or it is not the frozen schema.
FROZEN_BRIEF_SCHEMA_SHA256 = (
    "d9c28454ad7583fc53ac88ebb1eb42a0980d09c37b811bc6f6886a6146a97ff0"
)

#: Valid under both shapes: bundlebees' own launcher brief (objective,
#: source_rev, assigned_files, acceptance) and rizzstack.brief/1 (which also
#: requires schema, project and risk).
MINIMAL_BRIEF: dict[str, object] = {
    "schema": "rizzstack.brief/1",
    "objective": "Record brief schema drift without refusing the dispatch.",
    "project": "bundlebees",
    "source_rev": "0123456789abcdef0123456789abcdef01234567",
    "assigned_files": ["bundlebees_plugin/contract_drift.py"],
    "acceptance": {"criteria": ["drift is reported, never refused"]},
    "risk": {"tier": "medium"},
}


class _Client:
    """Launcher double: records what was sent, never talks to a socket."""

    def __init__(self) -> None:
        self.created: list[object] = []

    def attempts(self) -> list[object]:
        return []

    def list_attempts(self) -> list[object]:
        return []

    def create(self, brief, **kwargs):  # noqa: ANN001, ANN003 - test double
        self.created.append(brief)
        return {"attempt_id": "att_0123456789abcdef", "state": "queued"}

    def create_attempt(self, brief, **kwargs):  # noqa: ANN001, ANN003
        return self.create(brief, **kwargs)


def _load(name: str, path: Path):
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing; B8 has not landed"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _drift():
    return _load("b8_contract_drift", DRIFT_MODULE)


def _verdicts(brief: object) -> list[str]:
    module = _drift()
    problems = module.check_brief_drift(brief)
    assert isinstance(problems, list), f"check_brief_drift returned {type(problems).__name__}"
    for problem in problems:
        assert isinstance(problem, str) and problem, f"non-string problem: {problem!r}"
    return problems


# ---------------------------------------------------------------------------
# the vendored artifacts are the frozen ones
# ---------------------------------------------------------------------------


def test_the_vendored_brief_schema_is_the_frozen_schema() -> None:
    assert VENDORED_BRIEF_SCHEMA.is_file(), (
        f"{VENDORED_BRIEF_SCHEMA.relative_to(ROOT)} is missing; B8 has not landed"
    )
    digest = hashlib.sha256(VENDORED_BRIEF_SCHEMA.read_bytes()).hexdigest()
    assert digest == FROZEN_BRIEF_SCHEMA_SHA256, (
        "the vendored schema is not byte-identical to the frozen rizzstack.brief/1 "
        f"schema: {digest} != {FROZEN_BRIEF_SCHEMA_SHA256}"
    )


def test_the_vendored_validator_validates_the_minimal_brief() -> None:
    module = _load("b8_rizzstack_validators", VENDORED_VALIDATOR)
    problems = module.validate_brief(dict(MINIMAL_BRIEF), schema_dir=SCHEMAS_DIR)
    assert problems == [], f"the vendored validator refused the minimal brief: {problems}"

    unknown = {**dict(MINIMAL_BRIEF), "wave_b_unknown_field": True}
    assert module.validate_brief(unknown, schema_dir=SCHEMAS_DIR), (
        "the vendored validator accepted an unknown top-level field"
    )


def test_the_vendored_validator_imports_only_the_standard_library() -> None:
    assert VENDORED_VALIDATOR.is_file(), (
        f"{VENDORED_VALIDATOR.relative_to(ROOT)} is missing; B8 has not landed"
    )
    tree = ast.parse(VENDORED_VALIDATOR.read_text(encoding="utf-8"), filename=str(VENDORED_VALIDATOR))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module.split(".")[0])
    external = sorted(imported - sys.stdlib_module_names)
    assert not external, f"the vendored validator imports third-party modules: {external}"


# ---------------------------------------------------------------------------
# the drift check: report only, never refuse
# ---------------------------------------------------------------------------


def test_a_clean_brief_reports_no_drift() -> None:
    assert _verdicts(dict(MINIMAL_BRIEF)) == [], "a brief that matches the frozen schema drifted"


def test_an_unknown_field_is_reported_by_name() -> None:
    problems = _verdicts({**dict(MINIMAL_BRIEF), "wave_b_unknown_field": True})
    assert problems, "an unknown top-level field produced no drift report"
    assert any("wave_b_unknown_field" in problem for problem in problems), (
        f"the drift report does not name the field: {problems}"
    )


def test_the_check_never_raises_on_a_document_that_is_not_a_brief() -> None:
    for hostile in ("not a brief", None, [], 17, {"schema": "rizzstack.brief/1"}):
        problems = _verdicts(hostile)
        assert isinstance(problems, list), f"check_brief_drift({hostile!r}) did not return a list"


def test_recording_returns_the_report_only_block(tmp_path: Path) -> None:
    module = _drift()
    problems = _verdicts({**dict(MINIMAL_BRIEF), "wave_b_unknown_field": True})
    block = module.record_brief_schema_drift(tmp_path, dict(MINIMAL_BRIEF), problems)
    assert isinstance(block, dict), block
    assert block.get("mode") == "report_only"
    count = block.get("count")
    assert isinstance(count, int) and count == len(problems) and count >= 1
    assert block.get("brief_sha256"), "the record does not pin the brief it describes"


def test_recording_appends_one_line_that_pins_the_brief_and_the_problems(tmp_path: Path) -> None:
    module = _drift()
    problems = _verdicts({**dict(MINIMAL_BRIEF), "wave_b_unknown_field": True})
    block = module.record_brief_schema_drift(tmp_path, dict(MINIMAL_BRIEF), problems)

    path = tmp_path / "brief-schema-drift" / "report-only.jsonl"
    assert path.is_file(), "no report-only.jsonl was written"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(records) == 1, records
    assert records[0]["mode"] == "report_only"
    assert records[0]["brief_sha256"] == block["brief_sha256"]
    assert records[0]["problems"] == problems


def test_recording_never_raises_when_the_state_dir_is_unusable(tmp_path: Path) -> None:
    module = _drift()
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    block = module.record_brief_schema_drift(blocker, dict(MINIMAL_BRIEF), ["$: unexpected property 'x'"])
    assert isinstance(block, dict), "recording must return a block even when it cannot write"
    assert block.get("recorded") is False


def test_a_drifted_brief_still_dispatches_and_the_drift_is_in_the_envelope(tmp_path: Path) -> None:
    from bundlebees_plugin import tools

    client = _Client()
    brief = {**dict(MINIMAL_BRIEF), "wave_b_unknown_field": True}
    out = json.loads(tools._dispatch(client, {"brief": brief}, tmp_path))
    assert out.get("ok", True) is not False, out
    assert len(client.created) == 1, "a drifted brief was refused instead of reported"

    block = out.get("brief_schema_drift")
    assert isinstance(block, dict), f"the dispatch envelope carries no brief_schema_drift: {out}"
    assert block.get("mode") == "report_only"
    count = block.get("count")
    assert isinstance(count, int) and count >= 1
    assert (tmp_path / "brief-schema-drift" / "report-only.jsonl").is_file(), (
        "the drift was not recorded under the state dir"
    )
