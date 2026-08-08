"""Scheduler — queueing, locking, retry, and dependency ordering.

Responsibility (Master Part VIII, CONTRACTS.md §6): sit between the Event
Bus and the Dispatcher. Owns per-ticket sequential queues, a bounded worker
pool, ticket-level ``fcntl.flock`` locks, retry/timeout policy from the
descriptor, coarse debounce (dedupe by ``(ticket_id, run)``), and dependency
ordering (a ticket's jobs wait until its ``depends_on`` tickets reach
``completed`` in the registry).
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from lib.ticket_management.config import RuntimeConfig

logger = logging.getLogger(__name__)

__all__ = [
    "ScheduledJob",
    "Scheduler",
    "reap_stale_locks",
    "ticket_lock",
    "action_lock",
]


@contextlib.contextmanager
def action_lock(
    lock_dir: str | Path, ticket_id: str, action_name: str
) -> Iterator[Path]:
    """Exclusive ``fcntl.flock`` on ``locks/<ticket_id>.<action_name>.lock``.

    RFC-0006:19: action-level locks serialize concurrent runs of the SAME
    action on a ticket (a long hook must not start twice). Different
    actions on the same ticket remain concurrent. Stale files are reaped
    by :func:`reap_stale_locks` on boot (it globs ``*.lock``).
    """
    path = Path(lock_dir) / f"{ticket_id}.{action_name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextlib.contextmanager
def ticket_lock(lock_dir: str | Path, ticket_id: str) -> Iterator[Path]:
    """Exclusive ``fcntl.flock`` on ``locks/<ticket_id>.lock``.

    Shared locking primitive used by the Scheduler and the socket RPC path
    (Invariant I-8: acquired only around state mutations, never pure reads).
    The kernel releases the lock if the process dies; stale *files* are
    reaped by :func:`reap_stale_locks` on boot.
    """
    path = Path(lock_dir) / f"{ticket_id}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def reap_stale_locks(lock_dir: str | Path) -> int:
    """Remove lock files no process currently holds.

    ``fcntl.flock`` locks are released by the kernel on process death, but
    the lock *files* linger after a crash (``kill -9``). Reaping on boot
    (RFC-0006: "Stale locks (from crashed runtime) reaped on boot") tries a
    non-blocking exclusive lock on every ``*.lock``: if it succeeds, no
    holder exists and the file is safe to unlink.

    Returns the number of reaped files.
    """
    lock_path = Path(lock_dir)
    if not lock_path.is_dir():
        return 0
    reaped = 0
    for lock_file in sorted(lock_path.glob("*.lock")):
        fd = -1
        try:
            fd = os.open(str(lock_file), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # Still held by a live process: leave it alone.
                continue
            # Acquired => nobody holds it; stale artifact.
            lock_file.unlink()
            reaped += 1
        except OSError:
            continue
        finally:
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
    return reaped


@dataclass
class ScheduledJob:
    """A job waiting for (or running under) a ticket's queue."""

    ticket_id: str
    run: str
    descriptor: Any
    ticket_root: Path
    config: RuntimeConfig
    event_payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 2  # default band: hook
    action_name: str | None = None  # RFC-0006:19 action-level lock key
    submitted_at: float = field(default_factory=time.time)


class Scheduler:
    """Per-ticket sequential queues over a bounded worker pool.

    * Each ticket gets its own FIFO queue; jobs for the same ticket run
      sequentially (CONTRACTS.md §6 default when no workspace).
    * Different tickets run concurrently up to ``config.worker_concurrency``.
    * A ticket-level ``fcntl.flock`` guards each dispatch.
    * Retries are honored from ``descriptor.retry`` (fall back to
      ``config.default_retry``).
    * Debounce: enqueueing a job with the same ``(ticket_id, run)`` replaces
      the pending entry for that ticket.
    * Dependency ordering: before dispatch, jobs wait (polling the registry)
      until each ``depends_on`` ticket has lifecycle state ``completed``.
    """

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        registry: Any = None,
        runner: Callable[..., Any] | None = None,
        lock_dir: str | Path | None = None,
    ) -> None:
        self.config = config or RuntimeConfig()
        self.registry = registry
        # runner(ticket_id, descriptor, ticket_root, config, event_payload) -> result
        self.runner = runner
        self._queues: dict[str, list[ScheduledJob]] = {}
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], ScheduledJob] = {}
        self._last_seen: dict[tuple[str, str], float] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(self.config.worker_concurrency))
        )
        self._stopping = threading.Event()
        self._queue_workers: dict[str, threading.Thread] = {}
        self._workers_lock = threading.Lock()
        self._lock_dir = (
            Path(lock_dir) if lock_dir is not None else Path(".ticket-runtime") / "locks"
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def enqueue(
        self,
        ticket_id: str,
        descriptor: Any,
        ticket_root: str | Path,
        config: RuntimeConfig | None = None,
        event_payload: dict[str, Any] | None = None,
        priority: int = 2,
        action_name: str | None = None,
    ) -> None:
        """Queue *descriptor* for *ticket_id* (debounced by (id, run))."""
        job = ScheduledJob(
            ticket_id=ticket_id,
            run=getattr(descriptor, "run", str(descriptor)),
            descriptor=descriptor,
            ticket_root=Path(ticket_root).resolve(),
            config=config or self.config,
            event_payload=dict(event_payload or {}),
            priority=priority,
            action_name=action_name,
        )

        with self._lock:
            key = (ticket_id, job.run)
            now = time.time()
            window = float(getattr(self.config, "debounce_window_seconds", 1.0))

            # Debounce: coalesce duplicates within the debounce window. The
            # pending key lives until the job completes, and _last_seen
            # extends the window past completion, so rapid re-enqueues of the
            # same (ticket_id, run) collapse to one execution.
            if key in self._pending or now - self._last_seen.get(key, 0.0) < window:
                queue = self._queues.get(ticket_id)
                if queue:
                    for i, existing in enumerate(queue):
                        if existing.run == job.run:
                            queue[i] = job
                            break
                logger.debug("scheduler: debounced duplicate job %s", key)
                return

            self._pending[key] = job
            self._last_seen[key] = now
            self._queues.setdefault(ticket_id, []).append(job)

        self._ensure_queue_worker(ticket_id)

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting work and drain the pool.

        Queue workers are joined first (they exit naturally once their queue
        is empty), so jobs enqueued before shutdown still run. Only then is
        the worker pool shut down.
        """
        self._stopping.set()
        with self._workers_lock:
            workers = list(self._queue_workers.values())
        for worker in workers:
            worker.join(timeout=10 if wait else 0.1)
        self._pool.shutdown(wait=wait)

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _ensure_queue_worker(self, ticket_id: str) -> None:
        with self._workers_lock:
            if ticket_id in self._queue_workers:
                return
            thread = threading.Thread(
                target=self._drain_queue,
                args=(ticket_id,),
                name=f"sched-{ticket_id}",
                daemon=True,
            )
            self._queue_workers[ticket_id] = thread
            thread.start()

    def _drain_queue(self, ticket_id: str) -> None:
        # Exit only when this ticket's queue is empty. Shutdown sets the
        # stopping flag but workers keep draining whatever was enqueued
        # before shutdown (shutdown() joins them).
        while True:
            with self._lock:
                queue = self._queues.get(ticket_id)
                if not queue:
                    self._queues.pop(ticket_id, None)
                    with self._workers_lock:
                        self._queue_workers.pop(ticket_id, None)
                    return
                job = queue.pop(0)
                if not queue:
                    self._queues.pop(ticket_id, None)

            # Per-ticket sequential: wait for this job to finish before
            # dequeuing the next. Cross-ticket parallelism comes from each
            # ticket having its own queue worker; the pool caps total
            # concurrency at config.worker_concurrency.
            future = self._pool.submit(self._run_job, job)
            future.result()

    def _run_job(self, job: ScheduledJob) -> Any:
        try:
            self._wait_for_dependencies(job)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("scheduler: dependency wait failed for %s: %s", job.ticket_id, exc)

        retries = int(
            getattr(job.descriptor, "retry", None)
            if getattr(job.descriptor, "retry", None) is not None
            else job.config.default_retry
        )

        try:
            attempt = 0
            while True:
                try:
                    # RFC-0006:19: the same action on a ticket must not run
                    # concurrently (a long hook starting twice). Different
                    # actions on the same ticket stay concurrent.
                    action_key = job.action_name or job.run
                    with self._action_lock(job.ticket_id, action_key):
                        with self._ticket_lock(job.ticket_id):
                            if self.runner is not None:
                                result = self.runner(
                                    job.ticket_id,
                                    job.descriptor,
                                    job.ticket_root,
                                    job.config,
                                    job.event_payload,
                                )
                            else:
                                # No runner wired: report the would-be dispatch.
                                result = {
                                    "ticket_id": job.ticket_id,
                                    "run": job.run,
                                    "attempt": attempt,
                                    "dispatched": True,
                                }
                    return result
                except Exception as exc:  # noqa: BLE001
                    attempt += 1
                    logger.warning(
                        "scheduler: job %s/%s failed (attempt %d/%d): %s",
                        job.ticket_id,
                        job.run,
                        attempt,
                        retries,
                        exc,
                    )
                    if attempt > retries:
                        return {
                            "ticket_id": job.ticket_id,
                            "run": job.run,
                            "failed": True,
                            "attempts": attempt,
                            "error": str(exc),
                        }
                    # Backoff between attempts (RFC-0006:24): exponential
                    # 2^(attempt-1) * base, interruptible by shutdown so
                    # shutdown(wait=True) is never blocked for the full
                    # window.
                    delay = self._backoff_delay(attempt, job.config)
                    if delay > 0:
                        self._stopping.wait(delay)
                        if self._stopping.is_set():
                            logger.info(
                                "scheduler: job %s/%s aborted during backoff "
                                "(shutdown)",
                                job.ticket_id,
                                job.run,
                            )
                            return {
                                "ticket_id": job.ticket_id,
                                "run": job.run,
                                "failed": True,
                                "attempts": attempt,
                                "error": "shutdown during backoff",
                            }
        finally:
            # The debounce key lives until the job fully completes; only then
            # may a fresh event for the same (ticket_id, run) enqueue again.
            with self._lock:
                self._pending.pop((job.ticket_id, job.run), None)

    @staticmethod
    def _backoff_delay(attempt: int, config: RuntimeConfig) -> float:
        """Exponential backoff before retry *attempt*: base * 2**(attempt-1).

        RFC-0006:24. A non-positive base (or attempt <= 0) means no delay.
        """
        base = float(getattr(config, "retry_backoff_seconds", 1.0))
        if attempt <= 0 or base <= 0:
            return 0.0
        return base * (2 ** (attempt - 1))

    def _wait_for_dependencies(self, job: ScheduledJob) -> None:
        """Block until all ``depends_on`` tickets are ``completed`` (or the
        registry is unavailable). Polls on a short interval."""
        if self.registry is None:
            return
        try:
            metadata = self._load_metadata(job.ticket_root)
        except Exception:  # pragma: no cover - defensive
            return
        depends_on = metadata.get("depends_on") or []
        if not depends_on:
            return

        for dep_id in depends_on:
            while not self._dependency_completed(dep_id):
                time.sleep(0.05)

    def _dependency_completed(self, dep_id: str) -> bool:
        try:
            row = self.registry.get(dep_id)
        except Exception:  # pragma: no cover - defensive
            return True  # no registry row => treat as satisfied
        if row is None:
            return True
        return row.get("state") == "completed"

    def _load_metadata(self, ticket_root: Path) -> dict[str, Any]:
        import json

        meta_path = ticket_root / "metadata.json"
        if not meta_path.is_file():
            return {}
        return json.loads(meta_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ #
    # Ticket-level lock (fcntl.flock, CONTRACTS.md §7)
    # ------------------------------------------------------------------ #
    class _TicketLock:
        def __init__(self, path: Path) -> None:
            self._path = path
            self._fd: int | None = None

        def __enter__(self) -> "_TicketLock":
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc: Any) -> None:
            if self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
                self._fd = None

    def _ticket_lock(self, ticket_id: str) -> "_TicketLock":
        return self._TicketLock(self._lock_dir / f"{ticket_id}.lock")

    def _action_lock(
        self, ticket_id: str, action_name: str
    ) -> contextlib.AbstractContextManager[Path]:
        """Exclusive flock on ``locks/<id>.<action>.lock`` (RFC-0006:19)."""
        return action_lock(self._lock_dir, ticket_id, action_name)