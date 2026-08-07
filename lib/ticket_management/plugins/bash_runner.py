"""Bash runner plugin.

Responsibility (Master Part III §4.1, RFC-0004; CONTRACTS.md §7): execute
`runtime: bash` hooks/actions via ``/bin/bash`` in a new POSIX process group,
with optional timeout (SIGTERM then SIGKILL after 3s). Returns
``(exit_code, stdout, stderr)``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any


def bash_runner(
    script_path: str,
    ticket_root: str,
    env: dict[str, Any],
    timeout: int | None = None,
) -> tuple[int, str, str]:
    """Run *script_path* with ``/bin/bash``."""
    if not os.path.isfile(script_path):
        raise FileNotFoundError(f"script not found: {script_path}")

    run_env = {**os.environ, **dict(env)}

    proc = subprocess.Popen(
        ["/bin/bash", script_path],
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
