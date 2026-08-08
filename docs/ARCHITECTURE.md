# Architecture

Tessera v1 is a file-system-native ticket runtime. The core idea: **the file
system is the database and the event source.** Tickets are directories;
changes to them are filesystem events; the runtime turns those into domain
events that drive isolated subprocesses.

## Code layout

The framework is a `src/` layout with two installable packages:

```
src/
├── tessera_runtime/   the engine — watcher, bus, scheduler, executor,
│   │                  server, registry, repo, models, config
│   ├── runtime/       pipeline internals
│   └── cli.py         Typer CLI (console scripts: tessera, ticket)
└── tessera_sdk/       the client SDK (Runtime facade; direct + socket modes)
```

- Engine import root: `tessera_runtime`
- SDK import root: `tessera_sdk` (see [SDK.md](SDK.md))

Installed copies live under the install prefix `tessera/` in the user's home
(`~/.local/share/tessera`), with `tessera` and `ticket` symlinked into
`~/.local/bin` — no system directories, no `sudo`. See
[../INSTALL.md](../INSTALL.md).

## The pipeline

```
   file change
      │
      ▼
┌────────────┐   triggers    ┌────────────┐   events    ┌────────────┐   jobs   ┌────────────┐
│  Watcher   │ ────────────▶ │ Event Bus  │ ──────────▶ │ Scheduler  │ ──────▶ │  Executor  │
│ (inotify)  │               │  pub/sub   │             │  queues    │         │ subprocess │
└────────────┘               └────────────┘             └────────────┘         └────────────┘
     ▲                                                        │                      │
     │                  lifecycle events                       │                permission
     └────────────────── ticket.<status> ◀─────────────────────┘                + env + jail
```

### 1. Watcher (`tessera_runtime/runtime/watcher.py`)

- Registers the whole repo with **inotify** (non-recursive, so every
  directory is watched explicitly — nested ticket/asset dirs included)
- Debounces rapid duplicate events (`config.debounce_window_seconds`)
- Maps a changed path to a domain trigger:
  - `metadata.json` → `metadata.updated`
  - `MANIFEST.yaml` → `manifest.updated`
  - `state.json` → `state.updated`
  - `activity.jsonl` → `activity.updated`
  - anything else → `fs.changed`
- **Manifest `watch:` rules** add custom triggers for glob paths inside a
  ticket (e.g. `assets/**` → `asset_indexed`); the rule cache is refreshed
  when the manifest changes (CMP-10)
- Ignores events outside `TicketsRepository/*.ticket/` and never watches
  runtime-owned state (circular-watch guard)

### 2. Event Bus (`tessera_runtime/runtime/bus.py`)

- In-process pub/sub
- **Recursion guard** (`config.recursion_max_depth`): a hook that emits an
  event that triggers a hook that emits an event … is capped
- Subscriber exceptions are isolated — one bad subscriber never takes down
  the bus

### 3. Scheduler (`tessera_runtime/runtime/scheduler.py`)

- **Per-workspace queues**: tickets declaring a `workspace` share a queue with
  other tickets in that workspace (serialized); tickets without a workspace
  queue by ticket id (CONTRACTS §6, CMP-04)
- **Priority bands**: lower band number = higher priority
  (`0 emergency → 3 background`); FIFO within a band (CMP-11)
- **Action-level locks**: `locks/<id>.<action>.lock` — the same action on the
  same ticket never runs twice concurrently (RFC-0006:19, CMP-03)
- **Ticket-level locks**: `locks/<id>.lock` guards only state-mutation
  critical sections (socket transitions) — NOT held during hook subprocess
  runs, so a transition can proceed while a long hook runs (CMP-02)
- **Retry with exponential backoff** (`config.retry_backoff_seconds`, CMP-05);
  after retries are exhausted it writes an `activity.jsonl` record and
  publishes `ticket.action.failed` (CMP-06)

### 4. Executor (`tessera_runtime/runtime/executor.py`)

- Resolves environment: System → ticket `.env` → manifest `env` → event
  payload, with secret gating
- Enforces **permissions** (deny-all default; manifest capability grants)
- Runs hooks/actions in **isolated subprocesses** with a **path jail** and
  timeout

## Server & socket RPC (`tessera_runtime/runtime/server.py`)

The daemon (`tessera runtime start`) runs a Unix-socket JSON-RPC server at
`.ticket-runtime/runtime.sock`. The SDK's `tessera_sdk.Runtime.connect()`
speaks to it.
RPC methods: `discover`, `transition`, `invoke_action`, `emit`, `shutdown`.
Socket RPC transitions acquire the ticket lock (CMP-01); pure reads are
lock-free (I-8).

## Registry (`tessera_runtime/runtime/registry.py`)

A SQLite registry (`config.registry_path`, default `.ticket-runtime/
registry.db`) indexes tickets by id → state for dependency resolution and
fast discovery. It is rebuilt from the filesystem on demand (`repo scan`,
`Runtime.discover()` direct mode) and is disposable (recreated if corrupt).

## Concurrency model (summary)

| Concern | Mechanism |
|---------|-----------|
| Same action, same ticket | action lock (`<id>.<action>.lock`) |
| Same ticket, socket transition | ticket lock (`<id>.lock`) during mutation |
| Same workspace, different tickets | shared workspace queue |
| Hook subprocess vs transition | no lock held during subprocess run (CMP-02) |
| Event re-emission loops | recursion depth guard |
| Retries | bounded attempts + exponential backoff |

## Key invariants

- **Disposability (I-2)**: `.ticket-runtime/` can be deleted; boot recreates
  it with defaults
- **File system is the source of truth**: registry and daemon state are
  caches; `repo scan` restores them
- **Deny by default**: no manifest permission grant ⇒ no capability at
  runtime
