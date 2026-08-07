"""H1 — Hardening: crash recovery, stale artifacts, socket action invocation.

Covers the Phase H done-criteria (Part XII §4, RFC-0004 failure modes,
RFC-0006 locking):

* ``kill -9`` of a runtime leaves a stale ``runtime.sock``; the next boot
  must reap it and serve (RFC-0004 "Stale socket → reaped on boot").
* Stale ticket lock files left by a crashed runtime are reaped on boot
  (RFC-0006 "Stale locks (from crashed runtime) reaped on boot").
* A corrupt ``registry.db`` is recreated and rescanned (RFC-0004 "Corrupt
  registry → recreate+rescan"; Invariant I-9).
* ``invoke_action`` works over the socket (Part X §4.2 / RFC-0010).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib.ticket_management.config import RuntimeConfig
from lib.ticket_management.repo import REPO_DIR_NAME, RUNTIME_DIR_NAME, repo_init
from lib.ticket_management.runtime.registry import Registry
from lib.ticket_management.runtime.scheduler import reap_stale_locks
from lib.ticket_management.runtime.server import RuntimeServer, runtime_socket_path


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def framework(tmp_path: Path) -> Path:
    """An initialized framework root with one ticket (T-1)."""
    root = repo_init(tmp_path / "framework")
    tdir = root / REPO_DIR_NAME / "T-1.ticket"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "metadata.json").write_text(
        json.dumps(
            {
                "id": "T-1",
                "title": "Test ticket",
                "type": "task",
                "kind": "ticket",
                "owner": {"name": "cto", "type": "user"},
            }
        )
    )
    (tdir / "state.json").write_text(
        json.dumps({"status": "created", "updated_at": "2026-01-01T00:00:00Z"})
    )
    return root


# --------------------------------------------------------------------------- #
# Stale socket reaping (RFC-0004)
# --------------------------------------------------------------------------- #
def test_stale_socket_reaped_on_restart(framework: Path) -> None:
    """A socket file with no live listener must be reaped at boot."""
    sock_path = runtime_socket_path(framework)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    # Simulate the artifact a kill -9 leaves behind: a socket file whose
    # listener process is gone.
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sock_path))
    stale.close()  # file remains, nobody listening

    assert sock_path.exists()

    server = RuntimeServer(framework, RuntimeConfig(worker_concurrency=1))
    try:
        sock = server.start()
        assert sock == sock_path
        assert sock_path.exists()
    finally:
        server.stop()


def test_live_socket_is_not_reaped(framework: Path) -> None:
    """A live runtime owns the socket: second boot raises (Invariant I-6)."""
    server = RuntimeServer(framework, RuntimeConfig(worker_concurrency=1))
    server.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            RuntimeServer(framework, RuntimeConfig(worker_concurrency=1)).start()
    finally:
        server.stop()


# --------------------------------------------------------------------------- #
# Stale lock reaping (RFC-0006)
# --------------------------------------------------------------------------- #
def test_stale_lock_files_reaped(framework: Path) -> None:
    """Lock files no process holds are removed at boot."""
    lock_dir = framework / REPO_DIR_NAME / RUNTIME_DIR_NAME / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    stale = lock_dir / "T-1.lock"
    stale.write_text("")  # killed runtime left this behind
    (lock_dir / "T-2.lock").write_text("")

    assert reap_stale_locks(lock_dir) == 2
    assert not stale.exists()
    assert not (lock_dir / "T-2.lock").exists()


def test_held_lock_is_not_reaped(framework: Path) -> None:
    """A lock held by a live process survives reaping."""
    import fcntl

    lock_dir = framework / REPO_DIR_NAME / RUNTIME_DIR_NAME / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    held = lock_dir / "T-1.lock"
    fd = os.open(str(held), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert reap_stale_locks(lock_dir) == 0
        assert held.exists()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_pipeline_loads_config_yaml(tmp_path: Path) -> None:
    """Pipeline boot honors `.ticket-runtime/config.yaml` (RFC-0004)."""
    from lib.ticket_management.runtime.pipeline import Pipeline

    repo = repo_init(tmp_path / "fw")
    runtime_dir = repo / "TicketsRepository" / ".ticket-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "config.yaml").write_text(
        "recursion_max_depth: 3\n"
        "debounce_window_seconds: 0.25\n"
        "worker_concurrency: 2\n"
    )

    p = Pipeline(root=repo)
    p.start()
    try:
        assert p.config.recursion_max_depth == 3
        assert p.config.debounce_window_seconds == 0.25
        assert p.config.worker_concurrency == 2
        assert p.bus._recursion_max_depth == 3  # noqa: SLF001
        assert p.scheduler._pool._max_workers == 2  # noqa: SLF001
    finally:
        p.stop()


def test_pipeline_defaults_when_no_config(tmp_path: Path) -> None:
    """Missing config.yaml yields canonical defaults (Invariant I-2)."""
    from lib.ticket_management.runtime.pipeline import Pipeline

    repo = repo_init(tmp_path / "fw")
    p = Pipeline(root=repo)
    p.start()
    try:
        assert p.config.recursion_max_depth == 10
        assert p.config.worker_concurrency == 4
        assert p.bus._recursion_max_depth == 10  # noqa: SLF001
    finally:
        p.stop()


def test_pipeline_reaps_locks_at_boot(framework: Path) -> None:
    """Pipeline.start() reaps stale locks automatically."""
    from lib.ticket_management.runtime.pipeline import Pipeline

    lock_dir = framework / REPO_DIR_NAME / RUNTIME_DIR_NAME / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    stale = lock_dir / "T-1.lock"
    stale.write_text("")

    pipeline = Pipeline(root=framework, config=RuntimeConfig(worker_concurrency=1))
    try:
        pipeline.start()
        assert not stale.exists()
    finally:
        pipeline.stop()


# --------------------------------------------------------------------------- #
# Corrupt registry repair (RFC-0004)
# --------------------------------------------------------------------------- #
def test_corrupt_registry_is_recreated(framework: Path) -> None:
    """Garbage in registry.db must not break boot; rescan rebuilds."""
    db_path = framework / REPO_DIR_NAME / RUNTIME_DIR_NAME / "registry.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"this is not a sqlite database \x00\x01\x02")

    registry = Registry(str(framework))
    try:
        registry.rebuild(str(framework))
        rows = registry.list_all()
        assert len(rows) == 1
        assert rows[0]["id"] == "T-1"
    finally:
        registry.close()


# --------------------------------------------------------------------------- #
# kill -9 crash recovery (Part XII Phase H done-criterion)
# --------------------------------------------------------------------------- #
def test_kill9_restart_recovers(framework: Path) -> None:
    """A real kill -9 of the runtime process must not block restart.

    Starts the runtime in a subprocess, SIGKILLs it, then boots a fresh
    server in-process and verifies discovery and transitions work.
    """
    sock_path = runtime_socket_path(framework)

    # 1. Boot a real runtime process.
    code = (
        "import sys;"
        "from lib.ticket_management.config import RuntimeConfig;"
        "from lib.ticket_management.runtime.server import RuntimeServer;"
        f"RuntimeServer({str(framework)!r}, RuntimeConfig(worker_concurrency=1)).start();"
        "sys.stdout.write('READY\\n'); sys.stdout.flush();"
        "import time; time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # 2. Wait for READY + socket.
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.stdout is not None and proc.stdout.readline().strip() == "READY":
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert sock_path.exists(), "runtime did not come up"

        # 3. SIGKILL the runtime (simulates a crash; no cleanup runs).
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        assert sock_path.exists(), "socket file should linger after kill -9"

        # 4. Restart: a fresh server must reap the stale socket and serve.
        server = RuntimeServer(framework, RuntimeConfig(worker_concurrency=1))
        try:
            server.start()
            from tessera import Runtime

            rt = Runtime.connect(repo=framework)
            tickets = rt.discover()
            assert [t["id"] for t in tickets] == ["T-1"]
            result = rt.transition("T-1", "initialized")
            assert result["to_status"] == "initialized"
            rt.close()
        finally:
            server.stop()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# --------------------------------------------------------------------------- #
# invoke_action over the socket (Part X §4.2 / RFC-0010)
# --------------------------------------------------------------------------- #
def test_invoke_action_over_socket(framework: Path) -> None:
    """A manifest-declared action runs over the socket and returns output."""
    tdir = framework / REPO_DIR_NAME / "T-1.ticket"
    (tdir / "MANIFEST.yaml").write_text(
        "apiVersion: ticket/v1\n"
        "kind: Ticket\n"
        "metadata:\n"
        "  id: T-1\n"
        "  title: Test ticket\n"
        "  type: task\n"
        "actions:\n"
        "  greet:\n"
        "    run: echo hello-from-action\n"
        "    shell: bash\n"
    )

    server = RuntimeServer(framework, RuntimeConfig(worker_concurrency=1))
    try:
        server.start()
        from tessera import Runtime

        rt = Runtime.connect(repo=framework)
        result = rt.invoke_action("T-1", "greet")
        assert result["exit_code"] == 0
        assert "hello-from-action" in result["stdout"]
        rt.close()
    finally:
        server.stop()


def test_invoke_action_unknown_action_over_socket(framework: Path) -> None:
    """An undeclared action raises a clear SDK error over the socket."""
    tdir = framework / REPO_DIR_NAME / "T-1.ticket"
    (tdir / "MANIFEST.yaml").write_text(
        "apiVersion: ticket/v1\n"
        "kind: Ticket\n"
        "metadata:\n"
        "  id: T-1\n"
        "  title: Test ticket\n"
        "  type: task\n"
        "actions:\n"
        "  greet:\n"
        "    run: echo hi\n"
        "    shell: bash\n"
    )

    server = RuntimeServer(framework, RuntimeConfig(worker_concurrency=1))
    try:
        server.start()
        from tessera import Runtime
        from tessera import SDKError

        rt = Runtime.connect(repo=framework)
        with pytest.raises(SDKError, match="not declared"):
            rt.invoke_action("T-1", "missing")
        rt.close()
    finally:
        server.stop()
