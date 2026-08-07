"""Tests for the SDK + CLI (G1)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tessera import Runtime, RuntimeNotRunning

runner = CliRunner()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Scaffold a fresh framework root with one ticket."""
    from lib.ticket_management.repo import repo_init

    root = repo_init(tmp_path / "framework")
    ticket_dir = root / "TicketsRepository" / "T-1.ticket"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "metadata.json").write_text(
        json.dumps(
            {
                "id": "T-1",
                "title": "Test ticket",
                "type": "task",
                "kind": "ticket",
                "owner": {"name": "test", "type": "user"},
            }
        ),
        encoding="utf-8",
    )
    (ticket_dir / "state.json").write_text(
        '{"status": "created", "updated_at": "2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    (ticket_dir / "MANIFEST.yaml").write_text(
        "apiVersion: ticket/v1\n"
        "kind: Ticket\n"
        "metadata:\n"
        "  id: T-1\n"
        "  title: Test ticket\n"
        "  type: task\n"
        "actions:\n"
        "  greet:\n"
        "    run: echo \"hello\"\n"
        "    shell: bash\n"
        "permissions:\n"
        "  capabilities:\n"
        "    - run:bash\n",
        encoding="utf-8",
    )
    return root


# --------------------------------------------------------------------------- #
# SDK direct mode
# --------------------------------------------------------------------------- #
def test_sdk_direct_discover_and_get(repo: Path) -> None:
    rt = Runtime.direct(repo=repo)
    tickets = rt.discover()
    assert any(t["id"] == "T-1" for t in tickets)
    ticket = rt.get_ticket("T-1")
    assert ticket.id == "T-1"
    assert ticket.status == "created"
    assert ticket.metadata.title == "Test ticket"


def test_sdk_direct_transition_validates(repo: Path) -> None:
    rt = Runtime.direct(repo=repo)
    result = rt.transition("T-1", "initialized")
    assert result["to_status"] == "initialized"
    # Illegal transition raises a typed error.
    with pytest.raises(Exception):
        rt.transition("T-1", "completed")  # initialized -> completed is illegal


def test_sdk_direct_invoke_action(repo: Path) -> None:
    rt = Runtime.direct(repo=repo)
    result = rt.invoke_action("T-1", "greet")
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


def test_sdk_connect_raises_when_not_running(repo: Path) -> None:
    with pytest.raises(RuntimeNotRunning):
        Runtime.connect(repo=repo)


def test_sdk_emit_publishes_on_bus(repo: Path) -> None:
    rt = Runtime.direct(repo=repo)
    seen: list[str] = []
    rt.bus.subscribe(lambda event: seen.append(event.name))
    rt.emit("metadata.updated", ticket_id="T-1", data={"k": "v"})
    assert seen == ["metadata.updated"]


# --------------------------------------------------------------------------- #
# Runtime server (attach mode)
# --------------------------------------------------------------------------- #
def test_runtime_server_attach_roundtrip(repo: Path) -> None:
    from lib.ticket_management.config import RuntimeConfig
    from lib.ticket_management.runtime.server import RuntimeServer

    server = RuntimeServer(repo, RuntimeConfig(worker_concurrency=1))
    sock = server.start()
    try:
        rt = Runtime.connect(sock=sock)
        tickets = rt.discover()
        assert any(t["id"] == "T-1" for t in tickets)
        status = rt._rpc("status")
        assert status["running"] is True
        rt.close()
    finally:
        server.stop()
    assert not sock.exists()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_create_inspect_transition(repo: Path) -> None:
    from lib.ticket_management.cli import app
    from lib.ticket_management.config import RuntimeConfig
    from lib.ticket_management.runtime.server import RuntimeServer

    # Create T-2
    result = runner.invoke(app, ["create", "T-2", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert (repo / "TicketsRepository" / "T-2.ticket").is_dir()

    # Inspect T-2
    result = runner.invoke(app, ["inspect", "T-2", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "state: created" in result.output

    # Transition is a mutating command: without a runtime it errors (Part X §8).
    result = runner.invoke(app, ["transition", "T-2", "initialized", "--repo", str(repo)])
    assert result.exit_code == 1
    assert "runtime not running" in result.output

    # With a live runtime, the transition works.
    server = RuntimeServer(repo, RuntimeConfig(worker_concurrency=1))
    try:
        server.start()
        result = runner.invoke(app, ["transition", "T-2", "initialized", "--repo", str(repo)])
        assert result.exit_code == 0, result.output
        assert "initialized" in result.output
        # Illegal transition fails gracefully through the runtime.
        result = runner.invoke(app, ["transition", "T-2", "completed", "--repo", str(repo)])
        assert result.exit_code == 1
        assert "error:" in result.output
    finally:
        server.stop()


def test_cli_validate_and_log(repo: Path) -> None:
    from lib.ticket_management.cli import app

    result = runner.invoke(app, ["validate", "T-1", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "valid" in result.output

    # Log with no activity file reports cleanly.
    result = runner.invoke(app, ["log", "T-1", "--repo", str(repo)])
    assert result.exit_code == 1
    assert "error:" in result.output


def test_cli_repo_scan(repo: Path) -> None:
    from lib.ticket_management.cli import app

    result = runner.invoke(app, ["repo", "scan", str(repo)])
    assert result.exit_code == 0, result.output
    assert "1 tickets" in result.output
