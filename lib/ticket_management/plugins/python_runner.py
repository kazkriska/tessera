"""Python runner plugin.

Responsibility (Master Part III §4.1, RFC-0004; CONTRACTS.md §7): execute
`runtime: python` hooks/actions via the Python interpreter in a new POSIX
process group, passing the resolved environment, with optional timeout
(SIGTERM then SIGKILL after 3s). Returns ``(exit_code, stdout, stderr)``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any


def _run_with_timeout(
    cmd: list[str],
    ticket_root: str,
    env: dict[str, str],
    timeout: int | None,
) -> tuple[int, str, str]:
    """Run *cmd* in a new process group; enforce *timeout* if given.

    The subprocess environment is the caller's *env* merged over the current
    process environment, so PATH and other system variables are always
    available even when the caller passed a minimal dict.
    """
    run_env = {**os.environ, **dict(env)}

    proc = subprocess.Popen(
        cmd,
        cwd=ticket_root,
        env=run_env,
        preexec_fn=os.setsid,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    killed = threading.Event()

    def kill_group() -> None:
        killed.set()
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        # 3s grace, then SIGKILL (Part III R.A.3).
        time.sleep(3)
        try:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer: threading.Timer | None = None
    if timeout is not None and timeout > 0:
        timer = threading.Timer(timeout, kill_group)
        timer.daemon = True
        timer.start()

    try:
        stdout, stderr = proc.communicate()
        if killed.is_set():
            raise TimeoutError(
                f"script timed out after {timeout}s and was terminated"
            )
        return proc.returncode, stdout, stderr
    finally:
        if timer is not None:
            timer.cancel()


def python_runner(
    script_path: str,
    ticket_root: str,
    env: dict[str, Any],
    timeout: int | None = None,
) -> tuple[int, str, str]:
    """Run *script_path* with the Python interpreter.

    The interpreter is ``sys.executable`` when it is a real binary
    (``python3`` in normal operation), which guarantees the runtime's own
    interpreter runs hooks.
    """
    if not os.path.isfile(script_path):
        raise FileNotFoundError(f"script not found: {script_path}")
    python_bin = (
        sys.executable
        if os.path.basename(sys.executable).startswith("python")
        else "python3"
    )
    return _run_with_timeout(
        [python_bin, script_path], ticket_root, dict(env), timeout
    )
