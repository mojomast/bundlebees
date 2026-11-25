"""RED for wave A slice T4: the operator skill costs less per session.

The per-turn system-prompt section repeats what the skill already says, and
the verify reference (11 KB) is pointed at from dispatch-time text. Load the
verify reference only at the verify step and drop the per-turn section.
"""

from __future__ import annotations

import re

from tests.fakes import FakeCtx, ROOT, load_plugin

SKILL = ROOT / "skills" / "bundlebees-operator" / "SKILL.md"
VERIFY_REF = "references/verify-and-deploy.md"


class RecordingCtx(FakeCtx):
    def __init__(self):
        super().__init__()
        self.sections = []

    def register_system_prompt_section(self, name, text):
        self.sections.append((name, text))


def test_no_per_turn_system_section():
    ctx = RecordingCtx()
    load_plugin().register(ctx)
    assert ctx.sections == []


def _verify_step_offset(text: str) -> int:
    match = re.search(r"^7\. \*\*Ask the user to verify\.\*\*", text, flags=re.M)
    assert match, "step 7 (verify) heading moved; update this test with the owner"
    return match.start()


def test_verify_reference_only_named_from_the_verify_step_on():
    text = SKILL.read_text(encoding="utf-8")
    start = _verify_step_offset(text)
    helper_lines = [line for line in text.splitlines() if line.startswith("- `${HERMES_SKILL_DIR}/references/")]
    for line in helper_lines:
        if VERIFY_REF in line:
            assert re.search(r"step 7|verify step", line), line
    before = text[:start]
    pointers = [m.start() for m in re.finditer(re.escape(VERIFY_REF), before)]
    helper_text = "\n".join(helper_lines)
    assert all(VERIFY_REF in helper_text for _ in pointers)
    assert len(pointers) <= 1


def test_skill_does_not_grow():
    assert len(SKILL.read_text(encoding="utf-8")) <= 11_079
