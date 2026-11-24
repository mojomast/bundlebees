# Stack manifest

The coordinator driver is tested against this pinned stack. External
components are recorded as full revisions. The bundlebees row is the current
checkout, not a self-referential commit pin; its package/build identity is
derived from `pyproject.toml`.

| Component | Pin | Role |
|---|---|---|
| workflow | `ae9c080262ab593e7643e2fba79cd51a018f25df` | canonical packets, landing events, recovery and RETURN validators |
| bundlebees | `HEAD` | current checkout; build identity is generated below, never self-pinned |
| hyvv | `fdd235446e8fb28f8074fa06ea5c6ee1e466e129` | governed provisioning contract and launcher outbox |
| Hermes | `>=0.23.0` | plugin host API floor (`requires_hermes`) |
| launcher | `0.1.17` | local worker launcher result envelope |
| python | `3.12` | tested interpreter (project floor: `>=3.11`) |
| bundlebees package | `0.2.0` | `pyproject.toml` build identity |

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

The launcher identity is checked against the real worker-result fixture in
`tests/test_verify_result_shape.py`. The Hermes floor comes from `plugin.yaml`,
the package version from `pyproject.toml`, and the Python pin from the running
interpreter. `tests/test_factory_driver.py` checks these relationships rather
than freezing a release string.
