#!/usr/bin/env python3
"""Tessera v1 — standalone end-to-end smoke test.

Purpose
-------
Prove that a *distributed* copy of the framework actually works: the runtime
boots, the filesystem watcher turns a `state.json` write into a domain event,
the manifest hook for that event executes, and a manifest action can be
invoked over the runtime socket. Both the hook and the action must see the
canonical `TESSERA_TICKET_ID` environment variable.

Design constraints
------------------
* Standalone: **no pytest, no dev dependencies** — stdlib only.
* Hermetic: everything happens inside a fresh `tempfile.mkdtemp()` repo that
  is removed in a `finally` block.
* Bounded: every subprocess call carries a timeout; the hook wait polls with
  a deadline.

Usage
-----
    uv run python e2e/smoke_test.py          # from the framework root
    python e2e/smoke_test.py                 # any env where `tessera` is installed

Exit code 0 == every check passed. Non-zero == at least one FAIL line above.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
TICKET_ID = "SMOKE-01"
HOOK_MARKER = "hook_fired.marker"
CMD_TIMEOUT = 60          # seconds per CLI invocation
RUNTIME_START_TIMEOUT = 90  # `runtime start` waits up to 15s internally
HOOK_WAIT_SECONDS = 45    # debounce (1s) + scheduler + subprocess headroom
POLL_INTERVAL = 0.25

MANIFEST_TEMPLATE = """\
apiVersion: ticket/v1
kind: Ticket
metadata:
  id: {ticket_id}
  title: Tessera end-to-end smoke ticket
  type: task
permissions:
  capabilities:
    - run:bash
hooks:
  on_state_updated:
    - run: 'echo "hook=on_state_updated ticket=$TESSERA_TICKET_ID" > {marker}'
      shell: bash
      timeout: 30
actions:
  greet:
    run: 'echo "greet=ok ticket=$TESSERA_TICKET_ID"'
    shell: bash
    timeout: 30
"""

_failures: list[str] = []


# --------------------------------------------------------------------------- #
# Reporting helpers
# --------------------------------------------------------------------------- #
def check(condition: bool, label: str, detail: str = "") -> bool:
    """Record and print one assertion outcome. Never raises."""
    if condition:
        print(f"PASS: {label}")
        return True
    suffix = f" — {detail}" if detail else ""
    print(f"FAIL: {label}{suffix}")
    _failures.append(label)
    return False


def step(label: str) -> None:
    print(f"\n--- {label} ---")


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


# --------------------------------------------------------------------------- #
# CLI plumbing
# --------------------------------------------------------------------------- #
def cli_prefix() -> list[str]:
    """Resolve how to call the Tessera CLI.

    Prefers the installed `tessera` console script; falls back to running the
    runtime CLI module with the current interpreter.
    """
    exe = shutil.which("tessera")
    if exe:
        return [exe]
    return [sys.executable, "-m", "tessera_runtime.cli"]


def run(argv: list[str], timeout: int = CMD_TIMEOUT) -> subprocess.CompletedProcess:
    """Run *argv*, capturing output. A timeout is reported as returncode 124."""
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            argv, 124, exc.stdout or "", f"timed out after {timeout}s"
        )


# --------------------------------------------------------------------------- #
# Test body
# --------------------------------------------------------------------------- #
def smoke(root: Path, cli: list[str]) -> None:
    repo_dir = root / "TicketsRepository"
    ticket_dir = repo_dir / f"{TICKET_ID}.ticket"
    marker_path = ticket_dir / HOOK_MARKER
    # Whether *this* test brought the daemon up (so only we stop it). If the
    # daemon was already live — e.g. `repo init` auto-starts it on dev/v1 — we
    # must NOT stop it on teardown, or we would tear down a runtime the
    # environment is relying on.
    runtime_was_live = False
    runtime_started_here = False

    try:
        # ---------------------------------------------------------------- #
        step("1/6 repo init")
        proc = run(cli + ["repo", "init", str(root)])
        check(proc.returncode == 0, "tessera repo init exits 0", _tail(proc.stderr))
        check(
            (repo_dir / ".ticket-runtime").is_dir(),
            "TicketsRepository/.ticket-runtime scaffolded",
        )

        # ---------------------------------------------------------------- #
        step("2/6 create ticket")
        proc = run(cli + ["create", TICKET_ID, "--type", "task", "--repo", str(root)])
        check(proc.returncode == 0, f"tessera create {TICKET_ID} exits 0", _tail(proc.stderr))
        check(ticket_dir.is_dir(), f"{TICKET_ID}.ticket directory exists")

        # ---------------------------------------------------------------- #
        step("3/6 write MANIFEST.yaml (hook + action + permissions)")
        manifest_path = ticket_dir / "MANIFEST.yaml"
        manifest_path.write_text(
            MANIFEST_TEMPLATE.format(ticket_id=TICKET_ID, marker=HOOK_MARKER),
            encoding="utf-8",
        )
        proc = run(cli + ["validate", TICKET_ID, "--repo", str(root)])
        check(
            proc.returncode == 0,
            "MANIFEST.yaml validates",
            _tail(proc.stdout + proc.stderr),
        )

        # ---------------------------------------------------------------- #
        # The ticket must exist before boot: inotify watches are registered
        # per-directory at watcher start-up.
        step("4/6 ensure runtime is up")
        # `repo init` already starts the daemon on dev/v1, so tolerate an
        # already-running runtime instead of failing the smoke test.
        status = run(cli + ["runtime", "status", "--repo", str(root)])
        runtime_was_live = "running: yes" in status.stdout
        if runtime_was_live:
            runtime_started_here = False
            print("runtime already running (left as-is)")
        else:
            proc = run(cli + ["runtime", "start", "--repo", str(root)], RUNTIME_START_TIMEOUT)
            runtime_started_here = proc.returncode == 0
            check(
                runtime_started_here,
                "tessera runtime start exits 0",
                _tail(proc.stdout + proc.stderr),
            )
            if not runtime_started_here:
                return  # nothing downstream can succeed

        # ---------------------------------------------------------------- #
        step("5/6 hook fires on state.updated")
        state_path = ticket_dir / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "status": "created",
                    "updated_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            ),
            encoding="utf-8",
        )

        deadline = time.time() + HOOK_WAIT_SECONDS
        marker_text = ""
        while time.time() < deadline:
            if marker_path.is_file():
                marker_text = marker_path.read_text(encoding="utf-8").strip()
                if marker_text:
                    break
            time.sleep(POLL_INTERVAL)

        check(
            bool(marker_text),
            f"on_state_updated hook wrote {HOOK_MARKER}",
            f"no marker after {HOOK_WAIT_SECONDS}s",
        )
        check(
            f"ticket={TICKET_ID}" in marker_text,
            "TESSERA_TICKET_ID visible inside the hook",
            f"marker={marker_text!r}",
        )

        # ---------------------------------------------------------------- #
        step("6/6 invoke action")
        proc = run(cli + ["action", TICKET_ID, "greet", "--repo", str(root)])
        combined = f"{proc.stdout}\n{proc.stderr}"
        check(proc.returncode == 0, "tessera action exits 0", _tail(combined))
        check(
            "exit=0" in proc.stdout,
            "action subprocess reported exit=0",
            _tail(combined),
        )
        check(
            f"greet=ok ticket={TICKET_ID}" in proc.stdout,
            "TESSERA_TICKET_ID visible inside the action",
            _tail(combined),
        )

    finally:
        if runtime_started_here:
            step("teardown: stop runtime (started by this test)")
            stop = run(cli + ["runtime", "stop", "--repo", str(root)], 30)
            print(f"runtime stop: exit={stop.returncode} {_tail(stop.stdout + stop.stderr, 120)}")
            time.sleep(0.5)
        elif runtime_was_live:
            print("teardown: runtime was already live before this test — left running")


def main() -> int:
    cli = cli_prefix()
    print("Tessera v1 — end-to-end smoke test")
    print(f"CLI: {' '.join(cli)}")

    probe = run(cli + ["--help"], 30)
    if probe.returncode != 0:
        print(f"FAIL: Tessera CLI is not runnable — {_tail(probe.stderr)}")
        print("\nSMOKE TEST FAILED (1 check failed)")
        return 2

    root = Path(tempfile.mkdtemp(prefix="tessera-smoke-"))
    print(f"temp repo: {root}")
    try:
        smoke(root, cli)
    except Exception as exc:  # noqa: BLE001 — any crash is a failed smoke test
        print(f"FAIL: unexpected exception — {type(exc).__name__}: {exc}")
        _failures.append("unexpected exception")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        print(f"\ncleaned up temp repo (exists={root.exists()})")

    if _failures:
        print(f"\nSMOKE TEST FAILED ({len(_failures)} check(s) failed): {_failures}")
        return 1
    print("\nSMOKE TEST PASSED — runtime boots, hook fires, action runs, "
          "TESSERA_TICKET_ID propagated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
