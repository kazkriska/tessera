# CLI Reference

The CLI wraps the Python SDK (`tessera.Runtime`) — there is no duplicated
logic. Two console scripts are installed, both identical: `tessera` and
`ticket`.

Run `tessera …` directly once installed (it is on your `PATH`). Inside a source
checkout, prefix with `uv run` (`uv run tessera …`).

## Command groups

```
tessera                      show help
tessera runtime start        boot the runtime daemon
tessera runtime stop         graceful shutdown
tessera runtime status       daemon + ticket count
tessera repo init [PATH]     scaffold TicketsRepository/ + .ticket-runtime/
tessera repo scan [PATH]     force rediscovery + registry rebuild
tessera create <id>          scaffold a new ticket (--type)
tessera inspect <id>         metadata + state summary
tessera transition <id> <s>  lifecycle transition (requires daemon)
tessera action <id> <name>   invoke a manifest action (requires daemon)
tessera validate <id>        validate MANIFEST.yaml
tessera log <id> [--tail N]  tail activity.jsonl
```

## Details

### `runtime start [--repo PATH]`
Boots the watcher + bus + scheduler + socket RPC server as a detached daemon.
Creates the repository if needed (idempotent). Singleton: fails with "runtime
already running" if the socket is live; reaps stale sockets.

### `runtime stop [--repo PATH]`
Sends the shutdown RPC to the daemon. Errors with "Runtime is not running"
if no live socket.

### `runtime status [--repo PATH]`
Prints `root`, `socket`, `running: yes/no`, and (when running) the number of
tickets the daemon sees.

### `repo init [PATH]`
Scaffolds `TicketsRepository/` and `.ticket-runtime/` at PATH (default: cwd).

### `repo scan [PATH]`
Forces filesystem rediscovery and registry rebuild. Prints the ticket count.

### `create <id> [--type TYPE] [--repo PATH]`
Scaffolds `<id>.ticket/` with a complete `metadata.json` (all six required
fields), a `created` `state.json`, and a minimal `MANIFEST.yaml` with empty
hooks/actions. Errors if the ticket already exists.

### `inspect <id> [--repo PATH]`
Prints id, title, type, state, updated_at.

### `transition <id> <status> [--repo PATH]`
Requests a validated lifecycle transition (e.g. `running`). **Requires a
running daemon.** Rejects illegal transitions with the validation error.

### `action <id> <action> [--repo PATH]`
Invokes a named manifest action. **Requires a running daemon.** Prints
`exit=`, stdout, stderr. The action is denied if permissions don't grant the
needed capability.

### `validate <id> [--repo PATH]`
Validates `MANIFEST.yaml` against the schema. Prints `valid` or
`valid with warnings:` / `invalid: <error>`.

### `log <id> [--tail N] [--repo PATH]`
Tails the ticket's `activity.jsonl` (default last 20 lines).

## Path resolution

All commands accept `--repo`; without it, Tessera walks upward from the cwd
to find the directory containing `TicketsRepository/`. Commands that only read
the filesystem (inspect, validate, log, create, repo scan) work without a
daemon; mutating commands (transition, action) require one.
