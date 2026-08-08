"""Manifest — MANIFEST.yaml loader and strict validator.

Authoritative spec: CONTRACTS.md §4 (canonical manifest grammar, supersedes
Master Part V §4.2 / RFC-0003) and CONTRACTS.md §7 (tooling decisions).

Rules enforced here (fatal -> ``ManifestValidationError``):

* envelope ``apiVersion: ticket/v1`` and ``kind: Ticket``
* only canonical top-level keys are accepted
* YAML anchors (``&``), aliases (``*``) and merge keys (``<<``) are forbidden.
  Detection MUST happen *before* ``yaml.safe_load`` because PyYAML resolves
  anchors during construction; we scan the token stream instead.
* ``metadata.id`` must equal the ticket directory basename minus ``.ticket``
* uniform executable descriptor shape for ``hooks:`` / ``actions:`` entries
* circular-watch guard: a ``watch:`` rule may not watch runtime-owned state
  files (``state.json`` / ``activity.jsonl``)

Non-fatal findings are returned as warning strings by :func:`validate_manifest`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "API_VERSION",
    "KIND",
    "CANONICAL_TOP_LEVEL_KEYS",
    "KNOWN_SHELLS",
    "ManifestValidationError",
    "ExecDescriptor",
    "Metadata",
    "Manifest",
    "load_manifest",
    "parse_manifest",
    "validate_manifest",
]

API_VERSION = "ticket/v1"
KIND = "Ticket"

CANONICAL_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "apiVersion",
        "kind",
        "metadata",
        "runtime",
        "initialize",
        "hooks",
        "actions",
        "permissions",
        "env",
        "watch",
        "exports",
    }
)

#: Runners the Dispatcher is expected to provide (CONTRACTS §7.1 plugins/).
#: Unknown shells are *not* fatal here — they produce a warning only.
KNOWN_SHELLS: frozenset[str] = frozenset({"bash", "sh", "python", "node"})

#: Runtime-owned files that must never be watched (circular-watch guard, §4).
PROTECTED_WATCH_PATHS: frozenset[str] = frozenset({"state.json", "activity.jsonl"})

DEFAULT_SHELL = "bash"


class ManifestValidationError(Exception):
    """Raised for any fatal manifest problem (schema, envelope, identity)."""


@dataclass(frozen=True)
class ExecDescriptor:
    """Uniform executable descriptor used by ``hooks:`` and ``actions:``."""

    run: str
    shell: str = DEFAULT_SHELL
    timeout: int | None = None
    retry: int | None = None
    is_async: bool = False

    @property
    def shell_is_known(self) -> bool:
        return self.shell in KNOWN_SHELLS


@dataclass(frozen=True)
class Metadata:
    id: str
    title: str
    type: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Manifest:
    api_version: str
    kind: str
    metadata: Metadata
    runtime: dict[str, Any] | None = None
    initialize: list[ExecDescriptor] = field(default_factory=list)
    hooks: dict[str, list[ExecDescriptor]] = field(default_factory=dict)
    actions: dict[str, ExecDescriptor] = field(default_factory=dict)
    permissions: dict[str, Any] | None = None
    env: dict[str, Any] = field(default_factory=dict)
    watch: list[dict[str, Any]] = field(default_factory=list)
    exports: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None


# --------------------------------------------------------------------------
# pre-load stream scan
# --------------------------------------------------------------------------


def _reject_anchors_aliases_merges(text: str) -> None:
    """Scan the YAML token stream and reject anchors, aliases and merge keys.

    Must run *before* ``safe_load``: by the time a document is constructed the
    anchors have been resolved away and are undetectable (CONTRACTS §7).
    """
    try:
        tokens = list(yaml.scan(text))
    except yaml.YAMLError as exc:  # pragma: no cover - malformed YAML path
        raise ManifestValidationError(f"malformed YAML: {exc}") from exc

    for token in tokens:
        if isinstance(token, yaml.tokens.AnchorToken):
            raise ManifestValidationError(
                f"YAML anchors are forbidden (found '&{token.value}')"
            )
        if isinstance(token, yaml.tokens.AliasToken):
            raise ManifestValidationError(
                f"YAML aliases are forbidden (found '*{token.value}')"
            )
        if (
            isinstance(token, yaml.tokens.ScalarToken)
            and token.value == "<<"
            and token.style is None  # plain scalar => the merge key, not a string
        ):
            raise ManifestValidationError("YAML merge keys ('<<') are forbidden")

    # Belt and braces: the event stream exposes AliasEvent even where the
    # token scanner is bypassed by flow constructs.
    try:
        for event in yaml.parse(text):
            if isinstance(event, yaml.events.AliasEvent):
                raise ManifestValidationError(
                    f"YAML aliases are forbidden (found '*{event.anchor}')"
                )
            anchor = getattr(event, "anchor", None)
            if anchor:
                raise ManifestValidationError(
                    f"YAML anchors are forbidden (found '&{anchor}')"
                )
    except yaml.YAMLError as exc:  # pragma: no cover
        raise ManifestValidationError(f"malformed YAML: {exc}") from exc


# --------------------------------------------------------------------------
# descriptor coercion
# --------------------------------------------------------------------------


def _coerce_descriptor(raw: Any, where: str) -> ExecDescriptor:
    if isinstance(raw, str):
        # shorthand: a bare command string
        return ExecDescriptor(run=raw)
    if not isinstance(raw, dict):
        raise ManifestValidationError(
            f"{where}: executable descriptor must be a mapping or string, "
            f"got {type(raw).__name__}"
        )

    unknown = set(raw) - {"run", "shell", "timeout", "retry", "async"}
    if unknown:
        raise ManifestValidationError(
            f"{where}: unknown descriptor key(s): {sorted(unknown)}"
        )

    run = raw.get("run")
    if not isinstance(run, str) or not run.strip():
        raise ManifestValidationError(f"{where}: 'run' is required and must be a string")

    shell = raw.get("shell", DEFAULT_SHELL)
    if not isinstance(shell, str):
        raise ManifestValidationError(f"{where}: 'shell' must be a string")

    timeout = raw.get("timeout")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int)):
        raise ManifestValidationError(f"{where}: 'timeout' must be an integer")

    retry = raw.get("retry")
    if retry is not None and (isinstance(retry, bool) or not isinstance(retry, int)):
        raise ManifestValidationError(f"{where}: 'retry' must be an integer")

    is_async = raw.get("async", False)
    if not isinstance(is_async, bool):
        raise ManifestValidationError(f"{where}: 'async' must be a boolean")

    return ExecDescriptor(
        run=run, shell=shell, timeout=timeout, retry=retry, is_async=is_async
    )


def _coerce_descriptor_list(raw: Any, where: str) -> list[ExecDescriptor]:
    if raw is None:
        return []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        raise ManifestValidationError(f"{where}: expected a list of descriptors")
    return [_coerce_descriptor(item, f"{where}[{i}]") for i, item in enumerate(raw)]


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def ticket_id_from_dir(ticket_dir: str | os.PathLike[str]) -> str:
    """Return the expected ``metadata.id`` for a ticket directory.

    The directory basename minus a trailing ``.ticket`` suffix (CONTRACTS §4).
    """
    name = Path(ticket_dir).name
    return name[: -len(".ticket")] if name.endswith(".ticket") else name


def parse_manifest(
    text: str,
    ticket_id: str | None = None,
    source_path: str | os.PathLike[str] | None = None,
) -> Manifest:
    """Parse and strictly validate a MANIFEST.yaml document from a string."""
    _reject_anchors_aliases_merges(text)

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestValidationError(f"malformed YAML: {exc}") from exc

    if data is None:
        raise ManifestValidationError("manifest is empty")
    if not isinstance(data, dict):
        raise ManifestValidationError("manifest root must be a mapping")

    unknown = set(data) - CANONICAL_TOP_LEVEL_KEYS
    if unknown:
        raise ManifestValidationError(f"unknown top-level key(s): {sorted(unknown)}")

    api_version = data.get("apiVersion")
    if api_version != API_VERSION:
        raise ManifestValidationError(
            f"unsupported apiVersion {api_version!r}; expected {API_VERSION!r}"
        )

    kind = data.get("kind")
    if kind != KIND:
        raise ManifestValidationError(f"unsupported kind {kind!r}; expected {KIND!r}")

    raw_meta = data.get("metadata")
    if not isinstance(raw_meta, dict):
        raise ManifestValidationError("'metadata' is required and must be a mapping")
    for key in ("id", "title", "type"):
        if not isinstance(raw_meta.get(key), str) or not raw_meta[key].strip():
            raise ManifestValidationError(f"metadata.{key} is required and must be a string")

    metadata = Metadata(
        id=raw_meta["id"],
        title=raw_meta["title"],
        type=raw_meta["type"],
        extra={k: v for k, v in raw_meta.items() if k not in {"id", "title", "type"}},
    )

    if ticket_id is not None and metadata.id != ticket_id:
        raise ManifestValidationError(
            f"metadata.id {metadata.id!r} does not match ticket directory id {ticket_id!r}"
        )

    runtime = data.get("runtime")
    if runtime is not None and not isinstance(runtime, dict):
        raise ManifestValidationError("'runtime' must be a mapping")

    initialize = _coerce_descriptor_list(data.get("initialize"), "initialize")

    raw_hooks = data.get("hooks") or {}
    if not isinstance(raw_hooks, dict):
        raise ManifestValidationError("'hooks' must be a mapping of event -> descriptors")
    hooks = {
        str(event): _coerce_descriptor_list(value, f"hooks.{event}")
        for event, value in raw_hooks.items()
    }

    raw_actions = data.get("actions") or {}
    if not isinstance(raw_actions, dict):
        raise ManifestValidationError("'actions' must be a mapping of name -> descriptor")
    actions = {
        str(name): _coerce_descriptor(value, f"actions.{name}")
        for name, value in raw_actions.items()
    }

    permissions = data.get("permissions")
    if permissions is not None and not isinstance(permissions, dict):
        raise ManifestValidationError("'permissions' must be a mapping")

    env = data.get("env") or {}
    if not isinstance(env, dict):
        raise ManifestValidationError("'env' must be a mapping")

    watch = data.get("watch") or []
    if not isinstance(watch, list):
        raise ManifestValidationError("'watch' must be a list of rules")
    for i, rule in enumerate(watch):
        if not isinstance(rule, dict):
            raise ManifestValidationError(f"watch[{i}]: rule must be a mapping")
        path = rule.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ManifestValidationError(f"watch[{i}]: 'path' is required and must be a string")
        if Path(path).name in PROTECTED_WATCH_PATHS:
            raise ManifestValidationError(
                f"watch[{i}]: watching runtime-owned {path!r} is forbidden "
                "(circular-watch guard, CONTRACTS §4)"
            )
        if not isinstance(rule.get("trigger", ""), str):
            raise ManifestValidationError(f"watch[{i}]: 'trigger' must be a string")

    exports = data.get("exports") or {}
    if not isinstance(exports, dict):
        raise ManifestValidationError("'exports' must be a mapping")

    return Manifest(
        api_version=api_version,
        kind=kind,
        metadata=metadata,
        runtime=runtime,
        initialize=initialize,
        hooks=hooks,
        actions=actions,
        permissions=permissions,
        env=env,
        watch=watch,
        exports=exports,
        source_path=Path(source_path) if source_path is not None else None,
    )


def load_manifest(
    manifest_path: str | os.PathLike[str], ticket_id: str | None = None
) -> Manifest:
    """Load and validate ``MANIFEST.yaml`` from disk.

    ``ticket_id`` is supplied by the caller (the ticket directory basename minus
    ``.ticket``); when omitted it is derived from the manifest's parent dir.
    """
    path = Path(manifest_path)
    if not path.is_file():
        raise ManifestValidationError(f"manifest not found: {path}")
    if ticket_id is None:
        ticket_id = ticket_id_from_dir(path.parent)
    return parse_manifest(
        path.read_text(encoding="utf-8"), ticket_id=ticket_id, source_path=path
    )


# --------------------------------------------------------------------------
# non-fatal validation
# --------------------------------------------------------------------------


def validate_manifest(manifest: Manifest) -> list[str]:
    """Return non-fatal warnings for an already-parsed manifest."""
    warnings: list[str] = []

    def check(descriptor: ExecDescriptor, where: str) -> None:
        if not descriptor.shell_is_known:
            warnings.append(
                f"{where}: unknown shell {descriptor.shell!r} "
                "(Dispatcher will validate the runner at execution time)"
            )
        if descriptor.timeout is not None and descriptor.timeout <= 0:
            warnings.append(f"{where}: non-positive timeout {descriptor.timeout}")
        if descriptor.retry is not None and descriptor.retry < 0:
            warnings.append(f"{where}: negative retry {descriptor.retry}")

    for i, descriptor in enumerate(manifest.initialize):
        check(descriptor, f"initialize[{i}]")
    for event, descriptors in manifest.hooks.items():
        for i, descriptor in enumerate(descriptors):
            check(descriptor, f"hooks.{event}[{i}]")
    for name, descriptor in manifest.actions.items():
        check(descriptor, f"actions.{name}")

    for name, spec in manifest.exports.items():
        if not isinstance(spec, dict) or "type" not in spec:
            warnings.append(
                f"exports.{name}: expected a mapping with a 'type' key "
                "(introspection only, not fatal)"
            )

    if not manifest.hooks and not manifest.actions:
        warnings.append("manifest declares neither hooks nor actions")

    return warnings
