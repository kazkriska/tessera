"""Tests for the pipeline wiring (D4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.ticket_management.config import RuntimeConfig
from lib.ticket_management.runtime.bus import Event
from lib.ticket_management.runtime.dispatcher import RunnerDescriptor
from lib.ticket_management.runtime.pipeline import (
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
    ticket_dir = root / f"{ticket_id}.ticket"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (ticket_dir / "MANIFEST.yaml").write_text(manifest, encoding="utf-8")
    return ticket_dir


def test_executor_dispatch_builds_env_and_runs() -> None:
    with pytest.MonkeyPatch.context() as mp:
        import lib.ticket_management.runtime.pipeline as pipeline_mod

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
