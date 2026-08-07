# Tessera v1 — Framework Repository

This repository holds the **Tessera v1 framework**: a file-system-native ticket
runtime (watcher → event bus → scheduler → executor) plus its SDK and CLI.

This is Phase A scaffolding: directory layout, packaging and module stubs only.
No runtime behavior is implemented yet.

## Layout

```
pyproject.toml              uv-managed project, requires-python >=3.12
lib/ticket_management/      the runtime (importable package; underscore name)
  runtime/                  watcher, dispatcher, manifest, executor, state, env
  plugins/                  python / bash / node runners
  cli.py                    typer CLI entry point (console script: `tessera`)
tessera/                    SDK client package (`Runtime` facade) — see note below
TicketsRepository/          = TicketRepository (canonical, git-ignored at runtime)
  .ticket-runtime/          disposable runtime state (registry.db, locks, sock…)
tests/                      pytest suite
```

> **Why two Python packages (`lib/ticket_management` and `tessera`)?**
> The design spec (Master Part XI / RFC-0010) mandates a separate SDK client
> package `tessera/` published *alongside* the runtime `lib/ticket-management/`.
> The runtime is the engine; `tessera/` is the programmatic client (socket +
> direct modes) that the CLI and external tools/agents import. They are not
> redundant — `tessera/` depends on the runtime, not the other way around.

> **`TicketsRepository/`** is our canonical spelling of the docs'
> `TicketRepository`/`Tickets/` (the user renamed to avoid overloading
> "workspace"). It holds Ticket directories and contains `.ticket-runtime/`.
> It is git-ignored (Invariant I-2): the runtime creates it at boot.

> **`Skills/`** from the docs is a placeholder for a future resource type and
> is intentionally NOT created in v1.

## Contracts

- [CONTRACTS.md](CONTRACTS.md) — build/spec contract; resolves ambiguities in the
  vendored `tessera-docs/` design spec. It wins over the source docs.
- [GIT-CONTRACT.md](GIT-CONTRACT.md) — branch / worktree / commit standard.

`tessera-docs/` is a vendored upstream clone (read-only reference, git-ignored).

## Development

```bash
uv venv
uv pip install -e ".[dev]"
uv run pytest
```

## Note on runtime directories

`Tickets/`, `Skills/` and `.ticket-runtime/` are runtime data and are
**git-ignored** by the GIT-CONTRACT (Invariants I-2 / I-5). They are created at
boot and intentionally not tracked here.
