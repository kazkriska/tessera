"""Tests for the Scheduler (D3)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

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

    sched = Scheduler(config=RuntimeConfig(worker_concurrency=1), runner=runner)
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
