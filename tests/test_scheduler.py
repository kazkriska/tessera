"""Tests for the Scheduler (D3)."""

from __future__ import annotations

import json
import threading
import time
import types
from pathlib import Path

import pytest

from lib.ticket_management.config import RuntimeConfig
from lib.ticket_management.runtime.scheduler import Scheduler


class FakeRegistry:
    """Minimal registry stub exposing .get(id) -> row dict."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def get(self, ticket_id: str):
        return self.rows.get(ticket_id)


class FakeDescriptor:
    def __init__(self, run: str = "script.sh", retry: int = 0, timeout: int = 5):
        self.run = run
        self.retry = retry
        self.timeout = timeout


def _make_ticket(tmp_path: Path, ticket_id: str, depends_on: list[str] | None = None) -> Path:
    root = tmp_path / f"{ticket_id}.ticket"
    root.mkdir(parents=True)
    meta = {"id": ticket_id, "title": ticket_id, "kind": "ticket", "owner": {"name": "t", "type": "user"}}
    if depends_on:
        meta["depends_on"] = depends_on
    (root / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return root


def test_enqueue_runs_descriptor_under_ticket_lock(tmp_path: Path):
    runs: list[tuple[str, str]] = []
    lock = threading.Lock()

    def runner(ticket_id, descriptor, ticket_root, config, payload):
        with lock:
            runs.append((ticket_id, descriptor.run))
        return {"ok": True}

    sched = Scheduler(config=RuntimeConfig(worker_concurrency=2), runner=runner)
    root = _make_ticket(tmp_path, "T-001")
    sched.enqueue("T-001", FakeDescriptor("a.sh"), root)
    sched.enqueue("T-001", FakeDescriptor("b.sh"), root)
    sched.shutdown(wait=True)

    assert len(runs) == 2
    assert runs[0][1] == "a.sh"
    assert runs[1][1] == "b.sh"


def test_retry_on_failure(tmp_path: Path):
    attempts = {"n": 0}

    def runner(ticket_id, descriptor, ticket_root, config, payload):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("flaky")
        return {"ok": True}

    sched = Scheduler(
        config=RuntimeConfig(worker_concurrency=1, retry_backoff_seconds=0.0),
        runner=runner,
    )
    root = _make_ticket(tmp_path, "T-002")
    sched.enqueue("T-002", FakeDescriptor("retry.sh", retry=3), root)
    sched.shutdown(wait=True)

    assert attempts["n"] == 3  # 2 failures then success


def test_concurrent_queues_run_in_parallel(tmp_path: Path):
    active = {"n": 0}
    max_active = {"n": 0}
    lock = threading.Lock()

    def runner(ticket_id, descriptor, ticket_root, config, payload):
        with lock:
            active["n"] += 1
            max_active["n"] = max(max_active["n"], active["n"])
        time.sleep(0.15)
        with lock:
            active["n"] -= 1
        return {"ok": True}

    sched = Scheduler(config=RuntimeConfig(worker_concurrency=4), runner=runner)
    roots = [_make_ticket(tmp_path, f"T-{i:03d}") for i in range(4)]
    for i, root in enumerate(roots):
        sched.enqueue(f"T-{i:03d}", FakeDescriptor("x.sh"), root)
    sched.shutdown(wait=True)

    assert max_active["n"] >= 2  # at least two queues overlapped


def test_dependency_ordering_blocks_until_dependency_completed(tmp_path: Path):
    order: list[str] = []
    lock = threading.Lock()
    registry = FakeRegistry()
    registry.rows["T-DEP"] = {"state": "running"}  # starts not completed

    def runner(ticket_id, descriptor, ticket_root, config, payload):
        if ticket_id == "T-DEP":
            time.sleep(0.2)
            registry.rows["T-DEP"] = {"state": "completed"}
        with lock:
            order.append(ticket_id)
        return {"ok": True}

    sched = Scheduler(
        config=RuntimeConfig(worker_concurrency=2),
        runner=runner,
        registry=registry,
    )
    dep_root = _make_ticket(tmp_path, "T-DEP")
    child_root = _make_ticket(tmp_path, "T-CHILD", depends_on=["T-DEP"])
    sched.enqueue("T-DEP", FakeDescriptor("dep.sh"), dep_root)
    sched.enqueue("T-CHILD", FakeDescriptor("child.sh"), child_root)
    sched.shutdown(wait=True)

    assert order.index("T-DEP") < order.index("T-CHILD")


def test_debounce_dedupes_duplicate_enqueues(tmp_path: Path):
    runs: list[str] = []
    lock = threading.Lock()

    def runner(ticket_id, descriptor, ticket_root, config, payload):
        with lock:
            runs.append(descriptor.run)
        return {"ok": True}

    sched = Scheduler(config=RuntimeConfig(worker_concurrency=1), runner=runner)
    root = _make_ticket(tmp_path, "T-010")
    sched.enqueue("T-010", FakeDescriptor("dup.sh"), root)
    sched.enqueue("T-010", FakeDescriptor("dup.sh"), root)  # debounced
    sched.enqueue("T-010", FakeDescriptor("other.sh"), root)
    sched.shutdown(wait=True)

    assert runs.count("dup.sh") == 1
    assert runs.count("other.sh") == 1


def test_no_runner_reports_dispatch(tmp_path: Path):
    sched = Scheduler(config=RuntimeConfig(worker_concurrency=1), runner=None)
    root = _make_ticket(tmp_path, "T-020")
    sched.enqueue("T-020", FakeDescriptor("z.sh"), root)
    sched.shutdown(wait=True)
    # No exception raised; the pending map is cleared.
    assert sched._pending == {}


def test_backoff_delay_policy_is_exponential():
    """RFC-0006 Part VIII §4.3 backoff: base * 2^(attempt-1), floor from config."""
    base = 0.1
    assert Scheduler._backoff_delay(1, RuntimeConfig(retry_backoff_seconds=base)) == pytest.approx(0.1)
    assert Scheduler._backoff_delay(2, RuntimeConfig(retry_backoff_seconds=base)) == pytest.approx(0.2)
    assert Scheduler._backoff_delay(3, RuntimeConfig(retry_backoff_seconds=base)) == pytest.approx(0.4)
    assert Scheduler._backoff_delay(4, RuntimeConfig(retry_backoff_seconds=base)) == pytest.approx(0.8)
    # A zero base means no delay at all (used to keep fast tests fast).
    assert Scheduler._backoff_delay(3, RuntimeConfig(retry_backoff_seconds=0.0)) == 0.0
    # Negative config values are clamped to zero.
    assert Scheduler._backoff_delay(2, RuntimeConfig(retry_backoff_seconds=-1.0)) == 0.0


def test_retry_backoff_delays_between_attempts(tmp_path: Path):
    """RFC-0006 Part VIII §4.3: retries are spaced by backoff and stop on success."""
    starts: list[float] = []
    lock = threading.Lock()

    def runner(ticket_id, descriptor, ticket_root, config, payload):
        with lock:
            starts.append(time.monotonic())
            n = len(starts)
        if n < 3:
            raise RuntimeError("flaky")
        return {"ok": True}

    sched = Scheduler(
        config=RuntimeConfig(worker_concurrency=1, retry_backoff_seconds=0.1),
        runner=runner,
    )
    root = _make_ticket(tmp_path, "T-002")
    sched.enqueue("T-002", FakeDescriptor("retry.sh", retry=2), root)
    # Let the job finish its retry cycle before shutting down; otherwise
    # shutdown's interruptible-wait semantics abort it mid-backoff.
    deadline = time.monotonic() + 5.0
    while len(starts) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    sched.shutdown(wait=True)

    assert len(starts) == 3  # 2 failures then success; no 4th attempt
    gap1 = starts[1] - starts[0]
    gap2 = starts[2] - starts[1]
    # Each gap is a floor: wait(backoff) cannot return early.
    assert gap1 >= 0.1 - 0.02
    assert gap2 >= 0.2 - 0.02  # exponential: 0.1 then 0.2
    assert gap2 > gap1  # base-2 growth, not constant delay


def test_default_retry_zero_single_attempt_no_delay(tmp_path: Path):
    """default_retry: 0 (the default) => exactly one attempt, no backoff wait."""
    attempts = {"n": 0}

    def runner(ticket_id, descriptor, ticket_root, config, payload):
        attempts["n"] += 1
        raise RuntimeError("always fails")

    # Descriptor without a `retry` attribute => falls back to config.default_retry.
    desc = types.SimpleNamespace(run="fail.sh")

    start = time.monotonic()
    sched = Scheduler(config=RuntimeConfig(worker_concurrency=1), runner=runner)
    root = _make_ticket(tmp_path, "T-021")
    sched.enqueue("T-021", desc, root)
    sched.shutdown(wait=True)
    elapsed = time.monotonic() - start

    assert attempts["n"] == 1  # no retries
    assert elapsed < 0.5  # a 1.0s backoff wait would blow this bound


def test_shutdown_interrupts_retry_backoff(tmp_path: Path):
    """shutdown(wait=True) must not be blocked for the full backoff window."""
    attempts = {"n": 0}

    def runner(ticket_id, descriptor, ticket_root, config, payload):
        attempts["n"] += 1
        raise RuntimeError("always fails")

    sched = Scheduler(
        config=RuntimeConfig(worker_concurrency=1, retry_backoff_seconds=30.0),
        runner=runner,
    )
    root = _make_ticket(tmp_path, "T-030")
    sched.enqueue("T-030", FakeDescriptor("fail.sh", retry=10), root)

    # Let the first attempt fail so the worker sits inside the backoff wait.
    deadline = time.monotonic() + 2.0
    while attempts["n"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert attempts["n"] == 1

    start = time.monotonic()
    sched.shutdown(wait=True)
    elapsed = time.monotonic() - start

    assert attempts["n"] == 1  # aborted during backoff; no further attempts
    assert elapsed < 5.0  # far below the 30s backoff => wait was interrupted
