"""Watcher — inotify file-system observer.

Responsibility (Master Part II §4.2, Part III §4.1, RFC-0004; CONTRACTS §4):
watch the TicketRepository via `inotify_simple`, debounce raw events per
`config.debounce_window_seconds`, and translate low-level file events into
domain triggers using path-mapping rules.

The mapping, ownership, and debounce helpers are unit-tested in
`tests/test_watcher_bus.py`; `start()` runs the real inotify read loop.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from inotify_simple import flags as _IN

from lib.ticket_management.config import RuntimeConfig
from lib.ticket_management.runtime.bus import Event, EventBus

__all__ = ["FsWatcher"]

_TRIGGER_MAP = {
    "metadata.json": "metadata.updated",
    "manifest.yaml": "manifest.updated",
    "state.json": "state.updated",
    "activity.jsonl": "activity.updated",
}

# inotify events that matter for the ticketing domain.
_WATCH_FLAGS = (
    _IN.MODIFY
    | _IN.MOVED_FROM
    | _IN.MOVED_TO
    | _IN.CREATE
    | _IN.DELETE
    | _IN.CLOSE_WRITE
    | _IN.MOVE_SELF
)


@dataclass
class FsWatcher:
    """Thin inotify wrapper with path-to-trigger mapping and debounce.

    `watch()` registers the repo and prepares the debounce state; `start()`
    blocks in the inotify read loop (in a daemon thread when used by the
    Pipeline, or inline when called directly). `stop()` signals exit.
    """

    config: RuntimeConfig | None = None
    bus: EventBus | None = None
    repo_path: str | None = None
    _stop_flag: bool = False
    _debounce_window: float = 1.0
    _pending: dict[str, list[Event]] | None = None
    _flush_timer: threading.Timer | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None
    _inotify = None  # inotify_simple.INotify once watch() runs
    _wd_to_path: dict[int, str] = field(default_factory=dict)

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
    # Lifecycle
    # ------------------------------------------------------------------ #
    def watch(self, repo_path: str, bus: EventBus, config: RuntimeConfig) -> None:
        """Register watches and prepare debounce state (does not block)."""
        self.repo_path = repo_path
        self.bus = bus
        self.config = config
        self._debounce_window = getattr(config, "debounce_window_seconds", 1.0)
        self._stop_flag = False
        try:
            from inotify_simple import INotify
        except ImportError as exc:  # pragma: no cover — dependency declared in pyproject
            raise RuntimeError(
                "inotify_simple is required for the watcher; install the 'inotify-simple' extra"
            ) from exc
        self._inotify = INotify()
        self._wd_to_path.clear()
        # Watch the TicketsRepository tree. inotify_simple does NOT recurse
        # automatically on all kernels; we add a watch on the root and on
        # each immediate *.ticket directory so nested file changes fire.
        tickets_root = Path(repo_path) / "TicketsRepository"
        if tickets_root.is_dir():
            wd = self._inotify.add_watch(str(tickets_root), _WATCH_FLAGS)
            self._wd_to_path[wd] = str(tickets_root)
            for child in tickets_root.iterdir():
                if child.is_dir() and child.name.endswith(".ticket"):
                    wd = self._inotify.add_watch(str(child), _WATCH_FLAGS)
                    self._wd_to_path[wd] = str(child)

    def _watch_root(self) -> None:
        """(Re)register the TicketsRepository tree if not already watched."""
        if self._inotify is None or self.repo_path is None:
            return
        tickets_root = Path(self.repo_path) / "TicketsRepository"
        if not tickets_root.is_dir():
            return
        known = set(self._wd_to_path.values())
        if str(tickets_root) not in known:
            wd = self._inotify.add_watch(str(tickets_root), _WATCH_FLAGS)
            self._wd_to_path[wd] = str(tickets_root)
        for child in tickets_root.iterdir():
            if child.is_dir() and child.name.endswith(".ticket") and str(child) not in known:
                wd = self._inotify.add_watch(str(child), _WATCH_FLAGS)
                self._wd_to_path[wd] = str(child)

    def start(self, block: bool = False) -> None:
        """Run the inotify read loop.

        When *block* is True the call blocks until ``stop()``.  The Pipeline
        calls ``start(block=False)`` so the loop runs in a daemon thread and
        ``start()`` returns immediately (the daemon keeps the process alive).
        """
        if self._inotify is None:
            raise RuntimeError("watch() must be called before start()")
        if self.bus is None:
            raise RuntimeError("watcher has no bus; call watch() first")

        bus = self.bus

        def _loop() -> None:
            assert self._inotify is not None
            try:
                while not self._stop_flag:
                    try:
                        events = self._inotify.read(timeout=500)
                    except Exception:  # noqa: BLE001 — never let read errors kill the watcher
                        logger.exception("watcher: inotify read failed; retrying")
                        continue
                    for event in events:
                        if self._stop_flag:
                            break
                        try:
                            # Map the watch descriptor back to its directory.
                            base = self._wd_to_path.get(event.wd, "")
                            full = str(Path(base) / (event.name or "")) if event.name else base
                            self._on_fs_event(full, "modify", bus.publish)
                        except Exception:  # noqa: BLE001 — one bad event must not kill the loop
                            logger.exception("watcher: failed to dispatch event %r", event)
            finally:
                try:
                    self._inotify.close()
                except Exception:
                    pass

        if block:
            _loop()
        else:
            self._thread = threading.Thread(target=_loop, name="tessera-fswatcher", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Signal the read loop to exit and cancel any pending flush."""
        self._stop_flag = True
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None
        if self._inotify is not None:
            try:
                self._inotify.close()
            except Exception:
                pass
            self._inotify = None
