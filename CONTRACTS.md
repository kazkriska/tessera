# Tessera v1 — Consolidated Contracts & Decisions

**Status:** Ratified (CTO sign-off, 2026-08-07)
**Scope:** Binding resolutions to spec ambiguities found while reading `tessera-docs/`
(Charter + Master Parts I–XII + RFC-0000…0012).
**Authority:** Supersedes conflicting text in the source docs for any value defined here.
Where a source doc and this file disagree, **this file wins** until the source doc is amended.
**No code has been written.** This is the spec sub-agents (Igor/Josh) build against.

---

## 0. Why this doc exists

The cloned `tessera-docs/` tree is unusually complete, but it contains **7 concrete contract
contradictions** that would otherwise force the implementing engineer to guess. Phase A
(`manifest.py` loader/validator + models) is where those guesses harden into code, so the
resolutions below are locked *first*. Each item cites the conflicting source and the decision.

Global source-of-truth ordering for v1:
1. This `CONTRACTS.md` (explicit decisions)
2. `rfcs/` (versioned, newer than Master)
3. `master/` (Phase 1 book)
4. `foundation/charter.md` (root authority on philosophy/invariants only)

---

## 1. Canonical lifecycle state set (resolves Ambiguity #1 + #2)

**Conflict:**
- Lifecycle table (Master Part VI §4.1 / RFC-0007): `Created, Initialized, Ready, Running, Blocked, Delegated, Completed, Archived` (+ `failed` from Rev A R.A.6).
- FRAME `state.json` JSON Schema (Master Part IV R.A.4): enum `created, initializing, ready, running, blocked, handoff, completed, failed, archived` — **omits `delegated`**, spells it `initializing`, and adds `handoff` not present in the lifecycle table.

**Decision — single canonical enum, lowercase, stored as `state.json["status"]`:**

```
created | initialized | ready | running | blocked | delegated | completed | archived | failed
```

- `handoff` is **removed** (one-off in FRAME, never in the lifecycle table).
- `initializing` → renamed to **`initialized`** to match the lifecycle table exactly.
- `delegated` is **added** (was missing from the FRAME schema; it is a first-class state in Part VI).
- `failed` is part of the canonical set (Rev A R.A.6), with transitions:
  `running → failed`, `failed → ready`, `failed → initialized`, `failed → archived`.

**Casing convention (resolves Ambiguity #2):**
- `state.json["status"]` values: **lowercase** (`running`, not `Running`).
- Event names: **lowercase, dotted** (`ticket.running`, `metadata.updated`).
- Lifecycle event name = `ticket.<status-lowercase>`. Mapping table:

  | status (state.json) | emitted event |
  |---|---|
  | created | `ticket.created` |
  | initialized | `ticket.initialized` |
  | ready | `ticket.ready` |
  | running | `ticket.running` |
  | blocked | `ticket.blocked` |
  | delegated | `ticket.delegated` |
  | completed | `ticket.completed` |
  | archived | `ticket.archived` |
  | failed | `ticket.failed` |

  Plus `ticket.reinitialized` (Archived → Initialized) and `ticket.transition.rejected`.
- The FRAME `state.json` JSON Schema enum MUST be corrected to the list above (drop `handoff`, rename `initializing`→`initialized`, add `delegated`). This correction is authoritative; the prose table in Part VI/RFC-0007 is already correct and unchanged.

---

## 2. `metadata.json` schema — add relationships (resolves Ambiguity #3)

**Conflict:** Part IV §4.4 declares graph relationship fields in `metadata.json`
(`parent`, `children`, `depends_on`, `blocks`, `duplicates`, `references`, `related_to`,
`spawned_from`, `delegated_to`), but the FRAME JSON Schema (R.A.4) omits all of them.

**Decision — canonical `metadata.json` schema (JSON Schema draft-07), supersedes R.A.4 schema:**

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "TesseraTicketMetadata",
  "type": "object",
  "required": ["id", "title", "kind", "type", "created_at", "owner"],
  "properties": {
    "id":         {"type": "string", "pattern": "^[A-Za-z0-9_-]+$"},
    "title":      {"type": "string"},
    "kind":       {"type": "string", "default": "ticket",
                   "description": "Resource category. Always 'ticket' in v1."},
    "type":       {"type": "string",
                   "description": "Semantic subtype: task, memory, skill, ..."},
    "scope":      {"type": "string"},
    "version":    {"type": "string", "default": "1.0.0"},
    "created_at": {"type": "string", "format": "date-time"},
    "owner":      {"type": "object", "required": ["name", "type"],
                   "properties": {
                     "name":  {"type": "string"},
                     "type":  {"type": "string", "enum": ["user", "agent", "system"]},
                     "email": {"type": "string"}}},
    "tags":       {"type": "array", "items": {"type": "string"}},
    "custom":     {"type": "object"},

    "parent":        {"type": ["string", "null"]},
    "children":      {"type": "array", "items": {"type": "string"}},
    "depends_on":    {"type": "array", "items": {"type": "string"}},
    "blocks":        {"type": "array", "items": {"type": "string"}},
    "duplicates":    {"type": "array", "items": {"type": "string"}},
    "references":    {"type": "array", "items": {"type": "string"}},
    "related_to":    {"type": "array", "items": {"type": "string"}},
    "spawned_from":  {"type": ["string", "null"]},
    "delegated_to":  {"type": ["string", "null"]},
    "workspace":     {"type": "string",
                      "description": "External work dir; NOT where Tickets live."}
  }
}
```

- `parent`/`spawned_from`/`delegated_to` are single-value (string|null); all other relationship
  fields are arrays of ticket ids.
- Dangling ids are **ignored with a warning**, never fatal (Part IV §8).
- `children` is a convenience mirror; the runtime may derive it but MUST accept it when present.

---

## 3. `type` vs `kind` (resolves Ambiguity #4)

**Conflict:** Manifest envelope uses `kind: Ticket` (Master Part V §4.1); FRAME `metadata.json`
schema uses `kind` (default `ticket`); Part IV §11 example uses `type: task` with no `kind`.
Three different shapes across three places.

**Decision — two distinct, always-meaningful fields:**

| Field | Where | Meaning | v1 value |
|---|---|---|---|
| `kind` | Manifest envelope (`metadata.kind`) | **API object kind**, Kubernetes-style, capitalized | `Ticket` |
| `kind` | `metadata.json` | **Resource category** (lowercase) | `ticket` (constant in v1) |
| `type` | `metadata.json` | **Semantic subtype**, free-form | e.g. `task`, `memory`, `skill` |

- The manifest envelope `kind: Ticket` and the metadata `kind: ticket` are intentionally
  different namespaces (object kind vs resource category). Both are valid; do not unify them.
- `type` is optional but recommended; it is the only field that varies between Tickets
  (a feature request vs a memory entry differ by `type`, not `kind`).
- Loader reads identity as: `metadata.json.id` (immutable, must equal dir basename minus `.ticket`)
  + `metadata.json.kind` (must be `ticket`) + manifest `metadata.kind` (must be `Ticket`).
  Mismatch on `id==basename` is a hard reject; `kind` mismatch is a hard reject.

---

## 4. Manifest grammar — merge Rev A fields (resolves Ambiguity #5)

**Conflict:** `watch:` and `exports:` are specified only in Rev A (Master Part V R.A.5), absent
from the canonical grammar (§4.2) and from RFC-0003. A loader built from §4.2 alone would reject
valid Rev A manifests.

**Decision — these fields are part of the canonical v1 manifest and the loader MUST accept them:**

```yaml
watch:                       # low-level file change -> domain trigger (R.A.5)
  - path: "metadata.json"
    events: [modify]
    trigger: metadata_updated
  - path: "task/assets/**"
    events: [create, modify]
    trigger: asset_indexed

exports:                     # declared output variables for peer/agent introspection (R.A.5)
  task.status:     { type: string,  description: "Current task status" }
  task.report_path: { type: string, description: "Path to generated report" }
```

- `watch` entries map a **low-level** file event (path glob + inotify event set) to a **trigger**
  name. The trigger then routes to `hooks:` exactly like a domain event. This fills the
  Watcher→domain translation gap noted in Part II §4.2.
- **Circular-watch guard:** if a `watch` rule watches `state.json` / `activity.jsonl` and its
  target hook writes those files, the loader MUST reject the manifest (prevents self-recursion).
- `exports:` is documentation/introspection only; it does not change execution. Unknown export
  shapes are a validation warning, not fatal.
- Canonical v1 manifest top-level keys (final, supersedes Part V §4.2 + RFC-0003):
  `apiVersion, kind, metadata, runtime, initialize, hooks, actions, permissions, env, watch, exports`.
- Unchanged rules: `apiVersion: ticket/v1` required; YAML anchors/aliases/merge keys (`<<`)
  **forbidden** (loader rejects); `metadata.id == dir basename`.

---

## 5. `config.yaml` schema (resolves Ambiguity #6)

**Conflict:** Boot loads `Tickets/.ticket-runtime/config.yaml` (Master Part III §4.2) and Part IX
§10 references repo-wide policy, but no keys/schema are defined anywhere.

**Decision — canonical `config.yaml` (all keys optional; defaults shown):**

```yaml
# Tickets/.ticket-runtime/config.yaml
repo_path: null            # absolute path to TicketRepository; null => cwd's Tickets/
debounce_window_seconds: 1.0
worker_concurrency: 4      # max concurrent subprocesses across all queues
priority_bands:            # ordering key (FRAME R.A.8); lower = higher priority
  0: emergency            # cancellation handlers
  1: user                 # CLI / user-action commands
  2: hook                 # file-modification hooks
  3: background           # asset indexing / maintenance
recursion_max_depth: 10    # event re-emission depth guard (Part VII R.A.7)
default_timeout: 300       # seconds, used when descriptor omits timeout
default_retry: 0
approval_cache_path: cache/approvals   # relative to .ticket-runtime/
log_level: INFO
log_path: logs/runtime.log
lock_dir: locks                       # relative to .ticket-runtime/
registry_path: registry.db            # relative to .ticket-runtime/
```

- All paths under `.ticket-runtime/` are **disposable** (Invariant I-2); deleting the file reverts
  to defaults on next boot.
- `config.yaml` is the ONLY runtime-owned config; Tickets remain pure data (Charter principle 4).

---

## 6. Default queue key when a Ticket has no Workspace (resolves Ambiguity #7)

**Conflict:** Scheduler queue topology (Master Part VIII §4.1 / RFC-0006) keys queues on the
**external Workspace path** a Ticket points to. Most Tickets will not declare a `WORKSPACE`,
leaving the Scheduler with no queue key.

**Decision — queue-key resolution (in priority order):**

1. If the Ticket declares `workspace` (in `metadata.json` or `.env` `WORKSPACE=`), the queue key is
   that absolute path.
2. **Otherwise the queue key is the Ticket's own `id`** (per-Ticket queue).

Rationale: this preserves the spec's guarantees — within a queue, hooks/actions run sequentially
(serialized behind the ticket lock anyway); across queues (here, across ticket ids) they run
concurrently. A per-ticket key is the correct degenerate case of "per-workspace sequential,
workspaces concurrent" and needs no special-casing. The documented single-queue fallback
(Part VIII §4.1) remains available via `worker_concurrency` if a deployment prefers it.

---

## 7. Tooling & environment decisions (implementation substrate)

These are not spec contradictions but binding build choices so sub-agents don't re-litigate them.

- **Python:** pin `requires-python = ">=3.12"`. System `python3` here is **3.11.15** — insufficient.
  Provision 3.12 via `uv` (already installed, v0.12.0). Runtime runs under a `uv` venv, never system python.
- **Package manager:** `uv` (spec-mandated in Master Part III / RFC-0004). `pyproject.toml` at
  `lib/ticket-management/`.
- **YAML:** `PyYAML`. Strict loader requirement — reject anchors/aliases/merge keys. Implement by
  scanning the **event stream** (`yaml.parse` / `yaml.scan`) for `AnchorToken` / `AliasEvent` /
  `MergeKeyToken` and raising a `ManifestValidationError` before `safe_load`. (Post-`safe_load`
  detection is impossible because anchors are already resolved — must scan the stream.)
- **inotify:** no `pyinotify` on this box; kernel inotify present. Use **`inotify-simple`**
  (small, MIT, raw mask access — needed for precise low-level→domain translation). `watchdog` is
  rejected as too abstracting for the `watch:` translation layer.
- **Registry:** `sqlite3` (stdlib, v3.53.1 present). Derived, rebuildable (I-9).
- **CLI:** `typer` (spec-mandated). Talks to `runtime.sock`.
- **Tests:** `pytest` + `pytest-cov` (Phase H says "test suite green" but names no framework).
  Async paths (if any) use `pytest-asyncio`.
- **Secrets/env masking:** implement `DENYLIST` from Part IX R.A.9
  (`AWS_SECRET_ACCESS_KEY`, `DATABASE_URL`, `SSH_AUTH_SOCK`, `SUDO_USER`, …) in `env.py`;
  merge order System base → Workspace `.env` → Ticket `.env` → Manifest `env` → Event payload.
- **Path jail:** CWD pinned to Ticket root; runner sanitizes path args blocking `../` traversal;
  writes outside Ticket/granted workspace blocked unless declared (Part IX §4.1 + R.A.9).
- **Atomic writes:** `state.json`/`metadata.json` via tempfile + `os.replace`; `activity.jsonl`
  via `fcntl.flock` append (Part IV R.A.4 code is canonical).
- **Subprocess isolation:** each hook/action in a new POSIX process group (`os.setsid`); on
  `timeout` send `SIGTERM` to group, then `SIGKILL` after 3s (Part III R.A.3).

---

## 8. Open items explicitly deferred (do NOT build in v1)

Per Charter §7 Non-Goals and RFC-0012: agent-orchestration first-class contract, built-in AI
providers, general plugin marketplace, compiled/distributable Ticket packages, Windows/macOS,
cross-machine distributed scheduling, and the Skill/Workflow/Memory repositories. The framework
MUST stay resource-agnostic in structure, but these are out of v1 scope.

---

## 9. Sign-off

This document is the v1 build contract. Sub-agents implement Phases A–H (RFC-0012) against it.
Any further contradiction discovered during implementation is resolved here by the CTO and the
source doc is flagged for amendment — not silently worked around in code.
