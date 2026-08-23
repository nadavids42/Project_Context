"""Manual evidence ingestion service — the only supported way to submit
manual text or an uploaded file, and to read evidence back for the
Evidence list/viewer.

Owns transaction boundaries, content-addressed storage, parsing, and
chunking so UI code never touches `evidence_store`, the parser registry,
or the evidence repositories directly. See FR-005 through FR-009.

Manual-ingestion identity convention (documented limitation): a manual
text artifact is identified by a hash of its *normalized title*; a
manual file artifact is identified by a hash of its *normalized
filename*. Resubmitting under the same title/filename targets the same
artifact — unchanged content is deduplicated by SHA-256 (no new
version), changed content creates a new immutable version. Two
genuinely different documents that happen to share a title or filename
are, deliberately, treated as versions of one artifact; this is a
reasonable default for a single-user prototype and is not the identity
model future real connectors (which have true external IDs) will use.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from project_context.chunking import chunk_blocks
from project_context.db import evidence_repository, sources_repository
from project_context.db.connection import transaction
from project_context.domain.evidence import (
    ArtifactType,
    ManualFileUploadInput,
    ManualTextInput,
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
from project_context.parsers.txt_parser import parse_text
from project_context.services.projects import get_project

DEFAULT_ACTOR = "local-user"

_MANUAL_TEXT_MIME_TYPE = "text/plain"


class EvidenceTooLargeError(ValueError):
    """Raised when an uploaded file exceeds the configured maximum size."""


def _normalized_identity_digest(value: str) -> str:
    normalized = " ".join(value.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _manual_text_external_id(title: str) -> str:
    return f"text:{_normalized_identity_digest(title)}"


def _manual_file_external_id(filename: str) -> str:
    return f"file:{_normalized_identity_digest(filename)}"


@dataclass(frozen=True)
class IngestResult:
    artifact: SourceArtifact
    content: SourceContent
    chunks: tuple[SourceChunk, ...]
    created_new_version: bool


def _get_or_create_artifact(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    *,
    external_id: str,
    artifact_type: ArtifactType,
    data: ManualTextInput | ManualFileUploadInput,
) -> SourceArtifact:
    existing = evidence_repository.get_artifact_by_external_id(
        conn, project_id, source_id, external_id
    )
    if existing is not None:
        return existing
    return evidence_repository.insert_artifact(
        conn,
        project_id,
        source_id,
        external_id=external_id,
        artifact_type=artifact_type,
        title=data.title,
        author=data.author,
        occurred_at=data.occurred_at.isoformat(),
        external_url=data.external_url,
        source_type=data.source_type,
    )


def _store_content_and_chunks(
    conn: sqlite3.Connection,
    project_id: str,
    artifact: SourceArtifact,
    *,
    evidence_dir: Path,
    raw_bytes: bytes,
    mime_type: str,
    parse_result: ParseResult,
    original_filename: str | None,
    chunk_target_chars: int,
    chunk_overlap_ratio: float,
) -> IngestResult:
    sha256, storage_path = store_bytes(evidence_dir, raw_bytes)

    existing_content = evidence_repository.get_content_by_sha256(conn, artifact.id, sha256)
    if existing_content is not None:
        # Identical resubmission (FR-005/FR-006): no new version, no new
        # chunks. If it isn't already the current version (e.g. someone
        # reverted to an older version's exact bytes), make it current.
        if artifact.current_content_id != existing_content.id:
            artifact = evidence_repository.set_current_content(
                conn, project_id, artifact.id, existing_content.id
            )
        existing_chunks = evidence_repository.list_chunks_for_content(
            conn, project_id, existing_content.id
        )
        return IngestResult(
            artifact=artifact,
            content=existing_content,
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
        byte_size=len(raw_bytes),
        normalized_text=parse_result.normalized_text or None,
        parser_name=parse_result.parser_name,
        parser_version=parse_result.parser_version,
        parse_status=parse_result.status,
        location_map=parse_result.location_map(),
        original_filename=original_filename,
    )
    artifact = evidence_repository.set_current_content(conn, project_id, artifact.id, content.id)

    chunks: list[SourceChunk] = []
    if parse_result.status == ParseStatus.PARSED and parse_result.blocks:
        chunk_specs = chunk_blocks(
            parse_result.blocks,
            target_chars=chunk_target_chars,
            overlap_ratio=chunk_overlap_ratio,
        )
        chunks = evidence_repository.insert_chunks(conn, project_id, content.id, chunk_specs)

    return IngestResult(
        artifact=artifact, content=content, chunks=tuple(chunks), created_new_version=True
    )


def submit_manual_text(
    conn: sqlite3.Connection,
    project_id: str,
    data: ManualTextInput,
    *,
    evidence_dir: Path,
    chunk_target_chars: int,
    chunk_overlap_ratio: float,
) -> IngestResult:
    """FR-005: manual text ingestion."""
    get_project(conn, project_id)  # raises ProjectNotFoundError if missing
    raw_bytes = data.text.encode("utf-8")
    parse_result = parse_text(raw_bytes, markdown=False)
    external_id = _manual_text_external_id(data.title)

    with transaction(conn):
        source = sources_repository.ensure_manual_source(conn, project_id)
        artifact = _get_or_create_artifact(
            conn,
            project_id,
            source.id,
            external_id=external_id,
            artifact_type=ArtifactType.MANUAL_TEXT,
            data=data,
        )
        return _store_content_and_chunks(
            conn,
            project_id,
            artifact,
            evidence_dir=evidence_dir,
            raw_bytes=raw_bytes,
            mime_type=_MANUAL_TEXT_MIME_TYPE,
            parse_result=parse_result,
            original_filename=None,
            chunk_target_chars=chunk_target_chars,
            chunk_overlap_ratio=chunk_overlap_ratio,
        )


def submit_file_upload(
    conn: sqlite3.Connection,
    project_id: str,
    data: ManualFileUploadInput,
    *,
    evidence_dir: Path,
    max_upload_bytes: int,
    chunk_target_chars: int,
    chunk_overlap_ratio: float,
) -> IngestResult:
    """FR-006: file upload (TXT, MD, DOCX, PDF, VTT).

    A file whose content/extension can't be matched to a supported kind
    is not rejected outright — it is still stored (SHA-256 recorded) with
    `parse_status='unsupported'`, so the failure is visible in the
    Evidence list rather than silently discarded. Oversized files *are*
    rejected outright: that is an operational limit, not a parse outcome
    worth recording.
    """
    get_project(conn, project_id)  # raises ProjectNotFoundError if missing
    if len(data.data) > max_upload_bytes:
        raise EvidenceTooLargeError(
            f"file is {len(data.data)} bytes, which exceeds the configured maximum "
            f"of {max_upload_bytes} bytes"
        )

    try:
        kind = detect_kind(filename=data.filename, data=data.data)
    except UnsupportedEvidenceError as exc:
        mime_type = data.declared_mime_type or "application/octet-stream"
        parse_result = ParseResult(
            status=ParseStatus.UNSUPPORTED,
            parser_name=None,
            parser_version=None,
            normalized_text="",
            warnings=(str(exc),),
        )
    else:
        mime_type = MIME_TYPES[kind]
        parse_result = parse(kind, data.data)

    external_id = _manual_file_external_id(data.filename)

    with transaction(conn):
        source = sources_repository.ensure_manual_source(conn, project_id)
        artifact = _get_or_create_artifact(
            conn,
            project_id,
            source.id,
            external_id=external_id,
            artifact_type=ArtifactType.FILE,
            data=data,
        )
        return _store_content_and_chunks(
            conn,
            project_id,
            artifact,
            evidence_dir=evidence_dir,
            raw_bytes=data.data,
            mime_type=mime_type,
            parse_result=parse_result,
            original_filename=data.filename,
            chunk_target_chars=chunk_target_chars,
            chunk_overlap_ratio=chunk_overlap_ratio,
        )


# --- read paths for the Evidence list/viewer -------------------------------


@dataclass(frozen=True)
class EvidenceListItem:
    artifact: SourceArtifact
    current_parse_status: ParseStatus | None
    version_count: int


@dataclass(frozen=True)
class EvidenceDetail:
    artifact: SourceArtifact
    content: SourceContent | None
    chunks: tuple[SourceChunk, ...]
    version_count: int


def list_evidence(conn: sqlite3.Connection, project_id: str) -> list[EvidenceListItem]:
    """FR-005/FR-006 UI requirement: title, source type, occurred time,
    version, parser status, availability — one row per artifact."""
    items: list[EvidenceListItem] = []
    for artifact in evidence_repository.list_artifacts(conn, project_id):
        versions = evidence_repository.list_contents_for_artifact(conn, project_id, artifact.id)
        current = next((v for v in versions if v.id == artifact.current_content_id), None)
        items.append(
            EvidenceListItem(
                artifact=artifact,
                current_parse_status=current.parse_status if current else None,
                version_count=len(versions),
            )
        )
    return items


def get_evidence_detail(
    conn: sqlite3.Connection, project_id: str, artifact_id: str
) -> EvidenceDetail | None:
    """Everything the Evidence viewer needs for one artifact: metadata,
    the current version's normalized text, and its chunks."""
    artifact = evidence_repository.get_artifact(conn, project_id, artifact_id)
    if artifact is None:
        return None
    versions = evidence_repository.list_contents_for_artifact(conn, project_id, artifact_id)
    current = next((v for v in versions if v.id == artifact.current_content_id), None)
    chunks = (
        evidence_repository.list_chunks_for_content(conn, project_id, current.id)
        if current is not None
        else []
    )
    return EvidenceDetail(
        artifact=artifact, content=current, chunks=tuple(chunks), version_count=len(versions)
    )
