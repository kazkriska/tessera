"""Lifecycle state machine for Tessera v1.

Responsibility (Master Part VI, RFC-0007; CONTRACTS.md §1): own the legal
state-transition table, apply transitions to :class:`TicketState`, emit
``ticket.<status>`` lifecycle events on the Event Bus, and persist
``state.json`` atomically.

The canonical 9-state enum and the ``TicketState`` document model live in
:mod:`lib.ticket_management.models` (CONTRACTS.md §1). This module adds the
*behavior*: the transition table, validation, event emission, and durable
load/save helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.ticket_management.models import StateStatus, TicketState, atomic_write_json, read_json

__all__ = [
    "StateStatus",
    "TicketState",
    "TransitionError",
    "TransitionResult",
    "LEGAL_TRANSITIONS",
    "is_legal_transition",
    "transition",
    "emit_lifecycle_event",
    "load_state",
    "save_state",
]


class TransitionError(Exception):
    """Raised when a lifecycle transition is not permitted by the table."""


@dataclass
class TransitionResult:
    """Outcome of a requested lifecycle transition."""

    ticket_id: str
    from_status: StateStatus
    to_status: StateStatus
    allowed: bool
    state: TicketState | None = None

    @property
    def event_name(self) -> str:
        """Name of the event that would be (or was) emitted."""
        if self.from_status == StateStatus.ARCHIVED and self.to_status == StateStatus.INITIALIZED:
            return "ticket.reinitialized"
        return f"ticket.{self.to_status.value}"


# --------------------------------------------------------------------------- #
# Transition table (CONTRACTS.md §1 / Part VI §4.2)
# --------------------------------------------------------------------------- #
#: Legal ``from -> set(to)`` transitions. A transition to the same status is
#: always a legal no-op and is handled explicitly in :func:`is_legal_transition`.
LEGAL_TRANSITIONS: dict[StateStatus, set[StateStatus]] = {
    StateStatus.CREATED: {StateStatus.INITIALIZED},
    StateStatus.INITIALIZED: {StateStatus.READY},
    StateStatus.READY: {StateStatus.RUNNING, StateStatus.DELEGATED, StateStatus.BLOCKED},
    StateStatus.BLOCKED: {StateStatus.READY, StateStatus.RUNNING},
    StateStatus.RUNNING: {StateStatus.BLOCKED, StateStatus.DELEGATED, StateStatus.COMPLETED, StateStatus.FAILED},
    StateStatus.DELEGATED: {StateStatus.BLOCKED, StateStatus.READY, StateStatus.COMPLETED},
    StateStatus.FAILED: {StateStatus.READY, StateStatus.INITIALIZED, StateStatus.ARCHIVED},
    StateStatus.COMPLETED: {StateStatus.ARCHIVED},
    StateStatus.ARCHIVED: {StateStatus.INITIALIZED},  # renew
}


def is_legal_transition(
    from_status: StateStatus | str, to_status: StateStatus | str
) -> bool:
    """Return whether ``from -> to`` is legal per the canonical table.

    A transition to the same status is always legal (a no-op).
    """
    src = StateStatus(from_status)
    dst = StateStatus(to_status)
    if src == dst:
        return True
    return dst in LEGAL_TRANSITIONS.get(src, set())


# --------------------------------------------------------------------------- #
# Transition application
# --------------------------------------------------------------------------- #
def transition(
    ticket_id: str,
    from_status: StateStatus | str,
    to_status: StateStatus | str,
    registry: Any = None,
    reason: str | None = None,
) -> TransitionResult:
    """Validate and apply a lifecycle transition.

    Returns a :class:`TransitionResult`. Raises :class:`TransitionError` when
    the transition is illegal. ``registry`` is accepted for API compatibility
    (the scheduler may pass a Registry for dependency checks); the v1 table
    is static and does not consult it.
    """
    src = StateStatus(from_status)
    dst = StateStatus(to_status)

    if src != dst and dst not in LEGAL_TRANSITIONS.get(src, set()):
        raise TransitionError(
            f"illegal transition for ticket {ticket_id!r}: "
            f"{src.value} -> {dst.value}"
        )

    state = TicketState(status=src).transition(dst, reason=reason)
    return TransitionResult(
        ticket_id=ticket_id,
        from_status=src,
        to_status=dst,
        allowed=True,
        state=state,
    )


# --------------------------------------------------------------------------- #
# Event emission (Part VII)
# --------------------------------------------------------------------------- #
def emit_lifecycle_event(
    bus: Any,
    ticket_id: str,
    from_status: StateStatus | str,
    to_status: StateStatus | str,
) -> str | None:
    """Publish the lifecycle event for a (validated) transition.

    Emits ``ticket.<to_status>`` for ordinary transitions, and
    ``ticket.reinitialized`` for ``archived -> initializing`` (renew).

    Returns the event name published, or ``None`` when the transition is
    illegal (in which case ``ticket.transition.rejected`` is published and
    :class:`TransitionError` is NOT raised here — callers that want strict
    validation should call :func:`transition` first).
    """
    src = StateStatus(from_status)
    dst = StateStatus(to_status)

    if src != dst and dst not in LEGAL_TRANSITIONS.get(src, set()):
        if bus is not None:
            bus.publish(
                bus.Event(
                    name="ticket.transition.rejected",
                    ticket_id=ticket_id,
                    data={"from": src.value, "to": dst.value},
                )
            )
        return None

    if src == StateStatus.ARCHIVED and dst == StateStatus.INITIALIZED:
        event_name = "ticket.reinitialized"
    else:
        event_name = f"ticket.{dst.value}"

    if bus is not None:
        bus.publish(
            bus.Event(
                name=event_name,
                ticket_id=ticket_id,
                data={"from": src.value, "to": dst.value},
            )
        )
    return event_name


# --------------------------------------------------------------------------- #
# Persistence (atomic state.json)
# --------------------------------------------------------------------------- #
def load_state(path: str | Path) -> TicketState:
    """Load a ``state.json`` document into a :class:`TicketState`.

    A missing file yields the default state (``created``), matching
    Part IV §8: the runtime re-initializes default state when absent.
    """
    target = Path(path)
    if not target.is_file():
        return TicketState()
    data = read_json(target)
    if not isinstance(data, dict):
        return TicketState()
    return TicketState.from_dict(data)


def save_state(path: str | Path, state: TicketState) -> None:
    """Atomically persist a :class:`TicketState` to ``state.json``.

    Uses the canonical write-temp + ``os.replace`` protocol
    (CONTRACTS.md §7 / Part IV R.A.4).
    """
    if not isinstance(state, TicketState):
        raise TypeError("save_state expects a TicketState")
    atomic_write_json(Path(path), state.to_dict())
