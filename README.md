# Tessera v1

**File-system-native ticket runtime framework** — Python ≥ 3.12, `uv`-managed.

Tessera treats your file system as the database: tickets are directories under
`TicketsRepository/`, every change to a ticket is a filesystem event, and the
runtime turns those events into a stream of domain events that run hooks and
actions in isolated subprocesses.

```
TicketsRepository/
  HQ_BR-010.ticket/
    metadata.json     # identity, title, owner, relationships (CONTRACTS §2)
    state.json        # lifecycle state: created -> initialized -> ready -> ...
    activity.jsonl    # append-only audit trail (lifecycle + action records)
    MANIFEST.yaml     # hooks, actions, permissions, env, watch rules
    .env              # secret environment (optional, git-ignored)
```

The runtime is a pipeline: **watcher → event bus → scheduler → executor**.

| Stage | Module | Job |
|-------|--------|-----|
| Watcher | `tessera_runtime/runtime/watcher.py` | inotify observer; debounces raw file events into domain triggers (`metadata.updated`, `fs.changed`, or triggers declared in `MANIFEST.yaml` `watch:` rules) |
| Event bus | `tessera_runtime/runtime/bus.py` | pub/sub event dispatch with recursion guard |
| Scheduler | `tessera_runtime/runtime/scheduler.py` | per-workspace queues, priority bands, action-level locks, retry with exponential backoff |
| Executor | `tessera_runtime/runtime/executor.py` | resolves permissions/env, runs hooks in isolated subprocesses with a path jail |

Everything is configured from one optional file: `.ticket-runtime/config.yaml`
(all keys optional — delete the file to revert to defaults).

## Quick start

**End users:** install with the one-line bootstrap — see [INSTALL.md](INSTALL.md).
It installs to the prefix `tessera/` under your home (`~/.local/share/tessera`)
and puts `tessera` and `ticket` on your `PATH`. The steps below are the
from-source (contributor) path.

```bash
# 1. Install (Python >= 3.12 required)
uv venv
uv pip install -e ".[dev]"

# 2. Create a repository (scaffolds TicketsRepository/ + .ticket-runtime/)
uv run tessera repo init .

# 3. Create a ticket
uv run tessera create HQ_BR-010 --type task

# 4. Start the runtime daemon (watcher + scheduler + socket RPC)
uv run tessera runtime start

# 5. Drive it
uv run tessera inspect HQ_BR-010
uv run tessera transition HQ_BR-010 running
uv run tessera action HQ_BR-010 my-action
uv run tessera log HQ_BR-010
```

> `ticket` and `tessera` are both installed as console scripts (same CLI).

## Two ways to drive the system

1. **CLI** (`tessera …`) — wraps the SDK; see [docs/CLI.md](docs/CLI.md).
2. **Python SDK** (`import tessera_sdk`) — `Runtime.direct()` (filesystem, no daemon)
   or `Runtime.connect()` (attach to a running daemon over `runtime.sock`);
   see [docs/SDK.md](docs/SDK.md).

## Documentation (end-user)

- [Install](INSTALL.md) — one-line `curl … | bash` install (Linux, user-scope)
- [Getting Started](docs/GETTING_STARTED.md) — first repo, first ticket, first action
- [Ticket Authoring Guide](docs/TICKETS.md) — metadata.json, MANIFEST.yaml, hooks, actions, permissions, watch rules
- [Lifecycle & State Machine](docs/LIFECYCLE.md) — the 9-state model and legal transitions
- [Configuration](docs/CONFIGURATION.md) — every `config.yaml` key
- [CLI Reference](docs/CLI.md)
- [Python SDK](docs/SDK.md)
- [Architecture](docs/ARCHITECTURE.md) — watcher → bus → scheduler → executor

## Contracts

- [CONTRACTS.md](CONTRACTS.md) — build/spec contract; resolves ambiguities in the
  vendored `formal-specifications/` design spec. **It wins over the source docs.**
- [GIT-CONTRACT.md](GIT-CONTRACT.md) — branch / worktree / commit standard.
- `formal-specifications/` is a vendored upstream clone (read-only reference, git-ignored).

## Development

```bash
uv venv
uv pip install -e ".[dev]"
uv run pytest          # full suite (currently 160 tests)
```

## Repository layout

```
src/
  tessera_runtime/       the runtime engine (importable as tessera_runtime)
    runtime/             watcher, bus, scheduler, executor, server, registry, ...
    cli.py               Typer CLI (console scripts: tessera, ticket)
  tessera_sdk/           SDK client package (Runtime facade; direct + socket modes)
TicketsRepository/       canonical ticket store (git-ignored; created at boot)
  .ticket-runtime/       disposable runtime state (config.yaml, registry.db,
                         locks/, logs/, runtime.sock, cache/)
tests/                   pytest suite
docs/                    end-user documentation
```

When installed (rather than run from a checkout), the same two packages live
under the install prefix `tessera/` in the user's home —
`~/.local/share/tessera` — per [RFC-0013](INSTALL.md).

## Note on runtime directories

`TicketsRepository/`, `.ticket-runtime/` are runtime data and are **git-ignored**
by the GIT-CONTRACT (Invariants I-2 / I-5). They are created at boot and
intentionally not tracked here. `Skills/` from the design docs is a placeholder
for a future resource type and is intentionally NOT created in v1.
