"""Tests for repository init + rescan (B3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.ticket_management.repo import (
    REPO_DIR_NAME,
    RUNTIME_DIR_NAME,
    RUNTIME_SUBDIRS,
    get_repo_root,
    repo_init,
    rescan,
)


def test_repo_init_creates_directories(tmp_path: Path):
    root = repo_init(tmp_path)
    runtime_dir = tmp_path / REPO_DIR_NAME / RUNTIME_DIR_NAME
    assert runtime_dir.is_dir()
    for subdir in RUNTIME_SUBDIRS:
        assert (runtime_dir / subdir).is_dir()
    # registry.db is NOT created by repo_init (Registry's job).
    assert not (runtime_dir / "registry.db").exists()
    assert root == tmp_path.resolve()


def test_repo_init_idempotent(tmp_path: Path):
    repo_init(tmp_path)
    repo_init(tmp_path)  # must not raise
    runtime_dir = tmp_path / REPO_DIR_NAME / RUNTIME_DIR_NAME
    assert runtime_dir.is_dir()
    for subdir in RUNTIME_SUBDIRS:
        assert (runtime_dir / subdir).is_dir()


def test_get_repo_root_finds_framework_root(tmp_path: Path):
    repo_init(tmp_path)
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    found = get_repo_root(deep)
    assert found == tmp_path.resolve()


def test_get_repo_root_raises_when_missing(tmp_path: Path):
    with pytest.raises(RuntimeError):
        get_repo_root(tmp_path)


def test_rescan_builds_registry_and_index(tmp_path: Path):
    repo_init(tmp_path)

    # Create one valid ticket on disk.
    ticket_dir = tmp_path / REPO_DIR_NAME / "HQ_BR-001.ticket"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "metadata.json").write_text(
        '{"id": "HQ_BR-001", "title": "Onboard contributor", '
        '"kind": "ticket", "owner": {"name": "alice", "type": "user"}}',
        encoding="utf-8",
    )

    result = rescan(tmp_path)
    registry = result["registry"]
    rows = registry.list_all()
    assert len(rows) == 1
    assert rows[0]["id"] == "HQ_BR-001"

    index = result["relationship_index"]
    assert "HQ_BR-001" in index

    registry.close()
