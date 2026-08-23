"""Deterministic Current Project Brief fact builder (Section 8
"Retrieval"; Section 5.8; FR-024; ADR-011).

Pure SQL/domain-repository queries over one project's *accepted* ledger
state — current `ledger_items` and their most recent `ledger_versions`,
plus the project record itself. Never touches `source_chunks` or
`observations`, and never runs a keyword/semantic search: Section 8 is
explicit that "brief retrieval begins with SQL over current ledger
state, not semantic search." The output (`CurrentProjectBriefFacts`) is
the *only* thing `project_context.services.briefs` ever hands to the
Stage C model — it never sees raw evidence text.

Every query here takes `project_id` as a required, non-optional
parameter and every underlying repository call is itself project-scoped
(FR-022) — there is no code path in this module capable of returning
another project's fact.
"""

from __future__ import annotations

import sqlite3

from project_context.db import evidence_link_repository, ledger_repository, people_repository
from project_context.domain.briefs import (
    CURRENT_BRIEF_SECTIONS,
    BriefFact,
    BriefFactSection,
    BriefFactType,
    CurrentProjectBriefFacts,
)
from project_context.domain.evidence_links import EvidenceLinkTargetType
from project_context.domain.ledger import (
    LedgerItem,
    LedgerItemKind,
    LedgerItemStatus,
    LedgerVersion,
)
from project_context.ids import new_id
from project_context.services.projects import get_project
from project_context.timeutil import utc_now_iso

#: How many of the most recent transitions "Recent Changes" surfaces — a
#: bound that keeps the fact payload (and Stage C token cost)
#: proportional to a "compact" project, not a business rule.
RECENT_CHANGES_LIMIT = 15

_OPEN_STATUSES = (LedgerItemStatus.OPEN, LedgerItemStatus.ACTIVE)


def _owner_name(conn: sqlite3.Connection, owner_person_id: str | None) -> str | None:
    if owner_person_id is None:
        return None
    person = people_repository.get_person(conn, owner_person_id)
    return person.display_name if person is not None else None


def _evidence_link_ids(
    conn: sqlite3.Connection, project_id: str, target_type: EvidenceLinkTargetType, target_id: str
) -> tuple[str, ...]:
    links = evidence_link_repository.list_for_target(conn, project_id, target_type, target_id)
    return tuple(link.id for link in links)


def _current_state_fact(
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
        owner_name=_owner_name(conn, item.owner_person_id),
        due_date=item.due_date,
        effective_at=item.effective_at,
        evidence_link_ids=_evidence_link_ids(
            conn, project_id, EvidenceLinkTargetType.LEDGER_ITEM, item.id
        ),
    )


def _previous_summary(
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
        changes.append(f"was owned by {_owner_name(conn, prior.owner_person_id) or 'not stated'}")
    if prior.status != version.status:
        changes.append(f"was {prior.status.value}")
    if prior.canonical_title != version.canonical_title:
        changes.append(f"was titled {prior.canonical_title!r}")
    return "; ".join(changes) if changes else None


def _transition_fact(
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
        owner_name=_owner_name(conn, version.owner_person_id),
        due_date=version.due_date,
        effective_at=version.effective_at,
        transition_type=version.transition_type.value,
        previous_summary=_previous_summary(conn, project_id, version),
        evidence_link_ids=_evidence_link_ids(
            conn, project_id, EvidenceLinkTargetType.LEDGER_VERSION, version.id
        ),
    )


def _sort_key(item: LedgerItem) -> tuple[bool, str, str]:
    return (item.due_date is None, item.due_date or "", item.canonical_title)


def _sorted_open_items(items: list[LedgerItem]) -> list[LedgerItem]:
    return sorted(items, key=_sort_key)


def _current_state_facts(
    conn: sqlite3.Connection, project_id: str, section: str, items: list[LedgerItem]
) -> tuple[BriefFact, ...]:
    return tuple(_current_state_fact(conn, project_id, section, item) for item in items)


def build_current_project_brief_facts(
    conn: sqlite3.Connection, project_id: str
) -> CurrentProjectBriefFacts:
    """Build the full, deterministic Current Project Brief fact set for
    one project (FR-024's eight required sections, Section 5.8). Raises
    `project_context.services.projects.ProjectNotFoundError` if
    `project_id` does not exist — the same explicit-scope failure every
    other project-scoped read in this codebase raises rather than
    silently returning nothing.
    """
    project = get_project(conn, project_id)
    generated_at = utc_now_iso()
    sections: list[BriefFactSection] = []

    # --- objective_and_scope: the project record itself. Always exactly
    # one fact — a project's objective is a required, non-blank field —
    # so this section is never "missing." ------------------------------
    scope_fact = BriefFact(
        fact_id=new_id(),
        section="objective_and_scope",
        fact_type=BriefFactType.PROJECT_META,
        title=project.objective,
        detail=project.description,
    )
    sections.append(
        BriefFactSection(
            section="objective_and_scope", heading="Objective and Scope", facts=(scope_fact,)
        )
    )

    # --- current_stage: honestly empty when not stated. -----------------
    stage_facts: tuple[BriefFact, ...] = ()
    if project.stage:
        stage_facts = (
            BriefFact(
                fact_id=new_id(),
                section="current_stage",
                fact_type=BriefFactType.PROJECT_META,
                title=project.stage,
            ),
        )
    sections.append(
        BriefFactSection(section="current_stage", heading="Current Stage", facts=stage_facts)
    )

    # --- recent_changes: the most recent transitions across every item. -
    recent_versions = ledger_repository.list_recent_versions_for_project(
        conn, project_id, limit=RECENT_CHANGES_LIMIT
    )
    recent_facts = []
    for version in recent_versions:
        item = ledger_repository.get_item(conn, project_id, version.ledger_item_id)
        if item is None:
            continue  # defensive only; a version's item is never deleted
        recent_facts.append(
            _transition_fact(conn, project_id, "recent_changes", version, item.kind)
        )
    sections.append(
        BriefFactSection(
            section="recent_changes", heading="Recent Changes", facts=tuple(recent_facts)
        )
    )

    # --- next_milestone: the single nearest open/active milestone. ------
    milestones = _sorted_open_items(
        [
            item
            for item in ledger_repository.list_items_for_project(
                conn, project_id, kind=LedgerItemKind.MILESTONE
            )
            if item.status in _OPEN_STATUSES
        ]
    )
    next_milestone_facts: tuple[BriefFact, ...] = ()
    if milestones:
        next_milestone_facts = (
            _current_state_fact(conn, project_id, "next_milestone", milestones[0]),
        )
    sections.append(
        BriefFactSection(
            section="next_milestone", heading="Next Milestone", facts=next_milestone_facts
        )
    )

    # --- open_commitments -----------------------------------------------
    commitments = _sorted_open_items(
        [
            item
            for item in ledger_repository.list_items_for_project(
                conn, project_id, kind=LedgerItemKind.COMMITMENT
            )
            if item.status in _OPEN_STATUSES
        ]
    )
    sections.append(
        BriefFactSection(
            section="open_commitments",
            heading="Open Commitments",
            facts=_current_state_facts(conn, project_id, "open_commitments", commitments),
        )
    )

    # --- decisions: currently-active decisions only (history lives in
    # recent_changes/the item's own version history, not here). ---------
    decisions = sorted(
        (
            item
            for item in ledger_repository.list_items_for_project(
                conn, project_id, kind=LedgerItemKind.DECISION
            )
            if item.status is LedgerItemStatus.ACTIVE
        ),
        key=lambda item: item.canonical_title,
    )
    sections.append(
        BriefFactSection(
            section="decisions",
            heading="Decisions",
            facts=_current_state_facts(conn, project_id, "decisions", decisions),
        )
    )

    # --- risks_and_blockers ----------------------------------------------
    risk_items = sorted(
        (
            item
            for kind in (LedgerItemKind.RISK, LedgerItemKind.BLOCKER)
            for item in ledger_repository.list_items_for_project(conn, project_id, kind=kind)
            if item.status in _OPEN_STATUSES
        ),
        key=lambda item: (item.kind.value, item.canonical_title),
    )
    sections.append(
        BriefFactSection(
            section="risks_and_blockers",
            heading="Risks and Blockers",
            facts=_current_state_facts(conn, project_id, "risks_and_blockers", risk_items),
        )
    )

    # --- unresolved_questions ---------------------------------------------
    questions = sorted(
        (
            item
            for item in ledger_repository.list_items_for_project(
                conn, project_id, kind=LedgerItemKind.OPEN_QUESTION
            )
            if item.status is LedgerItemStatus.OPEN
        ),
        key=lambda item: item.canonical_title,
    )
    sections.append(
        BriefFactSection(
            section="unresolved_questions",
            heading="Unresolved Questions",
            facts=_current_state_facts(conn, project_id, "unresolved_questions", questions),
        )
    )

    expected_keys = tuple(key for key, _heading in CURRENT_BRIEF_SECTIONS)
    assert tuple(s.section for s in sections) == expected_keys
    return CurrentProjectBriefFacts(
        project_id=project_id, generated_at=generated_at, sections=tuple(sections)
    )
