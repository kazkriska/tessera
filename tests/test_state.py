"""Tests for the Lifecycle state machine (E1)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from lib.ticket_management.models import StateStatus, TicketState
from lib.ticket_management.runtime.state import (
    LEGAL_TRANSITIONS,
    TransitionError,
    emit_lifecycle_event,
    is_legal_transition,
    load_state,
    save_state,
    transition,
)


class FakeBus:
    """Minimal EventBus stand-in capturing published events."""

    class Event:
        def __init__(self, name, ticket_id=None, data=None):
            self.name = name
            self.ticket_id = ticket_id
            self.data = data or {}

    def __init__(self):
        self.events: list[FakeBus.Event] = []

    def publish(self, event):
        self.events.append(event)


def test_default_status_is_created():
    state = TicketState()
    assert state.status == StateStatus.CREATED


def test_legal_transitions_pass():
    result = transition("T-001", StateStatus.CREATED, StateStatus.INITIALIZED)
    assert result.allowed is True
    assert result.state is not None
    assert result.state.status == StateStatus.INITIALIZED

    # A full happy path chain.
    chain = [
        (StateStatus.CREATED, StateStatus.INITIALIZED),
        (StateStatus.INITIALIZED, StateStatus.READY),
        (StateStatus.READY, StateStatus.RUNNING),
        (StateStatus.RUNNING, StateStatus.COMPLETED),
        (StateStatus.COMPLETED, StateStatus.ARCHIVED),
    ]
    for src, dst in chain:
        assert is_legal_transition(src, dst)


def test_illegal_transition_raises():
    with pytest.raises(TransitionError):
        transition("T-001", StateStatus.CREATED, StateStatus.COMPLETED)
    with pytest.raises(TransitionError):
        transition("T-001", StateStatus.COMPLETED, StateStatus.RUNNING)
    with pytest.raises(TransitionError):
        transition("T-001", StateStatus.RUNNING, StateStatus.CREATED)


def test_archived_reinit_emits_reinitialized():
    bus = FakeBus()
    name = emit_lifecycle_event(bus, "T-001", StateStatus.ARCHIVED, StateStatus.INITIALIZED)
    assert name == "ticket.reinitialized"
    assert bus.events and bus.events[0].name == "ticket.reinitialized"


def test_noop_same_status():
    assert is_legal_transition(StateStatus.READY, StateStatus.READY)
    result = transition("T-001", StateStatus.READY, StateStatus.READY)
    assert result.allowed is True
    assert result.state.status == StateStatus.READY


def test_emit_lifecycle_event_publishes_correct_name():
    bus = FakeBus()
    name = emit_lifecycle_event(bus, "T-001", StateStatus.RUNNING, StateStatus.COMPLETED)
    assert name == "ticket.completed"
    assert bus.events[0].name == "ticket.completed"
    assert bus.events[0].ticket_id == "T-001"
    assert bus.events[0].data == {"from": "running", "to": "completed"}


def test_save_and_load_state_roundtrip(tmp_path: Path):
    state = TicketState(status=StateStatus.RUNNING).transition(
        StateStatus.BLOCKED, reason="waiting on dependency"
    )
    path = tmp_path / "state.json"
    save_state(path, state)
    loaded = load_state(path)
    assert loaded.status == StateStatus.BLOCKED
    assert loaded.reason == "waiting on dependency"
    assert loaded.previous_status == StateStatus.RUNNING


def test_transition_rejected_event_on_illegal_attempt():
    bus = FakeBus()
    name = emit_lifecycle_event(bus, "T-001", StateStatus.CREATED, StateStatus.COMPLETED)
    assert name is None
    assert bus.events and bus.events[0].name == "ticket.transition.rejected"


def test_load_state_missing_file_returns_default(tmp_path: Path):
    loaded = load_state(tmp_path / "does-not-exist.json")
    assert loaded.status == StateStatus.CREATED


def test_transition_table_covers_all_states():
    # Every state is a key in the table (or handled as a no-op).
    for status in StateStatus:
        assert status in LEGAL_TRANSITIONS
