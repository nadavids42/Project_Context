"""Calendar-sourced evidence storage (Section 11.4; FR-029; Prompt 12).

Mirrors `project_context.services.drive_ingestion`/`gmail_ingestion`:
same parser registry, same content-hash dedup, same immutable-version
storage. Two deliberate differences from both:

- **Match provenance is persisted.** `get_or_create_calendar_artifact`
  passes the connector's computed `match_rule`/`match_reason` (Section
  11.4: "Store the exact match reason and score/rule outcome") through
  to `source_artifacts.match_rule`/`match_reason` (migrations/0010) —
  Drive/Gmail have no equivalent concept, since their boundary alone
  already explains why an item was discovered.
- **Chunking is all-or-nothing, not partial.** Gmail chunks everything
  before a conservative quote boundary; Calendar chunks *only* the
  event's own `description` text, or nothing at all if there is none
  (Prompt 12: "It must not create a decision, commitment, completion,
  or risk merely because an event exists. Only explicit description
  text may enter extraction as text evidence"). The stored
  `normalized_text` — what the evidence viewer shows — always includes
  the full metadata header regardless; only the *chunked, extraction-
  visible* portion is ever narrowed to the description.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from project_context.chunking import chunk_blocks
from project_context.connectors.protocol import ArtifactMetadata, RawArtifact
from project_context.db import evidence_repository
from project_context.domain.evidence import (
    AssignmentMethod,
    SourceArtifact,
    SourceChunk,
    SourceContent,
)
from project_context.evidence_store import store_bytes
from project_context.parsers import MIME_TYPES, EvidenceKind, detect_kind, parse


@dataclass(frozen=True)
class CalendarStoreResult:
    artifact: SourceArtifact
    content: SourceContent
    chunks: tuple[SourceChunk, ...]
    created_new_version: bool


def get_or_create_calendar_artifact(
    conn: sqlite3.Connection, project_id: str, source_id: str, metadata: ArtifactMetadata
) -> SourceArtifact:
    """Look up this event by its real Calendar event ID, creating it
    (with its match provenance) on first sight — never touches content.
    An already-known event's `match_rule`/`match_reason` are not
    updated on a later resync even if the project's rules have since
    changed (the same "create on first sight only" precedent Drive/
    Gmail's equivalent functions already establish)."""
    existing = evidence_repository.get_artifact_by_external_id(
        conn, project_id, source_id, metadata.external_id
    )
    if existing is not None:
        return existing
    return evidence_repository.insert_artifact(
        conn,
        project_id,
        source_id,
        external_id=metadata.external_id,
        artifact_type=metadata.artifact_type,
        title=metadata.title,
        author=metadata.author,
        occurred_at=metadata.occurred_at,
        external_url=metadata.external_url,
        source_type=metadata.source_type,
        assignment_method=AssignmentMethod.BOUNDARY_MATCH,
        match_rule=metadata.extra.get("match_rule"),
        match_reason=metadata.extra.get("match_reason"),
    )


def store_calendar_artifact(
    conn: sqlite3.Connection,
    project_id: str,
    artifact: SourceArtifact,
    raw: RawArtifact,
    *,
    evidence_dir: Path,
    chunk_target_chars: int,
    chunk_overlap_ratio: float,
) -> CalendarStoreResult:
    """Parse, chunk (description only), and store one freshly built
    Calendar event as a new content version. `raw.data` is always the
    connector's complete normalized text (metadata header, plus the
    description verbatim if present — see
    `project_context.connectors.calendar._build_normalized_event_text`)."""
    filename = raw.filename or f"calendar-{raw.metadata.external_id}.txt"
    kind = detect_kind(filename=filename, data=raw.data)
    assert kind is EvidenceKind.TXT, "Calendar events are always synthesized as plain text"
    mime_type = MIME_TYPES[kind]
    parse_result = parse(kind, raw.data)

    sha256, storage_path = store_bytes(evidence_dir, raw.data)
    existing_by_hash = evidence_repository.get_content_by_sha256(conn, artifact.id, sha256)
    if existing_by_hash is not None:
        if artifact.current_content_id != existing_by_hash.id:
            evidence_repository.set_current_content(
                conn, project_id, artifact.id, existing_by_hash.id
            )
        existing_chunks = evidence_repository.list_chunks_for_content(
            conn, project_id, existing_by_hash.id
        )
        return CalendarStoreResult(
            artifact=artifact,
            content=existing_by_hash,
            chunks=tuple(existing_chunks),
            created_new_version=False,
        )

    try:
        relative_storage_path = str(storage_path.relative_to(evidence_dir.resolve()))
    except ValueError:
        relative_storage_path = str(storage_path)

    content = evidence_repository.insert_content(
        conn,
        project_id,
        artifact.id,
        sha256=sha256,
        raw_storage_path=relative_storage_path,
        mime_type=mime_type,
        byte_size=len(raw.data),
        normalized_text=parse_result.normalized_text or None,
        parser_name=parse_result.parser_name,
        parser_version=parse_result.parser_version,
        parse_status=parse_result.status,
        location_map=parse_result.location_map(),
        original_filename=filename,
        version_key=raw.metadata.version_marker,
    )
    evidence_repository.set_current_content(conn, project_id, artifact.id, content.id)

    chunks: list[SourceChunk] = []
    if parse_result.blocks:
        full_text = parse_result.normalized_text
        # The metadata header block is joined to the description (when
        # present) by exactly one blank line (see
        # `calendar._build_normalized_event_text`); when absent, the
        # header block *is* the whole text and there is no "\n\n" at
        # all — in that case there is nothing to chunk, by design.
        header_end = full_text.find("\n\n")
        if header_end != -1:
            description_start = header_end + 2
            visible_blocks = [
                block for block in parse_result.blocks if block.char_start >= description_start
            ]
            if visible_blocks:
                chunk_specs = chunk_blocks(
                    visible_blocks,
                    target_chars=chunk_target_chars,
                    overlap_ratio=chunk_overlap_ratio,
                )
                chunks = evidence_repository.insert_chunks(
                    conn, project_id, content.id, chunk_specs
                )

    return CalendarStoreResult(
        artifact=artifact, content=content, chunks=tuple(chunks), created_new_version=True
    )
