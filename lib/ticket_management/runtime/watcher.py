"""Watcher — inotify file-system observer.

Responsibility (Master Part II §4.2, Part III §4.1, RFC-0004; CONTRACTS §4):
watch the TicketRepository via `inotify_simple`, debounce raw events per
`config.debounce_window_seconds`, and translate low-level file events into
domain triggers using path-mapping rules.

The mapping, ownership, and debounce helpers are unit-tested in
`tests/test_watcher_bus.py`; `start()` runs the real inotify read loop.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from inotify_simple import flags as _IN

from lib.ticket_management.config import RuntimeConfig
from lib.ticket_management.runtime.bus import Event, EventBus

__all__ = ["FsWatcher"]

logger = logging.getLogger(__name__)

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


def _walk_dirs(root: Path) -> list[Path]:
    """Return *root* plus every descendant directory (recursive).

    inotify watches are non-recursive: each directory must be registered
    explicitly so file events in nested ticket/asset directories fire.
    """
    return [p for p in root.rglob("*") if p.is_dir()] + [root]


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
    _watch_rules: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Path mapping
    # ------------------------------------------------------------------ #
    def _trigger_for_path(self, changed_path: Path) -> str:
        """Map a changed file path to a domain event trigger name."""
        name = changed_path.name.lower()
        return _TRIGGER_MAP.get(name, "fs.changed")

    def _manifest_rules(self, ticket_id: str) -> list[dict[str, Any]]:
        """Load the ticket's declared ``watch:`` rules (cached).

        The rules come from ``MANIFEST.yaml`` (already validated by the
        manifest loader, including the circular-watch guard). The cache is
        invalidated whenever the manifest itself changes.
        """
        if ticket_id in self._watch_rules:
            return self._watch_rules[ticket_id]
        rules: list[dict[str, Any]] = []
        try:
            from lib.ticket_management.runtime.manifest import load_manifest

            if self.repo_path is not None:
                manifest_path = (
                    Path(self.repo_path)
                    / "TicketsRepository"
                    / f"{ticket_id}.ticket"
                    / "MANIFEST.yaml"
                )
                if manifest_path.is_file():
                    manifest = load_manifest(
                        manifest_path, ticket_id=ticket_id
                    )
                    rules = list(manifest.watch or [])
        except Exception:  # noqa: BLE001 — watcher must never die on a bad manifest
            rules = []
        self._watch_rules[ticket_id] = rules
        return rules

    def _rule_triggers_for_path(
        self, changed_path: Path, ticket_id: str
    ) -> list[str]:
        """Return declared trigger names whose ``watch.path`` matches.

        Rule paths are relative to the ticket root (e.g. ``assets/**``).
        A change to a nested asset fires the rule's trigger in addition to
        the default mapping (which stays ``fs.changed`` for unknown files).
        """
        if not self._manifest_rules(ticket_id):
            return []
        ticket_root = (
            Path(self.repo_path)
            / "TicketsRepository"
            / f"{ticket_id}.ticket"
        )
        try:
            rel = changed_path.resolve().relative_to(ticket_root.resolve())
        except ValueError:
            return []
        rel_str = rel.as_posix()
        triggers: list[str] = []
        for rule in self._manifest_rules(ticket_id):
            pattern = str(rule.get("path", "")).strip().lstrip("/")
            if not pattern:
                continue
            if fnmatch.fnmatch(rel_str, pattern) or fnmatch.fnmatch(
                rel_str, f"{pattern.rstrip('/')}/*"
            ):
                trigger = rule.get("trigger") or "fs.changed"
                if trigger not in triggers:
                    triggers.append(trigger)
        return triggers

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

        # Manifest watch rules (CMP-10): a declared rule adds its trigger
        # alongside the default mapping; a manifest change also refreshes
        # the cached rules.
        if changed_path.name.upper() == "MANIFEST.YAML":
            self._watch_rules.pop(ticket_id, None)
        rule_triggers = self._rule_triggers_for_path(changed_path, ticket_id)
        triggers = list(dict.fromkeys([trigger, *rule_triggers]))

        for trig in triggers:
            key = f"{ticket_id}:{trig}"
            event = Event(
                name=trig,
                ticket_id=ticket_id,
                data={"path": str(changed_path), "change": change_type},
            )
            with self._lock:
                if self._pending is None:
                    self._pending = {}
                bucket = self._pending.get(key, [])
                bucket.append(event)
                self._pending[key] = bucket
                # (Re)start flush timer
                if self._flush_timer is not None:
                    self._flush_timer.cancel()
                self._flush_timer = threading.Timer(
                    self._debounce_window, self._flush, args=(handler,)
                )
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
        # Watch the whole TicketsRepository tree. inotify does NOT recurse, so
        # we walk every directory (root, each *.ticket dir, and nested asset
        # dirs) and register a watch on each — nested file changes must fire.
        tickets_root = Path(repo_path) / "TicketsRepository"
        if tickets_root.is_dir():
            for dir_path in _walk_dirs(tickets_root):
                wd = self._inotify.add_watch(str(dir_path), _WATCH_FLAGS)
                self._wd_to_path[wd] = str(dir_path)

    def _watch_root(self) -> None:
        """(Re)register the TicketsRepository tree if not already watched."""
        if self._inotify is None or self.repo_path is None:
            return
        tickets_root = Path(self.repo_path) / "TicketsRepository"
        if not tickets_root.is_dir():
            return
        known = set(self._wd_to_path.values())
        for dir_path in _walk_dirs(tickets_root):
            if str(dir_path) not in known:
                wd = self._inotify.add_watch(str(dir_path), _WATCH_FLAGS)
                self._wd_to_path[wd] = str(dir_path)

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
