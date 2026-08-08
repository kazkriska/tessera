"""Tessera v1 — recursive Tickets discovery + SQLite-backed Registry.

Implements:

* :func:`discover_tickets` — scan ``<repo>/TicketsRepository/`` for ``*.ticket``
  dirs and return typed :class:`TicketRef` descriptors.
* :class:`Registry` — WAL-mode SQLite store of discovered tickets (derived,
  rebuildable per Invariant I-9).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tessera_runtime.models import ID_PATTERN

logger = logging.getLogger(__name__)

__all__ = [
    "TicketRef",
    "discover_tickets",
    "Registry",
]


# --------------------------------------------------------------------------- #
# TicketRef
# --------------------------------------------------------------------------- #
@dataclass
class TicketRef:
    """Lightweight descriptor of a Ticket on disk."""

    id: str
    path: Path
    metadata_path: Path
    manifest_path: Optional[Path]
    state_path: Path
    activity_path: Path
    has_manifest: bool


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def discover_tickets(repo_path: str) -> list[TicketRef]:
    """Recursively scan ``<repo_path>/TicketsRepository/`` for ``*.ticket/`` dirs.

    Returns a list of :class:`TicketRef` instances. Directories whose basename
    does not match ``<id>.ticket`` (or whose ``<id>`` fails the canonical
    ``ID_PATTERN``) are silently skipped.

    The function reads ``metadata.json`` to confirm the ticket id and to pick
    up the optional ``parent`` relationship, and probes for ``MANIFEST.yaml``.
    """
    repo = Path(repo_path).resolve()
    tickets_root = repo / "TicketsRepository"
    if not tickets_root.is_dir():
        return []

    refs: list[TicketRef] = []
    for candidate in tickets_root.rglob("*.ticket"):
        if not candidate.is_dir():
            continue

        base_name = candidate.name
        ticket_id = base_name[: -len(".ticket")]
        if not ID_PATTERN.match(ticket_id):
            continue

        metadata_path = candidate / "metadata.json"
        if not metadata_path.is_file():
            # A ticket directory without metadata.json is ignored — metadata is
            # mandatory per CONTRACTS.md §2.
            continue

        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        if metadata.get("id") != ticket_id:
            # Hard mismatch — directory claims a different id.
            continue

        manifest_path = candidate / "MANIFEST.yaml"
        has_manifest = manifest_path.is_file()

        state_path = candidate / "state.json"
        activity_path = candidate / "activity.jsonl"

        refs.append(
            TicketRef(
                id=ticket_id,
                path=candidate,
                metadata_path=metadata_path,
                manifest_path=manifest_path if has_manifest else None,
                state_path=state_path,
                activity_path=activity_path,
                has_manifest=has_manifest,
            )
        )

    return refs


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class Registry:
    """SQLite-backed, rebuildable ticket registry.

    The database lives at ``<repo>/TicketsRepository/.ticket-runtime/registry.db``
    (or ``db_path`` when given) and is opened in WAL mode for concurrent
    reads.

    All row ``dict`` results use string column names; ``last_scanned`` values
    are ISO-8601 strings (sqlite3 ``PARSE_DECLTYPES`` is enabled but we
    serialize timestamps explicitly to keep the surface simple).
    """

    def __init__(self, repo_path: str, db_path: str | Path | None = None) -> None:
        self._repo = Path(repo_path).resolve()
        if db_path is None:
            self._db_path = (
                self._repo / "TicketsRepository" / ".ticket-runtime" / "registry.db"
            )
        else:
            self._db_path = Path(db_path).resolve()
        self._ensure_dir()
        try:
            self._open()
        except sqlite3.DatabaseError:
            # Corrupt registry → recreate + rescan (RFC-0004 failure modes;
            # Invariant I-9: the registry is derived and rebuildable).
            logger.warning("registry: corrupt database %s; recreating", self._db_path)
            self.close()
            try:
                self._db_path.unlink()
            except OSError:
                pass
            self._open()

    def _open(self) -> None:
        self._conn = sqlite3.connect(
            str(self._db_path),
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    # -- internal -------------------------------------------------------- #
    def _ensure_dir(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id               TEXT    PRIMARY KEY,
                path             TEXT    NOT NULL,
                parent_id        TEXT,
                state            TEXT,
                manifest_version TEXT,
                last_scanned     TEXT,
                title            TEXT,
                kind             TEXT,
                ticket_type      TEXT
            )
            """
        )
        self._conn.commit()

    def _drop_schema(self) -> None:
        self._conn.execute("DROP TABLE IF EXISTS tickets")
        self._conn.commit()

    # -- public API ------------------------------------------------------ #
    def rebuild(self, repo_path: str) -> None:
        """Drop schema, rescan ``repo_path``, and re-populate the registry.

        Invariant I-9: rescanning must reconstruct an equivalent registry.
        This is safe against empty or corrupt databases because the existing
        table is dropped unconditionally before recreation.
        """
        self._drop_schema()
        self._ensure_schema()
        refs = discover_tickets(repo_path)
        for ref in refs:
            self.upsert(ref)

    def upsert(self, ticket: TicketRef) -> None:
        """Insert or replace a ticket row keyed by ``id``."""
        now = datetime.now(timezone.utc).isoformat()

        # Read optional parent from metadata.json for the relationship index.
        parent_id: Optional[str] = None
        try:
            metadata = json.loads(ticket.metadata_path.read_text(encoding="utf-8"))
            parent_id = metadata.get("parent")
        except (OSError, json.JSONDecodeError, ValueError):
            pass

        # Derive manifest_version if a manifest is present (best-effort).
        manifest_version: Optional[str] = None
        if ticket.has_manifest:
            try:
                manifest_text = ticket.manifest_path.read_text(encoding="utf-8")
                for line in manifest_text.splitlines():
                    if line.strip().startswith("version:"):
                        manifest_version = line.split(":", 1)[1].strip().strip('"').strip("'")
                        break
            except OSError:
                pass

        # Derive state, title, kind, ticket_type.
        state_val: Optional[str] = None
        try:
            state_data = json.loads(ticket.state_path.read_text(encoding="utf-8"))
            state_val = state_data.get("status")
        except (OSError, json.JSONDecodeError):
            pass

        title_val: Optional[str] = None
        kind_val: Optional[str] = None
        ticket_type_val: Optional[str] = None
        try:
            metadata = json.loads(ticket.metadata_path.read_text(encoding="utf-8"))
            title_val = metadata.get("title")
            kind_val = metadata.get("kind")
            ticket_type_val = metadata.get("type")
        except (OSError, json.JSONDecodeError):
            pass

        self._conn.execute(
            """
            INSERT OR REPLACE INTO tickets
                (id, path, parent_id, state, manifest_version, last_scanned,
                 title, kind, ticket_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket.id,
                str(ticket.path),
                parent_id,
                state_val,
                manifest_version,
                now,
                title_val,
                kind_val,
                ticket_type_val,
            ),
        )
        self._conn.commit()

    def get(self, id: str) -> Optional[dict]:  # noqa: A002
        """Return a single ticket row as a ``dict``, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (id,)
        ).fetchone()
        return dict(row) if row else None

    def list_all(self) -> list[dict]:
        """Return every row in the registry as a list of ``dict``."""
        return [dict(r) for r in self._conn.execute("SELECT * FROM tickets").fetchall()]

    def touch(self, id: str) -> None:  # noqa: A002
        """Update ``last_scanned`` to the current UTC timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE tickets SET last_scanned = ? WHERE id = ?",
            (now, id),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()



