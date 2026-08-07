"""State — lifecycle state machine and durable ticket state.

Responsibility (Master Part IV/VI, RFC-0007; CONTRACTS §1 and §2): own the
canonical lowercase status enum (created, initialized, ready, running, blocked,
delegated, completed, archived, failed), validate transitions, emit
`ticket.<status>` events, and persist `state.json`/`metadata.json` atomically
(tempfile + os.replace) with `activity.jsonl` appended under `fcntl.flock`.

TODO(Phase B): implement the state machine and atomic persistence.
"""
