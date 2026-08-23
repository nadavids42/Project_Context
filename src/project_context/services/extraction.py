"""Manually-triggered structured observation extraction (Section 12.3,
Section 12.8).

Owns the two things ``project_context.llm`` deliberately does not know
about: the database (reading one content version's chunks and its
artifact/project metadata) and *evidence validation* — the deterministic
check that a structurally-valid ``ExtractionBatch`` is also actually
supported by the exact chunk text sent to the model (Section 12.4: "A
structurally valid response is still subjected to evidence validation").

Nothing in this module writes to any table. ``extract_content`` returns
an in-memory ``ExtractionRunResult`` for the UI to display; there is no
``observations`` table yet (that is reconciliation/ledger scope — Section
9's ``observations``/``proposed_mutations`` tables, not built in this
step) and no path from an accepted observation to a ledger mutation
(Section 12.1: "Decide state transition — No in first prototype").

Rejected observations are kept — not discarded — with a safe reason
(Section 12: "gives safe validation reasons"), so the UI can show a
human exactly why a candidate was dropped without ever needing to trust
an un-validated model claim.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum

from project_context.config import DEFAULT_OPENAI_MODEL, AppConfig, load_config
from project_context.db import evidence_repository
from project_context.domain.evidence import SourceArtifact, SourceChunk, SourceContent
from project_context.domain.projects import Project
from project_context.llm.openai_provider import OpenAIProvider
from project_context.llm.prompts import (
    PROMPT_VERSION,
    build_extraction_input,
    load_extraction_system_prompt,
)
from project_context.llm.provider import (
    DEFAULT_REASONING_EFFORT,
    LLMProvider,
    LLMProviderError,
    LLMRefusalError,
    LLMSchemaFailureError,
    ModelConfig,
)
from project_context.llm.schemas import SCHEMA_VERSION, ExtractedObservation, ExtractionBatch
from project_context.observability import get_logger
from project_context.services.projects import get_project

logger = get_logger(__name__)

#: A deliberately loose sanity bound, not a business rule: a `date_value`
#: outside this range is far more likely to be a hallucinated/garbled
#: value than a real project date, and the model is never told this
#: bound, so it cannot game it. See RejectionReason.MALFORMED_DATE.
_MIN_PLAUSIBLE_YEAR = 2000
_MAX_PLAUSIBLE_YEAR = 2100

_WHITESPACE_RE = re.compile(r"\s+")


class RejectionReason(StrEnum):
    """Safe, non-quote-leaking labels for why a candidate observation was
    dropped. Every value here is deterministic — never a model-reported
    confidence or explanation (Section 10.12: "Model-reported confidence
    is stored only for analysis, never used alone")."""

    UNKNOWN_CHUNK_ID = "unknown_chunk_id"
    SPAN_OUT_OF_BOUNDS = "span_out_of_bounds"
    EMPTY_QUOTE = "empty_quote"
    QUOTE_MISMATCH = "quote_mismatch"
    UNSUPPORTED_OWNER = "unsupported_owner"
    MALFORMED_DATE = "malformed_date"
    PROVIDER_REFUSAL = "provider_refusal"


class ExtractionStatus(StrEnum):
    COMPLETED = "completed"
    NO_MATERIAL_CONTENT = "no_material_content"
    FAILED_REVIEWABLE = "failed_reviewable"


@dataclass(frozen=True)
class RejectedObservation:
    """One dropped candidate, kept for display (never silently discarded)."""

    chunk_id: str
    reason: RejectionReason
    detail: str
    kind: str | None = None
    subject: str | None = None


@dataclass(frozen=True)
class ExtractionRunResult:
    """The full result of one manually-triggered extraction over one
    content version's chunks — never persisted, never a ledger write."""

    project_id: str
    content_id: str
    status: ExtractionStatus
    accepted: tuple[ExtractedObservation, ...] = field(default_factory=tuple)
    rejected: tuple[RejectedObservation, ...] = field(default_factory=tuple)
    chunks_processed: int = 0
    chunks_total: int = 0
    model: str = DEFAULT_OPENAI_MODEL
    prompt_version: str = PROMPT_VERSION
    schema_version: str = SCHEMA_VERSION
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_estimated_cost_usd: float = 0.0
    total_latency_ms: int = 0
    safe_error: str | None = None


def _normalize_whitespace(text: str) -> str:
    """Conservative whitespace normalization (Section 8: "quoted text
    normalized for whitespace matches the span"): collapse any run of
    whitespace to a single space and strip the ends. Does not touch
    punctuation or case — a quote that differs in wording still fails."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def validate_observation(
    observation: ExtractedObservation,
    *,
    allowed_chunks: dict[str, str],
) -> RejectedObservation | None:
    """Deterministic evidence validation (Section 12.4/12.8). Returns the
    rejection if any evidence span or field fails; ``None`` means every
    check passed and the observation is accepted.

    ``allowed_chunks`` is the exact set of chunk IDs (mapped to their
    exact text) sent to the model for *this* request — not "any chunk
    that exists in the database" and not "any chunk in this project".
    A citation to a chunk outside that set is rejected as
    ``UNKNOWN_CHUNK_ID`` whether it belongs to a different content
    version, a different artifact, or a different project entirely (see
    the cross-project rejection test in
    tests/unit/test_extraction_service.py).
    """
    for span in observation.evidence:
        chunk_text = allowed_chunks.get(span.chunk_id)
        if chunk_text is None:
            return RejectedObservation(
                chunk_id=span.chunk_id,
                reason=RejectionReason.UNKNOWN_CHUNK_ID,
                detail="cited chunk_id was not one of the chunks sent in this request",
                kind=observation.kind.value,
                subject=observation.subject,
            )
        if span.char_end > len(chunk_text):
            return RejectedObservation(
                chunk_id=span.chunk_id,
                reason=RejectionReason.SPAN_OUT_OF_BOUNDS,
                detail=(
                    f"span [{span.char_start}, {span.char_end}) exceeds chunk length "
                    f"({len(chunk_text)})"
                ),
                kind=observation.kind.value,
                subject=observation.subject,
            )
        if not span.quote.strip():
            return RejectedObservation(
                chunk_id=span.chunk_id,
                reason=RejectionReason.EMPTY_QUOTE,
                detail="quote is empty or whitespace-only",
                kind=observation.kind.value,
                subject=observation.subject,
            )
        actual = chunk_text[span.char_start : span.char_end]
        if _normalize_whitespace(actual) != _normalize_whitespace(span.quote):
            return RejectedObservation(
                chunk_id=span.chunk_id,
                reason=RejectionReason.QUOTE_MISMATCH,
                detail="quote does not match the cited chunk text at that span",
                kind=observation.kind.value,
                subject=observation.subject,
            )

    if observation.owner_name:
        combined_quotes = " ".join(span.quote for span in observation.evidence)
        owner_needle = _normalize_whitespace(observation.owner_name).lower()
        haystack = _normalize_whitespace(combined_quotes).lower()
        if owner_needle not in haystack:
            return RejectedObservation(
                chunk_id=observation.evidence[0].chunk_id,
                reason=RejectionReason.UNSUPPORTED_OWNER,
                detail="owner_name does not appear in the observation's own cited evidence",
                kind=observation.kind.value,
                subject=observation.subject,
            )

    if observation.date_value is not None and not (
        _MIN_PLAUSIBLE_YEAR <= observation.date_value.year <= _MAX_PLAUSIBLE_YEAR
    ):
        return RejectedObservation(
            chunk_id=observation.evidence[0].chunk_id,
            reason=RejectionReason.MALFORMED_DATE,
            detail=f"date_value year {observation.date_value.year} is implausible",
            kind=observation.kind.value,
            subject=observation.subject,
        )

    return None


def _safe_error_message(exc: LLMProviderError) -> str:
    """A message safe to show in the UI and to log: the exception's own
    class/message only — never a raw model response or source text. A
    ``LLMSchemaFailureError``'s ``validation_errors`` are Pydantic field
    paths/messages, not source content, so they are safe to include."""
    if isinstance(exc, LLMSchemaFailureError) and exc.validation_errors:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}"
            for err in exc.validation_errors
        )
        return f"{exc}: {details}"
    return str(exc)


def build_default_provider(*, config: AppConfig | None = None) -> LLMProvider | None:
    """Construct the default (OpenAI) provider from ``OPENAI_API_KEY``.

    Returns ``None`` when no key is configured — this repository does not
    yet implement the OS-keyring/encrypted-secrets subsystem Section 16
    describes for production secrets; reading the standard
    ``OPENAI_API_KEY`` environment variable (which the OpenAI SDK itself
    also looks for) is the documented, minimal path for this prototype
    step, and callers must treat ``None`` as "not configured", not an
    error.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    _ = config or load_config()  # validated eagerly; not otherwise needed here
    return OpenAIProvider(api_key=api_key)


def extract_content(
    conn: sqlite3.Connection,
    project_id: str,
    content_id: str,
    *,
    provider: LLMProvider,
    model: str = DEFAULT_OPENAI_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> ExtractionRunResult:
    """Run Stage-A extraction over every chunk of one content version, one
    call per chunk (Section 8: "Use one call per chunk"), validating each
    returned observation before accepting it.

    Raises ``project_context.services.projects.ProjectNotFoundError`` if
    ``project_id`` does not exist, and ``ValueError`` if ``content_id``
    does not belong to that project — both are programming/UI errors, not
    part of the extraction result, so they are not folded into
    ``ExtractionRunResult.status``.
    """
    project: Project = get_project(conn, project_id)
    content: SourceContent | None = evidence_repository.get_content(conn, project_id, content_id)
    if content is None:
        raise ValueError(f"content {content_id!r} not found in project {project_id!r}")
    artifact: SourceArtifact | None = evidence_repository.get_artifact(
        conn, project_id, content.artifact_id
    )
    if artifact is None:
        raise ValueError(f"artifact for content {content_id!r} not found")

    chunks: list[SourceChunk] = evidence_repository.list_chunks_for_content(
        conn, project_id, content_id
    )
    base_result = {
        "project_id": project_id,
        "content_id": content_id,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    if not chunks:
        return ExtractionRunResult(
            status=ExtractionStatus.NO_MATERIAL_CONTENT, chunks_total=0, **base_result
        )

    system = load_extraction_system_prompt()
    config = ModelConfig(model=model, reasoning_effort=reasoning_effort, store=False)

    accepted: list[ExtractedObservation] = []
    rejected: list[RejectedObservation] = []
    total_input_tokens = total_output_tokens = total_latency_ms = 0
    total_cost = 0.0
    chunks_processed = 0

    for chunk in chunks:
        allowed_chunks = {chunk.id: chunk.text}
        user_input = build_extraction_input(project=project, artifact=artifact, chunk=chunk)
        try:
            result = provider.generate_structured(
                task="extract_observations",
                system=system,
                input_text=user_input,
                response_model=ExtractionBatch,
                config=config,
            )
        except LLMRefusalError as exc:
            chunks_processed += 1
            rejected.append(
                RejectedObservation(
                    chunk_id=chunk.id,
                    reason=RejectionReason.PROVIDER_REFUSAL,
                    detail=_safe_error_message(exc),
                )
            )
            logger.info(
                "extraction_chunk_refused",
                extra={"project_id": project_id, "content_id": content_id, "chunk_id": chunk.id},
            )
            continue
        except LLMProviderError as exc:
            logger.info(
                "extraction_failed_reviewable",
                extra={
                    "project_id": project_id,
                    "content_id": content_id,
                    "chunk_id": chunk.id,
                    "error_class": type(exc).__name__,
                },
            )
            return ExtractionRunResult(
                status=ExtractionStatus.FAILED_REVIEWABLE,
                accepted=tuple(accepted),
                rejected=tuple(rejected),
                chunks_processed=chunks_processed,
                chunks_total=len(chunks),
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_estimated_cost_usd=total_cost,
                total_latency_ms=total_latency_ms,
                safe_error=_safe_error_message(exc),
                **base_result,
            )

        chunks_processed += 1
        total_input_tokens += result.input_tokens
        total_output_tokens += result.output_tokens
        total_cost += result.estimated_cost_usd
        total_latency_ms += result.latency_ms

        batch = result.parsed
        assert isinstance(batch, ExtractionBatch)
        for observation in batch.observations:
            problem = validate_observation(observation, allowed_chunks=allowed_chunks)
            if problem is None:
                accepted.append(observation)
            else:
                rejected.append(problem)

    logger.info(
        "extraction_completed",
        extra={
            "project_id": project_id,
            "content_id": content_id,
            "chunks_processed": chunks_processed,
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_estimated_cost_usd": total_cost,
        },
    )
    return ExtractionRunResult(
        status=ExtractionStatus.COMPLETED,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        chunks_processed=chunks_processed,
        chunks_total=len(chunks),
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_estimated_cost_usd=total_cost,
        total_latency_ms=total_latency_ms,
        **base_result,
    )
