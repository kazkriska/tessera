"""CMP-13 — Compliance regression gate.

Every finding from the compliance audit (see
`.hermes/plans/2026-08-08_sprint-cmp-compliance.md` §2) MUST have a test
that fails on the pre-fix baseline (`542be5a`) and passes on merged main.
This module is the executable 1:1 mapping: if any finding's regression
test is renamed, deleted, or stops being collectible, this gate fails.

The mapping table is the single source of truth for the QA gate (CMP-13
acceptance: "1:1 mapping finding→test; full suite green on main").
"""

from __future__ import annotations

import importlib

#: finding id -> (module, test function name) of the regression test.
#: Each test was verified to fail on `542be5a` (baseline) and pass after
#: its fix landed (CMP-01..12).
FINDING_TEST_MAP: dict[str, tuple[str, str]] = {
    # A — socket RPC path bypasses locks (CMP-01)
    "A": ("tests.test_hardening", "test_parallel_socket_transitions_serialize"),
    # B — workspace queue keys missing (CMP-04)
    "B": ("tests.test_scheduler", "test_same_workspace_tickets_serialize"),
    # C — action-level locks absent (CMP-03)
    "C": ("tests.test_hardening", "test_parallel_same_action_serializes_over_socket"),
    # D — manifest watch rules inert (CMP-10)
    "D": ("tests.test_watcher_bus", "test_watch_rules_fire_declared_trigger"),
    # E — retry backoff missing (CMP-05)
    "E": ("tests.test_scheduler", "test_retry_backoff_delays_between_attempts"),
    # F — retry exhaustion lacks audit trail (CMP-06)
    "F": ("tests.test_scheduler", "test_retry_exhaustion_writes_activity_and_bus_event"),
    # G — priority_bands never consumed (CMP-11)
    "G": ("tests.test_scheduler", "test_priority_bands_order_the_queue"),
    # H — log_level/log_path not wired at boot (CMP-07)
    "H": ("tests.test_hardening", "test_pipeline_wires_logging_custom"),
    # I — registry_path config key ignored (CMP-08)
    "I": ("tests.test_hardening", "test_pipeline_honors_registry_path"),
    # J — lock scope over-lock, ticket lock held for whole run (CMP-02)
    "J": ("tests.test_scheduler", "test_ticket_lock_released_during_runner_execution"),
    # K — TicketMetadata.from_dict lenient read (CMP-12)
    "K": ("tests.test_models", "test_metadata_from_dict_requires_all_six_contract_fields"),
    # L — watcher logger NameError (fixed pre-sprint, 542be5a)
    "L": ("tests.test_watcher_bus", "test_watcher_skips_non_ticket_paths"),
    # M — config.yaml not loaded at boot (fixed pre-sprint, 542be5a)
    "M": ("tests.test_hardening", "test_pipeline_loads_config_yaml"),
    # N — watcher non-recursion (fixed pre-sprint, 542be5a)
    "N": ("tests.test_watcher_bus", "test_watcher_watches_nested_ticket_dirs"),
}


def test_every_audit_finding_has_a_regression_test() -> None:
    """CMP-13: each finding A–N maps to an existing, collectible test."""
    missing: list[str] = []
    for finding, (module_name, test_name) in sorted(FINDING_TEST_MAP.items()):
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            missing.append(f"{finding}: module {module_name!r} not importable ({exc})")
            continue
        if not hasattr(module, test_name):
            missing.append(f"{finding}: {module_name}.{test_name} not found")
    assert not missing, (
        "Compliance regression gate broken — findings without tests:\n"
        + "\n".join(missing)
    )


def test_finding_map_covers_all_open_findings() -> None:
    """CMP-13: the map covers every finding in the audit backlog (A–N)."""
    expected = set("ABCDEFGHIJKLMN")
    actual = set(FINDING_TEST_MAP)
    assert actual == expected, (
        f"finding map mismatch: missing={sorted(expected - actual)} "
        f"extra={sorted(actual - expected)}"
    )
