"""Tests for lib.ticket_management.runtime.registry."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from lib.ticket_management.runtime.registry import (
    Registry,
    TicketRef,
    discover_tickets,
)
from lib.ticket_management.relationships import build_relationship_index


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. discover_tickets
# --------------------------------------------------------------------------- #
def test_discover_tickets_empty_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "TicketsRepository").mkdir(parents=True)
    assert discover_tickets(str(repo)) == []


def test_discover_tickets_finds_ticket(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ticket_dir = repo / "TicketsRepository" / "HQ_BR-001.ticket"
    ticket_dir.mkdir(parents=True)
    _write_json(
        ticket_dir / "metadata.json",
        {
            "id": "HQ_BR-001",
            "title": "Test ticket",
            "kind": "ticket",
            "owner": {"name": "igor", "type": "user"},
        },
    )
    refs = discover_tickets(str(repo))
    assert len(refs) == 1
    ref = refs[0]
    assert ref.id == "HQ_BR-001"
    assert ref.path == ticket_dir
    assert ref.metadata_path == ticket_dir / "metadata.json"
    assert ref.state_path == ticket_dir / "state.json"
    assert ref.activity_path == ticket_dir / "activity.jsonl"
    assert ref.has_manifest is False
    assert ref.manifest_path is None


def test_discover_tickets_rejects_bad_id(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ticket_dir = repo / "TicketsRepository" / "bad id!.ticket"
    ticket_dir.mkdir(parents=True)
    _write_json(
        ticket_dir / "metadata.json",
        {
            "id": "bad id!",
            "title": "Bad",
            "kind": "ticket",
            "owner": {"name": "igor", "type": "user"},
        },
    )
    refs = discover_tickets(str(repo))
    assert refs == []


# --------------------------------------------------------------------------- #
# 2. Registry
# --------------------------------------------------------------------------- #
def _make_ticket_ref(path: Path, ticket_id: str = "T-001") -> TicketRef:
    metadata_path = path / "metadata.json"
    _write_json(
        metadata_path,
        {
            "id": ticket_id,
            "title": "Sample",
            "kind": "ticket",
            "type": "task",
            "owner": {"name": "igor", "type": "user"},
        },
    )
    return TicketRef(
        id=ticket_id,
        path=path,
        metadata_path=metadata_path,
        manifest_path=None,
        state_path=path / "state.json",
        activity_path=path / "activity.jsonl",
        has_manifest=False,
    )


def test_registry_rebuild_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    t1 = repo / "TicketsRepository" / "T-001.ticket"
    t2 = repo / "TicketsRepository" / "T-002.ticket"
    t1.mkdir(parents=True)
    t2.mkdir(parents=True)
    ref1 = _make_ticket_ref(t1, "T-001")
    ref2 = _make_ticket_ref(t2, "T-002")

    reg = Registry(str(repo))
    reg.rebuild(str(repo))
    assert len(reg.list_all()) == 2

    # Second rebuild on the same repo must be stable.
    reg.rebuild(str(repo))
    assert len(reg.list_all()) == 2
    reg.close()


def test_registry_rebuild_on_corrupt_db(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    t = repo / "TicketsRepository" / "T-001.ticket"
    t.mkdir(parents=True)
    _make_ticket_ref(t, "T-001")

    reg = Registry(str(repo))
    # Corrupt the DB.
    (Path(reg._db_path)).write_text("not a sqlite db", encoding="utf-8")  # noqa: SLF001
    reg.close()

    # Re-open; rebuild must recover gracefully.
    reg = Registry(str(repo))
    reg.rebuild(str(repo))
    assert len(reg.list_all()) == 1
    reg.close()


def test_registry_upsert_and_get(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ticket_dir = repo / "TicketsRepository" / "T-001.ticket"
    ticket_dir.mkdir(parents=True)
    ref = _make_ticket_ref(ticket_dir, "T-001")

    reg = Registry(str(repo))
    reg.upsert(ref)
    row = reg.get("T-001")
    assert row is not None
    assert row["id"] == "T-001"
    assert row["title"] == "Sample"
    assert row["ticket_type"] == "task"
    old_scanned = row["last_scanned"]

    reg.touch("T-001")
    row = reg.get("T-001")
    assert row is not None
    assert row["last_scanned"] != old_scanned
    reg.close()


def test_registry_wal_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    reg = Registry(str(repo))
    mode = reg._conn.execute("PRAGMA journal_mode").fetchone()[0]  # noqa: SLF001
    assert mode == "wal"
    reg.close()


# --------------------------------------------------------------------------- #
# 3. Relationship index
# --------------------------------------------------------------------------- #
def test_load_relationship_index(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    child = repo / "TicketsRepository" / "CHILD.ticket"
    parent = repo / "TicketsRepository" / "PARENT.ticket"
    child.mkdir(parents=True)
    parent.mkdir(parents=True)
    _write_json(
        child / "metadata.json",
        {
            "id": "CHILD",
            "title": "Child",
            "kind": "ticket",
            "owner": {"name": "igor", "type": "user"},
            "parent": "PARENT",
        },
    )
    _write_json(
        parent / "metadata.json",
        {
            "id": "PARENT",
            "title": "Parent",
            "kind": "ticket",
            "owner": {"name": "igor", "type": "user"},
        },
    )
    idx = build_relationship_index(str(repo))
    assert "CHILD" in idx
    # Canonical B2 index: parent edge + derived children mirror.
    assert idx["CHILD"]["parent"] == {"PARENT"}
    assert idx["PARENT"]["children"] == {"CHILD"}


# --------------------------------------------------------------------------- #
# 4. TicketRef path contract
# --------------------------------------------------------------------------- #
def test_discover_tickets_sets_all_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ticket_dir = repo / "TicketsRepository" / "T-001.ticket"
    ticket_dir.mkdir(parents=True)
    _write_json(
        ticket_dir / "metadata.json",
        {
            "id": "T-001",
            "title": "Full",
            "kind": "ticket",
            "owner": {"name": "igor", "type": "user"},
        },
    )
    refs = discover_tickets(str(repo))
    assert len(refs) == 1
    ref = refs[0]
    assert ref.path == ticket_dir
    assert ref.metadata_path.name == "metadata.json"
    assert ref.state_path.name == "state.json"
    assert ref.activity_path.name == "activity.jsonl"
