# Ready-column stocking flag

The factory never invents or self-approves work. This check exists so an empty
ready column is *visible* instead of silent: when approved work drops below the
bee slots the factory could still use, the beat and the driver raise a signal.

Everything is behind one flag:

```text
BUNDLEBEES_BOARD_STOCK=1          # the whole check; default off
BUNDLEBEES_BOARD_STOCK_CARD       # optional coordinator card for the comment
BUNDLEBEES_BOARD_STOCK_BOARD      # optional board slug for `hermes kanban --board`
```

Off (unset, empty or any value other than `1`) means no board read, no
subprocess and no file: the beat report and the service pass are exactly what
they were before this check existed. `tests/test_board_stock.py` proves both
directions.

`BUNDLEBEES_BOARD_STOCK` is resolved by `bundlebees_plugin/flags.py`, the same
loader `MEMORY_ENABLED` uses: process environment first, then the shared file
`~/.config/bundlebees/flags.env` (override `BUNDLEBEES_FLAGS_FILE`). The factory
driver and the Hermes gateway read that one file, so the flag means the same
thing in both processes, and each startup logs one
`bundlebees flags: BUNDLEBEES_BOARD_STOCK=0|1 ...` line.

## What is observed

Two numbers, and nothing else:

- **ready** — the number of cards the board reports in the `ready` status. The
  ready column *is* the board's approved-and-dispatchable queue; the check
  trusts whoever moved the card there and never re-classifies it.
- **free** — the shared bee cap (`grants.GLOBAL_MAX_BEES`) minus the work
  scheduler items currently `claimed`, floored at zero. This is the same cap
  and the same claimed items the dispatch gate uses.

The verdict:

| Condition | State |
| --- | --- |
| `free == 0` | `saturated` — nothing to stock, no flag |
| `ready < free` | `low` — short by `free - ready` |
| `ready >= free` | `stocked` |

The board is read through the public CLI (`hermes kanban list --status ready
--json`), never by opening the board's SQLite file. A payload that is not a
task list is a refusal, not a zero.

## The signal

One durable record per observation:

```text
<state>/factory/board-stock/signal.json      # the Hive signal
<state>/factory/board-stock/history.jsonl    # one line per change only
```

```json
{
  "schema": "rizzstack.board-stock/1",
  "at": "2026-11-18T19:00:00Z",
  "state": "low",
  "ready_approved": 2,
  "free_slots": 5,
  "shortfall": 3,
  "board": "default",
  "revision": 3,
  "changed": true,
  "notified": true
}
```

`revision` increments only when `state`, `ready_approved`, `free_slots`,
`shortfall` or `board` changes, so a consumer can tell a fresh observation from
the previous one. An *episode* is not every change: it starts at the first low
observation, or when a low observation follows a recovery, and ends at the next
recovery. The file is written atomically (`0600` in a `0700` directory);
it carries no paths, hosts, URLs or tokens — the board label is accepted only
if it matches `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.

## The comment

The check posts one comment when a low observation changes -- `ready_approved`,
`free_slots`, `shortfall` or `board` -- through `hermes kanban comment` on the
card named by `BUNDLEBEES_BOARD_STOCK_CARD`:

```text
factory stock: 2 ready card(s), 5 free bee slot(s); short by 3.
Observed only: the factory never creates, moves, approves or dispatches work.
```

Identical repeats do not comment again; a recovery followed by a new shortfall
starts a new episode and comments again. Without a configured card the signal is
still written and `notified` is `false`. A failed comment does not lose the
signal.

## Fail closed

| Code | Meaning | What is NOT done |
| --- | --- | --- |
| `board_unreadable` | the CLI failed, or the payload is not a task list | no signal, no comment |
| `cap_unreadable` | `grants.GLOBAL_MAX_BEES` is missing or not a positive int | no signal, no comment |
| `scheduler_unreadable` | `coordinator/work.json` cannot be read | no signal, no comment |

Both callers treat every code as one failed duty: the beat reports
`stock_ready_column` as `error` and stays `partial`; the service pass records
`stock.status = "error"` and keeps running. A refusal never stops dispatch and
never guesses a number.

## Where it runs

- `beat.beat()` — duty `stock_ready_column`, after the reconciliation sweep and
  the merge stream. `beat.beat()` runs from the factory driver's opt-in
  `--sweep` backstop, not a heartbeat; this duty is an observation, not a
  trigger to dispatch.
- `FactoryService.run_pass()` — after the dispatch gate, when `--service` is
  running. The pass result gains a `stock` key only when the flag is on.
- Manually:

```bash
python -m bundlebees_plugin.board_stock --state-dir "$BUNDLEBEES_STATE" --json
```

The manual form exits 0 when the flag is off and prints why; it never writes in
that case.

## Tests

`tests/test_board_stock.py` covers: the exact-`1` flag, both off paths writing
nothing, the low/stocked/saturated verdicts, the CLI read shape, a comment when
a low observation changes (with identical repeats staying silent and a drifting
shortfall commenting again), recovery then a new episode, a missing card, an
unreadable board or cap writing nothing, the signal containing no
paths/hosts/tokens, and the beat and service wiring failing soft.
