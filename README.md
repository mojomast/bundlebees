# bundlebees

A [Hermes](https://github.com/NousResearch/hermes-agent) plugin that helps an
agent set up and orchestrate **CUA bees**: cheap coding workers that run in
disposable sandboxes on a worker machine you control.

Status: **v0.1.0** on `feat/v0.1.0`. This is not a catalog listing yet.
Requires Hermes **0.23.0** or later.

## What it provides

- Four tools: `bundlebees_status`, `bundlebees_dispatch`,
  `bundlebees_attempt`, `bundlebees_host_prompt`
- User-run CLI: `hermes bundlebees verify` and `hermes bundlebees deploy`
  (not model tools). `deploy` prints steps unless you pass `--execute`
  with a stored host alias.
- A dark-mode Desktop pane (health, containers, models, concurrency,
  authorized projects, live attempts, verify results, remote-host prompt)
- A bundled skill, `bundlebees:bundlebees-operator`

Work is tracked on **Hermes' own kanban**, not an external board. If the
tools are not visible, call `tool_search(["bundlebees"])` or
`skill_view("bundlebees:bundlebees-operator")`.

## Disclosure

The plugin talks to a local or user-configured worker launcher (loopback
by default). Dispatching a bee can spend model budget on that worker
machine. The user-run CLI can push a feature branch, and `deploy` can
reach a remote host when you invoke it. Secrets are read only through
Hermes (`get_secret`). The pane never shows tokens or host addresses. A
Cua sandbox is not a sandbox for the Hermes process itself.

## Tests

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python scripts/measure_schemas.py
```

Dev extras include pytest, a strict YAML loader, tiktoken, and FastAPI
(for the Desktop pane tests). Combined tool schemas must stay at or under
**900** o200k tokens.

## License

MIT. See [LICENSE](LICENSE).

## Design notes

- [docs/RESEARCH.md](docs/RESEARCH.md)
- [docs/tasks/BB00_CONTRACTS.md](docs/tasks/BB00_CONTRACTS.md)
