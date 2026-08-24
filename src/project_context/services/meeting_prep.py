"""Meeting Preparation Brief generation (Section 5.9; Section 8 "Brief
generation"; Section 12.3 Stage C; FR-025/FR-026; Prompt 14).

Mirrors `project_context.services.briefs`'s orchestration exactly:
(1) the deterministic fact builder (`project_context.retrieval.
meeting_prep`, SQL over accepted ledger state plus meeting selection/
cutoff — never raw evidence), (2) at most one Stage C model call over
that fact payload alone, and (3) deterministic claim validation
(`project_context.services.brief_shared`, shared with the Current
Project Brief) before anything is rendered or shown as a fact.

Three differences from Current Project Brief, each following directly
from FR-025/Section 5.9:

- **Include/exclude before generation.** A caller may pass
  `excluded_fact_ids` — fact IDs the user unchecked in the UI's preview
  step — filtered out of every section *before* anything else happens
  (model input, empty-section detection, validation, the stored
  snapshot). An excluded fact is treated exactly as if it never existed
  for this run.
- **`suggested_topics` has no facts of its own but can still be model-
  eligible.** Every other section is sent to the model only if it has
  at least one fact (same rule as Current Project Brief). This one
  section is always empty by construction (`project_context.retrieval.
  meeting_prep`) yet is still sent to the model — with its own empty
  fact list — whenever *any other* section has real content, so the
  model can propose discussion topics grounded in what it already saw
  in the same call. If literally nothing exists across every other
  section, `suggested_topics` gets its deterministic "No suggested
  discussion topics" text without any model call at all.
- **Outstanding commitments render grouped by owner.** Section 5.9:
  "outstanding commitments grouped by participant and unassigned
  owner." The claims themselves are ordinary per-fact `PlannedClaim`s
  like any other section — grouping happens only in `_render_markdown`,
  by looking each claim's primary cited fact back up for its
  `owner_name` (or the `UNASSIGNED_OWNER_LABEL` group when there is
  none). No new persistence is needed: `BriefClaimRecord.cited_fact_ids`
  already carries what is needed to look this up.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from project_context.config import DEFAULT_OPENAI_MODEL
from project_context.db import brief_repository
from project_context.db.connection import transaction
from project_context.domain.briefs import (
    BriefClaimRecord,
    BriefFact,
    BriefStatus,
    BriefType,
    ClaimType,
    ClaimValidationStatus,
    GeneratedBrief,
)
from project_context.domain.meeting_prep import (
    MEETING_PREP_SECTIONS,
    UNASSIGNED_OWNER_LABEL,
    MeetingPrepBriefFacts,
)
from project_context.ids import new_id
from project_context.llm.prompts import (
    MEETING_PREP_PROMPT_VERSION,
    build_meeting_prep_input,
    load_meeting_prep_system_prompt,
)
from project_context.llm.provider import (
    DEFAULT_REASONING_EFFORT,
    LLMProvider,
    LLMProviderError,
    ModelConfig,
)
from project_context.llm.schemas import BRIEF_SCHEMA_VERSION, BriefClaimOutput, BriefComposition
from project_context.observability import get_logger
from project_context.retrieval.meeting_prep import build_meeting_prep_facts
from project_context.services.brief_shared import (
    PlannedClaim,
    citation_markdown,
    classify_claim,
    deterministic_claim_text,
    link_claim_evidence,
    plan_model_claim,
)
from project_context.services.projects import get_project

logger = get_logger(__name__)

#: `meeting_purpose` is rendered deterministically, never sent to the
#: model (same "single already-known fact, nothing to compress" reason
#: Current Project Brief's `objective_and_scope`/`current_stage` are
#: deterministic-only — see `project_context.services.briefs`).
_DETERMINISTIC_ONLY_SECTIONS = ("meeting_purpose",)

_MODEL_ELIGIBLE_SECTIONS = tuple(
    key for key, _heading in MEETING_PREP_SECTIONS if key not in _DETERMINISTIC_ONLY_SECTIONS
)

#: `suggested_topics` never has facts of its own — see module docstring.
_SUGGESTION_ONLY_SECTION = "suggested_topics"

_SELF_EVIDENCING_SECTIONS = frozenset(_DETERMINISTIC_ONLY_SECTIONS)

_EMPTY_SECTION_TEXT = {
    "changes_since_previous": "No accepted changes since the previous meeting/cutoff.",
    "outstanding_commitments": "No outstanding commitments.",
    "decisions_required": "No decisions required.",
    "risks_and_blockers": "No risks or blockers requiring discussion.",
    "unanswered_questions": "No unanswered questions.",
    "suggested_topics": "No suggested discussion topics.",
}


@dataclass(frozen=True)
class MeetingPrepBriefGenerationResult:
    brief: GeneratedBrief
    claims: tuple[BriefClaimRecord, ...]
    facts: MeetingPrepBriefFacts


def _filter_excluded_facts(
    facts: MeetingPrepBriefFacts, excluded_fact_ids: frozenset[str]
) -> MeetingPrepBriefFacts:
    """A fact the user unchecked in the preview step is treated exactly
    as if it never existed for this run (module docstring)."""
    if not excluded_fact_ids:
        return facts
    new_sections = tuple(
        section.model_copy(
            update={"facts": tuple(f for f in section.facts if f.fact_id not in excluded_fact_ids)}
        )
        for section in facts.sections
    )
    return facts.model_copy(update={"sections": new_sections})


def _plan_deterministic_sections(facts: MeetingPrepBriefFacts) -> list[PlannedClaim]:
    section_by_key = {section.section: section for section in facts.sections}
    purpose_facts = section_by_key["meeting_purpose"].facts
    if not purpose_facts:
        # Only reachable if a caller excluded the meeting_purpose fact
        # itself — the UI never offers that, but this stays honest
        # rather than raising.
        return [
            PlannedClaim(
                section="meeting_purpose",
                text="Purpose: Not stated.",
                claim_type=ClaimType.FACT,
                cited_fact_ids=(),
                validation_status=ClaimValidationStatus.VALID,
            )
        ]
    purpose_fact = purpose_facts[0]
    text = f"Purpose: {purpose_fact.detail}" if purpose_fact.detail else "Purpose: Not stated."
    return [
        PlannedClaim(
            section="meeting_purpose",
            text=text,
            claim_type=ClaimType.FACT,
            cited_fact_ids=(purpose_fact.fact_id,),
            validation_status=ClaimValidationStatus.VALID,
        )
    ]


def _plan_model_eligible_sections(
    facts: MeetingPrepBriefFacts, composition: BriefComposition | None
) -> list[PlannedClaim]:
    section_by_key = {section.section: section for section in facts.sections}
    fact_lookup = facts.fact_by_id()

    returned_by_key: dict[str, list[BriefClaimOutput]] = {}
    for section_out in composition.sections if composition is not None else []:
        if section_out.section not in _MODEL_ELIGIBLE_SECTIONS:
            logger.info(
                "meeting_prep_section_unknown_heading", extra={"section": section_out.section}
            )
            continue
        returned_by_key.setdefault(section_out.section, []).extend(section_out.claims)

    planned: list[PlannedClaim] = []
    for key in _MODEL_ELIGIBLE_SECTIONS:
        section = section_by_key[key]

        if key == _SUGGESTION_ONLY_SECTION:
            section_claims = [
                plan_model_claim(
                    section=key,
                    claim_out=claim_out,
                    fact_lookup=fact_lookup,
                    self_evidencing_sections=_SELF_EVIDENCING_SECTIONS,
                )
                for claim_out in returned_by_key.get(key, [])
            ]
            valid = [
                c for c in section_claims if c.validation_status is ClaimValidationStatus.VALID
            ]
            planned.extend(valid)
            if not valid:
                planned.append(
                    PlannedClaim(
                        section=key,
                        text=_EMPTY_SECTION_TEXT[key],
                        claim_type=ClaimType.FACT,
                        cited_fact_ids=(),
                        validation_status=ClaimValidationStatus.VALID,
                    )
                )
            continue

        if not section.facts:
            planned.append(
                PlannedClaim(
                    section=key,
                    text=_EMPTY_SECTION_TEXT[key],
                    claim_type=ClaimType.FACT,
                    cited_fact_ids=(),
                    validation_status=ClaimValidationStatus.VALID,
                )
            )
            continue

        section_claims = [
            plan_model_claim(
                section=key,
                claim_out=claim_out,
                fact_lookup=fact_lookup,
                self_evidencing_sections=_SELF_EVIDENCING_SECTIONS,
            )
            for claim_out in returned_by_key.get(key, [])
        ]
        planned.extend(section_claims)
        if not any(c.validation_status is ClaimValidationStatus.VALID for c in section_claims):
            # Safety net: the model produced nothing usable for a
            # section that does have accepted facts — never let that
            # silently drop real state from the brief.
            for fact in section.facts:
                status, resolved = classify_claim(
                    section=key,
                    claim_type=ClaimType.FACT,
                    requested_fact_ids=(fact.fact_id,),
                    fact_lookup=fact_lookup,
                    self_evidencing_sections=_SELF_EVIDENCING_SECTIONS,
                )
                planned.append(
                    PlannedClaim(
                        section=key,
                        text=deterministic_claim_text(fact),
                        claim_type=ClaimType.FACT,
                        cited_fact_ids=resolved,
                        validation_status=status,
                        ledger_item_id=fact.ledger_item_id,
                        ledger_version_id=fact.ledger_version_id,
                    )
                )
    return planned


def _claim_prefix(claim: BriefClaimRecord) -> str:
    if claim.claim_type is ClaimType.INFERENCE:
        return "**Inference:** "
    if claim.claim_type is ClaimType.SUGGESTION:
        return "**Suggestion:** "
    return ""


def _claim_owner_name(claim: BriefClaimRecord, fact_lookup: dict[str, BriefFact]) -> str | None:
    for fact_id in claim.cited_fact_ids:
        fact = fact_lookup.get(fact_id)
        if fact is not None and fact.owner_name:
            return fact.owner_name
    return None


def _render_outstanding_commitments(
    conn: sqlite3.Connection, project_id: str, claims: list[BriefClaimRecord], fact_lookup: dict
) -> list[str]:
    """Section 5.9: "grouped by participant and unassigned owner." Only
    claims that actually cite a fact are grouped — a placeholder "No
    outstanding commitments" claim (no `cited_fact_ids` at all) renders
    as a plain top-level line, never inside a fabricated owner group."""
    real_claims = [c for c in claims if c.cited_fact_ids]
    if not real_claims:
        return [f"- {c.claim_text}" for c in claims] or ["- No accepted items."]

    groups: dict[str, list[BriefClaimRecord]] = {}
    for claim in real_claims:
        owner = _claim_owner_name(claim, fact_lookup) or UNASSIGNED_OWNER_LABEL
        groups.setdefault(owner, []).append(claim)

    lines: list[str] = []
    for owner in sorted(groups, key=lambda name: (name == UNASSIGNED_OWNER_LABEL, name.lower())):
        lines.append(f"**{owner}**")
        for claim in groups[owner]:
            citation = citation_markdown(conn, project_id, claim.id)
            lines.append(f"- {_claim_prefix(claim)}{claim.claim_text}{citation}")
    return lines


def _render_markdown(
    conn: sqlite3.Connection,
    project_id: str,
    project_name: str,
    facts: MeetingPrepBriefFacts,
    claims: list[BriefClaimRecord],
) -> str:
    fact_lookup = facts.fact_by_id()
    by_section: dict[str, list[BriefClaimRecord]] = {}
    for claim in claims:
        if claim.validation_status is not ClaimValidationStatus.VALID:
            continue
        by_section.setdefault(claim.section, []).append(claim)

    meeting = facts.meeting
    lines = [f"# Meeting Preparation Brief — {meeting.title}", ""]
    lines.append(f"**Project:** {project_name}")
    lines.append(f"**Scheduled:** {meeting.scheduled_at or 'Not stated'}")
    if meeting.participants:
        rendered = []
        for participant in meeting.participants:
            outcome = participant.resolution.outcome
            suffix = "" if outcome == "resolved" else f" _({outcome})_"
            rendered.append(f"{participant.display_name}{suffix}")
        lines.append(f"**Participants:** {', '.join(rendered)}")
    else:
        lines.append("**Participants:** Not stated")
    cutoff_note = (
        f"[previous meeting](evidence?artifact_id={facts.previous_meeting_artifact_id})"
        if facts.previous_meeting_artifact_id
        else "project start — no earlier meeting found"
    )
    lines.append(f"**Changes since:** {facts.cutoff_at} ({cutoff_note})")
    lines.append("")

    for key, heading in MEETING_PREP_SECTIONS:
        lines.append(f"## {heading}")
        section_claims = sorted(by_section.get(key, []), key=lambda c: c.ordinal)
        if key == "outstanding_commitments":
            lines.extend(
                _render_outstanding_commitments(conn, project_id, section_claims, fact_lookup)
            )
            lines.append("")
            continue
        if not section_claims:
            lines.append("- No accepted items.")
        for claim in section_claims:
            citation = citation_markdown(conn, project_id, claim.id)
            lines.append(f"- {_claim_prefix(claim)}{claim.claim_text}{citation}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_meeting_prep_brief(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    facts: MeetingPrepBriefFacts | None = None,
    meeting_artifact_id: str | None = None,
    manual_title: str | None = None,
    manual_purpose: str | None = None,
    manual_scheduled_at: str | None = None,
    participant_lines: tuple[str, ...] = (),
    cutoff_override: str | None = None,
    excluded_fact_ids: frozenset[str] = frozenset(),
    provider: LLMProvider,
    model: str = DEFAULT_OPENAI_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> MeetingPrepBriefGenerationResult:
    """Generate one Meeting Preparation Brief (FR-025).

    `facts`, when given, is used exactly as built — this is what the
    UI's "preview facts, let the user include/exclude, then generate"
    flow (Prompt 14) must pass: the same `MeetingPrepBriefFacts` object
    the preview step already built and showed, so `excluded_fact_ids`
    (opaque IDs minted fresh on every `build_meeting_prep_facts` call)
    actually match what the user saw and unchecked. When omitted, facts
    are built fresh from the other keyword arguments — the simple,
    single-call path most direct callers (and most tests) want.

    Either way: applies any user-excluded facts, composes prose for
    non-empty model-eligible sections (plus `suggested_topics` when
    there is anything to suggest about) in at most one Stage C call,
    validates every claim, persists brief + claims + evidence links in
    one transaction, and marks this project's previous `valid` Meeting
    Preparation Brief `superseded`."""
    project = get_project(conn, project_id)
    if facts is None:
        facts = build_meeting_prep_facts(
            conn,
            project_id,
            meeting_artifact_id=meeting_artifact_id,
            manual_title=manual_title,
            manual_purpose=manual_purpose,
            manual_scheduled_at=manual_scheduled_at,
            participant_lines=participant_lines,
            cutoff_override=cutoff_override,
        )
    facts = _filter_excluded_facts(facts, excluded_fact_ids)
    fact_lookup = facts.fact_by_id()
    input_snapshot = facts.model_dump(mode="json")

    brief_id = new_id()
    with transaction(conn):
        brief_repository.insert_brief(
            conn,
            project_id,
            brief_type=BriefType.MEETING_PREPARATION,
            meeting_artifact_id=facts.meeting.meeting_artifact_id,
            cutoff_at=facts.cutoff_at,
            input_snapshot=input_snapshot,
            brief_id=brief_id,
        )

    section_by_key = {section.section: section for section in facts.sections}
    has_other_content = any(
        section_by_key[key].facts
        for key in _MODEL_ELIGIBLE_SECTIONS
        if key != _SUGGESTION_ONLY_SECTION
    )
    model_sections = tuple(
        section_by_key[key]
        for key in _MODEL_ELIGIBLE_SECTIONS
        if section_by_key[key].facts or (key == _SUGGESTION_ONLY_SECTION and has_other_content)
    )

    composition: BriefComposition | None = None
    input_tokens = output_tokens = latency_ms = 0
    cost = 0.0
    model_id: str | None = None
    prompt_version: str | None = None
    schema_version: str | None = None

    if model_sections:
        system = load_meeting_prep_system_prompt()
        user_input = build_meeting_prep_input(
            project=project,
            meeting=facts.meeting,
            cutoff_at=facts.cutoff_at,
            sections=model_sections,
        )
        config = ModelConfig(model=model, reasoning_effort=reasoning_effort, store=False)
        try:
            result = provider.generate_structured(
                task="compose_meeting_prep_brief",
                system=system,
                input_text=user_input,
                response_model=BriefComposition,
                config=config,
            )
        except LLMProviderError as exc:
            safe_error = str(exc)
            with transaction(conn):
                brief_repository.finalize_brief(
                    conn, project_id, brief_id, status=BriefStatus.FAILED, safe_error=safe_error
                )
            logger.info(
                "meeting_prep_brief_generation_failed",
                extra={
                    "project_id": project_id,
                    "brief_id": brief_id,
                    "error_class": type(exc).__name__,
                },
            )
            failed_brief = brief_repository.get_brief(conn, project_id, brief_id)
            assert failed_brief is not None
            return MeetingPrepBriefGenerationResult(brief=failed_brief, claims=(), facts=facts)

        composition = result.parsed
        assert isinstance(composition, BriefComposition)
        input_tokens, output_tokens = result.input_tokens, result.output_tokens
        latency_ms = result.latency_ms
        cost = result.estimated_cost_usd
        model_id = result.model
        prompt_version = MEETING_PREP_PROMPT_VERSION
        schema_version = BRIEF_SCHEMA_VERSION

    planned = _plan_deterministic_sections(facts)
    planned += _plan_model_eligible_sections(facts, composition)

    with transaction(conn):
        claim_records: list[BriefClaimRecord] = []
        for ordinal, planned_claim in enumerate(planned):
            claim = brief_repository.insert_claim(
                conn,
                project_id,
                brief_id=brief_id,
                section=planned_claim.section,
                ordinal=ordinal,
                claim_text=planned_claim.text,
                claim_type=planned_claim.claim_type,
                cited_fact_ids=planned_claim.cited_fact_ids,
                validation_status=planned_claim.validation_status,
                ledger_item_id=planned_claim.ledger_item_id,
                ledger_version_id=planned_claim.ledger_version_id,
            )
            claim_records.append(claim)
            if planned_claim.validation_status is ClaimValidationStatus.VALID:
                link_claim_evidence(
                    conn, project_id, claim.id, planned_claim.cited_fact_ids, fact_lookup
                )

        markdown = _render_markdown(conn, project_id, project.name, facts, claim_records)
        brief_repository.finalize_brief(
            conn,
            project_id,
            brief_id,
            status=BriefStatus.VALID,
            markdown=markdown,
            model_id=model_id,
            prompt_version=prompt_version,
            schema_version=schema_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
            latency_ms=latency_ms,
        )
        brief_repository.supersede_previous_valid_briefs(
            conn, project_id, brief_type=BriefType.MEETING_PREPARATION, except_brief_id=brief_id
        )

    final_brief = brief_repository.get_brief(conn, project_id, brief_id)
    assert final_brief is not None
    logger.info(
        "meeting_prep_brief_generated",
        extra={
            "project_id": project_id,
            "brief_id": brief_id,
            "claim_count": len(claim_records),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": cost,
        },
    )
    return MeetingPrepBriefGenerationResult(
        brief=final_brief, claims=tuple(claim_records), facts=facts
    )
