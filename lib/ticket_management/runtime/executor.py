"""Executor — subprocess isolation and runner invocation.

Responsibility (Master Part III R.A.3, Part IX §4.1; CONTRACTS.md §7): run
each hook/action in a new POSIX process group (``os.setsid``) with the CWD
pinned to the Ticket root, enforce timeouts (SIGTERM then SIGKILL after 3s),
apply the path jail, mask DENYLISTED environment keys, merge environment
layers in canonical order, and dispatch to the language runner selected by
the Dispatcher.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.ticket_management.config import RuntimeConfig
from lib.ticket_management.runtime.dispatcher import (
    PathJailError,
    RunnerDescriptor,
    resolve_runner,
)
from lib.ticket_management.plugins.bash_runner import bash_runner
from lib.ticket_management.plugins.node_runner import node_runner
from lib.ticket_management.plugins.python_runner import python_runner

__all__ = [
    "ExecutionResult",
    "ENV_DENYLIST",
    "PathJailError",
    "build_exec_env",
    "run_hook",
]

#: Environment keys never passed to hooks/actions (Part IX R.A.9).
ENV_DENYLIST = frozenset(
    {
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL",
        "SSH_AUTH_SOCK",
        "SUDO_USER",
    }
)

#: Runner registry shared with the Dispatcher.
RUNNERS = {
    "python": python_runner,
    "bash": bash_runner,
    "node": node_runner,
}


@dataclass
class ExecutionResult:
    """Result of executing a hook/action."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def build_exec_env(
    base_env: dict[str, str] | None,
    ticket_env: dict[str, str] | None,
    manifest_env: dict[str, str] | None,
    event_env: dict[str, str] | None,
    permissions: dict[str, Any] | None,
) -> dict[str, str]:
    """Build the final execution environment in canonical merge order.

    Order (later overrides earlier, CONTRACTS.md §7 / Part IX R.A.9):

        System base -> Ticket .env -> Manifest env -> Event payload env

    DENYLISTED keys are stripped from the base layer before merging.
    Secret keys are injected only when ``permissions.get('secrets')`` is
    truthy (Part IX).
    """
    base = dict(os.environ if base_env is None else base_env)
    # Strip denylisted keys from the base layer.
    for key in ENV_DENYLIST:
        base.pop(key, None)

    env: dict[str, str] = dict(base)
    for layer in (ticket_env, manifest_env, event_env):
        if layer:
            env.update({str(k): str(v) for k, v in layer.items()})

    permissions = permissions or {}
    secrets = permissions.get("secrets") or {}
    if permissions.get("secrets_enabled") is True and isinstance(secrets, dict):
        env.update({str(k): str(v) for k, v in secrets.items()})

    return env


def run_hook(
    descriptor: RunnerDescriptor,
    ticket_root: Path | str,
    config: RuntimeConfig,
    env: dict[str, str] | None = None,
    permissions: dict[str, Any] | None = None,
) -> ExecutionResult:
    """Execute *descriptor* under the ticket root with full isolation.

    Raises :class:`UnknownRunnerError` for an unknown shell and
    :class:`PathJailError` for paths escaping *ticket_root* (both propagate
    from :func:`dispatcher.resolve_runner`).
    """
    root = Path(ticket_root).resolve()
    dispatch = resolve_runner(descriptor, root, config)

    runner = RUNNERS.get(dispatch.runner_name)
    if runner is None:
        from lib.ticket_management.runtime.dispatcher import UnknownRunnerError

        raise UnknownRunnerError(f"unknown shell: {descriptor.shell!r}")

    timeout = descriptor.timeout if descriptor.timeout is not None else config.default_timeout

    # Path-jail: the resolved script must stay inside the ticket root.
    resolved = Path(dispatch.resolved_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise PathJailError(f"path escapes ticket root: {descriptor.path!r}")

    # Environment: base is already masked by build_exec_env; descriptor
    # env_overrides (ticket_root, script_path, etc.) are appended last.
    exec_env = build_exec_env(
        base_env=env,
        ticket_env=dispatch.env_overrides,
        manifest_env=None,
        event_env=None,
        permissions=permissions,
    )
    # Expose the owning ticket id to hooks/actions (CONTRACTS §7 / docs):
    # TESSERA_TICKET_ID is the canonical handle for scripts to know which
    # ticket they run for. Derive from the ticket root dir name.
    ticket_id = root.name.removesuffix(".ticket")
    if ticket_id and "TESSERA_TICKET_ID" not in exec_env:
        exec_env["TESSERA_TICKET_ID"] = ticket_id

    try:
        exit_code, stdout, stderr = runner(
            str(resolved) if resolved.is_file() else descriptor.path,
            str(root),
            exec_env,
            timeout,
        )
    except TimeoutError:
        return ExecutionResult(exit_code=-1, stdout="", stderr="timed out", timed_out=True)

    return ExecutionResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
