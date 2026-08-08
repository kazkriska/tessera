# Configuration

The runtime's **only** configuration file is
`TicketsRepository/.ticket-runtime/config.yaml`. It is loaded at boot
(CONTRACTS §5). Every key is optional; a missing file (or deleting it) reverts
the runtime to the defaults below — `.ticket-runtime/` is disposable
(Invariant I-2).

## Full reference

```yaml
# .ticket-runtime/config.yaml — all keys, with defaults

repo_path: null              # absolute path to the repository root;
                             # null => cwd's TicketsRepository/

debounce_window_seconds: 1.0 # filesystem event debounce window (seconds)

worker_concurrency: 4        # max concurrent subprocesses across all queues

priority_bands:              # scheduler ordering; lower number = higher priority
  0: emergency               #   cancellation handlers
  1: user                    #   CLI / user-action commands
  2: hook                    #   file-modification hooks
  3: background              #   asset indexing / maintenance

recursion_max_depth: 10      # event re-emission depth guard

default_timeout: 300         # seconds; used when a descriptor omits `timeout`
default_retry: 0             # retries; used when a descriptor omits `retry`
retry_backoff_seconds: 1.0   # base delay before retries; exponential 2^(n-1)

approval_cache_path: cache/approvals   # where escalation approval requests land
log_level: INFO              # DEBUG | INFO | WARNING | ERROR | CRITICAL
log_path: logs/runtime.log   # runtime log file (relative to .ticket-runtime/)
lock_dir: locks              # flock files: <id>.lock, <id>.<action>.lock
registry_path: registry.db   # ticket registry database
```

## Semantics

- **Path values** (`approval_cache_path`, `log_path`, `lock_dir`,
  `registry_path`) are **relative to `.ticket-runtime/`** and are resolved at
  the use site.
- **Unknown keys** are ignored with a warning.
- **Malformed values** fall back to the field default with a warning; boot
  never fails on a bad config (runtime state is disposable).

## Behavior notes (verified on v0.1.0)

- `log_level` / `log_path` configure the runtime's own logger at boot
  (CMP-07). The daemon also writes `logs/runtime.log` by default.
- `registry_path` is honored — the registry database lives where you say
  (CMP-08).
- `approval_cache_path` is created at boot and escalation requests are
  persisted there as `<ticket_id>.<action>.<timestamp>.json` (CMP-09).
- `priority_bands` orders each scheduler queue; lower band number = higher
  priority, FIFO within a band (CMP-11).

## Example: a tuned config

```yaml
# .ticket-runtime/config.yaml
debounce_window_seconds: 0.2
worker_concurrency: 8
log_level: DEBUG
log_path: logs/debug.log
default_retry: 2
retry_backoff_seconds: 2.0
```
