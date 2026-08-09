"""Tessera SDK — programmatic control of the Tessera runtime.

Part XI (SDK Architecture) / RFC-0010. Two modes:

* :meth:`Runtime.direct` — operate straight on the filesystem (discovery,
  state transitions, manifest validation, offline edits). No daemon needed.
* :meth:`Runtime.connect` — talk to a live runtime over ``runtime.sock``
  (framed JSON lines). Raises :class:`RuntimeNotRunning` when the socket is
  absent.

The CLI (``src/tessera_runtime/cli.py``) wraps this same client — there is
no duplicated logic (Part XI §4.4).
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tessera_runtime.config import RuntimeConfig, load_config
from tessera_runtime.models import TicketMetadata, TicketState
from tessera_runtime.repo import REPO_DIR_NAME, RUNTIME_DIR_NAME, repo_init, rescan
from tessera_runtime.runtime.bus import Event, EventBus
from tessera_runtime.runtime.manifest import (
    ManifestValidationError,
    load_manifest,
)
from tessera_runtime.runtime.registry import Registry
from tessera_runtime.runtime.state import (
    TransitionError,
    emit_lifecycle_event,
    transition,
)

__all__ = [
    "Runtime",
    "Ticket",
    "RuntimeNotRunning",
    "SDKError",
    "find_repo_root",
]


class SDKError(Exception):
    """Base class for SDK failures (typed mirror of runtime validation)."""


class RuntimeNotRunning(SDKError):
    """Raised when connecting to a runtime that is not listening."""


@dataclass
class Ticket:
    """A single ticket as seen through the SDK (filesystem-backed view)."""

    runtime: "Runtime"
    metadata: TicketMetadata
    state: TicketState

    @property
    def id(self) -> str:
        return self.metadata.id

    @property
    def status(self) -> str:
        return self.state.status.value

    def transition(self, status: str, reason: str | None = None) -> "Ticket":
        """Request a lifecycle transition (validated per Part VI)."""
        self.runtime.transition(self.id, status, reason=reason)
        return self

    def invoke_action(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """Invoke a named manifest action."""
        return self.runtime.invoke_action(self.id, action, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.metadata.title,
            "type": self.metadata.type,
            "status": self.status,
            "updated_at": self.state.updated_at.isoformat(),
        }


class Runtime:
    """Client for the Tessera runtime (direct or attached)."""

    def __init__(
        self,
        repo: str | Path,
        sock: str | Path | None = None,
        config: RuntimeConfig | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.sock = Path(sock) if sock is not None else None
        self.config = config or RuntimeConfig()
        self.bus = bus or EventBus()
        self._sock_fd: socket.socket | None = None

    # ------------------------------------------------------------------ #
    # Constructors
    # ------------------------------------------------------------------ #
    @classmethod
    def direct(
        cls,
        repo: str | Path | None = None,
        config: RuntimeConfig | None = None,
    ) -> "Runtime":
        """Operate directly on the filesystem (no daemon required)."""
        repo = find_repo_root(repo)
        return cls(repo=repo, config=config)

    @classmethod
    def connect(
        cls,
        sock: str | Path | None = None,
        repo: str | Path | None = None,
    ) -> "Runtime":
        """Attach to a live runtime over ``runtime.sock`` (Part XI §4.3)."""
        sock_path: Path
        if sock is not None:
            sock_path = Path(sock)
        else:
            root = Path(repo).resolve() if repo is not None else find_repo_root(None)
            from tessera_runtime.runtime.server import runtime_socket_path

            sock_path = runtime_socket_path(root)
        if not sock_path.exists():
            raise RuntimeNotRunning(
                f"runtime socket not found at {sock_path}; "
                "start the runtime or use Runtime.direct()"
            )
        if repo is None:
            # The socket lives under $XDG_RUNTIME_DIR/tessera/<hash>/, so its
            # path no longer reveals the repo root. Ask the running server for
            # its root via the `status` RPC instead of guessing from the path.
            try:
                probe = cls(sock=sock_path)
                status = probe._rpc("status")
                root = Path(status["root"])
                probe.close()
            except Exception:  # noqa: BLE001
                root = find_repo_root(None)
        else:
            root = Path(repo).resolve()
        return cls(repo=root, sock=sock_path)

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a framed JSON request to the attached runtime."""
        if self.sock is None:
            raise SDKError("Runtime is in direct mode; attach with Runtime.connect()")
        if self._sock_fd is None:
            self._sock_fd = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock_fd.connect(str(self.sock))
        payload = json.dumps({"method": method, "params": params or {}}) + "\n"
        self._sock_fd.sendall(payload.encode("utf-8"))
        line = b""
        while not line.endswith(b"\n"):
            chunk = self._sock_fd.recv(4096)
            if not chunk:
                raise SDKError("runtime closed the connection")
            line += chunk
        response = json.loads(line.decode("utf-8"))
        if response.get("error"):
            raise SDKError(str(response["error"]))
        return response.get("result")

    def close(self) -> None:
        if self._sock_fd is not None:
            self._sock_fd.close()
            self._sock_fd = None

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Operations (direct mode reads the filesystem; attach mode RPCs)
    # ------------------------------------------------------------------ #
    def discover(self) -> list[dict[str, Any]]:
        """List registered tickets (direct mode rescans the filesystem first)."""
        if self.sock is not None:
            return self._rpc("discover")
        registry = Registry(str(self.repo))
        try:
            registry.rebuild(str(self.repo))
            return [
                {
                    "id": row["id"],
                    "status": row.get("state") or "created",
                }
                for row in registry.list_all()
            ]
        finally:
            registry.close()

    def get_ticket(self, ticket_id: str) -> Ticket:
        """Fetch one ticket (metadata + current state)."""
        ticket_dir = self._ticket_dir(ticket_id)
        if not ticket_dir.is_dir():
            raise SDKError(f"ticket {ticket_id!r} not found in {self.repo}")
        metadata = TicketMetadata.load(ticket_dir / "metadata.json")
        state = TicketState.load(ticket_dir / "state.json")
        return Ticket(runtime=self, metadata=metadata, state=state)

    def _ticket_dir(self, ticket_id: str) -> Path:
        """Path of a ticket directory under the canonical repository."""
        return self.repo / REPO_DIR_NAME / f"{ticket_id}.ticket"

    def transition(
        self, ticket_id: str, status: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Request a lifecycle transition (validated per Part VI / CONTRACTS §1)."""
        if self.sock is not None:
            return self._rpc(
                "transition",
                {"ticket_id": ticket_id, "status": status, "reason": reason},
            )
        ticket_dir = self._ticket_dir(ticket_id)
        state_path = ticket_dir / "state.json"
        activity_path = ticket_dir / "activity.jsonl"
        current = TicketState.load(state_path)
        try:
            result = transition(
                ticket_id=ticket_id,
                from_status=current.status.value,
                to_status=status,
                registry=None,
                activity_path=activity_path,
            )
        except TransitionError as exc:
            raise SDKError(f"transition rejected: {exc}") from exc
        emit_lifecycle_event(
            self.bus, ticket_id, current.status.value, result.to_status.value
        )
        save = TicketState(
            status=result.to_status,
            updated_at=datetime.now(timezone.utc),
            previous_status=current.status,
            reason=reason,
        )
        save.write(state_path)
        return {"ticket_id": ticket_id, "to_status": result.to_status.value}

    def invoke_action(
        self, ticket_id: str, action: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Invoke a named manifest action (emits event, may prompt)."""
        if self.sock is not None:
            params = {"ticket_id": ticket_id, "action": action, **kwargs}
            return self._rpc("invoke_action", params)
        ticket_dir = self._ticket_dir(ticket_id)
        manifest_path = ticket_dir / "MANIFEST.yaml"
        if not manifest_path.is_file():
            raise SDKError(f"ticket {ticket_id!r} has no MANIFEST.yaml")
        manifest = load_manifest(manifest_path, ticket_id=ticket_id)
        descriptor = manifest.actions.get(action)
        if descriptor is None:
            raise SDKError(
                f"action {action!r} not declared in MANIFEST.yaml for {ticket_id}"
            )
        from tessera_runtime.runtime.dispatcher import RunnerDescriptor
        from tessera_runtime.runtime.executor import run_hook

        runner = RunnerDescriptor(
            path=descriptor.run,
            shell=descriptor.shell,
            timeout=descriptor.timeout,
            retry=descriptor.retry or 0,
            **{"async": descriptor.is_async},
        )
        result = run_hook(
            descriptor=runner,
            ticket_root=ticket_dir,
            config=self.config,
            permissions=manifest.permissions,
        )
        return {
            "ticket_id": ticket_id,
            "action": action,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def emit(
        self,
        event_name: str,
        ticket_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Publish a domain event on the runtime bus."""
        if self.sock is not None:
            self._rpc("emit", {"event_name": event_name, "ticket_id": ticket_id, "data": data})
            return
        self.bus.publish(Event(name=event_name, ticket_id=ticket_id, data=data))

    def subscribe(self, event_names: list[str] | None = None) -> Iterator[Event]:
        """Iterate matching events (attach mode streams; direct mode replays
        events published on this client's bus)."""
        if self.sock is not None:
            # v1: direct subscription over the socket is deferred; return an
            # empty iterator so callers degrade gracefully.
            return iter(())
        return self._bus_iter(event_names)

    def _bus_iter(self, event_names: list[str] | None) -> Iterator[Event]:
        queue: list[Event] = []

        def handler(event: Event) -> None:
            if event_names is None or event.name in event_names:
                queue.append(event)

        self.bus.subscribe(handler)
        while True:
            if queue:
                yield queue.pop(0)
            else:
                import time

                time.sleep(0.01)


def find_repo_root(start: str | Path | None = None) -> Path:
    """Locate the framework root (directory containing ``TicketsRepository``)."""
    from tessera_runtime.repo import get_repo_root

    return get_repo_root(start)
