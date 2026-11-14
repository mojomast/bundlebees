# Stack manifest

The coordinator driver is tested against this pinned stack. The commits name
the exact source trees used to validate the queue, landing, event and scheduler
paths; a branch tip is not a substitute.

| Component | Pin | Role |
|---|---|---|
| workflow | `9c69d3a` | canonical packets, landing events, recovery and RETURN validators |
| bundlebees | `389dca2` | queue, landing bridge, event inbox, work scheduler and this driver |
| hyvv | `e991c75` | governed provisioning contract and launcher outbox |
| Hermes | `0.23.0` | plugin host API floor (`requires_hermes`) |
| launcher | `0.1.16` | local worker launcher result envelope |
| python | `3.12.3` | runtime and test interpreter |

## Verify heads

Point the variables at the checkouts, then compare each head and the runtime
version with the pins above.

```bash
git -C "$WORKFLOW_SRC" rev-parse HEAD
git -C "$BUNDLEBEES_SRC" rev-parse HEAD
git -C "$HYVV_SRC" rev-parse HEAD
python3 --version
```

Hermes and the launcher are versioned releases rather than Git checkouts used
by the driver; verify them with their own version commands:

```bash
hermes --version
```

The launcher identity `0.1.16` is also asserted by the worker-result shape test
(`tests/test_verify_result_shape.py`). The Python pin is asserted by
`tests/test_factory_driver.py` together with every commit string above.
