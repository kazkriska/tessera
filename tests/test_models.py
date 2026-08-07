"""Tests for lib.ticket_management.models (Kanban A3).

Covers: metadata round-trip incl. relationship fields, id validation,
the canonical 9-state enum, atomic_write_json, and the flock-guarded
activity.jsonl append/read round-trip.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from lib.ticket_management.models import (
    Owner,
    OwnerType,
    StateStatus,
    TicketMetadata,
    TicketState,
    append_activity,
    atomic_write_json,
    read_activity,
    read_json,
)


def _metadata() -> TicketMetadata:
    return TicketMetadata(
        id="t_547e2a9d",
        title="Build models",
        owner=Owner(name="Kevin", type=OwnerType.USER, email="kevin@example.com"),
        type="task",
        scope="tessera/core",
        created_at=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
        tags=["phase-a", "models"],
        custom={"priority": 1},
        parent="t_parent",
        children=["t_child1", "t_child2"],
        depends_on=["t_a1"],
        blocks=["t_b2"],
        duplicates=["t_dup"],
        references=["t_ref"],
        related_to=["t_rel"],
        spawned_from="t_origin",
        delegated_to="igor",
        workspace="/tmp/ws",
    )


# --- 1. metadata round-trip + relationships -------------------------------- #
def test_metadata_round_trip_preserves_relationship_fields():
    meta = _metadata()
    restored = TicketMetadata.from_json(meta.to_json())

    assert restored.to_dict() == meta.to_dict()
    assert restored.kind == "ticket"
    assert restored.version == "1.0.0"
    assert restored.parent == "t_parent"
    assert restored.children == ["t_child1", "t_child2"]
    assert restored.depends_on == ["t_a1"]
    assert restored.blocks == ["t_b2"]
    assert restored.duplicates == ["t_dup"]
    assert restored.references == ["t_ref"]
    assert restored.related_to == ["t_rel"]
    assert restored.spawned_from == "t_origin"
    assert restored.delegated_to == "igor"
    assert restored.workspace == "/tmp/ws"
    assert restored.owner.type is OwnerType.USER
    assert restored.created_at == meta.created_at


@pytest.mark.parametrize("bad_id", ["has space", "bad/slash", "", "dot.id", None])
def test_metadata_rejects_invalid_id(bad_id):
    with pytest.raises(ValueError):
        TicketMetadata(id=bad_id, title="x", owner=Owner(name="k"))


def test_metadata_rejects_invalid_owner_type():
    with pytest.raises(ValueError):
        TicketMetadata(
            id="ok-1", title="x", owner={"name": "k", "type": "robot"}
        )


# --- 2. state enum --------------------------------------------------------- #
def test_state_default_and_canonical_enum():
    state = TicketState()
    assert state.status is StateStatus.CREATED
    assert state.to_dict()["status"] == "created"
    assert [s.value for s in StateStatus] == [
        "created",
        "initialized",
        "ready",
        "running",
        "blocked",
        "delegated",
        "completed",
        "archived",
        "failed",
    ]
    assert StateStatus.RUNNING.event == "ticket.running"


def test_state_round_trip_and_invalid_status():
    state = TicketState(status="running", reason="dispatched")
    restored = TicketState.from_json(state.to_json())
    assert restored.status is StateStatus.RUNNING
    assert restored.reason == "dispatched"

    moved = restored.transition(StateStatus.COMPLETED)
    assert moved.status is StateStatus.COMPLETED
    assert moved.previous_status is StateStatus.RUNNING

    for bad in ("initializing", "handoff", "Running", "nope"):
        with pytest.raises(ValueError):
            TicketState(status=bad)


# --- 3. atomic_write_json -------------------------------------------------- #
def test_atomic_write_json_creates_file_and_leaves_no_temp(tmp_path):
    target = tmp_path / "nested" / "state.json"
    state = TicketState(status=StateStatus.READY)
    atomic_write_json(target, state.to_dict())

    assert target.exists()
    assert read_json(target)["status"] == "ready"
    siblings = [p.name for p in target.parent.iterdir()]
    assert siblings == ["state.json"]

    # overwrite is atomic and complete
    atomic_write_json(target, {"status": "archived"})
    assert json.loads(target.read_text())["status"] == "archived"


def test_metadata_and_state_write_load_helpers(tmp_path):
    meta_path = tmp_path / "metadata.json"
    state_path = tmp_path / "state.json"
    _metadata().write(meta_path)
    TicketState(status=StateStatus.BLOCKED).write(state_path)

    assert TicketMetadata.load(meta_path).id == "t_547e2a9d"
    assert TicketState.load(state_path).status is StateStatus.BLOCKED


# --- 4. activity.jsonl ----------------------------------------------------- #
def test_append_activity_round_trip_one_line_per_record(tmp_path):
    log = tmp_path / "activity.jsonl"
    assert read_activity(log) == []

    append_activity(log, {"event": "ticket.created", "seq": 1})
    append_activity(log, {"event": "ticket.ready", "seq": 2, "at": datetime(2026, 8, 7)})

    raw_lines = log.read_text().splitlines()
    assert len(raw_lines) == 2
    assert all(json.loads(line) for line in raw_lines)

    records = read_activity(log)
    assert [r["event"] for r in records] == ["ticket.created", "ticket.ready"]
    assert records[1]["at"] == "2026-08-07T00:00:00"

    with pytest.raises(TypeError):
        append_activity(log, ["not", "a", "dict"])


def test_append_activity_concurrent_writers_keep_lines_intact(tmp_path):
    import threading

    log = tmp_path / "activity.jsonl"
    payload = "x" * 4096

    def writer(worker: int) -> None:
        for i in range(25):
            append_activity(log, {"worker": worker, "i": i, "pad": payload})

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    records = read_activity(log)
    assert len(records) == 100
    assert sorted((r["worker"], r["i"]) for r in records) == sorted(
        (w, i) for w in range(4) for i in range(25)
    )
