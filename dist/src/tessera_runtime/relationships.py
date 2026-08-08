"""Tessera v1 — relationship index builder (B2).

Builds a full adjacency index over all graph edge types declared in
CONTRACTS.md §2:

    parent, children, depends_on, blocks, duplicates, references,
    related_to, spawned_from, delegated_to

``children`` is a convenience mirror: it is derived from ``parent`` edges
so that callers can walk the graph in either direction without computing
the inverse themselves.

Single-value relationships (``parent``, ``spawned_from``, ``delegated_to``)
are stored as a set of size 0 or 1 for uniformity.  Array-valued
relationships are stored as sets of ticket ids.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

__all__ = [
    "ALL_RELATIONSHIP_TYPES",
    "SINGLE_VALUE_RELATIONSHIPS",
    "build_relationship_index",
    "validate_relationships",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
ALL_RELATIONSHIP_TYPES: list[str] = [
    "parent",
    "children",
    "depends_on",
    "blocks",
    "duplicates",
    "references",
    "related_to",
    "spawned_from",
    "delegated_to",
]

SINGLE_VALUE_RELATIONSHIPS: set[str] = {"parent", "spawned_from", "delegated_to"}


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _normalize_value(rel_type: str, value: object) -> set[str]:
    """Coerce a raw metadata field value into a uniform ``set[str]``."""
    if rel_type in SINGLE_VALUE_RELATIONSHIPS:
        if isinstance(value, str) and value:
            return {value}
        return set()
    # array-valued relationship
    if isinstance(value, list):
        return {str(v) for v in value if isinstance(v, str) and v}
    return set()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_relationship_index(repo_path: str) -> dict[str, dict[str, set[str]]]:
    """Return the full relationship index for *repo_path*.

    The index maps each discovered ticket id to a dict of relationship
    types to the set of ids referenced by that ticket:

        {
            "T-001": {
                "parent": set(),
                "children": {"T-002"},
                "depends_on": {"T-003"},
                ...
            },
            ...
        }

    Every ticket discovered in ``<repo_path>/TicketsRepository`` appears as
    a top-level key, even when it has no relationships.  ``children`` is
    derived by inverting ``parent`` edges.
    """
    repo = Path(repo_path).resolve()
    tickets_root = repo / "TicketsRepository"
    index: dict[str, dict[str, set[str]]] = {}

    if not tickets_root.is_dir():
        return index

    # Pass 1 — read direct metadata fields.
    for metadata_path in tickets_root.rglob("metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        ticket_id = metadata.get("id")
        if not ticket_id:
            continue

        if ticket_id not in index:
            index[ticket_id] = {rel: set() for rel in ALL_RELATIONSHIP_TYPES}

        for rel_type in ALL_RELATIONSHIP_TYPES:
            value = metadata.get(rel_type)
            index[ticket_id][rel_type] = _normalize_value(rel_type, value)

    # Pass 2 — derive ``children`` as the inverse of ``parent``.
    for ticket_id, rels in index.items():
        parent_ids = rels.get("parent", set())
        for parent_id in parent_ids:
            if parent_id in index:
                index[parent_id]["children"].add(ticket_id)

    return index


def validate_relationships(
    index: dict[str, dict[str, set[str]]],
    known_ids: set[str],
) -> list[str]:
    """Return a list of warning strings for dangling references.

    A reference is *dangling* when the target id is not present in
    *known_ids*.  The function never raises — callers are expected to
    log or surface the returned warnings per Part IV §8.
    """
    warnings: list[str] = []
    for ticket_id in sorted(index):
        rels = index[ticket_id]
        for rel_type in sorted(rels):
            targets = rels[rel_type]
            for target_id in sorted(targets):
                if target_id not in known_ids:
                    warnings.append(
                        f"Ticket '{ticket_id}' has dangling reference in "
                        f"'{rel_type}': '{target_id}'"
                    )
    return warnings
