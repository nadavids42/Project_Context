"""Deterministic Meeting Preparation Brief fact builder (Section 8
"Retrieval"; Section 5.9; FR-025; Prompt 14).

Mirrors `project_context.retrieval.briefs`'s shape exactly — pure SQL/
domain-repository queries over one project's *accepted* ledger state,
never evidence text, never a keyword/semantic search — reusing its
per-fact construction (`project_context.retrieval.brief_facts`)
verbatim. What is new here is meeting-specific: selecting a meeting
(a stored Calendar/Fathom artifact, or manual entry — Section 5.9:
"Always provide a manual fallback... so Calendar remains optional"),
determining the previous relevant meeting and its cutoff, and resolving
participant identities.

## Determining the previous meeting and its cutoff

"The previous relevant meeting" (Section 5.9) is the most recent
`source_artifacts` row of artifact_type `calendar_event` or `meeting`
in this project whose `occurred_at` is strictly before the selected/
entered meeting's own time — the same two artifact types the Meeting
Preparation Brief itself can select from (`list_meeting_candidates`).
When none exists (a project's first tracked meeting, or a project with
no meeting-type evidence at all), the cutoff falls back to the
project's own `created_at` — "changes since the project began" is
always a well-defined, always-resolvable cutoff, never an invented
date. Either way the exact cutoff and, when found, the previous
meeting's artifact ID are returned so the caller can show and let the
user override both (Section 5.9: "Permit the user to override the
prior-meeting cutoff before generation").

## Participant resolution

Section 5.9: "Resolve participant identities by exact email first,
known alias second, and explicit unresolved state third. Do not guess
between ambiguous names." `resolve_participants` is a thin per-line
wrapper around `project_context.db.people_repository.resolve_person`
(Prompt 6), which already implements exactly that precedence — nothing
here re-implements resolution.

## Decisions required vs. unanswered questions — a documented split

Section 5.9 requires two distinct sections drawn from ledger state:
"decisions required" and "unanswered questions." The ledger has exactly
one item kind for either (`open_question`; a `decision` item, by
`project_context.domain.ledger`'s own design, is only ever created once
a decision has actually been *made* — it has no "pending" status at
all, so it cannot represent a decision that still needs to be made).
Splitting the two sections therefore cannot come from `kind` alone.
This module uses the one already-accepted, already-structured signal
that distinguishes them without any model involvement: an open question
with a resolved `owner_person_id` — someone specific is on the hook to
decide — is "decisions required"; an open question with no owner is
"unanswered questions." This is a deliberate, documented interpretation
(not specified verbatim by the plan), chosen because it is fully
deterministic, uses only already-accepted ledger fields, and requires
no new schema or model classification.
"""

from __future__ import annotations

import re
import sqlite3

from project_context.db import evidence_repository, ledger_repository, people_repository
from project_context.domain.briefs import BriefFact, BriefFactSection, BriefFactType
from project_context.domain.evidence import ArtifactType, SourceArtifact
from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus
from project_context.domain.meeting_prep import (
    MEETING_PREP_SECTIONS,
    MeetingInfo,
    MeetingPrepBriefFacts,
    ResolvedParticipant,
)
from project_context.domain.projects import Project
from project_context.ids import new_id
from project_context.retrieval.brief_facts import current_state_facts, transition_fact
from project_context.services.projects import get_project
from project_context.timeutil import utc_now_iso

#: Bound on how many accepted transitions "Changes Since Previous
#: Meeting" surfaces — mirrors `project_context.retrieval.briefs.
#: RECENT_CHANGES_LIMIT`'s "proportional to a compact project" rationale,
#: just cutoff-bounded instead of count-bounded first.
CHANGES_SINCE_PREVIOUS_LIMIT = 200

_MEETING_ARTIFACT_TYPES = (ArtifactType.CALENDAR_EVENT, ArtifactType.MEETING)
_OPEN_STATUSES = (LedgerItemStatus.OPEN, LedgerItemStatus.ACTIVE)

_EMAIL_RE = re.compile(r"[^\s<>]+@[^\s<>]+\.[^\s<>]+")
_ANGLE_EMAIL_RE = re.compile(r"<\s*([^<>\s]+)\s*>")


class MeetingArtifactNotFoundError(LookupError):
    """Raised when a given `meeting_artifact_id` does not resolve to an
    existing artifact in the given project."""


def list_meeting_candidates(conn: sqlite3.Connection, project_id: str) -> list[SourceArtifact]:
    """Every Calendar/Fathom meeting-type artifact in this project,
    most recent first; an artifact with no known `occurred_at` sorts
    last — the Meeting Preparation Brief's meeting selector source
    list."""
    candidates = [
        a
        for a in evidence_repository.list_artifacts(conn, project_id)
        if a.artifact_type in _MEETING_ARTIFACT_TYPES
    ]
    with_time = sorted(
        (a for a in candidates if a.occurred_at is not None),
        key=lambda a: a.occurred_at,
        reverse=True,
    )
    without_time = [a for a in candidates if a.occurred_at is None]
    return with_time + without_time


def find_previous_meeting_artifact(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    before_at: str | None,
    exclude_artifact_id: str | None = None,
) -> SourceArtifact | None:
    """The most recent meeting-type artifact strictly before `before_at`
    (module docstring). `before_at=None` (no reference time at all —
    manual entry with no scheduled time given) never matches anything,
    since "before an unknown time" is not a well-defined comparison."""
    if before_at is None:
        return None
    candidates = [
        a
        for a in evidence_repository.list_artifacts(conn, project_id)
        if a.artifact_type in _MEETING_ARTIFACT_TYPES
        and a.occurred_at is not None
        and a.occurred_at < before_at
        and a.id != exclude_artifact_id
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda a: a.occurred_at)


def compute_cutoff(
    conn: sqlite3.Connection,
    project: Project,
    *,
    reference_at: str | None,
    exclude_artifact_id: str | None = None,
) -> tuple[str, SourceArtifact | None]:
    """`(cutoff_at, previous_meeting_artifact)` — the previous meeting's
    own `occurred_at` when one is found, else the project's own
    `created_at` (module docstring's documented fallback)."""
    previous = find_previous_meeting_artifact(
        conn, project.id, before_at=reference_at, exclude_artifact_id=exclude_artifact_id
    )
    if previous is not None and previous.occurred_at:
        return previous.occurred_at, previous
    return project.created_at, None


def parse_participant_line(line: str) -> tuple[str | None, str | None]:
    """Split one free-text participant line into `(name, email)`.
    Accepts `"Name <email>"`, a bare email, or a bare name — the same
    three shapes calendar/Fathom attendee text already uses elsewhere
    in this codebase. Never raises; an unparseable line degrades to
    `(line, None)`."""
    stripped = line.strip()
    if not stripped:
        return None, None
    angle_match = _ANGLE_EMAIL_RE.search(stripped)
    if angle_match:
        email = angle_match.group(1).strip()
        name = stripped[: angle_match.start()].strip().strip(",") or None
        return name, email or None
    email_match = _EMAIL_RE.fullmatch(stripped)
    if email_match:
        return None, stripped
    return stripped, None


def resolve_participants(
    conn: sqlite3.Connection, raw_lines: tuple[str, ...]
) -> tuple[ResolvedParticipant, ...]:
    """Resolve each non-blank line in `raw_lines` to a canonical person
    (module docstring: email first, alias second, explicit unresolved
    third — `people_repository.resolve_person` unchanged)."""
    resolved: list[ResolvedParticipant] = []
    for raw in raw_lines:
        if not raw.strip():
            continue
        name, email = parse_participant_line(raw)
        resolution = people_repository.resolve_person(conn, email=email, name=name)
        display_name = name or email or raw.strip()
        if resolution.outcome == "resolved" and resolution.person_id:
            person = people_repository.get_person(conn, resolution.person_id)
            if person is not None:
                display_name = person.display_name
        resolved.append(
            ResolvedParticipant(
                raw_input=raw.strip(),
                raw_name=name,
                raw_email=email,
                resolution=resolution,
                display_name=display_name,
            )
        )
    return tuple(resolved)


def _open_question_facts(
    conn: sqlite3.Connection, project_id: str
) -> tuple[tuple[BriefFact, ...], tuple[BriefFact, ...]]:
    """`(decisions_required, unanswered_questions)` — module docstring's
    documented owner-presence split of open `open_question` items."""
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
    decisions_required = [item for item in questions if item.owner_person_id is not None]
    unanswered = [item for item in questions if item.owner_person_id is None]
    return (
        current_state_facts(conn, project_id, "decisions_required", decisions_required),
        current_state_facts(conn, project_id, "unanswered_questions", unanswered),
    )


def build_meeting_prep_facts(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    meeting_artifact_id: str | None = None,
    manual_title: str | None = None,
    manual_purpose: str | None = None,
    manual_scheduled_at: str | None = None,
    participant_lines: tuple[str, ...] = (),
    cutoff_override: str | None = None,
) -> MeetingPrepBriefFacts:
    """Build the full, deterministic Meeting Preparation Brief fact set
    (FR-025's seven required sections, Section 5.9).

    Exactly one of `meeting_artifact_id` or `manual_title` should be
    given — a selected Calendar/Fathom meeting, or a fully manual entry
    (Section 5.9's Calendar-optional fallback); `manual_purpose`/
    `manual_scheduled_at` may still override a selected artifact's own
    title/time when supplied. Raises
    `project_context.services.projects.ProjectNotFoundError` if
    `project_id` does not exist, and
    `MeetingArtifactNotFoundError` if `meeting_artifact_id` is given but
    does not resolve within this project.
    """
    project = get_project(conn, project_id)
    generated_at = utc_now_iso()

    selected_artifact: SourceArtifact | None = None
    if meeting_artifact_id is not None:
        selected_artifact = evidence_repository.get_artifact(conn, project_id, meeting_artifact_id)
        if selected_artifact is None:
            raise MeetingArtifactNotFoundError(
                f"meeting artifact {meeting_artifact_id!r} not found in project {project_id!r}"
            )

    artifact_title = selected_artifact.title if selected_artifact else None
    artifact_occurred_at = selected_artifact.occurred_at if selected_artifact else None
    title = manual_title or artifact_title or "Untitled meeting"
    # Never guessed from the title — "unknown" stays unknown (module
    # docstring's Section 5.9 fidelity) unless the user actually states
    # a purpose, whether from a manual entry or a future edit.
    purpose = manual_purpose
    scheduled_at = manual_scheduled_at or artifact_occurred_at
    external_url = selected_artifact.external_url if selected_artifact else None

    cutoff_at, previous_artifact = compute_cutoff(
        conn, project, reference_at=scheduled_at, exclude_artifact_id=meeting_artifact_id
    )
    if cutoff_override:
        cutoff_at = cutoff_override

    participants = resolve_participants(conn, participant_lines)
    meeting = MeetingInfo(
        title=title,
        purpose=purpose,
        scheduled_at=scheduled_at,
        meeting_artifact_id=meeting_artifact_id,
        external_url=external_url,
        participants=participants,
    )

    sections: list[BriefFactSection] = []

    # --- meeting_purpose: the meeting record itself, self-evidencing. ---
    purpose_fact = BriefFact(
        fact_id=new_id(),
        section="meeting_purpose",
        fact_type=BriefFactType.MEETING_META,
        title=title,
        detail=purpose,
        effective_at=scheduled_at,
    )
    sections.append(
        BriefFactSection(
            section="meeting_purpose", heading="Meeting Purpose", facts=(purpose_fact,)
        )
    )

    # --- changes_since_previous ------------------------------------------
    versions = ledger_repository.list_versions_since(
        conn, project_id, cutoff_at, limit=CHANGES_SINCE_PREVIOUS_LIMIT
    )
    change_facts = []
    for version in versions:
        item = ledger_repository.get_item(conn, project_id, version.ledger_item_id)
        if item is None:
            continue
        change_facts.append(
            transition_fact(conn, project_id, "changes_since_previous", version, item.kind)
        )
    sections.append(
        BriefFactSection(
            section="changes_since_previous",
            heading="Changes Since Previous Meeting",
            facts=tuple(change_facts),
        )
    )

    # --- outstanding_commitments (grouping by owner happens at render
    # time, using each fact's own owner_name — Section 5.9). -------------
    commitments = sorted(
        (
            item
            for item in ledger_repository.list_items_for_project(
                conn, project_id, kind=LedgerItemKind.COMMITMENT
            )
            if item.status in _OPEN_STATUSES
        ),
        key=lambda item: (item.owner_person_id is None, item.due_date is None, item.due_date or ""),
    )
    sections.append(
        BriefFactSection(
            section="outstanding_commitments",
            heading="Outstanding Commitments",
            facts=current_state_facts(conn, project_id, "outstanding_commitments", commitments),
        )
    )

    # --- decisions_required / unanswered_questions (module docstring) ---
    decisions_required_facts, unanswered_facts = _open_question_facts(conn, project_id)
    sections.append(
        BriefFactSection(
            section="decisions_required",
            heading="Decisions Required",
            facts=decisions_required_facts,
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
            heading="Risks Requiring Discussion",
            facts=current_state_facts(conn, project_id, "risks_and_blockers", risk_items),
        )
    )

    sections.append(
        BriefFactSection(
            section="unanswered_questions", heading="Unanswered Questions", facts=unanswered_facts
        )
    )

    # --- suggested_topics: never has its own facts — see
    # project_context.services.meeting_prep for how/when it is still
    # sent to the model despite always being "empty" here. --------------
    sections.append(
        BriefFactSection(
            section="suggested_topics", heading="Suggested Discussion Topics", facts=()
        )
    )

    expected_keys = tuple(key for key, _heading in MEETING_PREP_SECTIONS)
    assert tuple(s.section for s in sections) == expected_keys
    return MeetingPrepBriefFacts(
        project_id=project_id,
        generated_at=generated_at,
        meeting=meeting,
        cutoff_at=cutoff_at,
        previous_meeting_artifact_id=previous_artifact.id if previous_artifact else None,
        sections=tuple(sections),
    )
