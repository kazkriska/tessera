"""Runtime socket location (dev/v1).

A Unix domain socket path is capped at ~107 bytes on Linux
(``sockaddr_un.sun_path``). A repo-rooted path such as
``<repo>/TicketsRepository/.ticket-runtime/runtime.sock`` blows past that
limit for any but the shallowest repos (e.g. a workspace under
``~/Desktop/Workspaces/...``), and the ``bind()`` then fails with
``OSError: AF_UNIX path too long`` — which is exactly how runtime state ends
up scattered/dislocated.

Correct, portable location: ``$XDG_RUNTIME_DIR/tessera/<repo-hash>/runtime.sock``.

* Short and stable (``/run/user/<uid>/`` is ~14 chars) → always within the
  OS limit, whatever the repo depth.
* Ephemeral (Invariant I-2: runtime state is disposable) — lives in the
  per-user runtime dir, not in ``$HOME`` and not inside the repo tree.
* Per-repo: a stable hash of the resolved repo path namespaces one socket
  per repository, preserving the singleton-per-repo invariant (I-6).

The durable runtime state (``registry.db``, logs, locks) still lives in
``<repo>/TicketsRepository/.ticket-runtime/`` per CONTRACTS §7.1; only the
socket — which must be short — is relocated here.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

SOCKET_NAME = "runtime.sock"


def _repo_hash(repo: str | Path) -> str:
    """Stable, filesystem-safe hash of the resolved repo path."""
    digest = hashlib.sha256(str(Path(repo).resolve()).encode("utf-8")).hexdigest()
    return digest[:16]


def runtime_dir(repo: str | Path) -> Path:
    """Directory holding this repo's socket under ``XDG_RUNTIME_DIR``."""
    base = os.environ.get("XDG_RUNTIME_DIR") or (Path.home() / ".cache" / "tessera")
    return Path(base) / "tessera" / _repo_hash(repo)


def runtime_socket_path(repo: str | Path) -> Path:
    """Absolute path of the runtime socket (short, per Invariant I-2)."""
    d = runtime_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    return d / SOCKET_NAME
