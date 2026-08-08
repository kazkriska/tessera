# Ticket Authoring Guide

A **ticket** is a directory under `TicketsRepository/` named `<id>.ticket`.
Everything the runtime knows about a ticket lives in four files:

```
HQ_BR-010.ticket/
  metadata.json     # identity + relationships (CONTRACTS §2)
  state.json        # lifecycle status (CONTRACTS §1)
  activity.jsonl    # append-only audit trail
  MANIFEST.yaml     # hooks, actions, permissions, env, watch rules (CONTRACTS §7)
```

## 1. `metadata.json` — identity

The canonical schema (JSON Schema draft-07, CONTRACTS §2) requires **six
fields** — there is no lenient read path (CMP-12):

```json
{
  "id": "HQ_BR-010",
  "title": "Fix login redirect",
  "kind": "ticket",
  "type": "task",
  "created_at": "2026-08-08T12:00:00Z",
  "owner": { "name": "alice", "type": "user" }
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `id` | ✅ | `^[A-Za-z0-9_-]+$`; must match the directory name |
| `title` | ✅ | non-empty string |
| `kind` | ✅ | always `ticket` in v1 |
| `type` | ✅ | semantic subtype: `task`, `memory`, `skill`, … |
| `created_at` | ✅ | ISO-8601 date-time |
| `owner` | ✅ | object with `name` + `type` (`user` / `agent` / `system`) |

Optional but supported: `scope`, `version`, `tags`, `custom`, and the graph
relationship fields `parent`, `children`, `depends_on`, `blocks`,
`duplicates`, `references`, `related_to`, `spawned_from`, `delegated_to`,
`workspace`.

Dangling relationship ids are **ignored with a warning**, never fatal.

## 2. `MANIFEST.yaml` — behavior

The manifest declares what the runtime may do with this ticket. It is
validated strictly at load time; YAML anchors/aliases/merge keys are
**forbidden** (security: CONTRACTS §7).

```yaml
apiVersion: ticket/v1      # required
kind: Ticket               # required
metadata:
  id: HQ_BR-010            # must equal directory name
  title: Fix login redirect
  type: task

env:                       # optional static env for every hook/action
  REGION: eu-west

hooks:                     # event-driven commands (see §3)
  on_state_running:
    - run: notify.sh
      shell: bash
      timeout: 60

actions:                   # user/CLI-invoked commands (see §4)
  deploy:
    run: ./deploy.sh
    shell: bash
    timeout: 300
    retry: 2

permissions:               # optional capability gate (see §5)
  capabilities:
    - run:bash

watch:                     # optional custom file triggers (see §6)
  - path: assets/**
    trigger: asset_indexed
```

### Executable descriptor shape

`hooks:` and `actions:` entries share one descriptor:

| Key | Default | Notes |
|-----|---------|-------|
| `run` | — | command to execute |
| `shell` | `bash` | must be a known shell (`bash`, `sh`, `python`, `node`, …) |
| `timeout` | config `default_timeout` (300s) | kill after this many seconds |
| `retry` | config `default_retry` (0) | retries on failure, exponential backoff |
| `async` | `false` | run without blocking the queue worker |

## 3. Hooks — event-driven

Hooks fire when the watcher detects a change. Built-in trigger mapping:

| File change | Trigger |
|-------------|---------|
| `metadata.json` | `metadata.updated` → `hooks.on_metadata_updated` |
| `MANIFEST.yaml` | `manifest.updated` → `hooks.on_manifest_updated` |
| `state.json` | `state.updated` → `hooks.on_state_updated` |
| `activity.jsonl` | `activity.updated` → `hooks.on_activity_updated` |
| anything else | `fs.changed` (no default hook) |

## 4. Actions — on demand

Actions are invoked explicitly (`tessera action <id> <action>` or
`Runtime.invoke_action`). Unlike hooks, they are **denied by default**: the
manifest must declare the action and, if permissions are set, grant the
capability.

## 5. Permissions

```yaml
permissions:
  capabilities:
    - run:bash
```

Without `permissions`, the default is **deny-all** for every hook/action run
via the runtime (the `executor_dispatch` path). `tessera create` writes no
permissions, so out of the box a new ticket's actions are denied — declare
permissions explicitly when you add actions you want to run.

## 6. Watch rules — custom triggers

Declare extra triggers for paths inside the ticket (CMP-10):

```yaml
watch:
  - path: task/assets/**
    trigger: asset_indexed
```

- `path` is relative to the ticket root and matched as a glob
- a change to a matching file emits the declared trigger **in addition to**
  the default mapping
- watching runtime-owned state (`state.json`, `activity.jsonl`) is **forbidden**
  (circular-watch guard)

## 7. `state.json` — lifecycle

Written by the runtime on every transition. See
[Lifecycle & State Machine](LIFECYCLE.md) for the 9 states and the legal
transition table.

## 8. `activity.jsonl` — audit trail

Append-only JSON-lines log. Every lifecycle transition appends a record;
failed action runs append a `ticket.action.failed` record after retries are
exhausted (CMP-06). Tail it with `tessera log <id>`.
