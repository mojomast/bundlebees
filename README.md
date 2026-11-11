# bundlebees

A [Hermes](https://github.com/NousResearch/hermes-agent) plugin that helps an
agent set up and orchestrate **CUA bees**: cheap coding workers that run in
disposable sandboxes on a worker machine you control.

Status: **in development** on `feat/v0.1.0`. This is not a catalog listing
yet. Requires Hermes **0.23.0** or later.

## What it will provide

- Four tools: `bundlebees_status`, `bundlebees_dispatch`,
  `bundlebees_attempt`, `bundlebees_host_prompt`
- User-run CLI: `hermes bundlebees verify` and `hermes bundlebees deploy`
  (not model tools)
- A dark-mode Desktop pane (health, containers, models, concurrency,
  authorized projects, live attempts, verify results, remote-host prompt)
- A bundled skill, `bundlebees:bundlebees-operator`

Work is tracked on **Hermes' own kanban**, not an external board.

## Disclosure

The plugin talks to a local or user-configured worker launcher. Dispatching
a bee can spend model budget on that worker machine. The user-run CLI can
push a feature branch, and `deploy` can reach a remote host when you invoke
it. Secrets are read only through Hermes (`get_secret`). The pane never
shows tokens or host addresses. A Cua sandbox is not a sandbox for the
Hermes process itself.

## License

MIT. See [LICENSE](LICENSE).

## Design notes

- [docs/RESEARCH.md](docs/RESEARCH.md)
- [docs/tasks/BB00_CONTRACTS.md](docs/tasks/BB00_CONTRACTS.md)
