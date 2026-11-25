VERDICT: CHANGES

# Review: WA-A1A-FUP3 subversion-scan deletion false positives (PR #292, head 266016a)

I did not write this code. I reviewed the checkout as it stands (synthetic local
commit `9806e5f` titled `base 266016a2adbcbf38215e3e23f1d9f4a2480fb61c`; the
real base `dd77792` and head `266016a` objects are absent, `git cat-file` -> 128).
The change is the five described hunks in `bundlebees_plugin/subversion_scan.py`
plus the coordinator `CHANGELOG.md` bullet; the approved test file
`tests/test_subversion_scan.py` is byte-identical to RED per the supplied fact
(sha256 `9ebc530b2f77d16ff5c0a3b12497caab802d77a321aac714a4d826473223e3fa`).
I could not run pytest (not installed in this image), so the AC assessment is
from static reading plus the probes below; the coordinator's GREEN/3783-case
compare is taken as supplied.

All twelve ACs are genuinely met by the code and by the named test, and for
text patches the intended fail-closed properties hold: a deleted file mode
cannot hide a later added line that `git apply` will accept, `file_level` is
reset in every `_flush_file`, the `--- /dev/null` test does not misfire on
valid ordinary paths, and `from os import _exit`, an aliased `os._exit(0)` and a
bare `_exit(0)` still produce `forced_exit`. However, the new "only a creation
or rename is file-level" rule introduces one real regression for `GIT binary
patch` blocks: a git-accepted binary-marked **modification** of an existing
risky path (for example replacing a text `tests/conftest.py` with
`os._exit(0)`) adds no `+` line and no creation marker, so it now scans as `[]`
where base reported `conftest@tests/conftest.py:1`. `git apply` accepts such a
   patch without `--binary` and writes the new text. The declared-files scope
   check does not replace the scan: a lease that includes `tests/conftest.py`
   (or any assigned risky path) passes it, and the scan is the only gate that
   is supposed to notice the content. That is a blocker for the fail-closed
   requirement (finding 1). Finding 2 is a narrow introduced forced-exit
edge. Finding 3 is a pre-existing parser-boundary bypass in the same function,
not introduced by these hunks; it needs its own slice.

## Findings

1. **blocker (introduced fail-open; `bundlebees_plugin/subversion_scan.py:241-251`,
   `:209-216`)** - the new file-level keys (`new file mode`, `deleted file mode`,
   `--- /dev/null`, `rename to`) do not cover a `GIT binary patch` block. A
   binary-marked **modification** of an existing risky path has neither a `+`
   line nor a creation/deletion marker, so `file_level` stays false and
   `_flush_file` emits nothing. Base emitted the path kind for every block with
   no added line, so this is a regression, not a pre-existing gap. Demonstrated
   below (Probe 6): starting from a normal text `tests/conftest.py` containing
   `import os`, a patch that marks the file binary in the diff (`GIT binary
   patch` with a literal payload) and changes it to `os._exit(0)` is accepted by
   the exact verify-style command `git apply --check --whitespace=nowarn -`
   (no `--binary` needed), applies the text `os._exit(0)`, and
   `scan_patch` returns `[]`; reconstructed base returns
   `conftest@tests/conftest.py:1`. A binary deletion is correctly silent and a
   binary addition is correctly flagged, so only the modification shape is
   missing. Concrete fix: in the `not state["in_hunk"]` block, add
   `elif diff_line.startswith("GIT binary patch"): state["file_level"] = True`.
   Because a binary deletion also carries `deleted file mode` (which sets
   `path=None`) and an addition carries `new file mode`, this restores exactly
   the modified-risky-path finding without affecting AC-003/AC-004 (text
   deletion-only and mode-only diffs never contain `GIT binary patch`). Add a
   test that generates this shape (or a real binary fixture) and asserts
   `conftest@tests/conftest.py:1`; Probe 7 shows the one-line fix keeps all
   twelve AC shapes unchanged.

2. **minor (introduced fail-open; `bundlebees_plugin/subversion_scan.py:182-184`,
   regex at `:24`)** - the `forced_exit` exclusion is applied to the whole added
   line: `if kind == "forced_exit" and _EXIT_DEFINITION_RE.search(content):
   continue`. A one-line helper that is also a real forced exit,
   `def _exit(code): os._exit(code)`, therefore produces no finding at all. That
   is a regression against base: reconstructed base reports
   `forced_exit@src/pkg/core.py:1` for that line, head reports `[]`. It becomes a
   complete forced-exit bypass when the call is aliased
   (`def _exit(code): os._exit(code)` / `ex = _exit` / `ex(0)` -> `[]`), because
   no added line then contains the literal `_exit(` call the pattern needs.
   Impact is limited: the gate is already explicitly a content-level filter (a
   plain `getattr(os, "_exit")(0)` is missed at base and head), and the multi-line
   `def _exit(code):` + `    os._exit(code)` case still reports at line 2.
   Concrete fix: do not skip the kind; search the part of the line after the
   definition prefix, e.g.
   `m = _EXIT_DEFINITION_RE.match(content); probe = content[m.end():] if m else content`
   and run the `forced_exit` pattern against `probe`. That keeps AC-011
   (`def _exit(code):` -> no finding) while still catching `os._exit(` on the
   same line.

3. **major (pre-existing, not introduced by this PR; `bundlebees_plugin/subversion_scan.py:238-267`,
   with `:151-167` and `:135-148`)** - file blocks are delimited only by
   `diff --git` (`:241`), and `_body_step` never ends a hunk on a `---`/`+++`
   header. A git-accepted unified diff with two file sections and **no**
   `diff --git` therefore keeps `in_hunk=True`; the second section's
   `---`/`+++`/`@@` are consumed as hunk body and its added lines are scanned
   against the first file's path. Demonstrated below with a patch that edits an
   assigned `docs/notes.md` and then adds `import os; os._exit(0)` to an assigned
   `src/b.py`: `git apply --check` ACCEPTs, `git apply` really writes the
   `os._exit(0)` line, and `scan_patch` returns `[]` (a second probe creates
   `tests/conftest.py` the same way with `scan_patch == []` after a real apply).
   This gap exists at base too - a reconstructed base returns `[]` for the same
   patch - so it is not caused by this change and must not block this PR, but
   the gate is bypassable today and this needs its own slice. Concrete fix:
   treat a `--- ` line while `state["in_hunk"]` as a new file section (flush,
   reset `path`/`in_hunk`/`addopts`, then run the header logic), or refuse any
   patch whose file headers are not preceded by `diff --git `; add the
   two-section no-`diff --git` shape to `tests/test_subversion_scan.py`.

One blocker finding (finding 1); no other blocker.

## AC assessment

| AC | Approved test (`tests/test_subversion_scan.py`) | Result |
| --- | --- | --- |
| AC-001 | `test_deleting_an_empty_conftest_is_not_a_finding` (:322) | PASS - `[]` for `deleted file mode` with no `+++`/hunk |
| AC-002 | `test_deleting_an_empty_risky_file_is_not_a_finding` (:326) | PASS - `[]` for empty `sitecustomize.py` and `src/evil.pth` |
| AC-003 | `test_deletion_only_edit_of_conftest_is_not_a_finding` (:341) | PASS - context + `-` line only, `path_reported` untouched, `file_level` false |
| AC-004 | `test_mode_only_change_of_conftest_is_not_a_finding` (:354) | PASS - `old mode`/`new mode` set no flag |
| AC-005 | `test_verify_accepts_deleting_a_conftest` (:335) | PASS - `check_patch_subversion` returns `[]` for both delete shapes (probed with a `jsonschema` import stub; the function itself is untouched) |
| AC-006 | `test_deleting_a_non_empty_conftest_is_not_a_finding` (:331) | PASS - `+++ /dev/null` keeps `path=None`; minus-only hunk |
| AC-007 | `test_edit_that_adds_a_line_to_conftest_is_still_a_finding` (:358) | PASS - added line reported at its new line (probe: line 3 when added at new line 3) |
| AC-008 | `test_empty_added_file_is_a_finding` (:149) | PASS - `new file mode` + empty block -> line 1 for conftest/startup/pth |
| AC-009 | `test_pure_rename_to_conftest_is_a_finding` (:154) | PASS - `rename to` sets `file_level`; path resolved by `_header_state` |
| AC-010 | `test_rename_away_from_conftest_is_not_a_finding` (:161) | PASS - new path is not risky, so `_path_kinds` is empty |
| AC-011 | `test_user_defined_exit_helper_is_not_a_forced_exit` (:366) | PASS - `def _exit` and `async def _exit` -> no finding (see finding 2 for the one-line caveat) |
| AC-012 | `test_evasive_runner_subversion_is_a_finding` (:168, `from os import _exit; _exit(0)`) | PASS - `forced_exit` at line 1; aliased and bare calls too |

The approved test file was read in full: 31 `test_` functions (parametrized),
no `skip`, `xfail`, `try/except` or weakened assertion; I made no edit to it.
I cannot byte-diff it against RED because this checkout has no base object, so I
rely on the supplied identity fact and on the content mapping above.

## Fail-closed checks requested in the brief

- **`file_level` leaking into the next block:** no. `_flush_file` resets
  `path_reported`, `file_level` and `addopts` (`:209-216`) and is called at the
  top of every `diff --git` line (`:241-242`). Probe: create a risky empty
  `tests/conftest.py`, then a mode change of `docs/x.md` -> only the conftest
  finding.
- **`deleted file mode` setting `path=None` hiding a later added line:** for a
  patch `git apply` accepts, no. The two crafted deletion-claim forms with added
  content are rejected by `git apply` (`deleted file ... still has contents` and
  `bad git-diff - expected /dev/null`); the form with `+++ b/<path>` restores the
  path and reports both `conftest` and `forced_exit`. Without `+++`, head misses
  the line but git rejects the patch, so the verify gate still refuses at
  `patch_conflict`/`patch_failed`.
- **Binary (`GIT binary patch`) blocks:** **this is the blocker (finding 1).**
  A binary-marked *modification* of a risky path has no `+` line, so the new
  `file_level` rule leaves it silent, while base reported the path at line 1.
  A binary deletion is correctly silent (`deleted file mode` clears `path`) and
  a binary addition is correctly caught (`new file mode`), so the fix is to
  also set `file_level` on `GIT binary patch`; the proposed one-line change is
  simulated in Probe 7 and leaves all twelve AC shapes unchanged.
- **`--- /dev/null` misfiring on an ordinary path that fails to parse:** no for
  valid paths. `_path_value` only returns `None` for the literal sentinel
  (after quote decoding and tab/control stripping); `a/dev/null`, `dev/null`,
  an unterminated quoted path and paths with a trailing timestamp are not
  `None`, and deletion-only edits with those headers stay `[]`. One theoretical
  edge: control-character stripping maps a quoted absolute
  `"/dev/nul\x01l"` to the sentinel; git rejects absolute paths, and the
  direction is only an extra `file_level` (a false positive, never a lost
  finding), so no action.
- **Forced-exit exclusion breadth:** `from os import _exit`,
  `from os import _exit; _exit(0)`, `import os as o; o._exit(0)` and bare
  `_exit(0)` all report `forced_exit`; `def _exit(code):` and
  `async def _exit(code):` do not. The one-line definition case is finding 2.
- **Gates/tests/sandbox:** no gate file, sandbox, check or approved test was
  edited, skipped or made optional. `verify_local.check_patch_subversion` still
  calls `subversion_scan.scan_patch` and still raises
  `VerifyError("test_subversion")` on findings; the locked verify path still
  calls it before the worktree (`verify_local.py:1034`). The scan's own
  fail-closed coverage is weakened only by finding 1 (binary modifications),
  which is why this review is CHANGES.
- **Other pre-existing shapes (not part of this diff, not blocking):** a pure
  `copy to tests/conftest.py` (git copy detection) is not treated like
  `rename to` and scans as `[]`. Default `git diff` renders a copy as a new
  file, so this needs `--find-copies` to appear and is much less likely than
  finding 3; mention it only so a future test does not assume all copy shapes
  are covered.
- **Scope, secrets, hosts, paths:** only `bundlebees_plugin/subversion_scan.py`
  in the bee patch and one `CHANGELOG.md` bullet (coordinator). No new
  dependency, network call, secret, host or absolute path; `/dev/null` is a
  diff sentinel. The CHANGELOG bullet matches the code, including `new file
  mode`, `--- /dev/null`, `rename to`, the three now-accepted deletions, the
  three preserved guards, and the `_exit` wording.

## Commands run (command -> exit code)

- `timeout 900 python3 -m pytest -q -p no:cacheprovider tests/test_subversion_scan.py`
  -> 1 (`/usr/bin/python3: No module named pytest`; environment fact).
- `timeout 2400 python3 -m pytest -q -p no:cacheprovider tests`
  -> 1 (same missing pytest; full suite not runnable in this image).
- `python3 -m py_compile bundlebees_plugin/subversion_scan.py tests/test_subversion_scan.py`
  -> 0 (both parse; generated `__pycache__` removed afterwards).
- `sha256sum tests/test_subversion_scan.py bundlebees_plugin/subversion_scan.py`
  -> 0:
  `9ebc530b2f77d16ff5c0a3b12497caab802d77a321aac714a4d826473223e3fa  tests/test_subversion_scan.py`
  `c7ba552d226c62708ac9de4d69a26536c06c1e5117808e6a6f04d94edaaff197  bundlebees_plugin/subversion_scan.py`
- `git cat-file -t dd77792a3e919d6857cb80942c19d5f7f6f91607` -> 128 (absent);
  `git cat-file -t 266016a2adbcbf38215e3e23f1d9f4a2480fb61c` -> 128 (absent);
  `git rev-parse HEAD` -> `9806e5feef928a95754a83169223a28af989e292`.
- `grep -c "^def test_\|^    def test_" tests/test_subversion_scan.py` -> 0
  (31); `grep -n "skip\|xfail" tests/test_subversion_scan.py` -> 1 (no matches,
  exit 1 is grep's "not found").
- `git status --porcelain` before writing this file -> empty.

## Probes (exact source run and its printed output)

### Probe 1 - synthetic AC shapes and break attempts

Source:

```python
PYTHONPATH=. python3 - <<'PY'
from bundlebees_plugin.subversion_scan import scan_patch
import sys, types

def ways(patch):
    return [(f["kind"], f["path"], f["line"]) for f in scan_patch(patch)]

def diff(path, extra):
    return f"diff --git a/{path} b/{path}\n" + extra

def empty_delete(path):
    return diff(path, "deleted file mode 100644\nindex e69de29..0000000\n")

def delete(path, *lines):
    body = "".join(f"-{l}\n" for l in lines)
    return diff(path, f"deleted file mode 100644\n--- a/{path}\n+++ /dev/null\n@@ -1,{len(lines)} +0,0 @@\n{body}")

def deletion_only_edit(path):
    return diff(path, "index c01d8a7..21b405d 100644\n"
                      f"--- a/{path}\n+++ b/{path}\n@@ -1,2 +1 @@\n import os\n-os._exit(0)\n")

def mode_change(path):
    return diff(path, "old mode 100644\nnew mode 100755\n")

def new_empty(path):
    return diff(path, "new file mode 100644\nindex 0000000..e69de29\n")

def rename(old, new):
    return f"diff --git a/{old} b/{new}\nsimilarity index 100%\nrename from {old}\nrename to {new}\n"

def edit(path, added, removed=()):
    body = "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return diff(path, f"--- a/{path}\n+++ b/{path}\n@@ -1,{len(removed)} +1,{len(added)} @@\n{body}")

print("AC-001/AC-002 empty deletions:")
for p in ("tests/conftest.py", "sitecustomize.py", "src/evil.pth"):
    print(f"  empty_delete({p}) -> {ways(empty_delete(p))}")
print("AC-003 deletion-only edit of conftest:")
print("  ->", ways(deletion_only_edit("tests/conftest.py")))
print("AC-004 mode-only change of conftest:")
print("  ->", ways(mode_change("tests/conftest.py")))
print("AC-006 non-empty deletion of conftest:")
print("  ->", ways(delete("tests/conftest.py", "import os", "os._exit(0)")))
print("AC-007 add one line to conftest at new line 3:")
p = ("diff --git a/tests/conftest.py b/tests/conftest.py\n"
     "--- a/tests/conftest.py\n+++ b/tests/conftest.py\n"
     "@@ -1,2 +1,3 @@\n import os\n import sys\n+import pytest\n")
print("  ->", ways(p))
print("AC-008 empty added risky files:")
for p in ("tests/conftest.py", "sitecustomize.py", "src/evil.pth"):
    print(f"  new_empty({p}) -> {ways(new_empty(p))}")
print("AC-009 pure rename to conftest / AC-010 rename away:")
print("  to   ->", ways(rename("tests/old_helpers.py", "tests/conftest.py")))
print("  away ->", ways(rename("tests/conftest.py", "tests/helpers.py")))
print("AC-011 user-defined helpers / AC-012 os _exit import:")
print("  def _exit(code):              ->", ways(edit("src/pkg/core.py", ["def _exit(code):"])))
print("  async def _exit(code):        ->", ways(edit("src/pkg/core.py", ["async def _exit(code):"])))
print("  def _exit + body os._exit     ->", ways(edit("src/pkg/core.py", ["def _exit(code):", "    os._exit(code)"])))
print("  from os import _exit          ->", ways(edit("src/pkg/core.py", ["from os import _exit"])))
print("  from os import _exit; _exit(0)->", ways(edit("src/pkg/core.py", ["from os import _exit; _exit(0)"])))
print("  import os as o; o._exit(0)    ->", ways(edit("src/pkg/core.py", ["import os as o; o._exit(0)"])))
print("  bare _exit(0)                 ->", ways(edit("src/pkg/core.py", ["_exit(0)"])))
print()
print("BREAK: create risky + delete another risky:")
print("  ->", ways(new_empty("tests/conftest.py") + "\n" + empty_delete("src/evil.pth")))
print("BREAK: delete risky + edit another risky:")
print("  ->", ways(empty_delete("tests/conftest.py") + "\n" + edit("sitecustomize.py", ["x = 1"])))
print("BREAK: deleted file mode with no path header:")
print("  ->", ways("diff --git a/tests/conftest.py b/tests/conftest.py\ndeleted file mode 100644\n"))
print("BREAK: deleted file mode + added lines, no +++ header:")
p = ("diff --git a/tests/conftest.py b/tests/conftest.py\ndeleted file mode 100644\n"
     "--- a/tests/conftest.py\n@@ -1 +1,2 @@\n import os\n+os._exit(0)\n")
print("  ->", ways(p))
print("BREAK: deleted file mode + added lines, with +++ b/ header:")
p = ("diff --git a/tests/conftest.py b/tests/conftest.py\ndeleted file mode 100644\n"
     "--- a/tests/conftest.py\n+++ b/tests/conftest.py\n@@ -1 +1,2 @@\n import os\n+os._exit(0)\n")
print("  ->", ways(p))
print("BREAK: hunk body line whose content starts with two plus signs:")
p = ("diff --git a/tests/conftest.py b/tests/conftest.py\nnew file mode 100644\n"
     "--- /dev/null\n+++ b/tests/conftest.py\n@@ -0,0 +1,2 @@\n+import pytest\n+++ b/src/benign.py\n")
print("  ->", ways(p))
print("BREAK: hunk body line whose content is '-- /dev/null':")
p = ("diff --git a/tests/conftest.py b/tests/conftest.py\n--- a/tests/conftest.py\n"
     "+++ b/tests/conftest.py\n@@ -1 +1,2 @@\n import os\n+-- /dev/null\n")
print("  ->", ways(p))
print("BREAK: +++ /dev/null with no deleted file mode line:")
print("  ->", ways(diff("tests/conftest.py", "--- a/tests/conftest.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-import os\n")))
print("BREAK: odd but non-/dev/null --- headers in a deletion-only edit (must stay empty):")
for hdr in ('--- "a/tests/conftest.py"', '--- a/tests/con\x01ftest.py',
            '--- "a/tests/conftest.py', '--- a/tests/conftest.py\t2026-01-01'):
    p = deletion_only_edit("tests/conftest.py").replace("--- a/tests/conftest.py", hdr, 1)
    print(f"  {hdr!r} -> {ways(p)}")
print("BREAK: file_level across a following diff --git block (must not leak):")
print("  ->", ways(new_empty("tests/conftest.py") + "\n" + mode_change("docs/x.md")))
print("BREAK: no diff --git separator, two file sections (git ACCEPTS this):")
p = ("--- a/src/benign.py\n+++ b/src/benign.py\n@@ -1 +1 @@\n-x\n+y\n"
     "--- /dev/null\n+++ b/tests/conftest.py\n@@ -0,0 +1 @@\n+import os\n")
print("  ->", ways(p))
print("BREAK: pure copy to conftest:")
print("  ->", ways("diff --git a/tests/helpers.py b/tests/conftest.py\nsimilarity index 100%\ncopy from tests/helpers.py\ncopy to tests/conftest.py\n"))
print("BREAK: one-line _exit helper that does force an exit, then alias call:")
p = edit("src/pkg/core.py", ["def _exit(code): os._exit(code)", "ex = _exit", "ex(0)"])
print("  ->", ways(p))
print("BREAK: mode change plus an added line at new line 2:")
p = diff("tests/conftest.py", "old mode 100644\nnew mode 100755\n--- a/tests/conftest.py\n+++ b/tests/conftest.py\n@@ -1 +1,2 @@\n import os\n+import pytest\n")
print("  ->", ways(p))
print()
print("convertible through verify_local.check_patch_subversion (jsonschema stubbed, unrelated import):")
sys.modules["jsonschema"] = types.ModuleType("jsonschema")
from bundlebees_plugin import verify_local
for label, patch in (("empty delete conftest", empty_delete("tests/conftest.py")),
                     ("non-empty delete conftest", delete("tests/conftest.py", "import os", "os._exit(0)"))):
    print(f"  {label} -> OK {verify_local.check_patch_subversion(patch)}")
try:
    verify_local.check_patch_subversion(new_empty("tests/conftest.py"))
except verify_local.VerifyError as e:
    print(f"  empty added conftest -> REFUSED {e.code}: {e.message}")
PY
```

Output (exact):

```text
AC-001/AC-002 empty deletions:
  empty_delete(tests/conftest.py) -> []
  empty_delete(sitecustomize.py) -> []
  empty_delete(src/evil.pth) -> []
AC-003 deletion-only edit of conftest:
  -> []
AC-004 mode-only change of conftest:
  -> []
AC-006 non-empty deletion of conftest:
  -> []
AC-007 add one line to conftest at new line 3:
  -> [('conftest', 'tests/conftest.py', 3)]
AC-008 empty added risky files:
  new_empty(tests/conftest.py) -> [('conftest', 'tests/conftest.py', 1)]
  new_empty(sitecustomize.py) -> [('startup_hook', 'sitecustomize.py', 1)]
  new_empty(src/evil.pth) -> [('pth_file', 'src/evil.pth', 1)]
AC-009 pure rename to conftest / AC-010 rename away:
  to   -> [('conftest', 'tests/conftest.py', 1)]
  away -> []
AC-011 user-defined helpers / AC-012 os _exit import:
  def _exit(code):              -> []
  async def _exit(code):        -> []
  def _exit + body os._exit     -> [('forced_exit', 'src/pkg/core.py', 2)]
  from os import _exit          -> [('forced_exit', 'src/pkg/core.py', 1)]
  from os import _exit; _exit(0)-> [('forced_exit', 'src/pkg/core.py', 1)]
  import os as o; o._exit(0)    -> [('forced_exit', 'src/pkg/core.py', 1)]
  bare _exit(0)                 -> [('forced_exit', 'src/pkg/core.py', 1)]

BREAK: create risky + delete another risky:
  -> [('conftest', 'tests/conftest.py', 1)]
BREAK: delete risky + edit another risky:
  -> [('startup_hook', 'sitecustomize.py', 1)]
BREAK: deleted file mode with no path header:
  -> []
BREAK: deleted file mode + added lines, no +++ header:
  -> []
BREAK: deleted file mode + added lines, with +++ b/ header:
  -> [('conftest', 'tests/conftest.py', 2), ('forced_exit', 'tests/conftest.py', 2)]
BREAK: hunk body line whose content starts with two plus signs:
  -> [('conftest', 'tests/conftest.py', 1), ('conftest', 'tests/conftest.py', 2)]
BREAK: hunk body line whose content is '-- /dev/null':
  -> [('conftest', 'tests/conftest.py', 2)]
BREAK: +++ /dev/null with no deleted file mode line:
  -> []
BREAK: odd but non-/dev/null --- headers in a deletion-only edit (must stay empty):
  '--- "a/tests/conftest.py"' -> []
  '--- a/tests/con\x01ftest.py' -> []
  '--- "a/tests/conftest.py' -> []
  '--- a/tests/conftest.py\t2026-01-01' -> []
BREAK: file_level across a following diff --git block (must not leak):
  -> [('conftest', 'tests/conftest.py', 1)]
BREAK: no diff --git separator, two file sections (git ACCEPTS this):
  -> []
BREAK: pure copy to conftest:
  -> []
BREAK: one-line _exit helper that does force an exit, then alias call:
  -> []
BREAK: mode change plus an added line at new line 2:
  -> [('conftest', 'tests/conftest.py', 2)]

convertible through verify_local.check_patch_subversion (jsonschema stubbed, unrelated import):
  empty delete conftest -> OK []
  non-empty delete conftest -> OK []
  empty added conftest -> REFUSED test_subversion: patch can subvert the approved test run: 1 finding(s): conftest@tests/conftest.py:1
```

Note: the last block imports `verify_local` with an in-memory `jsonschema` stub
because this image lacks `jsonschema`; `check_patch_subversion` itself does not
use it and is unchanged by the patch.

### Probe 2 - real `git diff --cached --binary` shapes

Source:

```python
PYTHONPATH=. python3 - <<'PY'
import os, pathlib, shutil, subprocess, tempfile
from bundlebees_plugin.subversion_scan import scan_patch

repo = pathlib.Path(tempfile.mkdtemp(prefix="bb-gitdiff-", dir="/tmp/opencode"))
def git(*a):
    return subprocess.run(["git", *a], cwd=repo, text=True, capture_output=True, check=True).stdout
git("init", "-q", ".")
git("config", "user.email", "a@b"); git("config", "user.name", "a")
(repo/"tests").mkdir(); (repo/"src").mkdir()
(repo/"tests/conftest.py").write_text("import os\nos._exit(0)\n")
(repo/"src/empty.pth").write_text("")
(repo/"src/old_helpers.py").write_text("x = 1\n")
git("add", "-A"); git("commit", "-qm", "base")
base = (repo/"tests/conftest.py").read_text()

def capture(name):
    git("add", "-A")
    d = git("diff", "--cached", "--binary")
    print(f"{name}: {[(f['kind'], f['path'], f['line']) for f in scan_patch(d)]}")
    git("reset", "-q"); git("checkout", "-q", "--", "."); git("clean", "-qfd")

(repo/"src/empty.pth").unlink(); capture("git delete empty .pth")
(repo/"tests/conftest.py").unlink(); capture("git delete non-empty conftest")
(repo/"tests/conftest.py").write_text("import os\n"); capture("git deletion-only edit conftest")
os.chmod(repo/"tests/conftest.py", 0o755); capture("git mode-only conftest")
(repo/"src/newempty.pth").write_text(""); capture("git empty add .pth")
(repo/"tests/conftest.py").unlink(); os.rename(repo/"src/old_helpers.py", repo/"tests/conftest.py"); capture("git delete conftest + move helper into its path")
os.rename(repo/"tests/conftest.py", repo/"tests/helpers.py"); capture("git rename conftest away")
(repo/"tests/conftest.py").unlink(); os.symlink("/etc/passwd", repo/"tests/conftest.py"); capture("git typechange conftest -> symlink (real git emits delete + create blocks)")
(repo/"tests/conftest.py").write_text(base + "import pytest\n"); capture("git add line 3 to conftest")
print("PURE RENAME SHAPE (git format when target is absent):", [(f['kind'], f['path'], f['line']) for f in scan_patch("diff --git a/tests/old_helpers.py b/tests/conftest.py\nsimilarity index 100%\nrename from tests/old_helpers.py\nrename to tests/conftest.py\n")])
shutil.rmtree(repo)
PY
```

Output (exact):

```text
git delete empty .pth: []
git delete non-empty conftest: []
git deletion-only edit conftest: []
git mode-only conftest: []
git empty add .pth: [('pth_file', 'src/newempty.pth', 1)]
git delete conftest + move helper into its path: [('conftest', 'tests/conftest.py', 1)]
git rename conftest away: []
git typechange conftest -> symlink (real git emits delete + create blocks): [('conftest', 'tests/conftest.py', 1)]
git add line 3 to conftest: [('conftest', 'tests/conftest.py', 3)]
PURE RENAME SHAPE (git format when target is absent): [('conftest', 'tests/conftest.py', 1)]
```

### Probe 3 - what `git apply` accepts for the deletion-claim and no-`diff --git` shapes

Source:

```python
PYTHONPATH=. python3 - <<'PY'
import os, pathlib, shutil, subprocess, tempfile
from bundlebees_plugin.subversion_scan import scan_patch

repo = pathlib.Path(tempfile.mkdtemp(prefix="bb-gapply-", dir="/tmp/opencode"))
def git(*a, **kw):
    return subprocess.run(["git", *a], cwd=repo, text=True, capture_output=True, **kw)
git("init", "-q", "."); git("config", "user.email", "a@b"); git("config", "user.name", "a")
(repo/"tests").mkdir(); (repo/"tests/conftest.py").write_text("import os\n")
git("add", "-A"); git("commit", "-qm", "base")

patches = {
 "claim delete + added line, no +++ header": (
   "diff --git a/tests/conftest.py b/tests/conftest.py\ndeleted file mode 100644\n"
   "--- a/tests/conftest.py\n@@ -1 +1,2 @@\n import os\n+import pytest\n"),
 "claim delete + added line, with +++ b/ header": (
   "diff --git a/tests/conftest.py b/tests/conftest.py\ndeleted file mode 100644\n"
   "--- a/tests/conftest.py\n+++ b/tests/conftest.py\n@@ -1 +1,2 @@\n import os\n+import pytest\n"),
 "real delete": (
   "diff --git a/tests/conftest.py b/tests/conftest.py\ndeleted file mode 100644\n"
   "--- a/tests/conftest.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-import os\n"),
 "no diff --git: delete conftest then create .pth": (
   "--- a/tests/conftest.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-import os\n"
   "--- /dev/null\n+++ b/src/evil.pth\n@@ -0,0 +1 @@\n+import os\n"),
 "no diff --git: benign edit then create conftest": (
   "--- a/src/benign.py\n+++ b/src/benign.py\n@@ -1 +1 @@\n-x\n+y\n"
   "--- /dev/null\n+++ b/tests/conftest.py\n@@ -0,0 +1 @@\n+import os\n"),
}
for name, patch in patches.items():
    p = repo/"p.patch"; p.write_text(patch)
    r = git("apply", "--check", "--whitespace=nowarn", "p.patch")
    verdict = "ACCEPT" if r.returncode == 0 else "REJECT: " + r.stderr.strip().replace("\n", " ")
    print(f"{name}\n  git apply --check: {verdict}\n  scan_patch: {[(f['kind'], f['path'], f['line']) for f in scan_patch(patch)]}")
shutil.rmtree(repo)
PY
```

Output (exact):

```text
claim delete + added line, no +++ header
  git apply --check: REJECT: error: deleted file tests/conftest.py still has contents
  scan_patch: []
claim delete + added line, with +++ b/ header
  git apply --check: REJECT: error: git apply: bad git-diff - expected /dev/null on line 2
  scan_patch: [('conftest', 'tests/conftest.py', 2)]
real delete
  git apply --check: ACCEPT
  scan_patch: []
no diff --git: delete conftest then create .pth
  git apply --check: ACCEPT
  scan_patch: []
no diff --git: benign edit then create conftest
  git apply --check: REJECT: error: src/benign.py: No such file or directory error: tests/conftest.py: already exists in working directory
  scan_patch: []
```

### Probe 4 - two git-accepted no-`diff --git` bypasses (finding 3)

Source:

```python
PYTHONPATH=. python3 - <<'PY'
import pathlib, shutil, subprocess, tempfile
from bundlebees_plugin.subversion_scan import scan_patch
repo = pathlib.Path(tempfile.mkdtemp(prefix="bb-gapply3-", dir="/tmp/opencode"))
def git(*a, **kw):
    return subprocess.run(["git", *a], cwd=repo, text=True, capture_output=True, **kw)
git("init", "-q", "."); git("config", "user.email", "a@b"); git("config", "user.name", "a")
(repo/"src").mkdir(); (repo/"src/benign.py").write_text("x\n")
git("add", "-A"); git("commit", "-qm", "base")
patch = ("--- a/src/benign.py\n+++ b/src/benign.py\n@@ -1 +1 @@\n-x\n+y\n"
         "--- /dev/null\n+++ b/tests/conftest.py\n@@ -0,0 +1 @@\n+import os\n")
(repo/"p.patch").write_text(patch)
r = git("apply", "--check", "--whitespace=nowarn", "p.patch")
print("git apply --check:", "ACCEPT" if r.returncode == 0 else "REJECT: " + r.stderr.strip())
print("after a real apply, does tests/conftest.py exist?", end=" ")
r2 = git("apply", "--whitespace=nowarn", "p.patch")
print((repo/"tests/conftest.py").exists(), "| scan_patch:", [(f["kind"], f["path"], f["line"]) for f in scan_patch(patch)])
print("getattr-based forced exit (baseline evasion, present at base too):",
      [(f["kind"], f["path"], f["line"]) for f in scan_patch(
       "diff --git a/src/pkg/core.py b/src/pkg/core.py\n--- a/src/pkg/core.py\n+++ b/src/pkg/core.py\n@@ -1 +1,2 @@\n-x\n+getattr(os, \"_exit\")(0)\n")])
shutil.rmtree(repo)
PY
```

Output (exact):

```text
git apply --check: ACCEPT
after a real apply, does tests/conftest.py exist? True | scan_patch: []
getattr-based forced exit (baseline evasion, present at base too): []
```

Second demonstration, using two assigned files so the scope check does not
intervene:

```python
PYTHONPATH=. python3 - <<'PY'
import pathlib, shutil, subprocess, tempfile
from bundlebees_plugin.subversion_scan import scan_patch
repo = pathlib.Path(tempfile.mkdtemp(prefix="bb-gapply4-", dir="/tmp/opencode"))
def git(*a, **kw):
    return subprocess.run(["git", *a], cwd=repo, text=True, capture_output=True, **kw)
git("init", "-q", "."); git("config", "user.email", "a@b"); git("config", "user.name", "a")
(repo/"docs").mkdir(); (repo/"src").mkdir()
(repo/"docs/notes.md").write_text("old\n"); (repo/"src/b.py").write_text("z = 1\n")
git("add", "-A"); git("commit", "-qm", "base")
patch = ("--- a/docs/notes.md\n+++ b/docs/notes.md\n@@ -1 +1 @@\n-old\n+new\n"
         "--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n-z = 1\n+import os; os._exit(0)\n")
(repo/"p.patch").write_text(patch)
r = git("apply", "--check", "--whitespace=nowarn", "p.patch")
print("git apply --check:", "ACCEPT" if r.returncode == 0 else "REJECT: " + r.stderr.strip())
print("scan_patch:", [(f["kind"], f["path"], f["line"]) for f in scan_patch(patch)])
git("apply", "--whitespace=nowarn", "p.patch")
print("applied src/b.py:", (repo/"src/b.py").read_text().strip())
shutil.rmtree(repo)
PY
```

Output (exact):

```text
git apply --check: ACCEPT
scan_patch: []
applied src/b.py: import os; os._exit(0)
```

This is the pre-existing gap in finding 3: the second file's added line is
scanned against `docs/notes.md`, so `_scans_content` is false and the literal
`os._exit(0)` is never evaluated.

### Probe 5 - reconstructed base vs head (is each behavior new?)

The real base object is absent, so I reconstructed it **in memory** by reverting
the five described hunks from the checkout source (remove `_EXIT_DEFINITION_RE`,
remove the `forced_exit` `continue`, remove `file_level`, restore the old
`_flush_file` condition, remove the new `if not state["in_hunk"]` block). It
reproduces the supplied pre-fix findings for the named shapes (empty delete,
deletion-only edit, mode-only change, `def _exit`), which corroborates the
reconstruction; it is not a substitute for the real base object.

Source:

```python
PYTHONPATH=. python3 - <<'PY'
import types
from bundlebees_plugin import subversion_scan as new
src = open("bundlebees_plugin/subversion_scan.py").read()
s = src
s = s.replace('_EXIT_DEFINITION_RE = re.compile(r"^\\s*(?:async\\s+)?def\\s+_exit\\s*\\(")\n', '')
s = s.replace('        if kind == "forced_exit" and _EXIT_DEFINITION_RE.search(content):\n            continue\n', '')
s = s.replace('        "file_level": False,\n', '')
s = s.replace('    if path is not None and state["file_level"] and not state["path_reported"]:\n',
              '    if path is not None and not state["path_reported"]:\n')
s = s.replace('    state["file_level"] = False\n', '')
s = s.replace('''        if not state["in_hunk"]:
            if diff_line.startswith("new file mode "):
                state["file_level"] = True
            elif diff_line.startswith("deleted file mode "):
                state["path"] = None
            elif diff_line.startswith("--- ") and _path_value(diff_line[4:]) is None:
                state["file_level"] = True
            elif diff_line.startswith("rename to "):
                state["file_level"] = True
''', '')
assert "_EXIT_DEFINITION_RE" not in s and '"file_level"' not in s
mod = types.ModuleType("base_scan"); exec(compile(s, "base_scan.py", "exec"), mod.__dict__)

def edit(path, added, removed=()):
    body = "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,{len(removed)} +1,{len(added)} @@\n{body}"
def ways(f, p): return [(x["kind"], x["path"], x["line"]) for x in f(p)]
cases = {
 "empty delete conftest": "diff --git a/tests/conftest.py b/tests/conftest.py\ndeleted file mode 100644\nindex e69de29..0000000\n",
 "deletion-only edit conftest": "diff --git a/tests/conftest.py b/tests/conftest.py\n--- a/tests/conftest.py\n+++ b/tests/conftest.py\n@@ -1,2 +1 @@\n import os\n-os._exit(0)\n",
 "mode-only conftest": "diff --git a/tests/conftest.py b/tests/conftest.py\nold mode 100644\nnew mode 100755\n",
 "def _exit helper (body raise SystemExit)": edit("src/pkg/core.py", ["def _exit(code):", "    raise SystemExit(code)"]),
 "ONE-LINE def _exit: os._exit": edit("src/pkg/core.py", ["def _exit(code): os._exit(code)"]),
 "ONE-LINE def _exit + alias call": edit("src/pkg/core.py", ["def _exit(code): os._exit(code)", "ex = _exit", "ex(0)"]),
 "no-diffgit delete conftest then create .pth": ("--- a/tests/conftest.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-import os\n--- /dev/null\n+++ b/src/evil.pth\n@@ -0,0 +1 @@\n+import os\n"),
 "empty add conftest": "diff --git a/tests/conftest.py b/tests/conftest.py\nnew file mode 100644\nindex 0000000..e69de29\n",
 "pure rename to conftest": "diff --git a/tests/old_helpers.py b/tests/conftest.py\nsimilarity index 100%\nrename from tests/old_helpers.py\nrename to tests/conftest.py\n",
}
for name, p in cases.items():
    print(f"{name}\n  base={ways(mod.scan_patch, p)}\n  head={ways(new.scan_patch, p)}")
PY
```

Output (exact):

```text
empty delete conftest
  base=[('conftest', 'tests/conftest.py', 1)]
  head=[]
deletion-only edit conftest
  base=[('conftest', 'tests/conftest.py', 1)]
  head=[]
mode-only conftest
  base=[('conftest', 'tests/conftest.py', 1)]
  head=[]
def _exit helper (body raise SystemExit)
  base=[('forced_exit', 'src/pkg/core.py', 1)]
  head=[]
ONE-LINE def _exit: os._exit
  base=[('forced_exit', 'src/pkg/core.py', 1)]
  head=[]
ONE-LINE def _exit + alias call
  base=[('forced_exit', 'src/pkg/core.py', 1)]
  head=[]
no-diffgit delete conftest then create .pth
  base=[]
  head=[]
empty add conftest
  base=[('conftest', 'tests/conftest.py', 1)]
  head=[('conftest', 'tests/conftest.py', 1)]
pure rename to conftest
  base=[('conftest', 'tests/conftest.py', 1)]
  head=[('conftest', 'tests/conftest.py', 1)]
```

This shows the intended resolution of the four RED shapes, the AC-011
false-positive removal, the introduced one-line forced-exit loss (finding 2),
and that the no-`diff --git` gap (finding 3) already existed at base.

### Probe 6 - binary-marked modification of a text conftest (finding 1, blocker)

A bee can generate the patch in its own checkout with a temporary
`.gitattributes` marking the path `binary`; the resulting patch is
self-contained (`GIT binary patch` + a literal payload), carries no `+` line,
and does not require `.gitattributes` in the target.

Source:

```python
PYTHONPATH=. python3 - <<'PY'
import pathlib, shutil, subprocess, tempfile, types
from bundlebees_plugin.subversion_scan import scan_patch as head_scan
s = open("bundlebees_plugin/subversion_scan.py").read()
s = s.replace('_EXIT_DEFINITION_RE = re.compile(r"^\\s*(?:async\\s+)?def\\s+_exit\\s*\\(")\n', '')
s = s.replace('        if kind == "forced_exit" and _EXIT_DEFINITION_RE.search(content):\n            continue\n', '')
s = s.replace('        "file_level": False,\n', '')
s = s.replace('    if path is not None and state["file_level"] and not state["path_reported"]:\n',
              '    if path is not None and not state["path_reported"]:\n')
s = s.replace('    state["file_level"] = False\n', '')
s = s.replace('''        if not state["in_hunk"]:
            if diff_line.startswith("new file mode "):
                state["file_level"] = True
            elif diff_line.startswith("deleted file mode "):
                state["path"] = None
            elif diff_line.startswith("--- ") and _path_value(diff_line[4:]) is None:
                state["file_level"] = True
            elif diff_line.startswith("rename to "):
                state["file_level"] = True
''', '')
base = types.ModuleType("base_scan"); exec(compile(s, "base_scan.py", "exec"), base.__dict__)

repo = pathlib.Path(tempfile.mkdtemp(prefix="bb-binarytext2-", dir="/tmp/opencode"))
def git(*a, **kw):
    return subprocess.run(["git", *a], cwd=repo, text=True, capture_output=True, **kw)
git("init", "-q", "."); git("config", "user.email", "a@b"); git("config", "user.name", "a")
(repo/"tests").mkdir(); (repo/"tests/conftest.py").write_text("import os\n")
git("add", "-A"); git("commit", "-qm", "base")
(repo/".gitattributes").write_text("tests/conftest.py binary\n")
(repo/"tests/conftest.py").write_text("os._exit(0)\n")
git("add", "tests/conftest.py")
d = git("diff", "--cached", "--binary").stdout
(repo/"p.patch").write_text(d)
git("reset", "-q", "--hard", "HEAD")
print("patch has GIT binary patch:", "GIT binary patch" in d, "| has +os._exit:", "+os._exit" in d)
print("head:", [(f["kind"], f["path"], f["line"]) for f in head_scan(d)])
print("base:", [(f["kind"], f["path"], f["line"]) for f in base.scan_patch(d)])
r = git("apply", "--check", "--whitespace=nowarn", "p.patch")
print("verify-style git apply --check (no --binary):", "ACCEPT" if r.returncode == 0 else "REJECT")
r = git("apply", "--whitespace=nowarn", "p.patch")
print("applied rc:", r.returncode, "| tests/conftest.py:", (repo/"tests/conftest.py").read_text().strip() if r.returncode == 0 else "")
shutil.rmtree(repo)
PY
```

Output (exact):

```text
patch has GIT binary patch: True | has +os._exit: False
head: []
base: [('conftest', 'tests/conftest.py', 1)]
verify-style git apply --check (no --binary): ACCEPT
applied rc: 0 | tests/conftest.py: os._exit(0)
```

I separately ran the exact stdin form
`subprocess.run(["git", "apply", "--check", "--whitespace=nowarn", "-"], input=d)`
and the same apply without `--check`: both returned 0, and
`tests/conftest.py` then contained `os._exit(0)`.

### Probe 7 - proposed `GIT binary patch` fix, all ACs replayed

Source:

```python
PYTHONPATH=. python3 - <<'PY'
import pathlib, shutil, subprocess, tempfile, types
src = open("bundlebees_plugin/subversion_scan.py").read()
fixed = src.replace('''            elif diff_line.startswith("rename to "):
                state["file_level"] = True
''', '''            elif diff_line.startswith("rename to "):
                state["file_level"] = True
            elif diff_line.startswith("GIT binary patch"):
                state["file_level"] = True
''')
assert fixed != src
mod = types.ModuleType("fixed_scan"); exec(compile(fixed, "fixed_scan.py", "exec"), mod.__dict__)
def ways(p): return [(f["kind"], f["path"], f["line"]) for f in mod.scan_patch(p)]
def diff(path, extra): return f"diff --git a/{path} b/{path}\n" + extra
def empty_delete(path): return diff(path, "deleted file mode 100644\nindex e69de29..0000000\n")
def delete(path, *lines):
    body = "".join(f"-{l}\n" for l in lines)
    return diff(path, f"deleted file mode 100644\n--- a/{path}\n+++ /dev/null\n@@ -1,{len(lines)} +0,0 @@\n{body}")
def deletion_only_edit(path): return diff(path, f"index c01d8a7..21b405d 100644\n--- a/{path}\n+++ b/{path}\n@@ -1,2 +1 @@\n import os\n-os._exit(0)\n")
def mode_change(path): return diff(path, "old mode 100644\nnew mode 100755\n")
def new_empty(path): return diff(path, "new file mode 100644\nindex 0000000..e69de29\n")
def rename(o,n): return f"diff --git a/{o} b/{n}\nsimilarity index 100%\nrename from {o}\nrename to {n}\n"
def edit(path, added, removed=()):
    body = "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return diff(path, f"--- a/{path}\n+++ b/{path}\n@@ -1,{len(removed)} +1,{len(added)} @@\n{body}")
checks = {
 "AC-001": empty_delete("tests/conftest.py"),
 "AC-002a": empty_delete("sitecustomize.py"),
 "AC-002b": empty_delete("src/evil.pth"),
 "AC-003": deletion_only_edit("tests/conftest.py"),
 "AC-004": mode_change("tests/conftest.py"),
 "AC-006": delete("tests/conftest.py", "import os", "os._exit(0)"),
 "AC-007": edit("tests/conftest.py", ["import pytest"], ["import os"]),
 "AC-008a": new_empty("tests/conftest.py"),
 "AC-008b": new_empty("sitecustomize.py"),
 "AC-008c": new_empty("src/evil.pth"),
 "AC-009": rename("tests/old_helpers.py", "tests/conftest.py"),
 "AC-010": rename("tests/conftest.py", "tests/helpers.py"),
 "AC-011": edit("src/pkg/core.py", ["def _exit(code):"]),
 "AC-012": edit("src/pkg/core.py", ["from os import _exit; _exit(0)"]),
}
for name, p in checks.items(): print(f"{name}: {ways(p)}")

repo = pathlib.Path(tempfile.mkdtemp(prefix="bb-fixedbin-", dir="/tmp/opencode"))
def git(*a, **kw): return subprocess.run(["git", *a], cwd=repo, text=True, capture_output=True, **kw)
git("init", "-q", "."); git("config", "user.email", "a@b"); git("config", "user.name", "a")
(repo/"tests").mkdir(); (repo/"src").mkdir()
(repo/"tests/conftest.py").write_text("import os\n"); (repo/"src/x.pth").write_text("import os\n")
(repo/".gitattributes").write_text("tests/conftest.py binary\nsrc/*.pth binary\n")
git("add", "-A"); git("commit", "-qm", "base")
def take(label, mutate):
    mutate(); git("add", "-A")
    d = git("diff", "--cached", "--binary").stdout
    git("reset", "-q", "--hard", "HEAD"); git("clean", "-qfd")
    print(label, "fixed:", ways(d), "| GIT binary patch:", "GIT binary patch" in d,
          "| new file mode:", "new file mode" in d, "| deleted file mode:", "deleted file mode" in d)
take("binary MODIFICATION (text payload)", lambda: ((repo/"tests/conftest.py").write_text("os._exit(0)\n"), (repo/"src/x.pth").write_text("evil\n")))
take("binary DELETION", lambda: (repo/"tests/conftest.py").unlink())
take("binary ADDITION", lambda: (repo/"src/new.pth").write_bytes(b"\x00evil\n"))
shutil.rmtree(repo)
PY
```

Output (exact):

```text
AC-001: []
AC-002a: []
AC-002b: []
AC-003: []
AC-004: []
AC-006: []
AC-007: [('conftest', 'tests/conftest.py', 1)]
AC-008a: [('conftest', 'tests/conftest.py', 1)]
AC-008b: [('startup_hook', 'sitecustomize.py', 1)]
AC-008c: [('pth_file', 'src/evil.pth', 1)]
AC-009: [('conftest', 'tests/conftest.py', 1)]
AC-010: []
AC-011: []
AC-012: [('forced_exit', 'src/pkg/core.py', 1)]
binary MODIFICATION (text payload) fixed: [('pth_file', 'src/x.pth', 1), ('conftest', 'tests/conftest.py', 1)] | GIT binary patch: True | new file mode: False | deleted file mode: False
binary DELETION fixed: [] | GIT binary patch: True | new file mode: False | deleted file mode: True
binary ADDITION fixed: [('pth_file', 'src/new.pth', 1)] | GIT binary patch: True | new file mode: True | deleted file mode: False
```

## Final tree state

Only this file was created. Product code, tests, `CHANGELOG.md` and all other
review files were not modified; nothing was committed, pushed or merged.
