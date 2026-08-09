"""Detached runtime-daemon launcher for Tessera v1.

Extracted from ``cli.runtime_start`` so both the ``tessera runtime start``
command and the post-init hook in ``tessera repo init`` can reuse the exact
same spawn logic. The daemon is a fresh interpreter process that owns the
:class:`~tessera_runtime.runtime.server.RuntimeServer`, blocks in ``wait()``
until a ``runtime stop`` shutdown RPC arrives, and detaches from the spawning
session (``start_new_session=True``) so it survives the CLI exiting.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from tessera_runtime.repo import RUNTIME_DIR_NAME, repo_init
from tessera_runtime.runtime.server import _socket_is_live, runtime_socket_path

#: Seconds to wait for the socket to come live before declaring failure.
START_TIMEOUT_SEC = 15


def build_daemon_code(root: Path) -> str:
    """Return the Python source run by the detached daemon process.

    ``root`` is baked in as a repr literal so the child needs no arg parsing
    and no ambient cwd (it chdir's to the package root anyway).
    """
    return (
        "import sys, signal\n"
        "from tessera_runtime.runtime.server import RuntimeServer\n"
        f"root = {str(root)!r}\n"
        # No config passed: the Pipeline loads `.ticket-runtime/config.yaml`
        # itself at boot (RFC-0004 boot step 1), so user-set values apply.
        "srv = RuntimeServer(root)\n"
        "def _term(*_):\n"
        "    srv.stop()\n"
        "signal.signal(signal.SIGTERM, _term)\n"
        "signal.signal(signal.SIGINT, _term)\n"
        "srv.start()\n"
        "sys.stdout.write('READY\\n'); sys.stdout.flush()\n"
        "srv.wait()\n"
    )


def launch_runtime_daemon(root: Path) -> int:
    """Spawn the detached runtime daemon for *root*.

    Ensures the directory tree exists, then forks the daemon. Returns the
    daemon PID. Raises :class:`RuntimeError` if the socket does not come live
    within :data:`START_TIMEOUT_SEC` or the child exits early.
    """
    root_path = Path(root).resolve()
    sock_path = runtime_socket_path(root_path)
    if sock_path.exists() and _socket_is_live(sock_path):
        raise RuntimeError(f"runtime already running at {sock_path}")

    repo_init(root_path)
    log_dir = root_path / "TicketsRepository" / RUNTIME_DIR_NAME / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "runtime.log"

    pkg_root = Path(__file__).resolve().parent.parent.parent
    daemon_code = build_daemon_code(root_path)
    proc = subprocess.Popen(
        [sys.executable, "-c", daemon_code],
        cwd=str(pkg_root),
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.time() + START_TIMEOUT_SEC
    while time.time() < deadline:
        if _socket_is_live(sock_path):
            return proc.pid
        if proc.poll() is not None:
            raise RuntimeError(
                f"runtime daemon exited with code {proc.returncode}; "
                f"see {log_file}"
            )
        time.sleep(0.1)
    raise RuntimeError(f"runtime did not start within {START_TIMEOUT_SEC}s")


def runtime_is_live(root: Path) -> bool:
    """Return True if a live runtime socket exists for *root*."""
    sock = runtime_socket_path(Path(root).resolve())
    return sock.exists() and _socket_is_live(sock)
