VERDICT: APPROVE

# Independent review: WA-A1A-FUP4 (PR #288, head 5cedbe4)

I did not write this code. I reviewed the whole of
`bundlebees_plugin/subversion_scan.py` (263 lines) against the 14 frozen
acceptance criteria, the approved test file `tests/test_subversion_scan.py`
(340 lines, read-only here), the changed CHANGELOG bullet, and the
`check_patch_subversion` refusal path.

## Scope and supplied evidence

- Checkout is the PR head (`5cedbe443fd80397546c9acd0af856ae1ee45240`); the
  base is the approved-tests commit `dd77792a3e919d6857cb80942c19d5f7f6f91607`.
  The sandbox has a single squashed commit, so the base sha is not diffable
  here; I relied on the supplied, coordinator-verified facts for the diff
  surface and test-file identity.
- Supplied and not re-derived: `t1634-check` says
  `bundlebees_plugin/subversion_scan.py` is the only product file changed;
  `tests/test_subversion_scan.py` is byte-identical between base and head;
  verify was GREEN (50 passed / 0 failed) on attempt `att_7537609ad8744dcd`
  (sha `cfafba3`); the coordinator commit adds one CHANGELOG bullet and no
  code.
- My own `git status --porcelain` is empty and `HEAD` is the landed tree.
- `timeout 300 python3 -m compileall -q bundlebees_plugin/subversion_scan.py`
  exits 0.

## AC mapping

| AC | Named test | Code that satisfies it | Result |
| --- | --- | --- | --- |
| AC-001 | `test_quoted_path_unicode_separators_cannot_spoof_the_refusal_message[u2028]` | `subversion_scan.py:18` adds `\u2028` to `_CONTROL_CHARS_RE`; `:49` strips it after `ast.literal_eval` | PASS |
| AC-002 | `...[u2029]` | same class at `:18`, stripped at `:49` | PASS |
| AC-003 | `test_quoted_pytest11_table_is_a_finding[double-quoted]` | `_PYTEST11_SECTION_RE` `:22-25`, applied for pyproject at `:102` | PASS |
| AC-004 | `...[dotted-spaces]` | `\s*` around every dotted part, `:22-25` | PASS |
| AC-005 | `...[single-quoted-spaces]` | quoted alternates plus `\s*`, `:22-25` | PASS |
| AC-006 | `test_addopts_plugin_option_at_end_of_line_is_a_finding[pyproject.toml-lines0]` | `-p(...|$)` at `:21`, continuation gate at `:106-108` | PASS |
| AC-007 | `...[setup.cfg-lines1]` | same `:21`, `indent` continuation `:119-129` | PASS |
| AC-008 | `...[pytest.ini-lines2]` | same `:21`; the flag is on the `addopts =` line itself | PASS |
| AC-009 | documentation AC | docstring `:238-242`; reset is real at `:247-248` | PASS |
| AC-010 | `test_quoted_path_control_characters_cannot_spoof_the_refusal_message` | C0/C1 range retained at `:18`, stripped at `:49` | PASS |
| AC-011 | `test_multiline_addopts_plugin_is_a_finding[pyproject.toml-lines0]` | `_next_addopts_continuation` `:111-129` unchanged | PASS |
| AC-012 | `test_pytest11_entry_point_is_a_finding[pyproject.toml-[project.entry-points.pytest11]]` | bare `pytest11` alternate at `:24`, plus `setup.cfg` `:26/104` | PASS |
| AC-013 | `test_legitimate_addopts_plugin_option_is_still_a_finding` | `-p` branch `:21`; `no:cacheprovider` still starts with a letter | PASS |
| AC-014 | `test_benign_patch_has_no_findings` | unchanged content patterns/gates; probe returns `[]` | PASS |

The docstring (AC-009) is accurate, not aspirational: `scan_patch` clears
`state["addopts"]` on every `_hunk_start` (`:247-248`), so an opener in an
earlier hunk is genuinely invisible. Context lines inside the same hunk are
still tracked through `_scan_context_line` (`:232-233`), which matches the
"outside the hunk" wording.

The RED guard is real. With the three base regexes reconstructed from the
supplied change description, exactly the 8 new parametrized inputs fail:
`u2028`, `u2029`, the three quoted-table spellings, and the three `-p`-at-EOL
forms. That is the 8-of-50 RED evidence, reproduced independently below.

The settled decision holds. `addopts = "-q -p no:cacheprovider"` is still a
`pytest_plugin` finding and `check_patch_subversion` still refuses with code
`test_subversion`. Nothing in the change weakens, skips or reorders a gate.

## Findings

1. **minor — new false positive on an INI comment line ending in `-p`.**
   `bundlebees_plugin/subversion_scan.py:21` (the new `|$` alternative),
   gated by `:106-108` and `:119-121`. A `setup.cfg` / `pytest.ini` / `tox.ini`
   addopts continuation containing a comment such as `    # prefer -p` or
   `    ; prefer -p` is now a `pytest_plugin` finding; `configparser` confirms
   the line is ignored comment text (`addopts` becomes `'\n-q\n-x'`), and the
   base regex did not flag it. This is fail-closed (a benign patch is refused,
   not accepted), so it is not a blocker.
   Concrete fix: in `_loads_pytest_plugin`, for the INI-style names only
   (`setup.cfg`, `pytest.ini`, `tox.ini`), return `False` when
   `line.lstrip().startswith(("#", ";"))` before the option/entry-point
   checks; do not apply that guard while `addopts_continuation` is `"""`/`'''`
   in `pyproject.toml`, where `#` is literal string content rather than a
   comment. Add one regression case to the approved file in a follow-up PR.

2. **minor — residual equivalent TOML spellings still evade; CHANGELOG
   over-claims exhaustiveness.** `bundlebees_plugin/subversion_scan.py:22-25`
   only quotes the `pytest11` segment. `tomllib` accepts
   `[project."entry-points".pytest11]`, `["project"."entry-points"."pytest11"]`,
   `[project.'entry-points'.'pytest11']` and
   `[project.entry-points."pytest\u0031\u0031"]` as the same table, but the
   scanner returns `[]` and `check_patch_subversion` does not refuse. This is
   pre-existing and outside the frozen ACs (which pin the `pytest11` key only),
   and the file is explicitly a content-level filter, so it is not a blocker.
   `CHANGELOG.md:20` however says "matches every equivalent TOML spelling",
   which the probe falsifies.
   Concrete fix: either normalise quoted key segments (small TOML key
   tokenizer, or a regex allowing optional quoting on `project` and
   `entry-points` and `\uXXXX` escapes in the `pytest11` key) with new tests,
   or, minimally, narrow the changelog wording to "the quoted and
   space-padded `pytest11` key spellings" so the claim matches the code.

3. **minor — `--plugin` at end of line is still an evasion.** `subversion_scan.py:21`
   gives `--plugin` only `(?:\s|=)`, while `-p` got `|$`. A triple-quoted or
   indented addopts block with `--plugin` alone on a line and the plugin name
   on the next line produces no finding (probe below). This is pre-existing,
   analogous to the form this PR fixed, and not pinned by an AC.
   Concrete fix: change the branch to `--plugin(?:\s|=|$)` and add the two
   EOL cases to the approved test file in a follow-up PR.

None of the three blocks the frozen criteria, weakens the settled
`no:cacheprovider` refusal, or touches the approved tests. All are
follow-up-sized; verdict APPROVE.

## Commands and probes (exact source and printed output)

### `git` and compile

```
$ git log --oneline -6
3b4e36e base 5cedbe443fd80397546c9acd0af856ae1ee45240
$ git status --porcelain
(no output)
$ timeout 300 python3 -m compileall -q bundlebees_plugin/subversion_scan.py; echo "compileall exit=$?"
compileall exit=0
```

### Probe 1 — the 14 acceptance criteria, current code

Exact source (heredoc body, run from the repo root with `PYTHONPATH=.`):

```python
import bundlebees_plugin.subversion_scan as s

def new_file(path, *lines):
    body = "".join(f"+{l}\n" for l in lines)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
            f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n" + body)

def edit(path, added, removed=None):
    removed = removed or []
    body = "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(removed)} +1,{len(added)} @@\n" + body)

def kinds(p):
    return sorted({f["kind"] for f in s.scan_patch(p)})

print("AC-001/002 quoted path unicode separators")
for esc, label in (("\\u2028","u2028"), ("\\u2029","u2029")):
    patch = ('diff --git a/src/evil.py b/src/evil.py\n'
             '--- /dev/null\n'
             f'+++ "b/src/x{esc}spoof.py"\n'
             '@@ -0,0 +1,1 @@\n+os._exit(0)\n')
    fs = s.scan_patch(patch)
    print(label, "findings=", fs,
          "clean=", all(chr(0x2028) not in f["path"] and chr(0x2029) not in f["path"] for f in fs))

print("\nAC-003/004/005 quoted pytest11 table")
for line, label in (('[project.entry-points."pytest11"]', "double-quoted"),
                    ("[project . entry-points . pytest11]", "dotted-spaces"),
                    ("[ project . entry-points . 'pytest11' ]", "single-quoted-spaces")):
    print(label, "->", kinds(edit("pyproject.toml", [line])))

print("\nAC-006/007/008 -p at end of line")
for path, lines, label in (
    ("pyproject.toml", ['addopts = """', "-q", "-p", "myplugin", '"""'], "pyproject-lines0"),
    ("setup.cfg", ["addopts =", "    -q", "    -p", "    myplugin"], "setup.cfg-lines1"),
    ("pytest.ini", ["addopts = -q -p", "    myplugin"], "pytest.ini-lines2")):
    print(label, "->", kinds(edit(path, lines)))

print("\nAC-010 existing C0 quoted path (newline/CR)")
patch = ('diff --git a/src/evil.py b/src/evil.py\n--- /dev/null\n'
         '+++ "b/src/x\\nspoof.py"\n@@ -0,0 +1,1 @@\n+os._exit(0)\n')
fs = s.scan_patch(patch)
print("findings=", fs, "clean=", all("\n" not in f["path"] and "\r" not in f["path"] for f in fs))

print("\nAC-011 multiline addopts")
for path, lines, label in (
    ("pyproject.toml", ['addopts = """', "-q", "-p myplugin", '"""'], "pyproject"),
    ("setup.cfg", ["addopts =", "    -q", "    -p myplugin"], "setup.cfg")):
    print(label, "->", kinds(edit(path, lines)))

print("\nAC-012 plain pytest11")
print("->", kinds(edit("pyproject.toml", ["[project.entry-points.pytest11]"])))
print("setup.cfg ->", kinds(edit("setup.cfg", ["pytest11 = mypkg.hooks"])))

print("\nAC-013 no:cacheprovider")
print("->", kinds(edit("pyproject.toml", ['addopts = "-q -p no:cacheprovider"'])))

print("\nAC-014 benign")
print("->", s.scan_patch(edit("src/pkg/core.py", ["def value():", "    return 2"], ["def value():", "    return 1"])))
```

Printed output:

```
AC-001/002 quoted path unicode separators
u2028 findings= [{'kind': 'forced_exit', 'path': 'src/xspoof.py', 'line': 1}] clean= True
u2029 findings= [{'kind': 'forced_exit', 'path': 'src/xspoof.py', 'line': 1}] clean= True

AC-003/004/005 quoted pytest11 table
double-quoted -> ['pytest_plugin']
dotted-spaces -> ['pytest_plugin']
single-quoted-spaces -> ['pytest_plugin']

AC-006/007/008 -p at end of line
pyproject-lines0 -> ['pytest_plugin']
setup.cfg-lines1 -> ['pytest_plugin']
pytest.ini-lines2 -> ['pytest_plugin']

AC-010 existing C0 quoted path (newline/CR)
findings= [{'kind': 'forced_exit', 'path': 'src/xspoof.py', 'line': 1}] clean= True

AC-011 multiline addopts
pyproject -> ['pytest_plugin']
setup.cfg -> ['pytest_plugin']

AC-012 plain pytest11
-> ['pytest_plugin']
setup.cfg -> ['pytest_plugin']

AC-013 no:cacheprovider
-> ['pytest_plugin']

AC-014 benign
-> []
```

### Probe 2 — RED at base, reconstructed base regexes

Exact source:

```python
import re, tomllib
import bundlebees_plugin.subversion_scan as s

# --- reproduce the base regexes to show the 8 new cases are real RED guards
old_control = re.compile(r"[\x00-\x1f\x7f-\x9f]")
old_pytest11 = re.compile(r"^\s*\[\s*project\.entry-points\.pytest11\s*\]\s*(?:#.*)?$")
old_plugin = re.compile(r"(?:^|[\s\"'])(?:-p(?:\s|=|[A-Za-z_])|--plugin(?:\s|=))")

def new_file(path, *lines):
    body = "".join(f"+{l}\n" for l in lines)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
            f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n" + body)

def edit(path, added, removed=None):
    removed = removed or []
    body = "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(removed)} +1,{len(added)} @@\n" + body)

def kinds(p):
    return {f["kind"] for f in s.scan_patch(p)}

new_cases = []
for esc, label in (("\\u2028", "u2028"), ("\\u2029", "u2029")):
    new_cases.append((label, ('diff --git a/src/evil.py b/src/evil.py\n--- /dev/null\n'
        f'+++ "b/src/x{esc}spoof.py"\n@@ -0,0 +1,1 @@\n+os._exit(0)\n'), None))
for line, label in (('[project.entry-points."pytest11"]', "double-quoted"),
                    ("[project . entry-points . pytest11]", "dotted-spaces"),
                    ("[ project . entry-points . 'pytest11' ]", "single-quoted-spaces")):
    new_cases.append((label, edit("pyproject.toml", [line]), "pytest_plugin"))
for path, lines, label in (
    ("pyproject.toml", ['addopts = """', "-q", "-p", "myplugin", '"""'], "pyproject-eol"),
    ("setup.cfg", ["addopts =", "    -q", "    -p", "    myplugin"], "setup.cfg-eol"),
    ("pytest.ini", ["addopts = -q -p", "    myplugin"], "pytest.ini-eol")):
    new_cases.append((label, edit(path, lines), "pytest_plugin"))

s._CONTROL_CHARS_RE = old_control
s._PYTEST11_SECTION_RE = old_pytest11
s._PLUGIN_OPTION_RE = old_plugin
print("BASE-SIMULATED failures (expect 8):")
failed = 0
for label, patch, _ in new_cases:
    if "u202" in label:
        fs = s.scan_patch(patch)
        ok = bool(fs) and all(chr(0x2028) not in f["path"] and chr(0x2029) not in f["path"] for f in fs)
    else:
        ok = kinds(patch) >= {"pytest_plugin"}
    print(f"  {label}: {'PASS' if ok else 'FAIL'}")
    failed += 0 if ok else 1
print("  total failed:", failed)
```

Printed output:

```
BASE-SIMULATED failures (expect 8):
  u2028: FAIL
  u2029: FAIL
  double-quoted: FAIL
  dotted-spaces: FAIL
  single-quoted-spaces: FAIL
  pyproject-eol: FAIL
  setup.cfg-eol: FAIL
  pytest.ini-eol: FAIL
  total failed: 8
```

### Probe 3 — false positives, equivalent TOML, `--plugin` EOL, regex scope

Exact source:

```python
import tomllib, re
import bundlebees_plugin.subversion_scan as s

def edit(path, added, removed=None):
    removed = removed or []
    body = "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(removed)} +1,{len(added)} @@\n" + body)

def kinds(p):
    return sorted({f["kind"] for f in s.scan_patch(p)})

print("Equivalent-TOML probe: does the scanner flag these?")
variants = [
    '[project.entry-points.pytest11]',
    '[project.entry-points."pytest11"]',
    '[project."entry-points".pytest11]',
    '["project"."entry-points"."pytest11"]',
    "[project.'entry-points'.'pytest11']",
    '[project.entry-points."pytest\\u0031\\u0031"]',
]
for v in variants:
    # semantic check: does tomllib produce the same project.entry-points.pytest11 path?
    try:
        doc = tomllib.loads(v + "\n")
        proj = doc.get("project", {}).get("entry-points", {})
        same = "pytest11" in proj
    except Exception as e:
        same = f"parse-error:{type(e).__name__}"
    print(f"  {v!r:56} scan={kinds(edit('pyproject.toml',[v]))}  tomllib_pytest11_table={same}")

print("\nFalse-positive probes (all should be no pytest_plugin):")
benign = [
    ("pyproject.toml", ['addopts = "-q"']),
    ("pyproject.toml", ['addopts = "-k test_one"']),
    ("pyproject.toml", ['[project.entry-points."pytest11x"]']),
    ("pyproject.toml", ["[project.entry-points.pytest11.foo]"]),
    ("pyproject.toml", ['[tool.pytest.ini_options]']),
    ("setup.cfg", ["filterwarnings = -p"]),
    ("setup.cfg", ["addopts = -q"]),
    ("pytest.ini", ["addopts = -q --maxfail=1"]),
    ("docs/notes.md", ["Use -p at the end of a line."]),
]
for path, lines in benign:
    print(f"  {path:16} {lines[0]!r:50} -> {kinds(edit(path, lines))}")

print("\nEnd-of-line --plugin probe (analogous form):")
for path, lines, label in (
    ("pyproject.toml", ['addopts = """', "-q", "--plugin", "myplugin", '"""'], "pyproject --plugin EOL"),
    ("setup.cfg", ["addopts =", "    -q", "    --plugin", "    myplugin"], "setup.cfg --plugin EOL")):
    print(f"  {label:32} -> {kinds(edit(path, lines))}")

print("\nOver-match probe for pytest11 regex against longer tables:")
for line in ['[project.entry-points.pytest11]', '[project.entry-points.pytest11x]',
             '[project.entry-points.pytest11.foo]', '[project.entry-points.pytest11] trailing']:
    print(f"  {line!r:50} section_match={bool(s._PYTEST11_SECTION_RE.search(line))}")
```

Printed output:

```
Equivalent-TOML probe: does the scanner flag these?
  '[project.entry-points.pytest11]'                        scan=['pytest_plugin']  tomllib_pytest11_table=True
  '[project.entry-points."pytest11"]'                      scan=['pytest_plugin']  tomllib_pytest11_table=True
  '[project."entry-points".pytest11]'                      scan=[]  tomllib_pytest11_table=True
  '["project"."entry-points"."pytest11"]'                  scan=[]  tomllib_pytest11_table=True
  "[project.'entry-points'.'pytest11']"                    scan=[]  tomllib_pytest11_table=True
  '[project.entry-points."pytest\\u0031\\u0031"]'          scan=[]  tomllib_pytest11_table=True

False-positive probes (all should be no pytest_plugin):
  pyproject.toml   'addopts = "-q"'                                   -> []
  pyproject.toml   'addopts = "-k test_one"'                          -> []
  pyproject.toml   '[project.entry-points."pytest11x"]'               -> []
  pyproject.toml   '[project.entry-points.pytest11.foo]'              -> []
  pyproject.toml   '[tool.pytest.ini_options]'                        -> []
  setup.cfg        'filterwarnings = -p'                              -> []
  setup.cfg        'addopts = -q'                                     -> []
  pytest.ini       'addopts = -q --maxfail=1'                         -> []
  docs/notes.md    'Use -p at the end of a line.'                     -> []

End-of-line --plugin probe (analogous form):
  pyproject --plugin EOL           -> []
  setup.cfg --plugin EOL           -> []

Over-match probe for pytest11 regex against longer tables:
  '[project.entry-points.pytest11]'                  section_match=True
  '[project.entry-points.pytest11x]'                 section_match=False
  '[project.entry-points.pytest11.foo]'              section_match=False
  '[project.entry-points.pytest11] trailing'         section_match=False
```

### Probe 4 — the real refusal message (`verify_local` with an in-memory stub)

`verify_local` imports `jsonschema` transitively, which is absent in this
sandbox. I injected a throwaway in-memory module (no files, no network) only
so the real `check_patch_subversion` could be imported and run.

Exact source:

```python
import sys, types

# In-memory stub only: no files, no network. verify_local imports jsonschema via tdd.
js = types.ModuleType("jsonschema")
class _E(Exception): pass
js.ValidationError = type("ValidationError", (_E,), {})
js.SchemaError = type("SchemaError", (_E,), {})
js.validate = lambda *a, **k: None
sys.modules["jsonschema"] = js

from bundlebees_plugin import verify_local, subversion_scan as s

def edit(path, added, removed=None):
    removed = removed or []
    body = "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(removed)} +1,{len(added)} @@\n" + body)

print("Refusal-message probe (U+2028/U+2029 must be absent):")
for esc, label in (("\\u2028", "u2028"), ("\\u2029", "u2029")):
    patch = ('diff --git a/src/evil.py b/src/evil.py\n--- /dev/null\n'
             f'+++ "b/src/x{esc}spoof.py"\n@@ -0,0 +1,1 @@\n+os._exit(0)\n')
    try:
        verify_local.check_patch_subversion(patch)
        print(label, "NO REFUSAL (unexpected)")
    except verify_local.VerifyError as e:
        m = e.message
        print(label, "code=", e.code,
              "u2028=", chr(0x2028) in m, "u2029=", chr(0x2029) in m,
              "nl=", "\n" in m, "cr=", "\r" in m, "msg=", repr(m))

print("\nSettled decision probe (-p no:cacheprovider must refuse):")
p = edit("pyproject.toml", ['addopts = "-q -p no:cacheprovider"'])
try:
    verify_local.check_patch_subversion(p)
    print("NO REFUSAL (BLOCKER)")
except verify_local.VerifyError as e:
    print("refused code=", e.code, "msg=", repr(e.message))

print("\nResidual-evasion refusal check (quoted entry-points spelling):")
for line in ('[project."entry-points".pytest11]',
             '["project"."entry-points"."pytest11"]'):
    p = edit("pyproject.toml", [line])
    try:
        verify_local.check_patch_subversion(p)
        print(repr(line), "-> NOT REFUSED")
    except verify_local.VerifyError as e:
        print(repr(line), "-> refused", e.code)
```

Printed output:

```
Refusal-message probe (U+2028/U+2029 must be absent):
u2028 code= test_subversion u2028= False u2029= False nl= False cr= False msg= 'patch can subvert the approved test run: 1 finding(s): forced_exit@src/xspoof.py:1'
u2029 code= test_subversion u2028= False u2029= False nl= False cr= False msg= 'patch can subvert the approved test run: 1 finding(s): forced_exit@src/xspoof.py:1'

Settled decision probe (-p no:cacheprovider must refuse):
refused code= test_subversion msg= 'patch can subvert the approved test run: 1 finding(s): pytest_plugin@pyproject.toml:1'

Residual-evasion refusal check (quoted entry-points spelling):
'[project."entry-points".pytest11]' -> NOT REFUSED
'["project"."entry-points"."pytest11"]' -> NOT REFUSED
```

### Probe 5 — comment-line over-match (Finding 1) and `configparser` check

Exact source:

```python
import configparser
import bundlebees_plugin.subversion_scan as s

def edit(path, added, removed=None):
    removed = removed or []
    body = "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(removed)} +1,{len(added)} @@\n" + body)

def kinds(p):
    return sorted({f["kind"] for f in s.scan_patch(p)})

print("Comment-line over-match probe (new due to -p$):")
cases = [
    ("setup.cfg", ["addopts =", "    -q", "    # prefer -p", "    -x"]),
    ("setup.cfg", ["addopts =", "    -q", "    ; prefer -p", "    -x"]),
    ("pytest.ini", ["addopts = -q", "    # prefer -p", "    -x"]),
    ("tox.ini", ["addopts =", "    -q", "    # prefer -p"]),
]
for path, lines in cases:
    print(f"  {path:12} {lines[2]!r:20} -> {kinds(edit(path, lines))}")

print("\nconfigparser view of the setup.cfg comment line:")
cp = configparser.ConfigParser()
cp.read_string("[tool:pytest]\naddopts =\n    -q\n    # prefer -p\n    -x\n")
print("  addopts=", repr(cp["tool:pytest"]["addopts"]))

print("\nSame comment line at base (old -p regex, no $):")
import re
saved = s._PLUGIN_OPTION_RE
s._PLUGIN_OPTION_RE = re.compile(r"(?:^|[\s\"'])(?:-p(?:\s|=|[A-Za-z_])|--plugin(?:\s|=))")
for path, lines in cases[:3]:
    print(f"  {path:12} -> {kinds(edit(path, lines))}")
s._PLUGIN_OPTION_RE = saved

print("\nNon-comment benign addopts lines (must stay clean):")
for path, lines in [
    ("setup.cfg", ["addopts =", "    -q", "    --strict-markers"]),
    ("pytest.ini", ["addopts = -q --maxfail=1"]),
]:
    print(f"  {path:12} -> {kinds(edit(path, lines))}")
```

Printed output:

```
Comment-line over-match probe (new due to -p$):
  setup.cfg    '    # prefer -p'    -> ['pytest_plugin']
  setup.cfg    '    ; prefer -p'    -> ['pytest_plugin']
  pytest.ini   '    -x'             -> ['pytest_plugin']
  tox.ini      '    # prefer -p'    -> ['pytest_plugin']

configparser view of the setup.cfg comment line:
  addopts= '\n-q\n-x'

Same comment line at base (old -p regex, no $):
  setup.cfg    -> []
  setup.cfg    -> []
  pytest.ini   -> []

Non-comment benign addopts lines (must stay clean):
  setup.cfg    -> []
  pytest.ini   -> []
```

(For the `pytest.ini` row the printed label is `lines[2]`, `'    -x'`; the
flagged comment is `lines[1]`, `'    # prefer -p'`. The `base` run for the
same three cases is the meaningful comparison and is clean.)

## Security and hygiene

- No secrets, tokens, tailnet hosts or absolute private paths are introduced
  by the product change; the file contains only regexes and path handling.
- No network, no `gh`, no push/merge was performed.
- The only file written by this review is this file.
