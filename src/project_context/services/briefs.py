"""Current Project Brief generation (Section 5.8; Section 8 "Brief
generation"; Section 12.3 Stage C; FR-024/FR-026; ADR-011; Prompt 9).

Orchestrates, in order: (1) the deterministic fact builder
(`project_context.retrieval.briefs`, SQL over accepted ledger state
only), (2) at most one Stage C model call over that fact payload alone
— never raw evidence, never the whole corpus — and (3) deterministic
claim validation before anything is rendered or shown as a fact.

Two sections (`objective_and_scope`, `current_stage`) are never sent to
the model at all — they are single already-known facts with nothing to
organize or compress, so the deterministic renderer writes them
directly ("Ledger before language": the model composes prose only where
compression is actually happening). The other six sections go to the
model only if they have at least one fact; an empty section's "No
accepted items" text is written by this module, never requested from
the model (Prompt 9: "Missing sections must be explicit and honest...
rather than model invention").

Every model-composed claim is validated against the exact fact payload
this run built before it is ever rendered: a `fact_id` that does not
resolve to a fact from that claim's own section (including, structurally,
any other project's ID — fact_ids are freshly generated per call and
never collide) is `invalid_reference`; a resolved fact with no evidence
links at all is `unsupported`; both are stored for audit but omitted
from the rendered Markdown and replaced by a deterministic, guaranteed-
correct per-fact fallback claim, so a model mistake never causes real
accepted state to silently vanish from the brief.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from project_context.config import DEFAULT_OPENAI_MODEL
from project_context.db import brief_repository, evidence_link_repository, evidence_repository
from project_context.db.connection import transaction
from project_context.domain.briefs import (
    CURRENT_BRIEF_SECTIONS,
    BriefClaimRecord,
    BriefFact,
    BriefFactType,
    BriefStatus,
    BriefType,
    ClaimType,
    ClaimValidationStatus,
    CurrentProjectBriefFacts,
    GeneratedBrief,
)
from project_context.domain.evidence_links import EvidenceLinkSupportRole, EvidenceLinkTargetType
from project_context.ids import new_id
from project_context.llm.prompts import (
    BRIEF_PROMPT_VERSION,
    build_brief_input,
    load_brief_system_prompt,
)
from project_context.llm.provider import (
    DEFAULT_REASONING_EFFORT,
    LLMProvider,
    LLMProviderError,
    ModelConfig,
)
from project_context.llm.schemas import BRIEF_SCHEMA_VERSION, BriefClaimOutput, BriefComposition
from project_context.observability import get_logger
from project_context.retrieval.briefs import build_current_project_brief_facts
from project_context.services.projects import get_project

logger = get_logger(__name__)

#: The two sections rendered deterministically, never sent to the model
#: (see module docstring).
_DETERMINISTIC_ONLY_SECTIONS = ("objective_and_scope", "current_stage")

#: The six sections eligible for model composition when non-empty.
_MODEL_ELIGIBLE_SECTIONS = tuple(
    key for key, _heading in CURRENT_BRIEF_SECTIONS if key not in _DETERMINISTIC_ONLY_SECTIONS
)

#: A fact from `objective_and_scope`/`current_stage` is the project
#: record itself — always valid with no evidence link required (see
#: `project_context.domain.briefs.BriefFactType.PROJECT_META`).
_SELF_EVIDENCING_SECTIONS = frozenset(_DETERMINISTIC_ONLY_SECTIONS)

_EMPTY_SECTION_TEXT = {
    "recent_changes": "No recent accepted changes.",
    "next_milestone": "No accepted upcoming milestone.",
    "open_commitments": "No accepted open commitments.",
    "decisions": "No accepted active decisions.",
    "risks_and_blockers": "No accepted open risks or blockers.",
    "unresolved_questions": "No accepted unresolved questions.",
}


@dataclass(frozen=True)
class _PlannedClaim:
    section: str
    text: str
    claim_type: ClaimType
    cited_fact_ids: tuple[str, ...]
    validation_status: ClaimValidationStatus
    ledger_item_id: str | None = None
    ledger_version_id: str | None = None


@dataclass(frozen=True)
class BriefGenerationResult:
    brief: GeneratedBrief
    claims: tuple[BriefClaimRecord, ...]
    facts: CurrentProjectBriefFacts


def _classify_claim(
    *,
    section: str,
    claim_type: ClaimType,
    requested_fact_ids: tuple[str, ...],
    fact_lookup: dict[str, BriefFact],
) -> tuple[ClaimValidationStatus, tuple[str, ...]]:
    """Resolve `requested_fact_ids` against the known fact payload for
    this claim's own section, and classify the result (Prompt 9: "Resolve
    every referenced fact... within the same project. Reject unknown or
    cross-project IDs.")."""
    resolved = tuple(
        fact_id
        for fact_id in requested_fact_ids
        if (fact := fact_lookup.get(fact_id)) is not None and fact.section == section
    )
    needs_citation = claim_type in (ClaimType.FACT, ClaimType.INFERENCE)
    if not needs_citation:
        return ClaimValidationStatus.VALID, resolved
    if not resolved:
        return ClaimValidationStatus.INVALID_REFERENCE, resolved
    if section in _SELF_EVIDENCING_SECTIONS:
        return ClaimValidationStatus.VALID, resolved
    has_evidence = any(fact_lookup[fact_id].evidence_link_ids for fact_id in resolved)
    if not has_evidence:
        return ClaimValidationStatus.UNSUPPORTED, resolved
    return ClaimValidationStatus.VALID, resolved


def _deterministic_claim_text(fact: BriefFact) -> str:
    """A guaranteed-correct, non-model sentence for one fact — used for
    `objective_and_scope`/`current_stage` always, and as the safety-net
    fallback for any other section where the model produced nothing
    valid (module docstring)."""
    if fact.fact_type is BriefFactType.TRANSITION:
        text = f"{fact.title} — {fact.transition_type} (now {fact.status})"
        if fact.previous_summary:
            text += f"; {fact.previous_summary}"
        if fact.owner_name:
            text += f"; owner {fact.owner_name}"
        return text + "."
    parts = [fact.title]
    if fact.status:
        parts.append(f"status: {fact.status}")
    if fact.owner_name:
        parts.append(f"owner: {fact.owner_name}")
    if fact.due_date:
        parts.append(f"due: {fact.due_date}")
    return " — ".join(parts) + "."


def _plan_deterministic_sections(
    facts: CurrentProjectBriefFacts,
) -> list[_PlannedClaim]:
    section_by_key = {section.section: section for section in facts.sections}
    planned: list[_PlannedClaim] = []

    scope_fact = section_by_key["objective_and_scope"].facts[0]
    text = f"Objective: {scope_fact.title}."
    if scope_fact.detail:
        text += f" Scope: {scope_fact.detail}"
    planned.append(
        _PlannedClaim(
            section="objective_and_scope",
            text=text,
            claim_type=ClaimType.FACT,
            cited_fact_ids=(scope_fact.fact_id,),
            validation_status=ClaimValidationStatus.VALID,
        )
    )

    stage_facts = section_by_key["current_stage"].facts
    if stage_facts:
        stage_fact = stage_facts[0]
        planned.append(
            _PlannedClaim(
                section="current_stage",
                text=f"Current stage: {stage_fact.title}.",
                claim_type=ClaimType.FACT,
                cited_fact_ids=(stage_fact.fact_id,),
                validation_status=ClaimValidationStatus.VALID,
            )
        )
    else:
        planned.append(
            _PlannedClaim(
                section="current_stage",
                text="Current stage: Not stated.",
                claim_type=ClaimType.FACT,
                cited_fact_ids=(),
                validation_status=ClaimValidationStatus.VALID,
            )
        )
    return planned


def _plan_model_claim(
    *, section: str, claim_out: BriefClaimOutput, fact_lookup: dict[str, BriefFact]
) -> _PlannedClaim:
    claim_type = ClaimType(claim_out.claim_type)
    status, resolved = _classify_claim(
        section=section,
        claim_type=claim_type,
        requested_fact_ids=tuple(claim_out.fact_ids),
        fact_lookup=fact_lookup,
    )
    primary = fact_lookup.get(resolved[0]) if resolved else None
    # Store whatever was actually cited: the resolved set when there is
    # one (or the claim didn't need citation at all), otherwise the raw
    # (unresolvable) request, so an invalid_reference claim's audit trail
    # still shows exactly what the model tried to cite.
    is_valid_or_resolved = status is ClaimValidationStatus.VALID or resolved
    cited = resolved if is_valid_or_resolved else tuple(claim_out.fact_ids)
    return _PlannedClaim(
        section=section,
        text=claim_out.text,
        claim_type=claim_type,
        cited_fact_ids=cited,
        validation_status=status,
        ledger_item_id=primary.ledger_item_id if primary else None,
        ledger_version_id=primary.ledger_version_id if primary else None,
    )


def _plan_model_eligible_sections(
    facts: CurrentProjectBriefFacts, composition: BriefComposition | None
) -> list[_PlannedClaim]:
    section_by_key = {section.section: section for section in facts.sections}
    fact_lookup = facts.fact_by_id()

    returned_by_key: dict[str, list[BriefClaimOutput]] = {}
    for section_out in (composition.sections if composition is not None else []):
        if section_out.section not in _MODEL_ELIGIBLE_SECTIONS:
            logger.info("brief_section_unknown_heading", extra={"section": section_out.section})
            continue
        returned_by_key.setdefault(section_out.section, []).extend(section_out.claims)

    planned: list[_PlannedClaim] = []
    for key in _MODEL_ELIGIBLE_SECTIONS:
        section = section_by_key[key]
        if not section.facts:
            planned.append(
                _PlannedClaim(
                    section=key,
                    text=_EMPTY_SECTION_TEXT[key],
                    claim_type=ClaimType.FACT,
                    cited_fact_ids=(),
                    validation_status=ClaimValidationStatus.VALID,
                )
            )
            continue

        section_claims = [
            _plan_model_claim(section=key, claim_out=claim_out, fact_lookup=fact_lookup)
            for claim_out in returned_by_key.get(key, [])
        ]
        planned.extend(section_claims)
        if not any(c.validation_status is ClaimValidationStatus.VALID for c in section_claims):
            # Safety net: the model produced nothing usable for a
            # section that does have accepted facts — never let that
            # silently drop real state from the brief.
            for fact in section.facts:
                status, resolved = _classify_claim(
                    section=key,
                    claim_type=ClaimType.FACT,
                    requested_fact_ids=(fact.fact_id,),
                    fact_lookup=fact_lookup,
                )
                planned.append(
                    _PlannedClaim(
                        section=key,
                        text=_deterministic_claim_text(fact),
                        claim_type=ClaimType.FACT,
                        cited_fact_ids=resolved,
                        validation_status=status,
                        ledger_item_id=fact.ledger_item_id,
                        ledger_version_id=fact.ledger_version_id,
                    )
                )
    return planned


def _link_claim_evidence(
    conn: sqlite3.Connection, project_id: str, claim_id: str, fact_ids: tuple[str, ...], fact_lookup
) -> None:
    seen: set[str] = set()
    for fact_id in fact_ids:
        fact = fact_lookup.get(fact_id)
        if fact is None:
            continue
        for link_id in fact.evidence_link_ids:
            if link_id in seen:
                continue
            seen.add(link_id)
            source_link = evidence_link_repository.get_link(conn, project_id, link_id)
            if source_link is None:
                continue
            evidence_link_repository.insert_link(
                conn,
                project_id,
                target_type=EvidenceLinkTargetType.BRIEF_CLAIM,
                target_id=claim_id,
                content_id=source_link.content_id,
                chunk_id=source_link.chunk_id,
                char_start=source_link.char_start,
                char_end=source_link.char_end,
                quote=source_link.quote,
                support_role=EvidenceLinkSupportRole.SUPPORTS,
                location=source_link.location,
            )


def _citation_markdown(conn: sqlite3.Connection, project_id: str, claim_id: str) -> str:
    links = evidence_link_repository.list_for_target(
        conn, project_id, EvidenceLinkTargetType.BRIEF_CLAIM, claim_id
    )
    if not links:
        return ""
    markers = []
    for index, link in enumerate(links, start=1):
        content = evidence_repository.get_content(conn, project_id, link.content_id)
        artifact = (
            evidence_repository.get_artifact(conn, project_id, content.artifact_id)
            if content is not None
            else None
        )
        if artifact is None:
            markers.append(f"[{index}]")
            continue
        view_url = (
            f"evidence?artifact_id={artifact.id}"
            f"&char_start={link.char_start}&char_end={link.char_end}"
        )
        marker = f"[{index}]({view_url})"
        if artifact.external_url:
            marker += f" [source]({artifact.external_url})"
        markers.append(marker)
    return " " + " ".join(markers)


def _render_markdown(
    conn: sqlite3.Connection, project_id: str, project_name: str, claims: list[BriefClaimRecord]
) -> str:
    by_section: dict[str, list[BriefClaimRecord]] = {}
    for claim in claims:
        if claim.validation_status is not ClaimValidationStatus.VALID:
            continue
        by_section.setdefault(claim.section, []).append(claim)

    lines = [f"# Current Project Brief — {project_name}", ""]
    for key, heading in CURRENT_BRIEF_SECTIONS:
        lines.append(f"## {heading}")
        section_claims = sorted(by_section.get(key, []), key=lambda c: c.ordinal)
        if not section_claims:
            lines.append("- No accepted items.")
        for claim in section_claims:
            prefix = ""
            if claim.claim_type is ClaimType.INFERENCE:
                prefix = "**Inference:** "
            elif claim.claim_type is ClaimType.SUGGESTION:
                prefix = "**Suggestion:** "
            citation = _citation_markdown(conn, project_id, claim.id)
            lines.append(f"- {prefix}{claim.claim_text}{citation}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_current_project_brief(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    provider: LLMProvider,
    model: str = DEFAULT_OPENAI_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> BriefGenerationResult:
    """Generate one Current Project Brief (FR-024). Builds facts fresh
    from accepted ledger state, composes prose for non-empty
    model-eligible sections in at most one Stage C call, validates every
    claim, persists brief + claims + evidence links in one transaction,
    and marks this project's previous `valid` Current Project Brief
    `superseded`.
    """
    project = get_project(conn, project_id)
    facts = build_current_project_brief_facts(conn, project_id)
    fact_lookup = facts.fact_by_id()
    input_snapshot = facts.model_dump(mode="json")

    brief_id = new_id()
    with transaction(conn):
        brief_repository.insert_brief(
            conn,
            project_id,
            brief_type=BriefType.CURRENT_PROJECT,
            cutoff_at=facts.generated_at,
            input_snapshot=input_snapshot,
            brief_id=brief_id,
        )

    section_by_key = {section.section: section for section in facts.sections}
    model_sections = tuple(
        section_by_key[key] for key in _MODEL_ELIGIBLE_SECTIONS if section_by_key[key].facts
    )

    composition: BriefComposition | None = None
    input_tokens = output_tokens = latency_ms = 0
    cost = 0.0
    model_id: str | None = None
    prompt_version: str | None = None
    schema_version: str | None = None

    if model_sections:
        system = load_brief_system_prompt()
        user_input = build_brief_input(project=project, sections=model_sections)
        config = ModelConfig(model=model, reasoning_effort=reasoning_effort, store=False)
        try:
            result = provider.generate_structured(
                task="compose_current_brief",
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
                "brief_generation_failed",
                extra={
                    "project_id": project_id,
                    "brief_id": brief_id,
                    "error_class": type(exc).__name__,
                },
            )
            failed_brief = brief_repository.get_brief(conn, project_id, brief_id)
            assert failed_brief is not None
            return BriefGenerationResult(brief=failed_brief, claims=(), facts=facts)

        composition = result.parsed
        assert isinstance(composition, BriefComposition)
        input_tokens, output_tokens = result.input_tokens, result.output_tokens
        latency_ms = result.latency_ms
        cost = result.estimated_cost_usd
        model_id = result.model
        prompt_version = BRIEF_PROMPT_VERSION
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
                _link_claim_evidence(
                    conn, project_id, claim.id, planned_claim.cited_fact_ids, fact_lookup
                )

        markdown = _render_markdown(conn, project_id, project.name, claim_records)
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
            conn, project_id, brief_type=BriefType.CURRENT_PROJECT, except_brief_id=brief_id
        )

    final_brief = brief_repository.get_brief(conn, project_id, brief_id)
    assert final_brief is not None
    logger.info(
        "brief_generated",
        extra={
            "project_id": project_id,
            "brief_id": brief_id,
            "claim_count": len(claim_records),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": cost,
        },
    )
    return BriefGenerationResult(brief=final_brief, claims=tuple(claim_records), facts=facts)
