# Tessera v1 — Framework Repository

This repository holds the **Tessera v1 framework**: a file-system-native ticket
runtime (watcher → event bus → scheduler → executor) plus its SDK and CLI.

This is Phase A scaffolding: directory layout, packaging and module stubs only.
No runtime behavior is implemented yet.

## Layout

```
pyproject.toml            uv-managed project, requires-python >=3.12
lib/ticket-management/    runtime home
  ticket_management/      importable package (dir name must be a valid
                          identifier, hence the nested package)
    runtime/              watcher, dispatcher, manifest, executor, state, env
    plugins/              python / bash / node runners
    cli.py                typer CLI entry point
tessera/                  SDK package (`Runtime` facade)
tests/                    pytest suite
```

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
