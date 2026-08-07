"""Permissions — capability model, escalation, and enforcement.

Responsibility (Master Part IX, RFC-0008; CONTRACTS.md §4): constrain what a
hook or action may do. Capabilities are declarative strings declared in the
MANIFEST ``permissions:`` block; every hook/action runs with the ticket's
``PermissionSet`` and any capability not granted is denied at runtime.

Capability grammar (Part IX §4.2):

* ``fs.read:<path>`` / ``fs.write:<path>`` — path-scoped filesystem access,
  jail root = ticket root
* ``net.http:<host>`` — outbound HTTP to a specific host
* ``secrets`` — allow secret env injection
* ``run:<shell>`` — allow running hooks with that shell
* ``exec.bg`` — allow background/asynchronous execution

Escalation (Part IX §4.3) requires an explicit static grant in the manifest
(``permissions.escalations.<target>``). Enforcement (Part IX §4.4) raises
:class:`PermissionError` on any capability that is not granted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "PermissionSet",
    "denylist_reject",
    "effective_permissions",
    "enforce",
    "escalate",
]

#: Capabilities that are exact-match only (no colon scope).
_ATOMIC_CAPS = frozenset({"secrets", "exec.bg"})

#: Capability families with a colon-scoped path/host argument.
_SCOPED_FAMILIES = ("fs.read", "fs.write", "net.http", "run")


def denylist_reject(capability: str) -> None:
    """Reject path-traversal capabilities at construction (Part IX R.A.9)."""
    if not isinstance(capability, str) or not capability.strip():
        raise ValueError(f"invalid capability: {capability!r}")
    for family in _SCOPED_FAMILIES:
        if capability.startswith(family + ":"):
            _, scope = capability.split(":", 1)
            segments = scope.replace("\\", "/").split("/")
            if ".." in segments:
                raise ValueError(
                    f"capability {capability!r} contains a '..' path segment "
                    "(denylist guard, Part IX R.A.9)"
                )
            return
    if capability not in _ATOMIC_CAPS:
        raise ValueError(
            f"unknown capability {capability!r}; expected one of "
            f"{sorted(_ATOMIC_CAPS)} or a scoped {list(_SCOPED_FAMILIES)}:<arg>"
        )


def _parse_capabilities(raw: Any) -> list[str]:
    """Accept a list of strings, a mapping (cap -> enabled), or ``None``."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, dict):
        return [cap for cap, enabled in raw.items() if enabled]
    if isinstance(raw, (list, tuple, set)):
        out = []
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and "capability" in item:
                out.append(str(item["capability"]))
        return out
    raise ValueError(f"cannot parse capabilities from {type(raw).__name__}")


def _parse_escalations(raw: Any) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    if not isinstance(raw, dict):
        return out
    for target, caps in raw.items():
        out[str(target)] = set(_parse_capabilities(caps))
    return out


@dataclass(frozen=True)
class PermissionSet:
    """A ticket's granted capabilities plus static escalation grants.

    Constructed from the MANIFEST ``permissions:`` block; immutable once
    created. ``has()`` does exact or prefix matching so a scope grant
    (``fs.write:data``) covers everything beneath it (``fs.write:data/x``).
    """

    capabilities: frozenset[str] = field(default_factory=frozenset)
    escalations: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def from_manifest(cls, manifest_permissions: dict[str, Any] | None) -> "PermissionSet":
        """Build a ``PermissionSet`` from a manifest ``permissions:`` block.

        Accepts either the canonical shape
        ``{"capabilities": [...], "escalations": {...}}`` or a bare list of
        capability strings.
        """
        raw = manifest_permissions or {}
        if isinstance(raw, (list, tuple, set, str)):
            caps = _parse_capabilities(raw)
            escalations: dict[str, set[str]] = {}
        elif isinstance(raw, dict):
            if "capabilities" in raw:
                caps = _parse_capabilities(raw.get("capabilities"))
                escalations = _parse_escalations(raw.get("escalations"))
            else:
                # Bare mapping of capability -> enabled.
                caps = _parse_capabilities(raw)
                escalations = {}
        else:
            raise ValueError(
                f"permissions must be a mapping or list, got {type(raw).__name__}"
            )

        for cap in caps:
            denylist_reject(cap)
        for target, granted in escalations.items():
            for cap in granted:
                denylist_reject(cap)
            escalations[target] = frozenset(granted)

        return cls(
            capabilities=frozenset(caps),
            escalations={k: frozenset(v) for k, v in escalations.items()},
        )

    def has(self, capability: str) -> bool:
        """Exact or prefix match for *capability* against this set."""
        if capability in self.capabilities:
            return True
        for granted in self.capabilities:
            if ":" in granted:
                if capability.startswith(granted + "/") or capability.startswith(
                    granted + ":"
                ):
                    return True
            else:
                if capability.startswith(granted + ":"):
                    return True
        return False

    def __contains__(self, capability: str) -> bool:
        return self.has(capability)

    def __repr__(self) -> str:
        return f"PermissionSet(capabilities={sorted(self.capabilities)})"


def effective_permissions(ticket_metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Merge manifest permissions with ticket metadata defaults.

    Ticket ``metadata.json`` may carry a ``permissions`` block providing
    defaults; the manifest block wins on conflicts. Returns the canonical
    ``{"capabilities": [...], "escalations": {...}}`` shape.
    """
    metadata = ticket_metadata or {}
    raw_manifest = metadata.get("manifest_permissions")
    raw_metadata = metadata.get("permissions")

    merged_caps: list[str] = []
    merged_escalations: dict[str, set[str]] = {}
    seen: set[str] = set()

    for raw in (raw_metadata, raw_manifest):
        raw = raw or {}
        if isinstance(raw, dict):
            caps = _parse_capabilities(raw.get("capabilities", raw))
            for cap in caps:
                if cap not in seen:
                    seen.add(cap)
                    merged_caps.append(cap)
            for target, granted in _parse_escalations(raw.get("escalations")).items():
                merged_escalations.setdefault(target, set()).update(granted)
        elif isinstance(raw, (list, tuple, set)):
            for cap in _parse_capabilities(raw):
                if cap not in seen:
                    seen.add(cap)
                    merged_caps.append(cap)

    return {
        "capabilities": merged_caps,
        "escalations": {k: sorted(v) for k, v in merged_escalations.items()},
    }


def escalate(
    permission_set: PermissionSet, target: str, granted: Iterable[str]
) -> PermissionSet:
    """Return a copy of *permission_set* with the *granted* capabilities added.

    Raises :class:`PermissionError` unless the manifest declared a static
    escalation grant for *target* covering every requested capability
    (Part IX §4.3; v1 grants are static).
    """
    requested = list(granted)
    if not requested:
        return permission_set
    declared = permission_set.escalations.get(target)
    if declared is None:
        raise PermissionError(
            f"escalation target {target!r} is not granted for any capability"
        )
    missing = [cap for cap in requested if cap not in declared]
    if missing:
        raise PermissionError(
            f"escalation to {target!r} denied: missing capability grant(s) "
            f"{missing}; declared grants: {sorted(declared)}"
        )
    return PermissionSet(
        capabilities=frozenset(set(permission_set.capabilities) | set(requested)),
        escalations=permission_set.escalations,
    )


def enforce(
    permission_set: PermissionSet,
    action: str,
    context: dict[str, Any] | None = None,
) -> None:
    """Enforce *action* against the capability set (Part IX §4.4).

    Raises :class:`PermissionError` when the capability is missing; allowed
    actions pass silently. ``fs.*`` actions additionally require the target
    path to stay inside the jail root (ticket root).
    """
    ctx = context or {}

    if action in ("fs.read", "fs.write"):
        path = ctx.get("path")
        jail_root = ctx.get("jail_root") or ctx.get("ticket_root")
        if not isinstance(path, (str, Path)) or not str(path):
            raise PermissionError(f"{action}: context 'path' is required")
        # Normalize and jail-check.
        if jail_root is not None:
            try:
                (Path(jail_root) / str(path)).resolve().relative_to(
                    Path(jail_root).resolve()
                )
            except (ValueError, OSError):
                raise PermissionError(
                    f"{action}: path {path!r} escapes jail root {jail_root!r}"
                )
        capability = f"{action}:{path}"
        if not permission_set.has(capability):
            raise PermissionError(f"{action} denied for {path!r}: no capability grant")

    elif action == "net.http":
        host = ctx.get("host")
        if not isinstance(host, str) or not host:
            raise PermissionError("net.http: context 'host' is required")
        if not permission_set.has(f"net.http:{host}"):
            raise PermissionError(f"net.http denied for {host!r}: no capability grant")

    elif action == "run":
        shell = ctx.get("shell")
        if not isinstance(shell, str) or not shell:
            raise PermissionError("run: context 'shell' is required")
        if not permission_set.has(f"run:{shell}"):
            raise PermissionError(f"run:{shell} denied: no capability grant")

    elif action in _ATOMIC_CAPS:
        if not permission_set.has(action):
            raise PermissionError(f"{action} denied: no capability grant")

    else:
        raise PermissionError(f"unknown action {action!r}")
