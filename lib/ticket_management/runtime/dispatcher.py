"""Dispatcher — runner selection for Tessera v1.

Responsibility: resolve a ``RunnerDescriptor`` to a ``DispatchResult``.
It does NOT execute the runner (Executor does that).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from lib.ticket_management.config import RuntimeConfig
from lib.ticket_management.plugins.bash_runner import bash_runner
from lib.ticket_management.plugins.node_runner import node_runner
from lib.ticket_management.plugins.python_runner import python_runner


class UnknownRunnerError(Exception):
    """Raised when the descriptor shell is not registered in KNOWN_RUNNERS."""


class PathJailError(Exception):
    """Raised when the resolved path would escape the ticket root."""


@dataclass(init=False)
class RunnerDescriptor:
    """Input descriptor for a hook/action runner."""

    path: str
    shell: str
    timeout: int | None = None
    retry: int = 0
    async_: bool = False

    def __init__(
        self,
        path: str,
        shell: str,
        timeout: int | None = None,
        retry: int = 0,
        **kwargs: Any,
    ) -> None:
        self.path = path
        self.shell = shell
        self.timeout = timeout
        self.retry = retry
        self.async_ = kwargs.pop("async", False)
        if kwargs:
            raise TypeError(
                "RunnerDescriptor.__init__() got unexpected keyword arguments: "
                f"{list(kwargs)}"
            )

    def __getattr__(self, name: str) -> Any:
        if name == "async":
            return self.async_
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "async":
            name = "async_"
        super().__setattr__(name, value)


@dataclass
class DispatchResult:
    """Resolved dispatch output passed to the Executor."""

    runner_name: str
    resolved_path: str
    env_overrides: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


KNOWN_RUNNERS: dict[str, Callable] = {
    "python": python_runner,
    "bash": bash_runner,
    "node": node_runner,
}


def resolve_runner(
    descriptor: RunnerDescriptor,
    ticket_root: Path,
    config: RuntimeConfig,
) -> DispatchResult:
    """Resolve a runner descriptor to a dispatch result.

    - Lower-cases the shell name and looks it up in ``KNOWN_RUNNERS``.
    - Resolves the script path relative to *ticket_root* (not CWD).
    - Rejects paths that escape *ticket_root*.
    - Builds the minimal ``env_overrides`` dict for the Executor.
    """
    shell = descriptor.shell.lower()
    if shell not in KNOWN_RUNNERS:
        raise UnknownRunnerError(f"unknown shell: {descriptor.shell!r}")

    resolved = (ticket_root / descriptor.path).resolve()
    try:
        resolved.relative_to(ticket_root.resolve())
    except ValueError:
        raise PathJailError(f"path escapes ticket root: {descriptor.path!r}")

    env_overrides = {
        "ticket_root": str(ticket_root),
        "script_path": str(resolved),
        "timeout": descriptor.timeout,
        "retry": descriptor.retry,
        "async": getattr(descriptor, "async"),
        "shell": shell,
    }

    return DispatchResult(
        runner_name=shell,
        resolved_path=str(resolved),
        env_overrides=env_overrides,
    )


def dispatch(
    event_payload: Any,
    descriptor: RunnerDescriptor,
    ticket_root: Path,
    config: RuntimeConfig,
) -> DispatchResult:
    """Thin wrapper so the Scheduler calls a single symbol."""
    return resolve_runner(descriptor, ticket_root, config)
