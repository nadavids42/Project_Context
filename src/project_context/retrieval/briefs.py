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

Per-fact construction (`current_state_fact`/`transition_fact`/owner and
evidence resolution) lives in `project_context.retrieval.brief_facts`,
shared verbatim with `project_context.retrieval.meeting_prep` (Prompt
14) — only section *selection* is specific to this module.
"""

from __future__ import annotations

import sqlite3

from project_context.db import ledger_repository
from project_context.domain.briefs import (
    CURRENT_BRIEF_SECTIONS,
    BriefFact,
    BriefFactSection,
    BriefFactType,
    CurrentProjectBriefFacts,
)
from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus
from project_context.ids import new_id
from project_context.retrieval.brief_facts import (
    current_state_fact,
    current_state_facts,
    sorted_open_items,
    transition_fact,
)
from project_context.services.projects import get_project
from project_context.timeutil import utc_now_iso

#: How many of the most recent transitions "Recent Changes" surfaces — a
#: bound that keeps the fact payload (and Stage C token cost)
#: proportional to a "compact" project, not a business rule.
RECENT_CHANGES_LIMIT = 15

_OPEN_STATUSES = (LedgerItemStatus.OPEN, LedgerItemStatus.ACTIVE)


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
            transition_fact(conn, project_id, "recent_changes", version, item.kind)
        )
    sections.append(
        BriefFactSection(
            section="recent_changes", heading="Recent Changes", facts=tuple(recent_facts)
        )
    )

    # --- next_milestone: the single nearest open/active milestone. ------
    milestones = sorted_open_items(
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
            current_state_fact(conn, project_id, "next_milestone", milestones[0]),
        )
    sections.append(
        BriefFactSection(
            section="next_milestone", heading="Next Milestone", facts=next_milestone_facts
        )
    )

    # --- open_commitments -----------------------------------------------
    commitments = sorted_open_items(
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
            facts=current_state_facts(conn, project_id, "open_commitments", commitments),
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
            facts=current_state_facts(conn, project_id, "decisions", decisions),
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
            facts=current_state_facts(conn, project_id, "risks_and_blockers", risk_items),
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
            facts=current_state_facts(conn, project_id, "unresolved_questions", questions),
        )
    )

    expected_keys = tuple(key for key, _heading in CURRENT_BRIEF_SECTIONS)
    assert tuple(s.section for s in sections) == expected_keys
    return CurrentProjectBriefFacts(
        project_id=project_id, generated_at=generated_at, sections=tuple(sections)
    )
