# slop-lint

A small, standard-library-only code-quality gate for Python written by agents. It finds three kinds of slop and compares the result against a checked-in baseline, so existing code passes and only new findings fail. It needs Python 3.12+ and can be copied into any repository.

```sh
cd tools/slop-lint
python3 -m slop_lint check . --baseline slop-baseline.json
python3 -m slop_lint check . --baseline slop-baseline.json --report-only
python3 -m slop_lint baseline . --write slop-baseline.json
python3 -m slop_lint prune . --baseline slop-baseline.json
python3 -m unittest discover -s tests -t . -v
```

Directories are walked for `*.py`. Any directory whose name starts with `.` is skipped, as are `node_modules`, `__pycache__`, `venv` and `.venv`. Paths stored in a baseline are POSIX and relative to the current directory.

## Rules

| Rule | Meaning |
|---|---|
| `complexity` | A function or method whose McCabe-style complexity is over 10, reported at its `def` line. The count is `1` plus `if`/`elif`, `for`, `while`, `except`, `with`, `assert`, boolean-operator operands, comprehension `if`s and `match` cases. Nested functions and classes are counted separately. |
| `swallowed-exception` | A bare `except:` or an `except Exception` / `except BaseException` handler whose body is only `pass`, `...` or `continue`. |
| `mutable-default-arg` | A default argument that is a list, dict or set literal, or a `list()`, `dict()` or `set()` call. |

A file that cannot be parsed produces exactly one `syntax-error` finding instead of crashing the scan.

## The ratchet

The baseline records a count per file and rule, in the style of ESLint bulk suppressions. `check` compares current counts with those allowances:

- A file or rule whose count is higher than the baseline, or is missing from it, is a **new finding** and fails.
- A file or rule whose count is lower than the baseline is a **stale entry**. It also fails, so the baseline cannot silently keep old room; `--pass-on-unpruned` tolerates it, and `prune` removes it.
- A count that is equal to the baseline passes.

`check` prints one line per finding above the allowance:

```
path:line: rule: message
```

`prune` rewrites the baseline with only the counts that still occur and never raises a count. New findings are never added by `prune`; they must be reviewed first.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean, or findings only while `--report-only` is set. |
| `1` | New findings, or stale baseline entries without `--pass-on-unpruned`. |
| `2` | Usage error, or a missing, unreadable, malformed or invalid baseline. Always `2`, even with `--report-only`. |

`--report-only` still prints findings and still exits `1` when the baseline is stale (unless `--pass-on-unpruned` is also given). A bad baseline is always `2`.

## Regenerating the baseline

Only regenerate the baseline as part of a reviewed change. Generate it, review every count that moved, and include the baseline update in the same change:

```sh
python3 -m slop_lint baseline . --write slop-baseline.json
python3 -m slop_lint check . --baseline slop-baseline.json
```

Use `prune` when findings were removed; it can only lower counts and never hides a new one.
