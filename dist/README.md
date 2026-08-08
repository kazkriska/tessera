# Tessera v1

**File-system-native ticket runtime framework** — Python ≥ 3.12, installed locally via `uv`.

Tessera treats your file system as the database: tickets are directories under
`TicketsRepository/`, every change to a ticket is a filesystem event, and the
runtime turns those events into a stream of domain events that run hooks and
actions in isolated subprocesses.

```text
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
| Watcher | `src/tessera_runtime/runtime/watcher.py` | inotify observer; debounces raw file events into domain triggers (`metadata.updated`, `fs.changed`, or triggers declared in `MANIFEST.yaml` `watch:` rules) |
| Event bus | `src/tessera_runtime/runtime/bus.py` | pub/sub event dispatch with recursion guard |
| Scheduler | `src/tessera_runtime/runtime/scheduler.py` | per-workspace queues, priority bands, action-level locks, retry with exponential backoff |
| Executor | `src/tessera_runtime/runtime/executor.py` | resolves permissions/env, runs hooks in isolated subprocesses with a path jail |

Everything is configured from one optional file: `.ticket-runtime/config.yaml`
(all keys optional — delete the file to revert to defaults).

## Installing Tessera

Tessera is distributed as a self-contained source tarball. The fastest path:

```bash
curl -fsSL https://github.com/kazkriska/tessera-v1/releases/download/v1.0.0/install.sh | bash
```

This downloads the release tarball, verifies its SHA256, extracts it, and runs
the installer. It installs to the user-scoped prefix `~/.local/share/tessera/`,
symlinks `tessera` and `ticket` into `~/.local/bin/`, and registers a systemd
**user** unit template (`tessera-runtime@.service`). No `sudo` is required.
Runs on Linux only. See `INSTALL.md` (in the repo) and RFC-0013 for the full
specification.

## Quick start (after install)

`tessera` and `ticket` are now on your `PATH`.

```bash
# 1. Create a repository (scaffolds TicketsRepository/ + .ticket-runtime/)
tessera repo init .

# 2. Create a ticket
tessera create HQ_BR-010 --type task

# 3. Start the runtime daemon (watcher + scheduler + socket RPC)
tessera runtime start

# 4. Drive it
tessera inspect HQ_BR-010
tessera transition HQ_BR-010 running
tessera action HQ_BR-010 my-action
tessera log HQ_BR-010
```

> `ticket` and `tessera` are both installed as console scripts (same CLI).

## Two ways to drive the system

1. **CLI** (`tessera …`) — wraps the SDK; see `docs/CLI.md`.
2. **Python SDK** (`import tessera_sdk`) — `Runtime.direct()` (filesystem, no daemon)
   or `Runtime.connect()` (attach to a running daemon over `runtime.sock`);
   see `docs/SDK.md`.

## Documentation (end-user)

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

## Repository layout (source tree)

```text
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

## Note on runtime directories

`TicketsRepository/`, `.ticket-runtime/` are runtime data and are **git-ignored**
by the GIT-CONTRACT (Invariants I-2 / I-5). They are created at boot and
intentionally not tracked here. `Skills/` from the design docs is a placeholder
for a future resource type and is intentionally NOT created in v1.
