"""Watcher — inotify file-system observer.

Responsibility (Master Part II §4.2, Part III §4.1, RFC-0004; CONTRACTS §4):
watch the TicketRepository via `inotify-simple`, debounce raw events per
`config.yaml:debounce_window_seconds`, and translate low-level file events into
domain triggers using path-mapping rules.

The full inotify loop is implemented in Phase C; the mapping and debounce
helpers are unit-testable now.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from lib.ticket_management.config import RuntimeConfig
from lib.ticket_management.runtime.bus import Event, EventBus

__all__ = ["FsWatcher"]

_TRIGGER_MAP = {
    "metadata.json": "metadata.updated",
    "manifest.yaml": "manifest.updated",
    "state.json": "state.updated",
    "activity.jsonl": "activity.updated",
}


@dataclass
class FsWatcher:
    """Thin inotify wrapper with path-to-trigger mapping and debounce.

    The public ``watch()`` / ``start()`` / ``stop()`` lifecycle is stubbed
    for Phase C1; the mapping and debounce helpers are fully implemented.
    """

    config: RuntimeConfig | None = None
    bus: EventBus | None = None
    repo_path: str | None = None
    _stop_flag: bool = False
    _debounce_window: float = 1.0
    _pending: dict[str, list[Event]] | None = None
    _flush_timer: threading.Timer | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ #
    # Path mapping
    # ------------------------------------------------------------------ #
    def _trigger_for_path(self, changed_path: Path) -> str:
        """Map a changed file path to a domain event trigger name."""
        name = changed_path.name.lower()
        return _TRIGGER_MAP.get(name, "fs.changed")

    def _owning_ticket_id(self, changed_path: Path) -> str | None:
        """Walk up from *changed_path* until a ``*.ticket`` directory is found."""
        for parent in [changed_path, *changed_path.parents]:
            if parent.name.endswith(".ticket") and parent.parent.name == "TicketsRepository":
                return parent.name.removesuffix(".ticket")
        return None

    # ------------------------------------------------------------------ #
    # Debounce
    # ------------------------------------------------------------------ #
    def _on_fs_event(
        self,
        changed_path_str: str,
        change_type: str,
        handler: Callable[[Event], None],
    ) -> None:
        """Coalesce rapid duplicate events for the same (ticket_id, trigger)."""
        changed_path = Path(changed_path_str)
        trigger = self._trigger_for_path(changed_path)
        ticket_id = self._owning_ticket_id(changed_path)
        if ticket_id is None:
            return
        key = f"{ticket_id}:{trigger}"
        event = Event(name=trigger, ticket_id=ticket_id, data={"path": str(changed_path), "change": change_type})
        with self._lock:
            if self._pending is None:
                self._pending = {}
            bucket = self._pending.get(key, [])
            bucket.append(event)
            self._pending[key] = bucket
            # (Re)start flush timer
            if self._flush_timer is not None:
                self._flush_timer.cancel()
            self._flush_timer = threading.Timer(self._debounce_window, self._flush, args=(handler,))
            self._flush_timer.daemon = True
            self._flush_timer.start()

    def _flush(self, handler: Callable[[Event], None]) -> None:
        """Emit the latest event per key and clear the pending map."""
        with self._lock:
            pending = self._pending or {}
            self._pending = {}
            self._flush_timer = None
        for bucket in pending.values():
            if bucket:
                handler(bucket[-1])

    # ------------------------------------------------------------------ #
    # Lifecycle stubs (Phase C)
    # ------------------------------------------------------------------ #
    def watch(self, repo_path: str, bus: EventBus, config: RuntimeConfig) -> None:
        """Register watches and begin processing events."""
        self.repo_path = repo_path
        self.bus = bus
        self.config = config
        self._debounce_window = getattr(config, "debounce_window_seconds", 1.0)
        self._stop_flag = False

    def start(self) -> None:
        """Block in the inotify read loop (Phase C)."""
        # Stub: real inotify read loop lives in Phase C implementation.
        while not self._stop_flag:
            pass

    def stop(self) -> None:
        """Signal the read loop to exit."""
        self._stop_flag = True
