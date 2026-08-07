"""Tests for the MANIFEST.yaml loader (CONTRACTS §4)."""

from __future__ import annotations

import textwrap

import pytest

from lib.ticket_management.runtime.manifest import (
    ExecDescriptor,
    ManifestValidationError,
    load_manifest,
    parse_manifest,
    ticket_id_from_dir,
    validate_manifest,
)

VALID_YAML = textwrap.dedent(
    """
    apiVersion: ticket/v1
    kind: Ticket
    metadata:
      id: build-report
      title: Build the weekly report
      type: task
    runtime:
      shell: bash
    initialize:
      - run: ./scripts/init.sh
    hooks:
      metadata_updated:
        - run: ./scripts/on_meta.py
          shell: python
          timeout: 30
          retry: 2
          async: true
    actions:
      publish:
        run: ./scripts/publish.sh
    permissions:
      write: ["task/**"]
    env:
      REPORT_FORMAT: pdf
    watch:
      - path: "metadata.json"
        events: [modify]
        trigger: metadata_updated
    exports:
      task.status: { type: string, description: "Current task status" }
    """
)


@pytest.fixture
def valid_manifest_text() -> str:
    return VALID_YAML


def test_valid_manifest_parses(valid_manifest_text: str) -> None:
    manifest = parse_manifest(valid_manifest_text, ticket_id="build-report")

    assert manifest.api_version == "ticket/v1"
    assert manifest.kind == "Ticket"
    assert manifest.metadata.id == "build-report"
    assert manifest.metadata.title == "Build the weekly report"
    assert manifest.metadata.type == "task"
    assert manifest.runtime == {"shell": "bash"}
    assert manifest.initialize == [ExecDescriptor(run="./scripts/init.sh")]

    hook = manifest.hooks["metadata_updated"][0]
    assert hook == ExecDescriptor(
        run="./scripts/on_meta.py", shell="python", timeout=30, retry=2, is_async=True
    )

    assert manifest.actions["publish"].run == "./scripts/publish.sh"
    assert manifest.actions["publish"].shell == "bash"  # default
    assert manifest.env == {"REPORT_FORMAT": "pdf"}
    assert manifest.watch[0]["trigger"] == "metadata_updated"
    assert "task.status" in manifest.exports
    assert validate_manifest(manifest) == []


def test_anchor_and_alias_rejected() -> None:
    text = textwrap.dedent(
        """
        apiVersion: ticket/v1
        kind: Ticket
        metadata:
          id: t
          title: T
          type: task
        actions:
          a: &base
            run: ./x.sh
          b: *base
        """
    )
    with pytest.raises(ManifestValidationError, match="anchors are forbidden"):
        parse_manifest(text, ticket_id="t")


def test_merge_key_rejected() -> None:
    text = textwrap.dedent(
        """
        apiVersion: ticket/v1
        kind: Ticket
        metadata:
          id: t
          title: T
          type: task
        actions:
          a:
            <<: {run: ./x.sh}
        """
    )
    with pytest.raises(ManifestValidationError):
        parse_manifest(text, ticket_id="t")


def test_id_mismatch_rejected(valid_manifest_text: str) -> None:
    with pytest.raises(ManifestValidationError, match="does not match ticket directory"):
        parse_manifest(valid_manifest_text, ticket_id="something-else")


def test_bad_api_version_rejected(valid_manifest_text: str) -> None:
    text = valid_manifest_text.replace("ticket/v1", "ticket/v2")
    with pytest.raises(ManifestValidationError, match="unsupported apiVersion"):
        parse_manifest(text, ticket_id="build-report")


def test_bad_kind_rejected(valid_manifest_text: str) -> None:
    text = valid_manifest_text.replace("kind: Ticket", "kind: Workspace")
    with pytest.raises(ManifestValidationError, match="unsupported kind"):
        parse_manifest(text, ticket_id="build-report")


def test_unknown_top_level_key_rejected(valid_manifest_text: str) -> None:
    with pytest.raises(ManifestValidationError, match="unknown top-level key"):
        parse_manifest(valid_manifest_text + "\nbogus: 1\n", ticket_id="build-report")


def test_descriptor_missing_run_rejected() -> None:
    text = textwrap.dedent(
        """
        apiVersion: ticket/v1
        kind: Ticket
        metadata: {id: t, title: T, type: task}
        actions:
          a: {shell: bash}
        """
    )
    with pytest.raises(ManifestValidationError, match="'run' is required"):
        parse_manifest(text, ticket_id="t")


def test_circular_watch_rejected() -> None:
    text = textwrap.dedent(
        """
        apiVersion: ticket/v1
        kind: Ticket
        metadata: {id: t, title: T, type: task}
        watch:
          - path: "state.json"
            events: [modify]
            trigger: looped
        """
    )
    with pytest.raises(ManifestValidationError, match="circular-watch guard"):
        parse_manifest(text, ticket_id="t")


def test_unknown_shell_is_warning_not_error() -> None:
    text = textwrap.dedent(
        """
        apiVersion: ticket/v1
        kind: Ticket
        metadata: {id: t, title: T, type: task}
        actions:
          a: {run: ./x, shell: ruby}
        """
    )
    manifest = parse_manifest(text, ticket_id="t")
    warnings = validate_manifest(manifest)
    assert any("unknown shell 'ruby'" in w for w in warnings)


def test_load_manifest_from_disk_derives_ticket_id(tmp_path, valid_manifest_text: str) -> None:
    ticket_dir = tmp_path / "build-report.ticket"
    ticket_dir.mkdir()
    path = ticket_dir / "MANIFEST.yaml"
    path.write_text(valid_manifest_text, encoding="utf-8")

    assert ticket_id_from_dir(ticket_dir) == "build-report"
    manifest = load_manifest(path)
    assert manifest.metadata.id == "build-report"
    assert manifest.source_path == path
