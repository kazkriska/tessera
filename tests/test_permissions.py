"""Tests for the capability permission model (F1)."""

from __future__ import annotations

import pytest

from tessera_runtime.runtime.permissions import (
    PermissionSet,
    effective_permissions,
    enforce,
    escalate,
)


def test_from_manifest_parses_capabilities() -> None:
    ps = PermissionSet.from_manifest(
        {
            "capabilities": ["fs.read:data", "fs.write:data", "net.http:api.example.com", "secrets", "run:bash", "exec.bg"],
            "escalations": {"deploy": ["fs.write:deploy", "secrets"]},
        }
    )
    assert ps.has("fs.read:data")
    assert ps.has("fs.write:data")
    assert ps.has("net.http:api.example.com")
    assert ps.has("secrets")
    assert ps.has("run:bash")
    assert ps.has("exec.bg")
    assert "deploy" in ps.escalations
    assert ps.escalations["deploy"] == frozenset({"fs.write:deploy", "secrets"})


def test_has_exact_and_prefix_match() -> None:
    ps = PermissionSet.from_manifest(["fs.write:data", "net.http:example.com"])
    # exact
    assert ps.has("fs.write:data")
    # prefix: dir grant covers children
    assert ps.has("fs.write:data/reports")
    assert ps.has("fs.write:data/sub/deep.txt")
    # sibling NOT covered
    assert not ps.has("fs.write:other")
    # host prefix NOT covered
    assert not ps.has("net.http:example.com.evil.org")
    assert ps.has("net.http:example.com")
    # atomic
    empty = PermissionSet.from_manifest([])
    assert not empty.has("secrets")


def test_escalation_requires_explicit_grant() -> None:
    ps = PermissionSet.from_manifest(
        {"capabilities": ["fs.read:data"], "escalations": {"deploy": ["fs.write:deploy"]}}
    )
    assert not ps.has("fs.write:deploy")
    escalated = escalate(ps, "deploy", ["fs.write:deploy"])
    assert escalated.has("fs.write:deploy")
    assert ps.has("fs.read:data")  # original unchanged
    with pytest.raises(PermissionError):
        escalate(ps, "deploy", ["fs.write:elsewhere"])
    with pytest.raises(PermissionError):
        escalate(ps, "undeclared-target", ["fs.write:deploy"])


def test_enforce_fs_write_allowed_and_denied() -> None:
    ps = PermissionSet.from_manifest(["fs.write:data"])
    # allowed: path under jail root and covered by grant
    enforce(ps, "fs.write", {"path": "data/out.txt", "jail_root": "/tickets/T-1.ticket"})
    # denied: capability not granted
    with pytest.raises(PermissionError):
        enforce(ps, "fs.write", {"path": "other/out.txt", "jail_root": "/tickets/T-1.ticket"})
    # denied: path escapes jail root
    with pytest.raises(PermissionError):
        enforce(ps, "fs.write", {"path": "../etc/passwd", "jail_root": "/tickets/T-1.ticket"})


def test_enforce_net_http_host() -> None:
    ps = PermissionSet.from_manifest(["net.http:api.example.com"])
    enforce(ps, "net.http", {"host": "api.example.com"})
    with pytest.raises(PermissionError):
        enforce(ps, "net.http", {"host": "evil.example.com"})


def test_denylist_rejects_path_traversal() -> None:
    with pytest.raises(ValueError):
        PermissionSet.from_manifest(["fs.write:data/../../etc"])
    with pytest.raises(ValueError):
        PermissionSet.from_manifest({"capabilities": ["fs.read:../secret"]})
    # atomic caps are fine; unknown caps rejected
    with pytest.raises(ValueError):
        PermissionSet.from_manifest(["nonsense:cap"])


def test_effective_permissions_merge() -> None:
    merged = effective_permissions(
        {
            # metadata defaults
            "permissions": {"capabilities": ["fs.read:data", "secrets"]},
            # manifest permissions (wins)
            "manifest_permissions": {
                "capabilities": ["fs.read:data", "fs.write:data"],
                "escalations": {"deploy": ["secrets"]},
            },
        }
    )
    caps = set(merged["capabilities"])
    assert "fs.read:data" in caps
    assert "fs.write:data" in caps
    assert "secrets" in caps  # merged from metadata defaults
    assert merged["escalations"] == {"deploy": ["secrets"]}


def test_from_manifest_accepts_documented_part_ix_grammar() -> None:
    """Part IX §4.1 / Part V §4.2 grammar: filesystem/network/subprocess/secrets."""
    ps = PermissionSet.from_manifest(
        {
            "filesystem": {"read": ["task/**", "metadata.json"], "write": ["state.json"]},
            "network": True,
            "subprocess": True,
            "secrets": False,
        }
    )
    # read dir grant covers children
    assert ps.has("fs.read:task/report.txt")
    assert ps.has("fs.read:metadata.json")
    assert ps.has("fs.write:state.json")
    assert not ps.has("fs.write:task/other.txt")
    # network true -> wildcard host grant
    assert ps.has("net.http:api.example.com")
    # subprocess true -> any shell
    assert ps.has("run:bash")
    assert not ps.has("secrets")


def test_defaults_when_no_permissions_declared() -> None:
    """Part IX §4.2 safe baseline applies when the manifest declares nothing."""
    ps = PermissionSet.from_manifest(None)
    assert ps.has("fs.read:.")
    assert ps.has("fs.write:state.json")
    assert ps.has("fs.write:activity.jsonl")
    assert ps.has("run:bash")
    assert not ps.has("net.http:api.example.com")
    assert not ps.has("secrets")
