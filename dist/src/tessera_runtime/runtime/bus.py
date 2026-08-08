"""In-process Event Bus for Tessera v1 (Part VII R.A.7).

Provides an in-memory pub/sub with:
- ticket-scoped and wildcard subscriptions
- event-name filtering per subscription
- depth-tracking recursion guard
- thread-safe subscriber mutation
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

__all__ = ["Event", "EventBus"]


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Event
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    """An immutable domain event circulated on the bus."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    ticket_id: str | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# EventBus
# --------------------------------------------------------------------------- #
class EventBus:
    """Thread-safe in-process pub/sub.

    Subscribers are matched by ``ticket_id`` (``None`` = wildcard) and
    ``event_names`` (``None`` = all events).  ``publish`` fans out to
    matching subscribers synchronously but is tracked by a depth counter
    so recursive re-emission is bounded by ``recursion_max_depth``.
    """

    def __init__(
        self,
        recursion_max_depth: int = 10,
        activity_sink: Callable[[dict], None] | None = None,
    ) -> None:
        self._recursion_max_depth = recursion_max_depth
        self._activity_sink = activity_sink
        self._lock = threading.Lock()
        # Each subscriber: {"ticket_id": str|None, "event_names": list[str]|None, "handler": Callable}
        self._subscribers: list[dict] = []
        # Depth counter for recursion guard (Part VII R.A.7)
        self._event_depth: int = 0

    # ------------------------------------------------------------------ #
    # Subscription
    # ------------------------------------------------------------------ #
    def subscribe(
        self,
        handler: Callable[[Event], None],
        ticket_id: str | None = None,
        event_names: list[str] | None = None,
    ) -> Callable[[Event], None]:
        """Register *handler* and return it so callers can hold a reference.

        * ``ticket_id=None``  → wildcard (receives every event).
        * ``event_names=None`` → all events (no name filtering).
        """
        entry = {
            "ticket_id": ticket_id,
            "event_names": list(event_names) if event_names is not None else None,
            "handler": handler,
        }
        with self._lock:
            self._subscribers.append(entry)
        return handler

    def _matches(self, entry: dict, event: Event) -> bool:
        """Return True if *entry* should receive *event*."""
        # ticket_id filter: None means wildcard
        if entry["ticket_id"] is not None and entry["ticket_id"] != event.ticket_id:
            return False
        # event_names filter: None means all
        if entry["event_names"] is not None and event.name not in entry["event_names"]:
            return False
        return True

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #
    def publish(self, event: Event) -> None:
        """Fan out *event* to all matching subscribers (fire-and-forget).

        Tracks recursion depth. If the guard is tripped, an emergency alert
        is logged and the bus halts (no further fan-out for this event).
        """
        if self._event_depth >= self._recursion_max_depth:
            logger.critical(
                "event_bus: recursion guard tripped at depth %d "
                "(max=%d); halting further event propagation",
                self._event_depth,
                self._recursion_max_depth,
            )
            return

        self._event_depth += 1
        try:
            # Snapshot under lock to avoid mutation during iteration
            with self._lock:
                snapshot = [e for e in self._subscribers if self._matches(e, event)]

            for entry in snapshot:
                try:
                    entry["handler"](event)
                except Exception:
                    logger.exception(
                        "event_bus: subscriber handler raised for event %r", event.id
                    )
        finally:
            self._event_depth -= 1

    # ------------------------------------------------------------------ #
    # Diagnostics / test helpers
    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        """Remove all subscribers (test helper)."""
        with self._lock:
            self._subscribers.clear()
            self._event_depth = 0
