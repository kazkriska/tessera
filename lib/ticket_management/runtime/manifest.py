"""Manifest loader and validator.

Responsibility (Master Part V §4.1–§4.2, RFC-0003; CONTRACTS §3 and §4):
load `ticket.yaml`, reject YAML anchors/aliases/merge keys by scanning the
event stream before `safe_load`, validate the canonical top-level key set
(apiVersion, kind, metadata, runtime, initialize, hooks, actions, permissions,
env, watch, exports), enforce `metadata.id == dir basename` and the
circular-watch guard.

TODO(Phase A): implement models + loader + validator.
"""
