"""Shared deterministic `BriefFact` builders (Section 8 "Retrieval";
ADR-011; Prompt 14).

Extracted from `project_context.retrieval.briefs` (Prompt 9) so
`project_context.retrieval.meeting_prep` (Prompt 14) can build the exact
same kind of fact — one ledger item's current accepted state, or one
accepted transition, with its owner name resolved and its evidence-link
IDs attached — without duplicating that logic or letting the two brief
types' fact shapes drift apart. Current Project Brief and Meeting
Preparation Brief differ in *which* items/transitions they select and
*how* they group/section them, never in what one fact record contains
or how its evidence is resolved.

Every function here takes `project_id` as a required parameter and
delegates project-scoping to the underlying repositories (FR-022) —
nothing here can return another project's fact.
"""

from __future__ import annotations

import sqlite3

from project_context.db import evidence_link_repository, ledger_repository, people_repository
from project_context.domain.briefs import BriefFact, BriefFactType
from project_context.domain.evidence_links import EvidenceLinkTargetType
from project_context.domain.ledger import LedgerItem, LedgerItemKind, LedgerVersion
from project_context.ids import new_id


def owner_name(conn: sqlite3.Connection, owner_person_id: str | None) -> str | None:
    if owner_person_id is None:
        return None
    person = people_repository.get_person(conn, owner_person_id)
    return person.display_name if person is not None else None


def evidence_link_ids(
    conn: sqlite3.Connection, project_id: str, target_type: EvidenceLinkTargetType, target_id: str
) -> tuple[str, ...]:
    links = evidence_link_repository.list_for_target(conn, project_id, target_type, target_id)
    return tuple(link.id for link in links)


def current_state_fact(
    conn: sqlite3.Connection, project_id: str, section: str, item: LedgerItem
) -> BriefFact:
    return BriefFact(
        fact_id=new_id(),
        section=section,
        fact_type=BriefFactType.CURRENT_STATE,
        kind=item.kind.value,
        ledger_item_id=item.id,
        ledger_version_id=item.current_version_id,
        title=item.canonical_title,
        detail=item.canonical_description,
        status=item.status.value,
        owner_name=owner_name(conn, item.owner_person_id),
        due_date=item.due_date,
        effective_at=item.effective_at,
        evidence_link_ids=evidence_link_ids(
            conn, project_id, EvidenceLinkTargetType.LEDGER_ITEM, item.id
        ),
    )


def current_state_facts(
    conn: sqlite3.Connection, project_id: str, section: str, items: list[LedgerItem]
) -> tuple[BriefFact, ...]:
    return tuple(current_state_fact(conn, project_id, section, item) for item in items)


def previous_summary(
    conn: sqlite3.Connection, project_id: str, version: LedgerVersion
) -> str | None:
    """A short, deterministic description of what a transition changed
    *from* (Section 12.3: "current value... and change type"), computed
    by diffing this version's own snapshot against its immediate
    predecessor's — never invented, never asked of the model."""
    if version.from_version_id is None:
        return None
    prior = ledger_repository.get_version(conn, project_id, version.from_version_id)
    if prior is None:
        return None
    changes: list[str] = []
    if prior.due_date != version.due_date:
        changes.append(f"was due {prior.due_date or 'not stated'}")
    if prior.owner_person_id != version.owner_person_id:
        changes.append(f"was owned by {owner_name(conn, prior.owner_person_id) or 'not stated'}")
    if prior.status != version.status:
        changes.append(f"was {prior.status.value}")
    if prior.canonical_title != version.canonical_title:
        changes.append(f"was titled {prior.canonical_title!r}")
    return "; ".join(changes) if changes else None


def transition_fact(
    conn: sqlite3.Connection,
    project_id: str,
    section: str,
    version: LedgerVersion,
    kind: LedgerItemKind,
) -> BriefFact:
    return BriefFact(
        fact_id=new_id(),
        section=section,
        fact_type=BriefFactType.TRANSITION,
        kind=kind.value,
        ledger_item_id=version.ledger_item_id,
        ledger_version_id=version.id,
        title=version.canonical_title,
        detail=version.canonical_description,
        status=version.status.value,
        owner_name=owner_name(conn, version.owner_person_id),
        due_date=version.due_date,
        effective_at=version.effective_at,
        transition_type=version.transition_type.value,
        previous_summary=previous_summary(conn, project_id, version),
        evidence_link_ids=evidence_link_ids(
            conn, project_id, EvidenceLinkTargetType.LEDGER_VERSION, version.id
        ),
    )


def sort_key(item: LedgerItem) -> tuple[bool, str, str]:
    return (item.due_date is None, item.due_date or "", item.canonical_title)


def sorted_open_items(items: list[LedgerItem]) -> list[LedgerItem]:
    return sorted(items, key=sort_key)
