"""Shared claim validation, evidence-linking, and citation rendering for
both brief types (Section 12.3 Stage C; FR-026; Prompt 14).

Extracted from `project_context.services.briefs` (Prompt 9) so
`project_context.services.meeting_prep` (Prompt 14) validates/links/cites
model-composed claims with exactly the same rules — "every fact/inference
claim's fact_ids must resolve to a fact from that claim's own section,
excluding self-evidencing sections from the evidence-link requirement,
storing whatever was actually cited either way" — rather than a second,
possibly-drifting copy of that logic. Only what genuinely differs between
brief types (which sections exist, how each is rendered, what counts as
"self-evidencing") stays in each service module.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from project_context.db import evidence_link_repository, evidence_repository
from project_context.domain.briefs import (
    BriefFact,
    BriefFactType,
    ClaimType,
    ClaimValidationStatus,
)
from project_context.domain.evidence_links import EvidenceLinkSupportRole, EvidenceLinkTargetType
from project_context.llm.schemas import BriefClaimOutput


@dataclass(frozen=True)
class PlannedClaim:
    section: str
    text: str
    claim_type: ClaimType
    cited_fact_ids: tuple[str, ...]
    validation_status: ClaimValidationStatus
    ledger_item_id: str | None = None
    ledger_version_id: str | None = None


def classify_claim(
    *,
    section: str,
    claim_type: ClaimType,
    requested_fact_ids: tuple[str, ...],
    fact_lookup: dict[str, BriefFact],
    self_evidencing_sections: frozenset[str],
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
    if section in self_evidencing_sections:
        return ClaimValidationStatus.VALID, resolved
    has_evidence = any(fact_lookup[fact_id].evidence_link_ids for fact_id in resolved)
    if not has_evidence:
        return ClaimValidationStatus.UNSUPPORTED, resolved
    return ClaimValidationStatus.VALID, resolved


def plan_model_claim(
    *,
    section: str,
    claim_out: BriefClaimOutput,
    fact_lookup: dict[str, BriefFact],
    self_evidencing_sections: frozenset[str],
) -> PlannedClaim:
    claim_type = ClaimType(claim_out.claim_type)
    status, resolved = classify_claim(
        section=section,
        claim_type=claim_type,
        requested_fact_ids=tuple(claim_out.fact_ids),
        fact_lookup=fact_lookup,
        self_evidencing_sections=self_evidencing_sections,
    )
    primary = fact_lookup.get(resolved[0]) if resolved else None
    # Store whatever was actually cited: the resolved set when there is
    # one (or the claim didn't need citation at all), otherwise the raw
    # (unresolvable) request, so an invalid_reference claim's audit trail
    # still shows exactly what the model tried to cite.
    is_valid_or_resolved = status is ClaimValidationStatus.VALID or resolved
    cited = resolved if is_valid_or_resolved else tuple(claim_out.fact_ids)
    return PlannedClaim(
        section=section,
        text=claim_out.text,
        claim_type=claim_type,
        cited_fact_ids=cited,
        validation_status=status,
        ledger_item_id=primary.ledger_item_id if primary else None,
        ledger_version_id=primary.ledger_version_id if primary else None,
    )


def deterministic_claim_text(fact: BriefFact) -> str:
    """A guaranteed-correct, non-model sentence for one fact — used for
    every self-evidencing section always, and as the safety-net fallback
    for any other section where the model produced nothing valid."""
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


def link_claim_evidence(
    conn: sqlite3.Connection,
    project_id: str,
    claim_id: str,
    fact_ids: tuple[str, ...],
    fact_lookup: dict[str, BriefFact],
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


def citation_markdown(conn: sqlite3.Connection, project_id: str, claim_id: str) -> str:
    """Markdown citation markers linking straight to the evidence
    viewer's exact character span for each of `claim_id`'s linked
    evidence — "Every factual citation opens the evidence viewer at its
    exact span" (Prompt 14 UI requirement), shared by both brief
    renderers."""
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
