"""Pipeline — Watcher -> Bus -> Scheduler -> Dispatcher -> Executor wiring.

Responsibility (Master Part II §4.1, CONTRACTS.md §6, §7): own the runtime
startup and the glue between stages. A :class:`Pipeline` assembles the
repository, registry, watcher, bus, scheduler and executor, maps filesystem
domain events to manifest hooks, and dispatches executions under the
ticket's permission set.

Stage wiring on ``start()``::

    repo_init(root)
      -> Registry(root)
        -> rescan (discovery)
          -> FsWatcher.watch(repo, bus, config)
            -> Scheduler(config, registry, runner=executor_dispatch)
              -> bus events subscribed -> scheduler.enqueue

Event -> hook mapping (from watcher triggers, CONTRACTS.md §7):

    metadata.updated  -> hooks.on_metadata_updated
    manifest.updated  -> hooks.on_manifest_updated
    state.updated     -> hooks.on_state_updated
    activity.updated  -> hooks.on_activity_updated
    fs.changed        -> no default hook (external watchers only)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lib.ticket_management.config import RuntimeConfig
from lib.ticket_management.repo import repo_init, rescan
from lib.ticket_management.runtime.bus import Event, EventBus
from lib.ticket_management.runtime.dispatcher import RunnerDescriptor
from lib.ticket_management.runtime.env import resolve_ticket_env
from lib.ticket_management.runtime.executor import ExecutionResult, run_hook
from lib.ticket_management.runtime.manifest import ManifestValidationError, load_manifest
from lib.ticket_management.runtime.registry import Registry
from lib.ticket_management.runtime.scheduler import Scheduler
from lib.ticket_management.runtime.watcher import FsWatcher

__all__ = ["Pipeline", "EVENT_HOOK_MAP", "executor_dispatch"]

logger = logging.getLogger(__name__)

#: Domain event -> default manifest hook name.
EVENT_HOOK_MAP: dict[str, str] = {
    "metadata.updated": "on_metadata_updated",
    "manifest.updated": "on_manifest_updated",
    "state.updated": "on_state_updated",
    "activity.updated": "on_activity_updated",
}


@dataclass
class Pipeline:
    """Runtime pipeline: repository, registry, watcher, bus, scheduler."""

    root: str | Path
    config: RuntimeConfig | None = None
    repo: Path | None = None
    registry: Registry | None = None
    bus: EventBus | None = None
    watcher: FsWatcher | None = None
    scheduler: Scheduler | None = None
    lock_dir: Path | None = None

    def start(self) -> None:
        """Assemble and start every stage in canonical order."""
        root = Path(self.root).resolve()

        # RFC-0004 boot step 1: load `.ticket-runtime/config.yaml` when the
        # caller did not hand us a config. A missing file yields defaults
        # (Invariant I-2 — deleting `.ticket-runtime/` reverts to defaults).
        if self.config is None:
            from lib.ticket_management.config import load_config

            cfg_path = (
                root
                / "TicketsRepository"
                / ".ticket-runtime"
                / "config.yaml"
            )
            self.config = load_config(str(cfg_path))

        self.repo = repo_init(root)

        # Resolve config paths relative to the canonical runtime dir:
        # `<repo>/TicketsRepository/.ticket-runtime/`. CONTRACTS §5 names the
        # defaults (`locks`, `registry.db`) relative to that dir.
        runtime_dir = self.repo / "TicketsRepository" / ".ticket-runtime"
        registry_path = Path(self.config.registry_path)
        if not registry_path.is_absolute():
            registry_path = runtime_dir / registry_path
        self.registry = Registry(str(self.repo), db_path=registry_path)
        rescan(self.repo, self.registry)

        lock_dir = Path(self.config.lock_dir)
        if not lock_dir.is_absolute():
            lock_dir = runtime_dir / lock_dir
        lock_dir = lock_dir.resolve()

        # CONTRACTS §5: honor log_level / log_path. Relative log_path is
        # resolved against the canonical runtime dir, same as lock_dir.
        log_path: str | None = None
        if self.config.log_path:
            resolved_log = Path(self.config.log_path)
            if not resolved_log.is_absolute():
                resolved_log = runtime_dir / resolved_log
            log_path = str(resolved_log.resolve())
        self._configure_logging(self.config, log_path)

        # Reap stale ticket locks left by a crashed runtime (RFC-0006).
        from lib.ticket_management.runtime.scheduler import reap_stale_locks

        reaped = reap_stale_locks(lock_dir)
        if reaped:
            logger.info("pipeline: reaped %d stale lock(s) at boot", reaped)
        self.lock_dir = lock_dir

        self.bus = EventBus(recursion_max_depth=self.config.recursion_max_depth)
        self.watcher = FsWatcher()
        self.watcher.watch(str(self.repo), self.bus, self.config)
        # Start the inotify loop in a daemon thread (returns immediately).
        self.watcher.start(block=False)

        # Scheduler runner = the dispatcher glue (executor_dispatch).
        self.scheduler = Scheduler(
            config=self.config,
            registry=self.registry,
            runner=executor_dispatch,
            lock_dir=lock_dir,
            bus=self.bus,
        )

        # Bus -> Scheduler: every domain event on a known ticket triggers a
        # hook enqueue for that ticket (hook lookup happens in the runner).
        self.bus.subscribe(
            lambda event: self._on_event(event),
        )

        logger.info(
            "pipeline: started root=%s repo=%s", root, self.repo
        )

    # ------------------------------------------------------------------ #
    # Event handling
    # ------------------------------------------------------------------ #
    def _on_event(self, event: Event) -> None:
        """Translate a bus event into a scheduler enqueue (if hook exists)."""
        if self.scheduler is None or event.ticket_id is None:
            return
        hook_name = EVENT_HOOK_MAP.get(event.name)
        if hook_name is None:
            # fs.changed and other events have no default hook.
            return

        descriptor = self._resolve_hook_descriptor(event.ticket_id, hook_name)
        if descriptor is None:
            # No manifest or no matching hook declared: skip silently.
            logger.debug(
                "pipeline: no hook %s for ticket %s (event %s)",
                hook_name,
                event.ticket_id,
                event.name,
            )
            return

        ticket_root = self._ticket_root(event.ticket_id)
        if ticket_root is None:
            return
        self.scheduler.enqueue(
            ticket_id=event.ticket_id,
            descriptor=descriptor,
            ticket_root=ticket_root,
            config=self.config,
            event_payload=event.data,
            action_name=hook_name,
        )

    def _resolve_hook_descriptor(
        self, ticket_id: str, hook_name: str
    ) -> RunnerDescriptor | None:
        """Load the ticket manifest and return the named hook's descriptor."""
        ticket_root = self._ticket_root(ticket_id)
        if ticket_root is None:
            return None
        manifest_path = ticket_root / "MANIFEST.yaml"
        if not manifest_path.is_file():
            return None
        try:
            manifest = load_manifest(manifest_path, ticket_id=ticket_id)
        except ManifestValidationError:
            logger.warning(
                "pipeline: invalid manifest for %s; skipping hook %s",
                ticket_id,
                hook_name,
            )
            return None
        descriptors = manifest.hooks.get(hook_name) or []
        if not descriptors:
            return None
        desc = descriptors[0]
        return RunnerDescriptor(
            path=desc.run,
            shell=desc.shell,
            timeout=desc.timeout,
            retry=desc.retry or 0,
            **{"async": desc.is_async},
        )

    def _ticket_root(self, ticket_id: str) -> Path | None:
        if self.repo is None:
            return None
        candidate = self.repo / f"{ticket_id}.ticket"
        return candidate if candidate.is_dir() else None

    # ------------------------------------------------------------------ #
    # Logging wiring (CONTRACTS.md §5)
    # ------------------------------------------------------------------ #
    _LOGGING_CONFIGURED = False

    @classmethod
    def _configure_logging(cls, config: RuntimeConfig, log_path: str | None = None) -> None:
        """Honor ``log_level`` / ``log_path`` without stacking handlers.

        Idempotent for the same target (repeated boots keep one handler);
        a *different* target replaces the old handler, so an in-process
        reconfiguration (tests, re-boot) takes effect. ``log_path`` is the
        runtime-dir-resolved absolute path, or None for stderr.
        """
        level = getattr(logging, str(config.log_level).upper(), logging.INFO)

        root = logging.getLogger("lib.ticket_management")
        # Remove handlers whose target differs from the requested one;
        # keep a matching handler so repeated boots don't stack.
        for handler in list(root.handlers):
            if getattr(handler, "_tessera_log_path", object()) != log_path:
                root.removeHandler(handler)
                handler.close()
        if not root.handlers:
            handler: logging.Handler
            if log_path:
                Path(log_path).parent.mkdir(parents=True, exist_ok=True)
                handler = logging.FileHandler(log_path)
            else:
                handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s %(name)s: %(message)s"
                )
            )
            handler._tessera_log_path = log_path  # type: ignore[attr-defined]
            root.addHandler(handler)
        root.setLevel(level)
        root.propagate = False
        cls._LOGGING_CONFIGURED = True

    # ------------------------------------------------------------------ #
    # Teardown
    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        """Stop every stage in reverse order."""
        if self.watcher is not None:
            self.watcher.stop()
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=True)
        if self.registry is not None:
            self.registry.close()
        logger.info("pipeline: stopped")


def executor_dispatch(
    ticket_id: str,
    descriptor: RunnerDescriptor,
    ticket_root: str | Path,
    config: RuntimeConfig,
    event_payload: dict[str, Any] | None = None,
) -> ExecutionResult:
    """Scheduler runner glue: resolve permissions, build env, run the hook.

    Called by the Scheduler for every queued job. Layers:

    * permissions from the ticket manifest (defaults = no capabilities)
    * environment: System -> Ticket ``.env`` -> Manifest ``env`` -> Event
      payload, with DENYLIST masking and secret gating
    * ``run_hook`` enforces the path jail and process isolation.

    Raises are intentionally NOT caught here: the Scheduler records failed
    jobs via its retry policy.
    """
    root = Path(ticket_root).resolve()

    # Permissions from the manifest (default: deny-all).
    permissions: dict[str, Any] = {}
    manifest_path = root / "MANIFEST.yaml"
    if manifest_path.is_file():
        try:
            manifest = load_manifest(manifest_path, ticket_id=ticket_id)
            permissions = manifest.permissions or {}
        except ManifestValidationError:
            logger.warning(
                "executor_dispatch: invalid manifest for %s; denying permissions",
                ticket_id,
            )
            permissions = {}

    # Environment: System base -> Ticket .env -> Manifest env -> Event payload.
    event_env = dict(event_payload or {})
    env = resolve_ticket_env(
        root,
        base_env=None,
        event_env=event_env,
        permissions=permissions,
    )

    return run_hook(
        descriptor=descriptor,
        ticket_root=root,
        config=config,
        env=env,
        permissions=permissions,
    )
