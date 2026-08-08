# Python SDK

`import tessera_sdk` — the programmatic client for the Tessera runtime
(Part XI / RFC-0010). One facade, two modes:

- **Direct** (`Runtime.direct`) — operate straight on the filesystem. No
  daemon needed; good for scripts, offline edits, and tests.
- **Attached** (`Runtime.connect`) — talk to a live daemon over
  `runtime.sock` using framed JSON-line RPC.

## Quick example

```python
from tessera_sdk import Runtime

# Direct mode — no daemon required
rt = Runtime.direct(repo=".")          # or omit repo to auto-detect

for t in rt.discover():
    print(t["id"], t["status"])

ticket = rt.get_ticket("HQ_BR-010")
print(ticket.title, ticket.status)

ticket.transition("running", reason="starting work")
result = ticket.invoke_action("deploy")
print(result["exit_code"], result["stdout"])
```

## Constructors

### `Runtime.direct(repo=None, config=None)`
Operate on the filesystem. `repo` may be omitted to auto-detect the nearest
directory containing `TicketsRepository/` (raises if none found).

### `Runtime.connect(sock=None, repo=None)`
Attach to a running daemon. With no args, uses the auto-detected repo root's
`.ticket-runtime/runtime.sock`. Raises `RuntimeNotRunning` if the socket is
absent. Use as a context manager to auto-close:

```python
with Runtime.connect(repo=".") as rt:
    rt.transition("HQ_BR-010", "running")
```

## Operations

| Method | Direct mode | Attached mode |
|--------|-------------|---------------|
| `discover()` | rescans filesystem, returns `[{id, status}]` | RPC `discover` |
| `get_ticket(id)` → `Ticket` | reads files | RPC via `_rpc` not exposed per-method; use direct mode or CLI for single-ticket reads |
| `transition(id, status, reason=None)` | validates + writes state.json + appends activity | RPC `transition` |
| `invoke_action(id, action, **kwargs)` | runs hook locally via executor | RPC `invoke_action` |
| `emit(name, ticket_id=None, data=None)` | publishes on local bus | RPC `emit` |
| `subscribe(names=None)` → iterator | local bus replay | v1: returns empty iterator (deferred) |

> Note: in v1, `get_ticket` in attached mode is not an RPC endpoint; the
> practical pattern is direct mode for reads, attached mode for mutating RPCs,
> or the CLI. `subscribe` over the socket is deferred — it degrades to an
> empty iterator so callers fail gracefully.

## `Ticket` object

`get_ticket` returns a `Ticket`:

- `.id`, `.title`, `.metadata` (`TicketMetadata`), `.state` (`TicketState`), `.status`
- `.transition(status, reason=None)` — returns self for chaining
- `.invoke_action(action, **kwargs)` — returns `{exit_code, stdout, stderr}`
- `.to_dict()` — `{id, title, type, status, updated_at}`

## Errors

- `SDKError` — base class (typed mirror of runtime validation)
- `RuntimeNotRunning(SDKError)` — socket missing when connecting

## Environment & permissions

`invoke_action` in direct mode builds permissions from the ticket's
`MANIFEST.yaml` (deny-all default) and runs the hook through the executor with
the same jail/env logic as the daemon.

## Example: subscribe to lifecycle events (direct mode)

```python
import threading
from tessera_sdk import Runtime

rt = Runtime.direct(repo=".")
events = rt.subscribe(["ticket.completed"])
stop = threading.Event()

def watch():
    for ev in events:
        print(ev.ticket_id, ev.name, ev.data)

threading.Thread(target=watch, daemon=True).start()
# ... trigger transitions elsewhere ...
```

For event-driven automation with the live daemon, use manifest hooks
([TICKETS.md](TICKETS.md#3-hooks--event-driven)) instead.
