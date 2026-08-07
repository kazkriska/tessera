"""Env — environment resolution, merging and secret masking.

Responsibility (Master Part IX R.A.9; CONTRACTS §7): merge environment in the
order System base -> Workspace `.env` -> Ticket `.env` -> Manifest `env` ->
Event payload, and apply the secret DENYLIST (AWS_SECRET_ACCESS_KEY,
DATABASE_URL, SSH_AUTH_SOCK, SUDO_USER, ...) before handing the environment to
a subprocess.

The canonical merge lives in :func:`lib.ticket_management.runtime.executor.build_exec_env`;
this module adds the file-loading layer (``.env`` parsing) and a convenience
resolver that loads Ticket ``.env`` and Manifest ``env`` from a ticket root.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from lib.ticket_management.runtime.executor import build_exec_env

__all__ = ["load_dotenv", "resolve_ticket_env"]

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_dotenv(path: str | os.PathLike[str] | None) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` dotenv file into a plain dict.

    Supports ``export KEY=...`` prefixes, optional double/single quotes, and
    ``#`` comments. Values are unquoted; no shell interpolation is performed
    (deterministic env, no variable expansion).
    """
    if path is None:
        return {}
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def resolve_ticket_env(
    ticket_root: str | os.PathLike[str],
    base_env: dict[str, str] | None = None,
    event_env: dict[str, str] | None = None,
    permissions: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Resolve the execution env for a ticket using canonical merge order.

    Layers: System base -> Global ``Tickets/.env`` -> Ticket ``.env`` ->
    Manifest ``env`` -> Event payload. Manifest ``env.inherit: false``
    suppresses the global layer (RFC-0003 / RFC-0004: "Global ``Tickets/.env``
    → Ticket ``.env`` (later overrides). ``env.inherit:false`` disables
    global."). Secrets are injected only when ``permissions`` grants
    ``secrets_enabled`` (delegated to :func:`build_exec_env`).
    """
    root = Path(ticket_root)
    global_env: dict[str, str] = {}
    ticket_env = load_dotenv(root / ".env")

    manifest_env: dict[str, str] = {}
    inherit_global = True
    manifest_path = root / "MANIFEST.yaml"
    if manifest_path.is_file():
        try:
            from lib.ticket_management.runtime.manifest import load_manifest

            manifest = load_manifest(manifest_path)
            manifest_env = {
                str(k): str(v)
                for k, v in (manifest.env or {}).items()
                if str(k) != "inherit"
            }
            inherit_global = bool(
                (manifest.env or {}).get("inherit", True)
            )
        except Exception:  # noqa: BLE001 - malformed manifest => ignore env
            manifest_env = {}

    if inherit_global:
        # Global defaults live next to the repository's Tickets/ directory:
        # <repo>/TicketsRepository/.env  (RFC-0004 / Master index note).
        global_env = load_dotenv(root.parent / ".env")

    return build_exec_env(
        base_env=base_env,
        ticket_env={**global_env, **ticket_env},
        manifest_env=manifest_env,
        event_env=event_env,
        permissions=permissions,
    )
