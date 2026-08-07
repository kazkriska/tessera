"""Runtime server — Unix domain socket daemon.

Part II §4.3 (singleton runtime) / Part X §4.2 (invocation model): boot the
full :class:`Pipeline` and serve framed JSON requests over
``runtime.sock``. The SDK and CLI attach to this socket.

Protocol: newline-delimited JSON. Request::

    {"method": "discover" | "get_ticket" | "transition" | "invoke_action"
              | "emit" | "status" | "shutdown",
     "params": {...}}

Response::

    {"result": ...} | {"error": "..."}

``runtime start`` must be a singleton (Invariant I-6): if the socket already
exists the server refuses to boot a competing watcher.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any

from lib.ticket_management.config import RuntimeConfig
from lib.ticket_management.repo import RUNTIME_DIR_NAME, repo_init
from lib.ticket_management.runtime.pipeline import Pipeline

logger = logging.getLogger(__name__)

SOCKET_NAME = "runtime.sock"


def runtime_socket_path(root: str | Path) -> Path:
    """Absolute path of the runtime socket under ``.ticket-runtime/``."""
    repo = repo_init(root)
    return Path(repo) / RUNTIME_DIR_NAME / SOCKET_NAME


class RuntimeServer:
    """Framed-JSON server backed by a live :class:`Pipeline`."""

    def __init__(self, root: str | Path, config: RuntimeConfig | None = None) -> None:
        self.root = Path(root).resolve()
        self.config = config or RuntimeConfig()
        self.pipeline: Pipeline | None = None
        self.sock_path: Path | None = None
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> Path:
        """Boot the pipeline and begin serving on ``runtime.sock``."""
        sock_path = runtime_socket_path(self.root)
        if sock_path.exists():
            raise RuntimeError(
                f"runtime already running at {sock_path} (singleton, Invariant I-6); "
                "use 'tessera runtime status' or 'tessera runtime stop'"
            )
        sock_path.parent.mkdir(parents=True, exist_ok=True)

        self.pipeline = Pipeline(root=self.root, config=self.config)
        self.pipeline.start()

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(16)
        self._server = server
        self.sock_path = sock_path

        self._thread = threading.Thread(
            target=self._accept_loop, name="tessera-runtime-server", daemon=True
        )
        self._thread.start()
        logger.info("runtime server listening at %s", sock_path)
        return sock_path

    def stop(self) -> None:
        """Graceful shutdown: stop accepting, close socket, stop pipeline."""
        self._stop.set()
        if self._server is not None:
            try:
                self._server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._server.close()
            self._server = None
        if self.sock_path is not None and self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except FileNotFoundError:
                pass
        if self.pipeline is not None:
            self.pipeline.stop()
        if self._thread is not None:
            self._thread.join(timeout=2)
        logger.info("runtime server stopped")

    # ------------------------------------------------------------------ #
    # Accept loop
    # ------------------------------------------------------------------ #
    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                break
            handler = threading.Thread(
                target=self._handle, args=(conn,), daemon=True
            )
            handler.start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            buf = b""
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        request = json.loads(line.decode("utf-8"))
                        result = self._dispatch(request)
                        response = {"result": result}
                    except Exception as exc:  # noqa: BLE001
                        response = {"error": str(exc)}
                    try:
                        conn.sendall(
                            (json.dumps(response) + "\n").encode("utf-8")
                        )
                    except OSError:
                        return

    def _dispatch(self, request: dict[str, Any]) -> Any:
        method = request.get("method")
        params = request.get("params") or {}
        if method == "status":
            return {"running": True, "root": str(self.root)}
        if method == "discover":
            if self.pipeline is None:
                raise RuntimeError("runtime not initialized")
            # Registry connections are thread-bound (sqlite3); the pipeline's
            # registry lives in the main thread, so open a fresh one here.
            from lib.ticket_management.runtime.registry import Registry

            registry = Registry(str(self.root))
            try:
                registry.rebuild(str(self.root))
                return [
                    {
                        "id": row["id"],
                        "status": row.get("state") or "created",
                    }
                    for row in registry.list_all()
                ]
            finally:
                registry.close()
        if method == "transition":
            if self.pipeline is None:
                raise RuntimeError("runtime not initialized")
            from lib.ticket_management.runtime.state import TransitionError, transition

            ticket_id = params.get("ticket_id")
            status = params.get("status")
            ticket_dir = self.root / "TicketsRepository" / f"{ticket_id}.ticket"
            state_path = ticket_dir / "state.json"
            activity_path = ticket_dir / "activity.jsonl"
            current = _load_state(state_path)
            try:
                result = transition(
                    ticket_id=ticket_id,
                    from_status=current,
                    to_status=status,
                    registry=None,
                    reason=params.get("reason"),
                    activity_path=activity_path,
                )
            except TransitionError as exc:
                raise RuntimeError(f"transition rejected: {exc}") from exc
            _write_state(state_path, result.state)
            return {"ticket_id": ticket_id, "to_status": result.to_status.value}
        if method == "invoke_action":
            raise NotImplementedError(
                "invoke_action over the socket lands with the H1 hardening pass"
            )
        if method == "emit":
            if self.pipeline is None or self.pipeline.bus is None:
                raise RuntimeError("runtime not initialized")
            from lib.ticket_management.runtime.bus import Event

            self.pipeline.bus.publish(
                Event(
                    name=params.get("event_name", ""),
                    ticket_id=params.get("ticket_id"),
                    data=params.get("data"),
                )
            )
            return {"published": params.get("event_name")}
        if method == "shutdown":
            self.stop()
            return {"stopped": True}
        raise RuntimeError(f"unknown method {method!r}")


def _load_state(path: Path) -> str:
    """Return the current status string of a ticket state file."""
    from lib.ticket_management.models import TicketState

    if not path.is_file():
        return "created"
    try:
        return TicketState.load(path).status.value
    except Exception:  # noqa: BLE001
        return "created"


def _write_state(path: Path, state: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state.write(path)


def serve(root: str | Path, config: RuntimeConfig | None = None) -> RuntimeServer:
    """Convenience factory used by the CLI ``runtime start`` command."""
    server = RuntimeServer(root, config)
    server.start()
    return server
