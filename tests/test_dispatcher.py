"""Tests for the Dispatcher component."""

from pathlib import Path

import pytest

from tessera_runtime.config import RuntimeConfig
from tessera_runtime.runtime.dispatcher import (
    DispatchResult,
    PathJailError,
    RunnerDescriptor,
    UnknownRunnerError,
    dispatch,
    resolve_runner,
)


def test_resolve_runner_python(tmp_path: Path) -> None:
    ticket_root = tmp_path / "ticket"
    ticket_root.mkdir()
    script = ticket_root / "run.py"
    script.write_text("print('hello')")

    descriptor = RunnerDescriptor(path="run.py", shell="python", timeout=10, retry=2)
    result = resolve_runner(descriptor, ticket_root, RuntimeConfig())

    assert result.runner_name == "python"
    assert result.resolved_path == str(script)
    assert result.env_overrides["ticket_root"] == str(ticket_root)
    assert result.env_overrides["script_path"] == str(script)
    assert result.env_overrides["timeout"] == 10
    assert result.env_overrides["retry"] == 2
    assert result.env_overrides["async"] is False
    assert result.env_overrides["shell"] == "python"
    assert result.error is None


def test_resolve_runner_unknown_shell_raises(tmp_path: Path) -> None:
    descriptor = RunnerDescriptor(path="run.py", shell="perl")
    with pytest.raises(UnknownRunnerError):
        resolve_runner(descriptor, tmp_path, RuntimeConfig())


def test_resolve_runner_path_traversal_rejected(tmp_path: Path) -> None:
    ticket_root = tmp_path / "ticket"
    ticket_root.mkdir()
    descriptor = RunnerDescriptor(path="../../etc/passwd", shell="python")
    with pytest.raises(PathJailError):
        resolve_runner(descriptor, ticket_root, RuntimeConfig())


def test_resolve_runner_relative_path_inside_ticket(tmp_path: Path) -> None:
    ticket_root = tmp_path / "ticket"
    ticket_root.mkdir()
    subdir = ticket_root / "hooks"
    subdir.mkdir()
    script = subdir / "hook.py"
    script.write_text("print('hook')")

    descriptor = RunnerDescriptor(path="hooks/hook.py", shell="bash")
    result = resolve_runner(descriptor, ticket_root, RuntimeConfig())

    assert result.resolved_path == str(script)
    assert result.runner_name == "bash"


def test_dispatch_wrapper_returns_result(tmp_path: Path) -> None:
    ticket_root = tmp_path / "ticket"
    ticket_root.mkdir()
    script = ticket_root / "action.js"
    script.write_text("console.log('node')")

    descriptor = RunnerDescriptor(path="action.js", shell="node")
    result = dispatch("payload", descriptor, ticket_root, RuntimeConfig())

    assert isinstance(result, DispatchResult)
    assert result.runner_name == "node"
    assert result.resolved_path == str(script)
