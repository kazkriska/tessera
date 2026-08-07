"""Tests for the `.ticket-runtime/config.yaml` loader (CONTRACTS.md §5)."""

from __future__ import annotations

import logging

import pytest
import yaml

from lib.ticket_management.config import (
    DEFAULT_PRIORITY_BANDS,
    RuntimeConfig,
    config_example_yaml,
    load_config,
)


def test_defaults_when_no_file(tmp_path):
    """No path, and a missing path, both yield the canonical defaults."""
    assert load_config() == RuntimeConfig()
    assert load_config(str(tmp_path / "absent.yaml")) == RuntimeConfig()

    cfg = RuntimeConfig()
    assert cfg.repo_path is None
    assert cfg.debounce_window_seconds == 1.0
    assert cfg.worker_concurrency == 4
    assert cfg.priority_bands == DEFAULT_PRIORITY_BANDS
    assert cfg.recursion_max_depth == 10
    assert cfg.default_timeout == 300
    assert cfg.default_retry == 0
    assert cfg.approval_cache_path == "cache/approvals"
    assert cfg.log_level == "INFO"
    assert cfg.log_path == "logs/runtime.log"
    assert cfg.lock_dir == "locks"
    assert cfg.registry_path == "registry.db"


def test_load_custom_values(tmp_path):
    """Values in the file win; relative paths are stored verbatim."""
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "repo_path": "/srv/TicketsRepository",
                "debounce_window_seconds": 0.25,
                "worker_concurrency": 8,
                "priority_bands": {0: "emergency", 1: "user"},
                "recursion_max_depth": 3,
                "default_timeout": 60,
                "default_retry": 2,
                "approval_cache_path": "cache/custom",
                "log_level": "DEBUG",
                "log_path": "logs/custom.log",
                "lock_dir": "mylocks",
                "registry_path": "custom.db",
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(str(path))
    assert cfg.repo_path == "/srv/TicketsRepository"
    assert cfg.debounce_window_seconds == 0.25
    assert cfg.worker_concurrency == 8
    assert cfg.priority_bands == {0: "emergency", 1: "user"}
    assert cfg.recursion_max_depth == 3
    assert cfg.default_timeout == 60
    assert cfg.default_retry == 2
    assert cfg.approval_cache_path == "cache/custom"
    assert cfg.log_level == "DEBUG"
    assert cfg.log_path == "logs/custom.log"
    assert cfg.lock_dir == "mylocks"
    assert cfg.registry_path == "custom.db"


def test_partial_file_keeps_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("worker_concurrency: 16\n", encoding="utf-8")

    cfg = load_config(str(path))
    assert cfg.worker_concurrency == 16
    assert cfg.log_path == "logs/runtime.log"
    assert cfg.priority_bands == DEFAULT_PRIORITY_BANDS


@pytest.mark.parametrize(
    "bands_yaml",
    [
        "priority_bands: not-a-mapping\n",
        "priority_bands: [emergency, user]\n",
        "priority_bands:\n  emergency: 0\n",
        "priority_bands:\n  0:\n",
    ],
)
def test_malformed_priority_bands_falls_back_with_warning(tmp_path, caplog, bands_yaml):
    path = tmp_path / "config.yaml"
    path.write_text(bands_yaml, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="lib.ticket_management.config"):
        cfg = load_config(str(path))

    assert cfg.priority_bands == DEFAULT_PRIORITY_BANDS
    assert any("priority_bands" in rec.getMessage() for rec in caplog.records)


def test_unknown_key_ignored_with_warning(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("worker_concurrency: 2\nnope: 1\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="lib.ticket_management.config"):
        cfg = load_config(str(path))

    assert cfg.worker_concurrency == 2
    assert not hasattr(cfg, "nope")
    assert any("unknown key" in rec.getMessage() for rec in caplog.records)


def test_invalid_scalar_and_bad_yaml_fall_back(tmp_path, caplog):
    bad_scalar = tmp_path / "a.yaml"
    bad_scalar.write_text("worker_concurrency: many\n", encoding="utf-8")
    bad_yaml = tmp_path / "b.yaml"
    bad_yaml.write_text("worker_concurrency: [\n", encoding="utf-8")
    empty = tmp_path / "c.yaml"
    empty.write_text("", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="lib.ticket_management.config"):
        assert load_config(str(bad_scalar)).worker_concurrency == 4
        assert load_config(str(bad_yaml)) == RuntimeConfig()
        assert load_config(str(empty)) == RuntimeConfig()


def test_config_example_yaml_parses_to_defaults():
    """The shipped sample must be valid YAML that reproduces the defaults."""
    text = config_example_yaml()
    assert text.lstrip().startswith("#")

    data = yaml.safe_load(text)
    assert isinstance(data, dict)

    cfg = RuntimeConfig(**{k: v for k, v in data.items()})
    assert cfg == RuntimeConfig()
