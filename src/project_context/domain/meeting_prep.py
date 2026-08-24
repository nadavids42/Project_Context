"""Meeting Preparation Brief domain model (FR-025; Section 5.9; Section
9's `generated_briefs`/`brief_claims`, already `meeting_preparation`-
ready; Prompt 14).

Mirrors `project_context.domain.briefs`'s split: persisted rows reuse
that module's `GeneratedBrief`/`BriefClaimRecord`/`BriefFact`/
`BriefFactSection` unchanged (a Meeting Preparation Brief's claims and
facts are stored exactly like a Current Project Brief's — same tables,
same columns, same `BriefFactType` vocabulary plus the one addition,
`MEETING_META`). What is new here is meeting-specific: the deterministic
fact payload's top-level shape (`MeetingPrepBriefFacts`, carrying
meeting metadata alongside the same `BriefFactSection` tuple Current
Project Brief uses) and participant resolution
(`ResolvedParticipant`) — Section 5.9's "Resolve participant identities
by exact email first, known alias second, and explicit unresolved state
third," built directly on
`project_context.db.people_repository.resolve_person`'s existing
email-then-alias-then-unresolved behavior (Prompt 6) rather than a new
resolution algorithm.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from project_context.domain.briefs import BriefFact, BriefFactSection
from project_context.domain.people import PersonResolution

#: The seven required Meeting Preparation Brief sections (FR-025;
#: Section 5.9), in the fixed order the prompt specifies. The
#: application owns this structure, not the model — same rationale as
#: `project_context.domain.briefs.CURRENT_BRIEF_SECTIONS`.
MEETING_PREP_SECTIONS: tuple[tuple[str, str], ...] = (
    ("meeting_purpose", "Meeting Purpose"),
    ("changes_since_previous", "Changes Since Previous Meeting"),
    ("outstanding_commitments", "Outstanding Commitments"),
    ("decisions_required", "Decisions Required"),
    ("risks_and_blockers", "Risks Requiring Discussion"),
    ("unanswered_questions", "Unanswered Questions"),
    ("suggested_topics", "Suggested Discussion Topics"),
)

#: Display label used for the outstanding-commitments owner sub-group
#: when a commitment has no resolved owner (Section 5.9: "outstanding
#: commitments... grouped by participant and unassigned owner").
UNASSIGNED_OWNER_LABEL = "Unassigned"


class ResolvedParticipant(BaseModel):
    """One meeting participant, as entered (manually, or pre-filled from
    a selected Calendar/Fathom meeting artifact and then confirmed/
    edited by the user) and resolved (Section 5.9: "exact email first,
    known alias second, explicit unresolved state third — do not guess
    between ambiguous names").

    `outcome`/`person_id`/`candidate_person_ids` mirror
    `project_context.domain.people.PersonResolution` exactly —
    `resolve_participants` (`project_context.retrieval.meeting_prep`)
    is a thin per-line wrapper around
    `project_context.db.people_repository.resolve_person`, not a new
    resolution algorithm."""

    model_config = ConfigDict(frozen=True)

    raw_input: str
    raw_name: str | None = None
    raw_email: str | None = None
    resolution: PersonResolution
    #: The best available display text for this participant — the
    #: resolved person's canonical name when resolved, otherwise the raw
    #: name/email as entered. Never invented.
    display_name: str


class MeetingInfo(BaseModel):
    """Meeting selection/metadata — either drawn from a selected
    Calendar/Fathom `source_artifacts` row or entered manually (Section
    5.9: "Always provide a manual fallback... so Calendar remains
    optional"). Never carries raw evidence text; `meeting_artifact_id`
    is the pointer a citation/evidence-viewer link can use for the
    selected meeting itself."""

    model_config = ConfigDict(frozen=True)

    title: str
    purpose: str | None = None
    scheduled_at: str | None = None
    meeting_artifact_id: str | None = None
    external_url: str | None = None
    participants: tuple[ResolvedParticipant, ...] = ()


class MeetingPrepBriefFacts(BaseModel):
    """The full deterministic input to one Meeting Preparation Brief
    generation run — frozen into `generated_briefs.input_snapshot_json`
    exactly like `project_context.domain.briefs.CurrentProjectBriefFacts`
    (Prompt 9's reproducibility requirement, extended here to also cover
    meeting selection/cutoff so a stored brief shows exactly what
    meeting and cutoff produced it, not just what facts)."""

    model_config = ConfigDict(frozen=True)

    project_id: str
    generated_at: str
    meeting: MeetingInfo
    cutoff_at: str
    #: The artifact this cutoff was computed from — `None` when no
    #: earlier meeting exists and the project's own `created_at` was
    #: used instead (`project_context.retrieval.meeting_prep`'s
    #: documented fallback).
    previous_meeting_artifact_id: str | None
    sections: tuple[BriefFactSection, ...]

    def fact_by_id(self) -> dict[str, BriefFact]:
        """Every fact across every section, keyed by its opaque
        `fact_id` — mirrors `CurrentProjectBriefFacts.fact_by_id`."""
        return {fact.fact_id: fact for section in self.sections for fact in section.facts}
