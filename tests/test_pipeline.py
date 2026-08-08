"""Tests for the pipeline wiring (D4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera_runtime.config import RuntimeConfig
from tessera_runtime.repo import repo_init
from tessera_runtime.runtime.bus import Event
from tessera_runtime.runtime.dispatcher import RunnerDescriptor
from tessera_runtime.runtime.pipeline import (
    EVENT_HOOK_MAP,
    Pipeline,
    executor_dispatch,
)

MANIFEST_OK = """\
apiVersion: ticket/v1
kind: Ticket
metadata:
  id: T-1
  title: Test ticket
  type: task
hooks:
  on_metadata_updated:
    - run: echo "hi"
      shell: bash
permissions:
  capabilities:
    - run:bash
"""


def _make_ticket(root: Path, ticket_id: str = "T-1", manifest: str = MANIFEST_OK) -> Path:
    # Real layout (CONTRACTS §7.1): tickets live under TicketsRepository/.
    ticket_dir = root / "TicketsRepository" / f"{ticket_id}.ticket"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (ticket_dir / "MANIFEST.yaml").write_text(manifest, encoding="utf-8")
    return ticket_dir


def test_executor_dispatch_builds_env_and_runs() -> None:
    with pytest.MonkeyPatch.context() as mp:
        import tessera_runtime.runtime.pipeline as pipeline_mod

        captured: dict[str, object] = {}

        def fake_run_hook(descriptor, ticket_root, config, env, permissions):
            captured["env"] = env
            captured["permissions"] = permissions
            return {"exit_code": 0, "stdout": "ok", "stderr": ""}

        mp.setattr(pipeline_mod, "run_hook", fake_run_hook)

        root = _make_ticket(Path("/tmp/d4-test-env"))
        (root / ".env").write_text("TICKET_VAR=ticket-value\n", encoding="utf-8")
        descriptor = RunnerDescriptor(path="echo", shell="bash")
        result = executor_dispatch(
            "T-1",
            descriptor,
            root,
            RuntimeConfig(),
            {"EVENT_VAR": "event-value"},
        )
        assert result["exit_code"] == 0
        # Env layers merged: system base + ticket .env + event payload.
        assert captured["env"]["TICKET_VAR"] == "ticket-value"
        assert captured["env"]["EVENT_VAR"] == "event-value"
        assert "PATH" in captured["env"]
        # Permissions from manifest.
        assert "run:bash" in captured["permissions"]["capabilities"]


def test_pipeline_event_to_hook_mapping() -> None:
    assert EVENT_HOOK_MAP["metadata.updated"] == "on_metadata_updated"
    assert EVENT_HOOK_MAP["manifest.updated"] == "on_manifest_updated"
    assert EVENT_HOOK_MAP["state.updated"] == "on_state_updated"
    assert EVENT_HOOK_MAP["activity.updated"] == "on_activity_updated"
    # fs.changed has no default hook.
    assert "fs.changed" not in EVENT_HOOK_MAP


def test_pipeline_resolves_hook_descriptor_from_manifest() -> None:
    root = Path("/tmp/d4-test-hook")
    ticket_dir = _make_ticket(root)
    pipeline = Pipeline(root=root)
    pipeline.repo = root
    desc = pipeline._resolve_hook_descriptor("T-1", "on_metadata_updated")
    assert desc is not None
    assert desc.shell == "bash"


def test_pipeline_skips_ticket_without_manifest() -> None:
    root = Path("/tmp/d4-test-nomanifest")
    ticket_dir = _make_ticket(root, manifest=None)
    pipeline = Pipeline(root=root)
    pipeline.repo = root
    assert pipeline._resolve_hook_descriptor("T-1", "on_metadata_updated") is None
    # Missing hook name also skips.
    assert pipeline._resolve_hook_descriptor("T-1", "on_state_updated") is None


def test_pipeline_start_and_stop_closes_resources() -> None:
    root = Path("/tmp/d4-test-pipeline")
    _make_ticket(root)
    pipeline = Pipeline(root=root, config=RuntimeConfig(worker_concurrency=1))
    pipeline.start()
    assert pipeline.repo is not None
    assert pipeline.registry is not None
    assert pipeline.bus is not None
    assert pipeline.scheduler is not None
    # Bus events with a declared hook reach the scheduler queue.
    assert pipeline._on_event is not None
    pipeline.stop()


def test_pipeline_ticket_root_resolves_under_tickets_repository() -> None:
    """CMP-E2E-1: `_ticket_root` must resolve the real layout.

    Tickets live at `<root>/TicketsRepository/<id>.ticket` (CONTRACTS §7.1).
    A previous bug looked at `<root>/<id>.ticket`, so the pipeline silently
    skipped every hook enqueue in the live daemon.
    """
    root = Path("/tmp/d4-test-real-layout")
    ticket_dir = _make_ticket(root)
    pipeline = Pipeline(root=root)
    pipeline.repo = repo_init(root)
    assert pipeline._ticket_root("T-1") == ticket_dir
    assert pipeline._ticket_root("nope") is None


def test_pipeline_event_to_hook_enqueues_with_real_layout(tmp_path: Path) -> None:
    """CMP-E2E-1: a bus event for a real-layout ticket RUNS its hook.

    The regression test for the live-daemon hook failure: publish
    `metadata.updated` for a ticket under TicketsRepository/ and assert the
    declared hook actually executes (durable side effect), proving the
    pipeline no longer silently skips hook enqueue due to a wrong ticket-root
    lookup.
    """
    ticket_dir = _make_ticket(tmp_path, "T-E2E")
    (ticket_dir / "metadata.json").write_text('{"id": "T-E2E"}', encoding="utf-8")
    (ticket_dir / "MANIFEST.yaml").write_text(
        "apiVersion: ticket/v1\n"
        "kind: Ticket\n"
        "metadata:\n"
        "  id: T-E2E\n"
        "  title: E2E ticket\n"
        "  type: task\n"
        "hooks:\n"
        "  on_metadata_updated:\n"
        "    - run: echo hook-ran >> hook_marker.txt\n"
        "      shell: bash\n"
        "permissions:\n"
        "  capabilities:\n"
        "    - run:bash\n",
        encoding="utf-8",
    )
    pipeline = Pipeline(root=tmp_path, config=RuntimeConfig(worker_concurrency=1))
    pipeline.start()
    try:
        marker = ticket_dir / "hook_marker.txt"
        pipeline.bus.publish(
            Event(name="metadata.updated", ticket_id="T-E2E", data={})
        )
        import time

        deadline = time.time() + 3
        while time.time() < deadline:
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.exists(), "declared hook did not execute for a real-layout ticket"
    finally:
        pipeline.stop()
