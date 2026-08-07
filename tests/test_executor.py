"""Tests for the Executor + runner plugins (D2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.ticket_management.config import RuntimeConfig
from lib.ticket_management.plugins.bash_runner import bash_runner
from lib.ticket_management.plugins.node_runner import node_runner
from lib.ticket_management.plugins.python_runner import python_runner
from lib.ticket_management.runtime.dispatcher import PathJailError, RunnerDescriptor
from lib.ticket_management.runtime.executor import (
    ENV_DENYLIST,
    build_exec_env,
    run_hook,
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_python_runner_executes_script_and_captures_output(tmp_path: Path):
    script = _write(tmp_path, "hello.py", "print('hello from python')\n")
    code, out, err = python_runner(str(script), str(tmp_path), {})
    assert code == 0
    assert "hello from python" in out
    assert err == ""


def test_bash_runner_executes_script(tmp_path: Path):
    script = _write(tmp_path, "hello.sh", "#!/bin/bash\necho 'hello from bash'\n")
    code, out, err = bash_runner(str(script), str(tmp_path), {})
    assert code == 0
    assert "hello from bash" in out


def test_node_runner_executes_script(tmp_path: Path):
    script = _write(tmp_path, "hello.js", "console.log('hello from node')\n")
    code, out, err = node_runner(str(script), str(tmp_path), {})
    assert code == 0
    assert "hello from node" in out


def test_executor_timeout_kills_process_group(tmp_path: Path):
    script = _write(
        tmp_path,
        "sleep.sh",
        "#!/bin/bash\nsleep 30\necho 'never reached'\n",
    )
    config = RuntimeConfig(default_timeout=2)
    descriptor = RunnerDescriptor(path="sleep.sh", shell="bash", timeout=1)
    result = run_hook(descriptor, tmp_path, config)
    assert result.exit_code != 0
    assert result.timed_out is True


def test_executor_path_jail_blocks_traversal(tmp_path: Path):
    outside = tmp_path.parent / "outside.sh"
    outside.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    config = RuntimeConfig()
    descriptor = RunnerDescriptor(path="../outside.sh", shell="bash", timeout=5)
    with pytest.raises(PathJailError):
        run_hook(descriptor, tmp_path, config)


def test_executor_env_masks_denylisted_keys():
    base = {
        "AWS_SECRET_ACCESS_KEY": "leak",
        "DATABASE_URL": "postgres://x",
        "SSH_AUTH_SOCK": "/run/ssh",
        "SUDO_USER": "root",
        "KEEP_ME": "yes",
    }
    env = build_exec_env(base_env=base, ticket_env=None, manifest_env=None, event_env=None, permissions=None)
    for key in ENV_DENYLIST:
        assert key not in env
    assert env["KEEP_ME"] == "yes"


def test_executor_env_merge_order_and_secrets():
    env = build_exec_env(
        base_env={"A": "base", "B": "base"},
        ticket_env={"B": "ticket"},
        manifest_env={"C": "manifest"},
        event_env={"B": "event"},
        permissions={"secrets": {"TOKEN": "s3cr3t"}, "secrets_enabled": True},
    )
    assert env["A"] == "base"
    assert env["B"] == "event"  # event layer wins
    assert env["C"] == "manifest"
    assert env["TOKEN"] == "s3cr3t"


def test_resolve_ticket_env_global_and_inherit(tmp_path: Path):
    """Global Tickets/.env -> Ticket .env, with env.inherit:false opt-out."""
    from lib.ticket_management.runtime.env import resolve_ticket_env

    repo = tmp_path / "framework"
    tickets = repo / "TicketsRepository"
    tickets.mkdir(parents=True)
    (tickets / ".env").write_text("GLOBAL_KEY=from-global\nSHARED=global-value\n")
    tdir = tickets / "T-1.ticket"
    tdir.mkdir()
    (tdir / ".env").write_text("TICKET_KEY=from-ticket\nSHARED=ticket-value\n")

    env = resolve_ticket_env(tdir, base_env={"BASE": "x"})
    assert env["GLOBAL_KEY"] == "from-global"
    assert env["TICKET_KEY"] == "from-ticket"
    # Ticket overrides global on collision.
    assert env["SHARED"] == "ticket-value"
    assert env["BASE"] == "x"

    # env.inherit: false suppresses the global layer (RFC-0003).
    (tdir / "MANIFEST.yaml").write_text(
        "apiVersion: ticket/v1\n"
        "kind: Ticket\n"
        "metadata:\n"
        "  id: T-1\n"
        "  title: Test ticket\n"
        "  type: task\n"
        "env:\n"
        "  inherit: false\n"
    )
    env2 = resolve_ticket_env(tdir, base_env={"BASE": "x"})
    assert "GLOBAL_KEY" not in env2
    assert env2["TICKET_KEY"] == "from-ticket"
    assert env2["SHARED"] == "ticket-value"
