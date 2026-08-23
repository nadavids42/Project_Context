"""Observation persistence orchestration: writes one validated
`project_context.llm.schemas.ExtractedObservation` (Prompt 5's
already-evidence-validated proposal shape) into `observations` plus one
`evidence_links` row per cited span, in a single transaction.

Nothing calls this yet — `project_context.services.extraction` still
returns an in-memory-only `ExtractionRunResult` (Prompt 5's scope), and
wiring it to persist is deliberately left for the reconciliation/review
work this prompt excludes. This module exists so that capability is
available, tested, and consistent with the observation repository's
fingerprint-dedup contract, per Prompt 6's "Observation repository for
validated extraction results."
"""

from __future__ import annotations

import sqlite3

from project_context.db import evidence_link_repository, observation_repository
from project_context.db.connection import transaction
from project_context.domain.evidence_links import (
    EvidenceLink,
    EvidenceLinkSupportRole,
    EvidenceLinkTargetType,
)
from project_context.domain.observations import Observation
from project_context.llm.schemas import ExtractedObservation


def persist_observation(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    content_id: str,
    chunk_id: str,
    extracted: ExtractedObservation,
    owner_person_id: str | None = None,
    model_id: str | None = None,
    prompt_version: str | None = None,
    schema_version: str | None = None,
) -> tuple[Observation, tuple[EvidenceLink, ...], bool]:
    """Persist one accepted `ExtractedObservation`. Idempotent by exact
    fingerprint (`observation_repository.insert_observation`): if an
    identical observation already exists, its existing evidence links
    are returned and no new rows are written.

    All evidence spans on `extracted` are assumed to cite `chunk_id` —
    true by construction of the current extraction design (Section 12.7:
    one chunk per call), and asserted here rather than silently trusted.
    """
    for span in extracted.evidence:
        if span.chunk_id != chunk_id:
            raise ValueError(
                f"evidence span cites chunk_id {span.chunk_id!r}, expected {chunk_id!r} "
                "(this observation's declared source chunk)"
            )

    evidence_spans = [
        (span.chunk_id, span.char_start, span.char_end) for span in extracted.evidence
    ]

    with transaction(conn):
        observation, created = observation_repository.insert_observation(
            conn,
            project_id,
            content_id=content_id,
            chunk_id=chunk_id,
            kind=extracted.kind.value,
            subject=extracted.subject,
            statement=extracted.statement,
            owner_text=extracted.owner_name,
            owner_person_id=owner_person_id,
            date_value=extracted.date_value.isoformat() if extracted.date_value else None,
            date_text=extracted.date_text,
            explicitness=extracted.explicitness,
            model_id=model_id,
            prompt_version=prompt_version,
            schema_version=schema_version,
            evidence_spans=evidence_spans,
        )

        if not created:
            links = evidence_link_repository.list_for_target(
                conn, project_id, EvidenceLinkTargetType.OBSERVATION, observation.id
            )
            return observation, tuple(links), False

        links = tuple(
            evidence_link_repository.insert_link(
                conn,
                project_id,
                target_type=EvidenceLinkTargetType.OBSERVATION,
                target_id=observation.id,
                content_id=content_id,
                chunk_id=span.chunk_id,
                char_start=span.char_start,
                char_end=span.char_end,
                quote=span.quote,
                support_role=EvidenceLinkSupportRole.SUPPORTS,
                location={"location_label": span.location_label} if span.location_label else None,
            )
            for span in extracted.evidence
        )
    return observation, links, True
