"""Tests for the relationship index builder (B2)."""
from pathlib import Path
import json

from tessera_runtime.relationships import (
    ALL_RELATIONSHIP_TYPES,
    SINGLE_VALUE_RELATIONSHIPS,
    build_relationship_index,
    validate_relationships,
)


def _write_metadata(path: Path, data: dict):
    path.write_text(json.dumps(data))


def _make_repo(tmp_path: Path) -> Path:
    """Return FrameworkRoot with TicketsRepository/ inside."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "TicketsRepository").mkdir()
    return repo


def test_build_empty_repo_returns_empty(tmp_path: Path):
    repo = _make_repo(tmp_path)
    index = build_relationship_index(str(repo))
    assert index == {}


def test_single_ticket_no_parent(tmp_path: Path):
    repo = _make_repo(tmp_path)
    t = repo / "TicketsRepository" / "HQ_BR-001.ticket"
    t.mkdir()
    _write_metadata(t / "metadata.json", {"id": "HQ_BR-001", "title": "t1", "kind": "ticket", "created_at": "2026-01-01T00:00:00Z", "owner": {"name": "u", "type": "user"}})
    index = build_relationship_index(str(repo))
    assert "HQ_BR-001" in index
    assert index["HQ_BR-001"]["parent"] == set()


def test_parent_relationship_indexed(tmp_path: Path):
    repo = _make_repo(tmp_path)
    for name, parent in [("child", "parent"), ("parent", None)]:
        t = repo / "TicketsRepository" / f"{name}.ticket"
        t.mkdir()
        meta = {"id": name, "title": name, "kind": "ticket", "created_at": "2026-01-01T00:00:00Z", "owner": {"name": "u", "type": "user"}}
        if parent:
            meta["parent"] = parent
        _write_metadata(t / "metadata.json", meta)
    index = build_relationship_index(str(repo))
    assert index["child"]["parent"] == {"parent"}
    assert index["parent"]["parent"] == set()


def test_multiple_relationship_types(tmp_path: Path):
    repo = _make_repo(tmp_path)
    t = repo / "TicketsRepository" / "HQ_BR-001.ticket"
    t.mkdir()
    _write_metadata(t / "metadata.json", {
        "id": "HQ_BR-001", "title": "t", "kind": "ticket",
        "created_at": "2026-01-01T00:00:00Z", "owner": {"name": "u", "type": "user"},
        "depends_on": ["HQ_BR-000"], "blocks": ["HQ_BR-002"], "references": ["SKILL-foo"],
    })
    index = build_relationship_index(str(repo))
    assert index["HQ_BR-001"]["depends_on"] == {"HQ_BR-000"}
    assert index["HQ_BR-001"]["blocks"] == {"HQ_BR-002"}
    assert index["HQ_BR-001"]["references"] == {"SKILL-foo"}


def test_dangling_reference_warning(tmp_path: Path):
    repo = _make_repo(tmp_path)
    t = repo / "TicketsRepository" / "HQ_BR-001.ticket"
    t.mkdir()
    _write_metadata(t / "metadata.json", {
        "id": "HQ_BR-001", "title": "t", "kind": "ticket",
        "created_at": "2026-01-01T00:00:00Z", "owner": {"name": "u", "type": "user"},
        "depends_on": ["MISSING"],
    })
    index = build_relationship_index(str(repo))
    warnings = validate_relationships(index, {"HQ_BR-001"})
    assert any("MISSING" in w for w in warnings)


def test_children_mirror_matches_parent_count(tmp_path: Path):
    repo = _make_repo(tmp_path)
    for cid in ["c1", "c2"]:
        t = repo / "TicketsRepository" / f"{cid}.ticket"
        t.mkdir()
        _write_metadata(t / "metadata.json", {"id": cid, "title": cid, "kind": "ticket", "created_at": "2026-01-01T00:00:00Z", "owner": {"name": "u", "type": "user"}, "parent": "parent"})
    pt = repo / "TicketsRepository" / "parent.ticket"
    pt.mkdir()
    _write_metadata(pt / "metadata.json", {"id": "parent", "title": "p", "kind": "ticket", "created_at": "2026-01-01T00:00:00Z", "owner": {"name": "u", "type": "user"}})
    index = build_relationship_index(str(repo))
    assert index["parent"]["children"] == {"c1", "c2"}
    assert index["c1"]["parent"] == {"parent"}
    assert index["c2"]["parent"] == {"parent"}
