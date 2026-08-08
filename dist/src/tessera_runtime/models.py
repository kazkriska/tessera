"""Tessera v1 — core persistence data models.

Implements the canonical on-disk shapes for a Ticket:

* ``metadata.json``  -> :class:`TicketMetadata`   (CONTRACTS.md §2)
* ``state.json``     -> :class:`TicketState`      (CONTRACTS.md §1)
* ``activity.jsonl`` -> :func:`append_activity` / :func:`read_activity`

plus the durability primitives mandated by CONTRACTS.md §7:

* :func:`atomic_write_json` — tempfile + ``os.replace`` for JSON documents.
* :func:`append_activity`   — ``fcntl.flock`` guarded single-line append.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "ID_PATTERN",
    "OwnerType",
    "Owner",
    "TicketMetadata",
    "StateStatus",
    "TicketState",
    "atomic_write_json",
    "read_json",
    "append_activity",
    "read_activity",
]

#: Canonical ticket-id pattern (CONTRACTS.md §2).
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

DEFAULT_VERSION = "1.0.0"
DEFAULT_KIND = "ticket"


# --------------------------------------------------------------------------- #
# Durability primitives (CONTRACTS.md §7 — "Atomic writes")
# --------------------------------------------------------------------------- #
def atomic_write_json(path: str | os.PathLike[str], data: Any) -> None:
    """Atomically write ``data`` as JSON to ``path``.

    Writes to a temporary file in the same directory (so ``os.replace`` stays
    on one filesystem and is therefore atomic), fsyncs it, then renames over
    the destination. A reader never observes a partially written document.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=False, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def read_json(path: str | os.PathLike[str]) -> Any:
    """Read a JSON document from ``path``."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def append_activity(path: str | os.PathLike[str], record: dict) -> None:
    """Append one JSON record as a single line to an ``activity.jsonl`` file.

    The write + flush is guarded by an exclusive ``fcntl.flock`` so concurrent
    writers never interleave partial lines (CONTRACTS.md §7, Part IV R.A.4).
    """
    if not isinstance(record, dict):
        raise TypeError("activity record must be a dict")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(record, ensure_ascii=False, default=_json_default)
    if "\n" in line:  # defensive: json.dumps never emits raw newlines
        raise ValueError("serialized activity record must be single-line")

    with open(target, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_activity(path: str | os.PathLike[str]) -> list[dict]:
    """Read an ``activity.jsonl`` file into a list of records.

    A missing file yields an empty list (the log is created lazily). Blank
    lines are skipped.
    """
    target = Path(path)
    if not target.exists():
        return []

    records: list[dict] = []
    with open(target, "r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return records


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    raise ValueError(f"invalid datetime value: {value!r}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# metadata.json (CONTRACTS.md §2)
# --------------------------------------------------------------------------- #
class OwnerType(str, Enum):
    """Allowed values of ``metadata.json[\"owner\"][\"type\"]``."""

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


@dataclass
class Owner:
    """Owner block of ``metadata.json``."""

    name: str
    type: OwnerType = OwnerType.USER
    email: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError("owner.name must be a non-empty string")
        try:
            self.type = OwnerType(self.type)
        except ValueError as exc:
            allowed = ", ".join(t.value for t in OwnerType)
            raise ValueError(
                f"invalid owner.type {self.type!r}; must be one of: {allowed}"
            ) from exc

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"name": self.name, "type": self.type.value}
        if self.email is not None:
            data["email"] = self.email
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Owner":
        if not isinstance(data, dict):
            raise ValueError("owner must be an object")
        return cls(
            name=data.get("name", ""),
            type=data.get("type", OwnerType.USER),
            email=data.get("email"),
        )


@dataclass
class TicketMetadata:
    """Canonical model of a Ticket's ``metadata.json``."""

    id: str
    title: str
    owner: Owner
    kind: str = DEFAULT_KIND
    type: str | None = None
    scope: str | None = None
    version: str = DEFAULT_VERSION
    created_at: datetime = field(default_factory=_utcnow)
    tags: list[str] = field(default_factory=list)
    custom: dict[str, Any] = field(default_factory=dict)

    # --- graph relationships (CONTRACTS.md §2) ---
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    related_to: list[str] = field(default_factory=list)
    spawned_from: str | None = None
    delegated_to: str | None = None
    workspace: str | None = None

    RELATIONSHIP_LIST_FIELDS = (
        "children",
        "depends_on",
        "blocks",
        "duplicates",
        "references",
        "related_to",
    )
    RELATIONSHIP_SCALAR_FIELDS = ("parent", "spawned_from", "delegated_to")

    def __post_init__(self) -> None:
        self.validate_id(self.id)
        if not isinstance(self.title, str) or not self.title:
            raise ValueError("title must be a non-empty string")
        if isinstance(self.owner, dict):
            self.owner = Owner.from_dict(self.owner)
        if not isinstance(self.owner, Owner):
            raise ValueError("owner must be an Owner or a mapping")
        if isinstance(self.created_at, str):
            self.created_at = _parse_datetime(self.created_at)
        if not isinstance(self.created_at, datetime):
            raise ValueError("created_at must be a datetime or ISO-8601 string")
        if self.kind != DEFAULT_KIND:
            raise ValueError(
                f"metadata.kind must be {DEFAULT_KIND!r} in v1 (got {self.kind!r})"
            )
        for name in self.RELATIONSHIP_LIST_FIELDS + ("tags",):
            value = getattr(self, name)
            if not isinstance(value, list) or any(
                not isinstance(item, str) for item in value
            ):
                raise ValueError(f"{name} must be a list of strings")
        for name in self.RELATIONSHIP_SCALAR_FIELDS:
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or null")

    # --- validation ---------------------------------------------------- #
    @staticmethod
    def validate_id(ticket_id: Any) -> str:
        """Validate a ticket id against ``^[A-Za-z0-9_-]+$``."""
        if not isinstance(ticket_id, str) or not ID_PATTERN.match(ticket_id):
            raise ValueError(
                f"invalid ticket id {ticket_id!r}: must match {ID_PATTERN.pattern}"
            )
        return ticket_id

    # --- serialization --------------------------------------------------- #
    def to_dict(self) -> dict:
        data: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "type": self.type,
            "scope": self.scope,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "owner": self.owner.to_dict(),
            "tags": list(self.tags),
            "custom": dict(self.custom),
            "parent": self.parent,
            "children": list(self.children),
            "depends_on": list(self.depends_on),
            "blocks": list(self.blocks),
            "duplicates": list(self.duplicates),
            "references": list(self.references),
            "related_to": list(self.related_to),
            "spawned_from": self.spawned_from,
            "delegated_to": self.delegated_to,
            "workspace": self.workspace,
        }
        return data

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "TicketMetadata":
        if not isinstance(data, dict):
            raise ValueError("metadata must be a JSON object")
        # CONTRACTS.md §2: the canonical schema requires all six fields on
        # read. Hand-written tickets must include them; defaults exist only
        # for the dataclass constructor, not for file reads (CMP-12).
        for required in ("id", "title", "kind", "type", "created_at", "owner"):
            if required not in data:
                raise ValueError(
                    f"metadata.json missing required field: {required}"
                )
        return cls(
            id=data["id"],
            title=data["title"],
            owner=Owner.from_dict(data["owner"]),
            kind=data.get("kind", DEFAULT_KIND),
            type=data.get("type"),
            scope=data.get("scope"),
            version=data.get("version", DEFAULT_VERSION),
            created_at=(
                _parse_datetime(data["created_at"])
                if data.get("created_at")
                else _utcnow()
            ),
            tags=list(data.get("tags") or []),
            custom=dict(data.get("custom") or {}),
            parent=data.get("parent"),
            children=list(data.get("children") or []),
            depends_on=list(data.get("depends_on") or []),
            blocks=list(data.get("blocks") or []),
            duplicates=list(data.get("duplicates") or []),
            references=list(data.get("references") or []),
            related_to=list(data.get("related_to") or []),
            spawned_from=data.get("spawned_from"),
            delegated_to=data.get("delegated_to"),
            workspace=data.get("workspace"),
        )

    @classmethod
    def from_json(cls, text: str) -> "TicketMetadata":
        return cls.from_dict(json.loads(text))

    # --- file I/O -------------------------------------------------------- #
    def write(self, path: str | os.PathLike[str]) -> None:
        """Atomically persist this metadata document."""
        atomic_write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "TicketMetadata":
        return cls.from_dict(read_json(path))


# --------------------------------------------------------------------------- #
# state.json (CONTRACTS.md §1)
# --------------------------------------------------------------------------- #
class StateStatus(str, Enum):
    """Canonical 9-state lifecycle enum (CONTRACTS.md §1), lowercase."""

    CREATED = "created"
    INITIALIZED = "initialized"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    DELEGATED = "delegated"
    COMPLETED = "completed"
    ARCHIVED = "archived"
    FAILED = "failed"

    @property
    def event(self) -> str:
        """Lifecycle event emitted on entering this state (``ticket.<status>``)."""
        return f"ticket.{self.value}"


@dataclass
class TicketState:
    """Canonical model of a Ticket's ``state.json``."""

    status: StateStatus = StateStatus.CREATED
    updated_at: datetime = field(default_factory=_utcnow)
    previous_status: StateStatus | None = None
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.status = self._coerce(self.status, "status")
        if self.previous_status is not None:
            self.previous_status = self._coerce(self.previous_status, "previous_status")
        if isinstance(self.updated_at, str):
            self.updated_at = _parse_datetime(self.updated_at)
        if not isinstance(self.updated_at, datetime):
            raise ValueError("updated_at must be a datetime or ISO-8601 string")

    @staticmethod
    def _coerce(value: Any, fieldname: str) -> StateStatus:
        try:
            return StateStatus(value)
        except ValueError as exc:
            allowed = ", ".join(s.value for s in StateStatus)
            raise ValueError(
                f"invalid {fieldname} {value!r}; must be one of: {allowed}"
            ) from exc

    @property
    def event(self) -> str:
        return self.status.event

    def transition(self, status: Any, reason: str | None = None) -> "TicketState":
        """Return a new state moved to ``status``, recording the previous one."""
        new_status = self._coerce(status, "status")
        return TicketState(
            status=new_status,
            updated_at=_utcnow(),
            previous_status=self.status,
            reason=reason,
            detail=dict(self.detail),
        )

    # --- serialization --------------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "updated_at": self.updated_at.isoformat(),
            "previous_status": (
                self.previous_status.value if self.previous_status else None
            ),
            "reason": self.reason,
            "detail": dict(self.detail),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "TicketState":
        if not isinstance(data, dict):
            raise ValueError("state must be a JSON object")
        return cls(
            status=data.get("status", StateStatus.CREATED),
            updated_at=(
                _parse_datetime(data["updated_at"])
                if data.get("updated_at")
                else _utcnow()
            ),
            previous_status=data.get("previous_status"),
            reason=data.get("reason"),
            detail=dict(data.get("detail") or {}),
        )

    @classmethod
    def from_json(cls, text: str) -> "TicketState":
        return cls.from_dict(json.loads(text))

    # --- file I/O -------------------------------------------------------- #
    def write(self, path: str | os.PathLike[str]) -> None:
        atomic_write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "TicketState":
        return cls.from_dict(read_json(path))
