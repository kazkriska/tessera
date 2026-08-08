"""Tests for the Event Bus + Watcher (C1)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from lib.ticket_management.config import RuntimeConfig
from lib.ticket_management.runtime.bus import Event, EventBus
from lib.ticket_management.runtime.watcher import FsWatcher


# --------------------------------------------------------------------------- #
# Bus tests
# --------------------------------------------------------------------------- #
def test_bus_publish_delivers_to_subscriber():
    bus = EventBus()
    received: list[Event] = []

    bus.subscribe(handler=received.append, ticket_id="T-1", event_names=["metadata.updated"])
    bus.publish(Event(name="metadata.updated", ticket_id="T-1", data={"path": "m.json"}))

    assert len(received) == 1
    assert received[0].ticket_id == "T-1"
    assert received[0].name == "metadata.updated"


def test_bus_wildcard_subscriber_receives_all():
    bus = EventBus()
    received: list[Event] = []

    bus.subscribe(handler=received.append)  # no filters
    bus.publish(Event(name="a.b", ticket_id="T-1"))
    bus.publish(Event(name="c.d", ticket_id="T-2"))

    assert len(received) == 2
    assert received[0].name == "a.b"
    assert received[1].ticket_id == "T-2"


def test_bus_ticket_scoped_subscriber_filters():
    bus = EventBus()
    received: list[Event] = []

    bus.subscribe(handler=received.append, ticket_id="T-1")
    bus.publish(Event(name="x", ticket_id="T-1"))
    bus.publish(Event(name="x", ticket_id="T-2"))

    assert len(received) == 1
    assert received[0].ticket_id == "T-1"


def test_bus_recursion_guard_halts_at_max_depth():
    bus = EventBus(recursion_max_depth=3)
    received: list[Event] = []

    def handler(ev: Event):
        received.append(ev)
        if ev.name == "reemit":
            bus.publish(Event(name="reemit", ticket_id=ev.ticket_id))

    bus.subscribe(handler=handler, event_names=["reemit"])
    bus.publish(Event(name="reemit", ticket_id="T-1"))

    assert len(received) == 3


def test_bus_subscriber_exception_does_not_bring_down_bus():
    bus = EventBus()
    received: list[Event] = []

    def bad(ev: Event):
        raise RuntimeError("boom")

    def good(ev: Event):
        received.append(ev)

    bus.subscribe(handler=bad, event_names=["x"])
    bus.subscribe(handler=good, event_names=["x"])
    bus.publish(Event(name="x"))

    assert len(received) == 1


# --------------------------------------------------------------------------- #
# Watcher tests
# --------------------------------------------------------------------------- #
def test_watcher_maps_metadata_json_to_metadata_updated():
    watcher = FsWatcher()
    assert watcher._trigger_for_path(Path("HQ_BR-001.ticket/metadata.json")) == "metadata.updated"
    assert watcher._trigger_for_path(Path("HQ_BR-001.ticket/MANIFEST.yaml")) == "manifest.updated"
    assert watcher._trigger_for_path(Path("HQ_BR-001.ticket/state.json")) == "state.updated"
    assert watcher._trigger_for_path(Path("HQ_BR-001.ticket/activity.jsonl")) == "activity.updated"
    assert watcher._trigger_for_path(Path("HQ_BR-001.ticket/scripts/foo.py")) == "fs.changed"


def test_watcher_skips_non_ticket_paths():
    watcher = FsWatcher()
    assert watcher._owning_ticket_id(Path("README.md")) is None
    assert watcher._owning_ticket_id(Path("lib/foo.py")) is None
    assert watcher._owning_ticket_id(Path("TicketsRepository/HQ_BR-001.ticket/scripts/foo.py")) == "HQ_BR-001"


def test_watcher_debounce_coalesces_rapid_events():
    watcher = FsWatcher()
    watcher._debounce_window = 0.05
    events: list[Event] = []

    for _ in range(5):
        watcher._on_fs_event("TicketsRepository/HQ_BR-001.ticket/metadata.json", "modify", events.append)
    time.sleep(0.12)

    assert len(events) <= 1


def test_watcher_inotify_loop_emits_domain_event(tmp_path: Path):
    """Real inotify read loop publishes a domain event on file change."""
    import threading
    import time as _time

    from lib.ticket_management.config import RuntimeConfig

    tickets = tmp_path / "TicketsRepository"
    tickets.mkdir()
    (tickets / "HQ_BR-001.ticket").mkdir()

    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(handler=received.append)

    w = FsWatcher()
    w.watch(str(tmp_path), bus, RuntimeConfig())
    w.start(block=False)

    # Touch a watched file; the watcher should translate it to a domain event.
    (tickets / "HQ_BR-001.ticket" / "metadata.json").write_text('{"id": "HQ_BR-001"}')

    try:
        for _ in range(50):  # up to ~2.5s
            if any(e.ticket_id == "HQ_BR-001" and e.name == "metadata.updated" for e in received):
                break
            _time.sleep(0.05)
    finally:
        w.stop()

    assert any(e.ticket_id == "HQ_BR-001" and e.name == "metadata.updated" for e in received)


def test_watcher_start_requires_watch():
    w = FsWatcher()
    try:
        w.start(block=False)
    except RuntimeError as exc:
        assert "watch()" in str(exc)
    else:
        raise AssertionError("expected RuntimeError before watch()")


def test_watcher_survives_inotify_read_error(tmp_path: Path):
    """A read error must be logged and retried, never kill the loop."""
    import threading
    import time as _time

    from lib.ticket_management.config import RuntimeConfig

    tickets = tmp_path / "TicketsRepository"
    tickets.mkdir()
    (tickets / "HQ_BR-001.ticket").mkdir()

    bus = EventBus()
    w = FsWatcher()
    w.watch(str(tmp_path), bus, RuntimeConfig())
    w.start(block=False)
    _time.sleep(0.1)

    thread_errors: list = []
    threading.excepthook = lambda args: thread_errors.append(args)

    # Force a read error by closing the inotify fd underneath the loop.
    w._inotify.close()  # noqa: SLF001
    _time.sleep(0.4)
    w.stop()

    assert thread_errors == [], f"watcher thread crashed: {thread_errors}"


def test_watcher_watches_nested_ticket_dirs(tmp_path: Path):
    """Events inside nested ticket/asset directories must fire (recursion)."""
    import time as _time

    from lib.ticket_management.config import RuntimeConfig

    tickets = tmp_path / "TicketsRepository"
    nested = tickets / "HQ_BR-001.ticket" / "task" / "assets"
    nested.mkdir(parents=True)

    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(handler=received.append)

    w = FsWatcher()
    w.watch(str(tmp_path), bus, RuntimeConfig())
    w.start(block=False)

    (nested / "asset.txt").write_text("x")

    try:
        for _ in range(50):
            if any(e.ticket_id == "HQ_BR-001" and e.name == "fs.changed" for e in received):
                break
            _time.sleep(0.05)
    finally:
        w.stop()

    assert any(
        e.ticket_id == "HQ_BR-001" and e.name == "fs.changed"
        and str(e.data.get("path", "")).endswith("asset.txt")
        for e in received
    )


def test_watch_rules_fire_declared_trigger(tmp_path: Path):
    """CMP-10: a manifest watch rule fires its declared trigger."""
    ticket_root = tmp_path / "TicketsRepository" / "HQ_BR-001.ticket"
    assets = ticket_root / "task" / "assets"
    assets.mkdir(parents=True)
    (ticket_root / "MANIFEST.yaml").write_text(
        "apiVersion: ticket/v1\n"
        "kind: Ticket\n"
        "metadata:\n"
        "  id: HQ_BR-001\n"
        "  title: Watch rule test\n"
        "  type: task\n"
        "watch:\n"
        "  - path: task/assets/**\n"
        "    trigger: asset_indexed\n",
        encoding="utf-8",
    )
    (assets / "logo.png").write_text("PNG", encoding="utf-8")

    received: list[Event] = []
    w = FsWatcher()
    w.watch(str(tmp_path), EventBus(), RuntimeConfig())
    w._on_fs_event(str(assets / "logo.png"), "modify", received.append)
    w._flush(received.append)

    names = {e.name for e in received if e.ticket_id == "HQ_BR-001"}
    assert "asset_indexed" in names, f"declared trigger missing: {names}"


def test_watch_rules_cache_refreshed_on_manifest_change(tmp_path: Path):
    """CMP-10: editing MANIFEST.yaml invalidates the cached watch rules."""
    ticket_root = tmp_path / "TicketsRepository" / "HQ_BR-002.ticket"
    ticket_root.mkdir(parents=True)
    (ticket_root / "MANIFEST.yaml").write_text(
        "apiVersion: ticket/v1\n"
        "kind: Ticket\n"
        "metadata:\n"
        "  id: HQ_BR-002\n"
        "  title: Cache test\n"
        "  type: task\n",
        encoding="utf-8",
    )

    w = FsWatcher()
    w.watch(str(tmp_path), EventBus(), RuntimeConfig())

    assert w._manifest_rules("HQ_BR-002") == []

    # Add a rule and touch MANIFEST.yaml — the cache must reload.
    (ticket_root / "MANIFEST.yaml").write_text(
        "apiVersion: ticket/v1\n"
        "kind: Ticket\n"
        "metadata:\n"
        "  id: HQ_BR-002\n"
        "  title: Cache test\n"
        "  type: task\n"
        "watch:\n"
        "  - path: data/**\n"
        "    trigger: data_updated\n",
        encoding="utf-8",
    )
    w._on_fs_event(str(ticket_root / "MANIFEST.yaml"), "modify", lambda e: None)

    rules = w._manifest_rules("HQ_BR-002")
    assert len(rules) == 1
    assert rules[0]["trigger"] == "data_updated"
