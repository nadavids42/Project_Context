"""Drive-sourced evidence storage: turns one fetched `RawArtifact` into
`source_artifacts`/`source_contents`/`source_chunks` rows through the
exact same parser registry and chunker manual ingestion uses (Section
11: connectors "normalize metadata but do not extract project facts" —
nothing here parses Drive-specific structure; it is the same TXT/MD/
DOCX/PDF/VTT parser registry `project_context.services.evidence` uses).

The one deliberate difference from manual ingestion: identity and
change detection use the connector's own external ID and version
marker (Drive's `file.id`/`modifiedTime`) directly, never a hash of a
user-entered title (see `project_context.services.evidence`'s
documented limitation for why that convention exists for manual
uploads and is not appropriate here — Drive gives a real external ID
for free).
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
    ParseStatus,
    SourceArtifact,
    SourceChunk,
    SourceContent,
)
from project_context.evidence_store import store_bytes
from project_context.parsers import (
    MIME_TYPES,
    ParseResult,
    UnsupportedEvidenceError,
    detect_kind,
    parse,
)


@dataclass(frozen=True)
class DriveStoreResult:
    artifact: SourceArtifact
    content: SourceContent
    chunks: tuple[SourceChunk, ...]
    created_new_version: bool


def get_or_create_drive_artifact(
    conn: sqlite3.Connection, project_id: str, source_id: str, metadata: ArtifactMetadata
) -> SourceArtifact:
    """Look up this artifact by its real Drive file ID (the connector's
    `external_id`), creating it on first sight. Never touches content —
    that only happens once the caller has decided a fetch is needed."""
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
    )


def is_unchanged(
    conn: sqlite3.Connection, project_id: str, artifact: SourceArtifact, metadata: ArtifactMetadata
) -> bool:
    """True when the artifact's current content's `version_key` already
    equals this discovery's `version_marker` — decided from metadata
    already in hand (Section 11.2: "compare ... modified time/version
    ... before downloading"), with no fetch and no extra API call."""
    if artifact.current_content_id is None:
        return False
    current = evidence_repository.get_content(conn, project_id, artifact.current_content_id)
    return current is not None and current.version_key == metadata.version_marker


def store_raw_artifact(
    conn: sqlite3.Connection,
    project_id: str,
    artifact: SourceArtifact,
    raw: RawArtifact,
    *,
    evidence_dir: Path,
    chunk_target_chars: int,
    chunk_overlap_ratio: float,
) -> DriveStoreResult:
    """Parse, chunk, and store one freshly fetched `RawArtifact` as a
    new content version keyed by its connector-native version marker.
    Content-hash dedup still applies underneath (Section 9's `(artifact_id,
    sha256)` unique index) — a file Drive reports as modified but whose
    bytes are actually unchanged still reuses the existing content row
    rather than creating a duplicate."""
    filename = raw.filename or raw.metadata.title
    try:
        kind = detect_kind(filename=filename, data=raw.data)
    except UnsupportedEvidenceError as exc:
        parse_result = ParseResult(
            status=ParseStatus.UNSUPPORTED,
            parser_name=None,
            parser_version=None,
            normalized_text="",
            warnings=(str(exc),),
        )
        mime_type = raw.mime_type
    else:
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
        return DriveStoreResult(
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
    if parse_result.status == ParseStatus.PARSED and parse_result.blocks:
        chunk_specs = chunk_blocks(
            parse_result.blocks, target_chars=chunk_target_chars, overlap_ratio=chunk_overlap_ratio
        )
        chunks = evidence_repository.insert_chunks(conn, project_id, content.id, chunk_specs)

    return DriveStoreResult(
        artifact=artifact, content=content, chunks=tuple(chunks), created_new_version=True
    )
