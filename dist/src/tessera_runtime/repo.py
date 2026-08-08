"""Repository initialization and rescan for Tessera v1.

Responsibility (Master Part II §6, RFC-0012; CONTRACTS.md §7.1): create and
maintain the canonical framework directory tree, locate the framework root,
and drive the CLI-agnostic rescan that rebuilds the derived registry and
relationship index.

The directory tree under the framework root:

    <root>/
    └── TicketsRepository/            # canonical Ticket repository
        └── .ticket-runtime/          # disposable runtime state (I-2)
            ├── locks/
            ├── cache/
            ├── logs/
            ├── plugins/
            └── tmp/
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tessera_runtime.config import load_config
from tessera_runtime.relationships import build_relationship_index
from tessera_runtime.runtime.registry import Registry

__all__ = [
    "REPO_DIR_NAME",
    "RUNTIME_DIR_NAME",
    "RUNTIME_SUBDIRS",
    "repo_init",
    "rescan",
    "get_repo_root",
]

#: Canonical directory names (CONTRACTS.md §7.1).
REPO_DIR_NAME = "TicketsRepository"
RUNTIME_DIR_NAME = ".ticket-runtime"
RUNTIME_SUBDIRS = ("locks", "cache", "logs", "plugins", "tmp")


def repo_init(root: str | Path) -> Path:
    """Ensure the canonical directory tree exists under *root*.

    Creates ``<root>/TicketsRepository/.ticket-runtime/`` plus the runtime
    subdirectories (``locks``, ``cache``, ``logs``, ``plugins``, ``tmp``).

    Idempotent: calling twice is safe. ``registry.db`` is intentionally NOT
    created here — that is the Registry's job (Invariant I-9).
    """
    root_path = Path(root).resolve()
    runtime_dir = root_path / REPO_DIR_NAME / RUNTIME_DIR_NAME
    runtime_dir.mkdir(parents=True, exist_ok=True)
    for subdir in RUNTIME_SUBDIRS:
        (runtime_dir / subdir).mkdir(parents=True, exist_ok=True)
    return root_path


def rescan(root: str | Path, registry: Registry | None = None) -> dict[str, Any]:
    """Perform a full rescan + rebuild of the derived runtime state.

    Steps:
    1. Ensure the directory tree exists (:func:`repo_init`).
    2. Load ``.ticket-runtime/config.yaml`` (:func:`load_config` — a missing
       file yields canonical defaults).
    3. Rebuild the SQLite registry from discovered tickets (I-9).
    4. Rebuild the relationship index and return it.

    *registry* defaults to a fresh :class:`Registry` bound to *root*.
    Returns the relationship index (``{ticket_id: {rel_type: set[ids]}}``).
    """
    root_path = repo_init(root)
    config = load_config(str(root_path / REPO_DIR_NAME / RUNTIME_DIR_NAME / "config.yaml"))

    reg = registry if registry is not None else Registry(str(root_path))
    reg.rebuild(str(root_path))

    index = build_relationship_index(str(root_path))
    return {
        "config": config,
        "registry": reg,
        "relationship_index": index,
    }


def get_repo_root(start: str | Path | None = None) -> Path:
    """Locate the framework root by walking up from *start* (default: cwd).

    Returns the nearest ancestor directory containing ``TicketsRepository/``.
    Raises :class:`RuntimeError` when no ancestor qualifies.
    """
    current = Path(start).resolve() if start is not None else Path.cwd().resolve()
    if not current.is_dir():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / REPO_DIR_NAME).is_dir():
            return candidate

    raise RuntimeError(
        f"no Tessera framework root found from {current} "
        f"(no '{REPO_DIR_NAME}/' directory in any ancestor)"
    )
