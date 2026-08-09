"""systemd user-unit management for the Tessera runtime (dev/v1).

On ``tessera repo init`` we now generate a **non-template** unit
``tessera-runtime.service`` whose ``WorkingDirectory=`` and
``TESSERA_REPO=`` are hardcoded to the absolute canonical repo path. This
replaces the old per-repo ``tessera-runtime@.service`` template (RFC-0013
§6.2), which is removed from the installer in favour of this command-owned
unit.

When a user session bus / systemd is unavailable (containers, CI, macOS),
:func:`start_runtime` degrades gracefully to a detached direct daemon via
:mod:`tessera_runtime.daemon`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tessera_runtime.config import load_config
from tessera_runtime.daemon import launch_runtime_daemon, runtime_is_live
from tessera_runtime.repo import REPO_DIR_NAME, RUNTIME_DIR_NAME, repo_init

UNIT_NAME = "tessera-runtime.service"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"


def unit_text(repo_root: str | Path) -> str:
    """Return the non-template systemd unit file contents for *repo_root*.

    *repo_root* is baked in as a resolved absolute path — systemd does NOT
    expand ``~``, so we must write the literal path. The ``tessera`` binary
    path and ``--repo`` argument are double-quoted because they may contain
    spaces (e.g. a workspace under ``/home/user/My Stuff``); an unquoted
    ``ExecStart`` token list would misinterpret the space as an argv split
    and fail with status 203/EXEC.
    """
    root = Path(repo_root).resolve()
    working_dir = root / REPO_DIR_NAME
    tessera_bin = shutil.which("tessera") or "tessera"
    return "\n".join(
        [
            "[Unit]",
            "Description=Tessera runtime daemon",
            "Documentation=https://github.com/kazkriska/tessera",
            "After=network.target",
            "",
            "[Service]",
            "Type=oneshot",
            "RemainAfterExit=yes",
            f"WorkingDirectory={working_dir}",
            "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin",
            f"Environment=TESSERA_REPO={root}",
            f'ExecStart="{tessera_bin}" runtime start --repo {root}',
            f'ExecStop="{tessera_bin}" runtime stop --repo {root}',
            "TimeoutStartSec=60",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def write_unit(repo_root: str | Path) -> Path:
    """Write the unit file to the user systemd dir. Returns its path."""
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    unit_path = SYSTEMD_USER_DIR / UNIT_NAME
    unit_path.write_text(unit_text(repo_root), encoding="utf-8")
    unit_path.chmod(0o644)
    return unit_path


def _systemd_available() -> bool:
    """Best-effort check that systemctl --user can talk to a session bus."""
    if shutil.which("systemctl") is None:
        return False
    probe = subprocess.run(
        ["systemctl", "--user", "is-active", "default.target"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # is-active returns 3 when the unit is inactive but the bus works.
    return probe.returncode in (0, 3)


def start_runtime(repo_root: str | Path) -> str:
    """Start the runtime for *repo_root* under systemd if possible, else direct.

    Returns a human-readable status line describing what happened. When systemd
    is used, the unit is verified to be *active* before reporting success —
    a unit that fails to start (e.g. 203/EXEC) triggers the direct-daemon
    fallback rather than a false "started" message.
    """
    root = Path(repo_root).resolve()
    if runtime_is_live(root):
        return f"runtime already running (sock at {root / RUNTIME_DIR_NAME / 'runtime.sock'})"

    if _systemd_available():
        unit_path = write_unit(root)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        en = subprocess.run(
            ["systemctl", "--user", "enable", "--now", UNIT_NAME],
            capture_output=True,
            text=True,
        )
        # Verify the unit actually came up — enable --now returns 0 even when
        # the service later fails, so we must check is-active explicitly.
        active = subprocess.run(
            ["systemctl", "--user", "is-active", UNIT_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
        if en.returncode == 0 and active:
            return f"enabled + started systemd unit {UNIT_NAME} (from {unit_path})"
        # systemd failed (or unit not active) — fall back to a direct daemon.
        reason = (en.stderr.strip() or f"exit code {en.returncode}") if en.returncode != 0 else "unit not active"
        try:
            pid = launch_runtime_daemon(root)
        except RuntimeError as exc:
            return f"systemd failed ({reason}) and direct start failed: {exc}"
        return (
            f"systemd start failed ({reason}); "
            f"started runtime directly (detached pid {pid})"
        )

    # Direct detached daemon.
    pid = launch_runtime_daemon(root)
    return f"started runtime directly (detached pid {pid}); systemd user session not available"
