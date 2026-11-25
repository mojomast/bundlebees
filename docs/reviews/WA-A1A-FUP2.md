VERDICT: APPROVE

Independent review of PR #281 at head `31be61a7c1f88e669c6fdd43b01d0e8e98cc5215`.
The two product files match the pasted diff, `tests/test_subversion_scan.py` is
the approved 42-case file (unchanged), and `CHANGELOG.md:12` is the
coordinator-owned bullet; it accurately describes the code. Review findings
2-8 are implemented and covered by the named approved cases. No gate, sandbox,
check or test is weakened, skipped or made optional; no secrets, hosts or
absolute paths were added. The findings below are non-blocking hardening
edges, not regressions in the approved behaviour.

## Findings

1. **minor** - `bundlebees_plugin/subversion_scan.py:205-211` (`_flush_file`,
   fed by `:221-226`): because `_flush_file` emits file-level kinds whenever
   `path` is set and `path_reported` is false, it does not only cover the
   intended empty-new-file and pure-rename shapes. Any diff that names a risky
   path but adds no line now yields a line-1 path finding, including a
   deletion-only edit of an existing `tests/conftest.py` (a `+++ b/...` header
   plus only `-`/context lines) and a mode-only change (`old mode`/`new mode`,
   no hunk). A read-only probe returns `[("conftest", "tests/conftest.py", 1)]`
   for both, where the old scanner returned `[]`. The disclosed limitation only
   covers deletion of an *empty* risky file; this broader case is undisclosed.
   It is fail-closed (nothing is weakened), but it contradicts the approved test
   module's "removing such code is never a finding" for path-level findings and
   can block a patch that only removes suspicious lines from a conftest. Fix:
   record whether the file is being created (`new file mode` or a `--- /dev/null`
   source) or renamed (`rename to`) and flush only for those shapes (or, if the
   conservative behaviour is intended, card it with the empty-deletion
   limitation and add deletion-only/mode-only regression guards).

2. **minor** - `bundlebees_plugin/subversion_scan.py:18` (`_CONTROL_CHARS_RE`),
   applied at `:46`: the class strips C0/C1 controls but not the Unicode line
   and paragraph separators U+2028/U+2029. A read-only probe of the quoted path
   `"b/src/x\u2028spoof.py"` yields `src/x\u2028spoof.py`; some terminals and
   log pipelines render U+2028 as a line break, so the finding-7 intent ("cannot
   inject a newline into `Finding["path"]` or the refusal message") is not fully
   met even though the approved test only asserts `\n`/`\r`. Fix: add
   `\u2028\u2029` to the class (or strip on `str.isprintable()` / the Cc, Cf, Zl
   and Zp Unicode categories) and add a regression case.

3. **minor** - `bundlebees_plugin/subversion_scan.py:22`
   (`_PYTEST11_SECTION_RE`, used at `:99`): valid TOML for the same entry point,
   `[project.entry-points."pytest11"]`, and whitespace around the dotted parts,
   `[project . entry-points . pytest11]`, do not match; the quoted form returns
   `[]` in a read-only probe. Since those tables are equivalent, a `pytest11`
   plugin load can evade the check. (`setup.py`'s
   `entry_points={"pytest11": ...}` is also uncovered, although it is outside
   the config files named in the brief.) Fix: allow optional quotes and
   whitespace around the dots in the section regex, or normalise the header
   before matching, and add a regression case.

4. **minor** - `bundlebees_plugin/subversion_scan.py:21` (`_PLUGIN_OPTION_RE`,
   used at `:103`): `-p(?:\s|=|[A-Za-z_])` requires a character after `-p`, so
   `-p` alone on an addopts continuation line with the plugin name on the next
   line is missed. A probe of a triple-quoted `addopts` containing `-p` then
   `myplugin` returns `[]`, although pytest's shlex parses those as
   `-p myplugin`. Fix: accept end-of-line after `-p` when
   `addopts_continuation` is active (for example `-p(?:\s|=|[A-Za-z_]|$)`), or
   inspect the next non-empty continuation line, and add a regression case.
   Related, disclosed limitation: `scan_patch` resets `addopts` at every hunk
   (`:238-239`), so an opener outside the hunk context makes a long
   triple-quoted `addopts` block undetectable; that is inherent to a patch-only
   scan and is acceptable if documented.

5. **informational** - `bundlebees_plugin/subversion_scan.py:59-69` plus
   `:205-211`: the acknowledged empty-risky-file deletion false positive
   (`deleted file mode`, no `---`/`+++`) remains, as disclosed and carded in the
   brief. A non-empty risky file deletion is clean because `+++ /dev/null`
   clears the path. No change required for this PR; findings 1 and 2 are the
   broader follow-ups worth carding with it.

## Acceptance criteria mapped to the approved tests

| Item | Approved test(s) | Code |
| --- | --- | --- |
| Empty added file / pure rename, `rename from` ignored | `test_empty_added_file_is_a_finding`, `test_pure_rename_to_conftest_is_a_finding`, `test_rename_away_from_conftest_is_not_a_finding` | `_diff_git_new_path` (`subversion_scan.py:59`), `rename to` (`:142`), `_flush_file` (`:205`) |
| Evasions | `test_evasive_runner_subversion_is_a_finding` | content patterns (`:24-30`) |
| `pytest11` and multiline addopts | `test_pytest11_entry_point_is_a_finding`, `test_multiline_addopts_plugin_is_a_finding`, `test_legitimate_addopts_plugin_option_is_still_a_finding` | `_loads_pytest_plugin` (`:89`), `_next_addopts_continuation` (`:108`) |
| Prose/comparison false positives | `test_doc_prose_about_runner_hooks_is_not_a_finding`, `test_assertion_comparison_is_not_a_patched_assertion`, `test_pytest_plugins_in_docs_is_not_a_finding` | `_scans_content` (`:84`), `=(?!=)` (`:28-29`) |
| Locked verify wiring | `test_locked_verify_refuses_subversion_before_runner_or_worktree` | `check_patch_subversion` first in `_verify_and_commit_locked` (`verify_local.py:959`) |
| Path hygiene | `test_quoted_path_control_characters_cannot_spoof_the_refusal_message` | `_path_value` (`:35-49`) |
| Refusal reporting | `test_refusal_message_lists_findings_and_counts_them` | `verify_local.py:106-121` |

## Test commands

| command | exit | result |
| --- | --- | --- |
| `python3 -m py_compile bundlebees_plugin/subversion_scan.py bundlebees_plugin/verify_local.py` | 0 | both changed modules compile |
| `python3 -c "import pytest"` | 1 | `ModuleNotFoundError: No module named 'pytest'`; pytest is absent in this image, so the two approved pytest commands were not run. The supplied facts give the RED/GREEN evidence: 42 collected / 17 RED then 42 passed, and the full suite added 0 new ids. |
| `timeout 900 python3 -m pytest -q -p no:cacheprovider tests/test_subversion_scan.py` | not run | pytest absent (environmental, per the supplied note) |
| `timeout 2400 python3 -m pytest -q -p no:cacheprovider tests` | not run | pytest absent (environmental, per the supplied note) |
| read-only `python3` import probe of `subversion_scan.scan_patch` | 0 | confirmed the residual edge cases in findings 1-4; no files were written |
| `git status --short` | 0 | only this review file is added; the working tree is otherwise clean |
