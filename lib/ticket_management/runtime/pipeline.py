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

    def start(self) -> None:
        """Assemble and start every stage in canonical order."""
        self.config = self.config or RuntimeConfig()
        root = Path(self.root).resolve()

        self.repo = repo_init(root)
        self.registry = Registry(str(self.repo))
        rescan(self.repo, self.registry)

        # Reap stale ticket locks left by a crashed runtime (RFC-0006).
        from lib.ticket_management.runtime.scheduler import reap_stale_locks

        lock_dir = self.repo / "TicketsRepository" / ".ticket-runtime" / "locks"
        reaped = reap_stale_locks(lock_dir)
        if reaped:
            logger.info("pipeline: reaped %d stale lock(s) at boot", reaped)

        self.bus = EventBus()
        self.watcher = FsWatcher()
        self.watcher.watch(str(self.repo), self.bus, self.config)

        # Scheduler runner = the dispatcher glue (executor_dispatch).
        lock_dir = (
            self.repo
            / "TicketsRepository"
            / ".ticket-runtime"
            / "locks"
        )
        self.scheduler = Scheduler(
            config=self.config,
            registry=self.registry,
            runner=executor_dispatch,
            lock_dir=lock_dir,
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
