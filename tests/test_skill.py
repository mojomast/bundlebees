"""Offline checks for the bundled bundlebees-operator skill tree.

These tests only read files under ``skills/bundlebees-operator``. They require
no network, launcher, token, Docker, or SSH access.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "bundlebees-operator"
SKILL_MD = SKILL_DIR / "SKILL.md"

EXPECTED_FILES = {
    "SKILL.md",
    "references/kanban.md",
    "references/briefs-and-leases.md",
    "references/verify-and-deploy.md",
    "references/security.md",
    "templates/host-setup-prompt.md",
}

REQUIRED_SECTIONS = [
    "when to use",
    "quick reference",
    "procedure",
    "pitfalls",
    "verification",
    "hard rules",
]

PLACEHOLDERS = [
    "{{os}}",
    "{{reach}}",
    "{{provider}}",
    "{{repos}}",
    "{{max_concurrent}}",
    "{{branch_rules}}",
]

PRIVATE_IP_RE = re.compile(
    r"\b(?:"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"
    r"|169\.254\.\d{1,3}\.\d{1,3}"
    r"|127\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r")\b"
)
PUBLIC_URL_RE = re.compile(r"https?://", re.IGNORECASE)
SCP_HOST_RE = re.compile(r"\b[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
TOKEN_SHAPE_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
NEGATION_RE = re.compile(r"(?i)\b(no|not|never|don't|do not|cannot|must not|without)\b")

# Instructions that would contradict the security model. Each pattern is
# checked line by line and rejected unless the line is clearly negated.
FORBIDDEN_INSTRUCTIONS = [
    re.compile(
        r"(?i)\b(?:write|create|save|store|dump|put)\b.{0,60}"
        r"\b(?:token|api[ _-]?key|secret|credential)\b.{0,40}"
        r"\b(?:file|disk|path|somewhere)\b"
    ),
    re.compile(
        r"(?i)\b(?:write|create|save|store|dump)\b.{0,60}"
        r"\b(?:token|key|secret)\b"
    ),
    re.compile(r"(?i)\brun\s+(?:the\s+)?(?:verify|deploy)\b.{0,30}\byourself\b"),
    re.compile(
        r"(?i)\b(?:you|the model|the assistant|the agent)\b.{0,40}"
        r"\b(?:run|execute|invoke)\b.{0,40}"
        r"\bhermes bundlebees (?:verify|deploy)\b"
    ),
    re.compile(r"(?i)\b(?:run|execute)\s+`?hermes bundlebees (?:verify|deploy)\b"),
    re.compile(r"(?i)\brestart\b.{0,20}\blauncher\b"),
    re.compile(r"(?i)\bmerge\b.{0,20}\b(?:in)?to\s+main\b"),
]


def _read(path: Path) -> str:
    data = path.read_bytes()
    assert b"\x00" not in data, f"{path} must be plain UTF-8 text"
    return data.decode("utf-8")


def _frontmatter(text: str) -> str:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    assert match is not None, "SKILL.md must start with YAML frontmatter"
    return match.group(1)


def _body(text: str) -> str:
    return re.sub(r"\A---\r?\n.*?\r?\n---\r?\n", "", text, count=1, flags=re.DOTALL)


def _all_text_files() -> dict[Path, str]:
    return {path: _read(path) for path in sorted(SKILL_DIR.rglob("*")) if path.is_file()}


def test_skill_tree_contains_expected_files_only() -> None:
    assert SKILL_DIR.is_dir(), "skills/bundlebees-operator is missing"
    found = {
        str(path.relative_to(SKILL_DIR)).replace("\\", "/")
        for path in SKILL_DIR.rglob("*")
        if path.is_file()
    }
    assert EXPECTED_FILES <= found, f"missing skill files: {sorted(EXPECTED_FILES - found)}"
    assert found == EXPECTED_FILES, f"unexpected files in skill tree: {sorted(found - EXPECTED_FILES)}"


def test_skill_files_are_non_empty_utf8_text() -> None:
    for path, text in _all_text_files().items():
        assert text.strip(), f"{path} is empty"
        assert text.isprintable() or "\n" in text


def test_frontmatter_has_name_trigger_description_and_license() -> None:
    front = _frontmatter(_read(SKILL_MD))
    assert re.search(r"(?m)^name:\s*bundlebees-operator\s*$", front), "name must be bundlebees-operator"
    assert re.search(r"(?m)^license:\s*MIT\s*$", front), "license must be MIT"
    description = re.search(r"(?m)^description:\s*(.+)$", front)
    assert description is not None, "frontmatter needs a description"
    text = description.group(1).strip()
    assert len(text) >= 80, "description must be trigger-oriented, not a stub"
    assert re.search(r"(?i)\buse (?:when|this|for)\b", text), "description must say when to use the skill"
    assert not re.search(r"(?m)^requires_tools\s*:", front), "do not require the four plugin tools"


def test_required_sections_exist_in_order() -> None:
    headings = [
        match.group(1).strip().lower()
        for match in re.finditer(r"(?m)^##\s+(.+?)\s*$", _body(_read(SKILL_MD)))
    ]
    indexes = []
    for wanted in REQUIRED_SECTIONS:
        matches = [i for i, heading in enumerate(headings) if heading == wanted]
        assert matches, f"missing section: {wanted}"
        indexes.append(matches[0])
    assert indexes == sorted(indexes), f"sections out of order: {headings}"


def test_skill_id_and_deferred_tool_recovery_are_documented() -> None:
    text = _read(SKILL_MD)
    assert "bundlebees:bundlebees-operator" in text
    assert "plugin:bundlebees" not in text
    assert 'tool_search(["bundlebees"])' in text or "tool_search(['bundlebees'])" in text
    assert 'skill_view("bundlebees:bundlebees-operator")' in text or "skill_view('bundlebees:bundlebees-operator')" in text
    assert "${HERMES_SKILL_DIR}" in text
    for helper in (
        "references/kanban.md",
        "references/briefs-and-leases.md",
        "references/verify-and-deploy.md",
        "references/security.md",
        "templates/host-setup-prompt.md",
    ):
        assert helper in text, f"SKILL.md must point at {helper}"


def test_skill_md_length_is_between_800_and_1500_words() -> None:
    words = len(re.findall(r"\S+", _body(_read(SKILL_MD))))
    assert 800 <= words <= 1500, f"SKILL.md body is {words} words; expected 800-1500"


def test_tool_operations_and_user_run_cli_are_documented() -> None:
    text = _read(SKILL_MD)
    for name in (
        "bundlebees_status",
        "bundlebees_dispatch",
        "bundlebees_attempt",
        "bundlebees_host_prompt",
    ):
        assert name in text, f"missing tool {name}"
    for op in ("health", "capacity", "attempts", "events", "containers", "projects"):
        assert op in text
    assert "hermes bundlebees verify" in text
    assert "hermes bundlebees deploy" in text
    assert "user" in text.lower(), "verify and deploy must be described as user-run"
    assert "deepseek-v4.1-flash" in text


def test_no_forbidden_instructions() -> None:
    for path, text in _all_text_files().items():
        for line in text.splitlines():
            for pattern in FORBIDDEN_INSTRUCTIONS:
                if pattern.search(line) and not NEGATION_RE.search(line):
                    raise AssertionError(f"{path}: forbidden instruction: {line.strip()}")


def test_no_private_addresses_hosts_urls_or_token_shapes() -> None:
    for path, text in _all_text_files().items():
        assert not PRIVATE_IP_RE.search(text), f"{path} contains a private/loopback IP"
        assert not PUBLIC_URL_RE.search(text), f"{path} contains a URL"
        assert not SCP_HOST_RE.search(text), f"{path} contains an SSH-style host"
        assert not TOKEN_SHAPE_RE.search(text), f"{path} looks like it contains a token"
        assert ".ts.net" not in text.lower(), f"{path} contains a tailnet host"
        assert "os.environ" not in text, f"{path} must not read the ambient environment"
        assert "dotenv" not in text.lower(), f"{path} must not use dotenv"


def test_template_has_placeholders_and_required_phases_without_author_values() -> None:
    template = _read(SKILL_DIR / "templates" / "host-setup-prompt.md")
    for placeholder in PLACEHOLDERS:
        assert placeholder in template, f"template is missing {placeholder}"
    lower = template.lower()
    for phrase in ("phase 0", "worker setup", "coordinator setup", "first wave", "hard rules"):
        assert phrase in lower, f"template is missing phase text: {phrase}"
    assert "path" in lower and "env" in lower, "template must name paths and env names only"
    assert "BUNDLEBEES_LAUNCHER_TOKEN" in template
    assert "ORBIT_WORKER_GO_KEY" in template
    assert not re.search(r"(?i)\b(?:bearer|authorization)\s*[:=]\s*\S{8,}", template)
