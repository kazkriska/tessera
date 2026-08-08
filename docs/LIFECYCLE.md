# Lifecycle & State Machine

Every ticket has a lifecycle status persisted in `state.json`. Transitions
are validated against a fixed legal-transition table (CONTRACTS §1 / Part VI
§4.2); illegal transitions raise `TransitionError` and are rejected.

## The 9 states

| Status | Meaning |
|--------|---------|
| `created` | directory scaffolded; no work started |
| `initialized` | setup complete, ready to be prepared |
| `ready` | work can begin |
| `running` | work in progress |
| `blocked` | waiting on a dependency or external input |
| `delegated` | handed off to another owner/agent |
| `failed` | work ended in failure |
| `completed` | work finished successfully |
| `archived` | closed; terminal state |

## Legal transitions

```
created      -> initialized
initialized  -> ready
ready        -> running | delegated | blocked
blocked      -> ready | running
running      -> blocked | delegated | completed | failed
delegated    -> blocked | ready | completed
failed       -> ready | initialized | archived
completed    -> archived
archived     -> initialized          (reopen; emits ticket.reinitialized)
```

A transition to the **same status** is always a legal no-op (e.g.
`running -> running`).

## Events

Every successful transition publishes a lifecycle event on the runtime bus:

- `ticket.<status>` — e.g. `ticket.running`, `ticket.completed`
- `ticket.reinitialized` — for `archived -> initialized`

The event payload carries the ticket id, from/to status, and reason. These
events are what manifest `hooks` subscribe to (see the built-in trigger
mapping in [TICKETS.md](TICKETS.md#3-hooks--event-driven)).

## Driving transitions

- CLI: `tessera transition <id> <status>` (requires a running daemon)
- SDK: `runtime.transition(id, status, reason=...)` (direct mode works too)
- The transition is written atomically to `state.json` and appended to
  `activity.jsonl`.

## Failure handling

When an action/hook fails with retries exhausted:

- an `activity.jsonl` record is appended (`ticket.action.failed`)
- a `ticket.action.failed` bus event is published (CMP-06)
- the ticket's `state.json` is not mutated by the failure itself — lifecycle
  transitions are explicit user/runtime decisions
