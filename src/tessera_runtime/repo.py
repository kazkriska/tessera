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
    "DEFAULT_PREFIX",
    "ROOT_MARKER",
    "repo_init",
    "write_root_marker",
    "rescan",
    "get_repo_root",
]

#: Canonical directory names (CONTRACTS.md §7.1).
REPO_DIR_NAME = "TicketsRepository"
RUNTIME_DIR_NAME = ".ticket-runtime"
RUNTIME_SUBDIRS = ("locks", "cache", "logs", "plugins", "tmp")

#: Canonical framework prefix (matches install.sh TESSERA_PREFIX).
#: ``tessera repo init`` (no args) initializes here by default.
DEFAULT_PREFIX = Path.home() / ".local" / "share" / "tessera"

#: Marker written at the framework root so discovery can find the repo
#: without walking up from cwd. Presence => this dir is the root.
ROOT_MARKER = ".tessera-root"


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


def write_root_marker(root: str | Path) -> Path:
    """Write the canonical-root marker file at *root* and return its path.

    The file is a sentinel: its *presence* is what discovery keys on
    (:func:`get_repo_root`). For human readability and a cheap sanity check,
    its contents are the resolved absolute path of *root* (a comment-style
    value, not parsed by discovery).
    """
    marker = Path(root).resolve() / ROOT_MARKER
    marker.write_text(f"# Tessera canonical root\ntessera_root={marker.parent}\n", encoding="utf-8")
    return marker


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
    """Locate the framework root.

    Resolution order (dev/v1 canonical-prefix model):

    1. Legacy cwd-walk — the nearest ancestor of *start* (default cwd)
       containing ``TicketsRepository/``. This wins when you are standing
       inside (or below) a repo, so local/non-canonical repos keep working.
    2. Canonical marker — when ``<prefix>/.tessera-root`` exists, the prefix
       is the framework root. This is the *default* location used when you
       are NOT inside any repo (e.g. running ``tessera`` from a random cwd
       after ``tessera repo init``).

    The marker is therefore a fallback, not an override: it only applies when
    no repo is found by walking up from cwd. Explicit ``--repo`` overrides are
    applied by the caller *before* this function runs (see ``cli._find_root``).

    Raises :class:`RuntimeError` when neither a cwd repo nor a marker exists.
    """
    # 1. Legacy cwd-walk (wins when standing inside a repo).
    current = Path(start).resolve() if start is not None else Path.cwd().resolve()
    if not current.is_dir():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / REPO_DIR_NAME).is_dir():
            return candidate

    # 2. Canonical marker (default location when not in a repo).
    marker_root = DEFAULT_PREFIX / ROOT_MARKER
    if marker_root.is_file():
        return DEFAULT_PREFIX

    raise RuntimeError(
        f"no Tessera framework root found from {current} "
        f"(no '{REPO_DIR_NAME}/' directory in any ancestor and no "
        f"canonical root marker at {DEFAULT_PREFIX})"
    )
