"""Loader for `.ticket-runtime/config.yaml` (CONTRACTS.md §5).

The runtime's only configuration file. Every key is optional; a missing or
absent file yields the canonical defaults (Invariant I-2: `.ticket-runtime/`
is disposable, deleting it must revert to defaults on next boot).

Path-valued keys (``approval_cache_path``, ``log_path``, ``lock_dir``,
``registry_path``) are declared **relative to `.ticket-runtime/`** and are
stored here exactly as given -- resolution happens at the use site, never in
this loader.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "RuntimeConfig",
    "DEFAULT_PRIORITY_BANDS",
    "load_config",
    "config_example_yaml",
]

logger = logging.getLogger(__name__)

#: Ordering key (FRAME R.A.8); lower band number = higher priority.
DEFAULT_PRIORITY_BANDS: dict[int, str] = {
    0: "emergency",
    1: "user",
    2: "hook",
    3: "background",
}


def _default_priority_bands() -> dict[int, str]:
    return dict(DEFAULT_PRIORITY_BANDS)


@dataclass
class RuntimeConfig:
    """Canonical runtime configuration (CONTRACTS.md §5)."""

    repo_path: str | None = None
    debounce_window_seconds: float = 1.0
    worker_concurrency: int = 4
    priority_bands: dict[int, str] = field(default_factory=_default_priority_bands)
    recursion_max_depth: int = 10
    default_timeout: int = 300
    default_retry: int = 0
    retry_backoff_seconds: float = 1.0
    approval_cache_path: str = "cache/approvals"
    log_level: str = "INFO"
    log_path: str = "logs/runtime.log"
    lock_dir: str = "locks"
    registry_path: str = "registry.db"


def _coerce_priority_bands(raw: Any) -> dict[int, str]:
    """Coerce a raw ``priority_bands`` value; fall back to defaults + warn."""
    if not isinstance(raw, dict) or not raw:
        logger.warning(
            "config: 'priority_bands' must be a non-empty mapping of int -> str; "
            "got %r -- falling back to default bands %r",
            raw,
            DEFAULT_PRIORITY_BANDS,
        )
        return _default_priority_bands()

    bands: dict[int, str] = {}
    for key, value in raw.items():
        try:
            band = int(key)
        except (TypeError, ValueError):
            logger.warning(
                "config: 'priority_bands' key %r is not an integer -- "
                "falling back to default bands",
                key,
            )
            return _default_priority_bands()
        if not isinstance(value, str) or not value.strip():
            logger.warning(
                "config: 'priority_bands' value for band %r is not a non-empty "
                "string (%r) -- falling back to default bands",
                key,
                value,
            )
            return _default_priority_bands()
        bands[band] = value
    return bands


_SCALAR_COERCERS: dict[str, Any] = {
    "debounce_window_seconds": float,
    "worker_concurrency": int,
    "recursion_max_depth": int,
    "default_timeout": int,
    "default_retry": int,
    "retry_backoff_seconds": float,
    "approval_cache_path": str,
    "log_level": str,
    "log_path": str,
    "lock_dir": str,
    "registry_path": str,
}


def load_config(path: str | None = None) -> RuntimeConfig:
    """Load `config.yaml` from *path*, returning defaults when absent.

    Unknown keys are ignored with a warning. Malformed values fall back to the
    field default with a warning; the loader never raises on a bad config,
    because runtime state is disposable and boot must always succeed.
    """
    if path is None:
        return RuntimeConfig()

    file_path = Path(path)
    if not file_path.is_file():
        logger.debug("config: %s not found -- using defaults", file_path)
        return RuntimeConfig()

    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        logger.warning("config: failed to parse %s (%s) -- using defaults", file_path, exc)
        return RuntimeConfig()

    if raw is None:
        return RuntimeConfig()
    if not isinstance(raw, dict):
        logger.warning(
            "config: %s must contain a YAML mapping, got %s -- using defaults",
            file_path,
            type(raw).__name__,
        )
        return RuntimeConfig()

    known = {f.name for f in fields(RuntimeConfig)}
    for key in raw:
        if key not in known:
            logger.warning("config: ignoring unknown key %r in %s", key, file_path)

    kwargs: dict[str, Any] = {}

    if "repo_path" in raw:
        value = raw["repo_path"]
        kwargs["repo_path"] = None if value is None else str(value)

    if "priority_bands" in raw:
        kwargs["priority_bands"] = _coerce_priority_bands(raw["priority_bands"])

    for name, coerce in _SCALAR_COERCERS.items():
        if name not in raw:
            continue
        value = raw[name]
        if value is None:
            logger.warning("config: key %r is null -- using default", name)
            continue
        try:
            kwargs[name] = coerce(value)
        except (TypeError, ValueError):
            logger.warning(
                "config: key %r has invalid value %r -- using default", name, value
            )

    return RuntimeConfig(**kwargs)


def config_example_yaml() -> str:
    """Return a commented sample `config.yaml` showing every default."""
    return """\
# .ticket-runtime/config.yaml -- Tessera v1 runtime configuration.
#
# Every key is optional; deleting this file reverts the runtime to the
# defaults shown below (Invariant I-2: .ticket-runtime/ is disposable).
# All path values are relative to .ticket-runtime/ unless noted.

repo_path: null              # absolute path to the TicketRepository; null => cwd's TicketsRepository/
debounce_window_seconds: 1.0 # filesystem event debounce window
worker_concurrency: 4        # max concurrent subprocesses across all queues

priority_bands:              # ordering key; lower number = higher priority
  0: emergency               # cancellation handlers
  1: user                    # CLI / user-action commands
  2: hook                    # file-modification hooks
  3: background              # asset indexing / maintenance

recursion_max_depth: 10      # event re-emission depth guard
default_timeout: 300         # seconds, used when a descriptor omits `timeout`
default_retry: 0             # retries, used when a descriptor omits `retry`
retry_backoff_seconds: 1.0   # base delay (s) before each retry; exponential 2^(n-1)

approval_cache_path: cache/approvals
log_level: INFO              # DEBUG | INFO | WARNING | ERROR | CRITICAL
log_path: logs/runtime.log
lock_dir: locks
registry_path: registry.db
"""
