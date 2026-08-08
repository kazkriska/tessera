# Getting Started

This guide takes you from an empty machine to a running Tessera runtime with
your first ticket and your first automated action. Everything here was verified
against the current `main` (v0.1.0).

## 1. Prerequisites

- **Python ≥ 3.12** (Tessera is `uv`-managed; PEP 668 systems work fine with `uv`)
- **uv** — https://docs.astral.sh/uv/
- **Linux** — the watcher uses inotify, so Linux is required for the live
  runtime daemon. The SDK's `Runtime.direct()` mode is pure filesystem and
  works anywhere Python runs.

## 2. Install

```bash
cd tessera-v1
uv venv
uv pip install -e ".[dev]"
```

Verify the CLI is on your path inside the venv:

```bash
uv run tessera --help
```

## 3. Create a repository

```bash
uv run tessera repo init .
```

This scaffolds:

```
TicketsRepository/
  .ticket-runtime/     # disposable runtime state (config, locks, logs, socket)
```

Tessera searches upward for the directory containing `TicketsRepository/`, so
you can run commands from any subdirectory of your project.

## 4. Create a ticket

```bash
uv run tessera create HQ_BR-010 --type task
```

This writes a complete ticket directory:

```
TicketsRepository/HQ_BR-010.ticket/
  metadata.json     # id, title, type, owner — all six required fields
  state.json        # {"status": "created", ...}
  MANIFEST.yaml     # envelope + empty hooks/actions
```

Inspect it:

```bash
uv run tessera inspect HQ_BR-010
# id:    HQ_BR-010
# title: HQ_BR-010
# type:  task
# state: created
```

## 5. Add an action

A manifest action is a named command the runtime can run in an isolated
subprocess. Edit `TicketsRepository/HQ_BR-010.ticket/MANIFEST.yaml`:

```yaml
apiVersion: ticket/v1
kind: Ticket
metadata:
  id: HQ_BR-010
  title: HQ_BR-010
  type: task
actions:
  greet:
    run: echo "hello from $TESSERA_TICKET_ID"
    shell: bash
```

Validate it:

```bash
uv run tessera validate HQ_BR-010     # -> valid
```

## 6. Start the runtime

```bash
uv run tessera runtime start
uv run tessera runtime status
```

The daemon boots the watcher (inotify), the event bus, the scheduler, and a
socket RPC server (`TicketsRepository/.ticket-runtime/runtime.sock`). It is a
singleton — starting twice fails with "runtime already running".

## 7. Run your action

Mutating commands (`transition`, `action`) require the running daemon:

```bash
uv run tessera action HQ_BR-010 greet
# action greet on HQ_BR-010: exit=0
# hello from HQ_BR-010
```

## 8. Walk the lifecycle

```bash
uv run tessera transition HQ_BR-010 initialized
uv run tessera transition HQ_BR-010 ready
uv run tessera transition HQ_BR-010 running
uv run tessera transition HQ_BR-010 completed
```

Each transition is validated against the state machine (see
[Lifecycle & State Machine](LIFECYCLE.md)) and appended to
`activity.jsonl`:

```bash
uv run tessera log HQ_BR-010
```

## 9. Stop the runtime

```bash
uv run tessera runtime stop
```

## Next steps

- Learn the ticket format in depth: [Ticket Authoring Guide](TICKETS.md)
- Configure the runtime: [Configuration](CONFIGURATION.md)
- Drive it from Python: [Python SDK](SDK.md)
- Understand the pipeline: [Architecture](ARCHITECTURE.md)
