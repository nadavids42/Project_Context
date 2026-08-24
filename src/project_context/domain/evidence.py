"""Evidence domain models: artifacts, immutable content versions, chunks,
and validated manual-ingestion input.

See docs/Project_Context_Product_Plan_v1.md FR-005/FR-006 and Section 9
(`source_artifacts`, `source_contents`, `source_chunks`).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

TITLE_MAX_LENGTH = 300
AUTHOR_MAX_LENGTH = 200
EXTERNAL_URL_MAX_LENGTH = 2000
FILENAME_MAX_LENGTH = 255
#: A generous but bounded cap on pasted text — protects the database and
#: UI from a pathological paste, not a meaningful document-size limit.
MANUAL_TEXT_MAX_LENGTH = 2_000_000


class EvidenceSourceType(StrEnum):
    """User-facing description of what kind of evidence this is —
    distinct from `ArtifactType`, which describes *how* it was ingested.
    Matches the `source_artifacts.source_type` CHECK constraint exactly
    (migrations/0003)."""

    MEETING_NOTES = "meeting_notes"
    EMAIL = "email"
    DOCUMENT = "document"
    CHAT_TRANSCRIPT = "chat_transcript"
    CALL_RECORDING = "call_recording"
    OTHER = "other"


class ArtifactType(StrEnum):
    """Matches the `source_artifacts.artifact_type` CHECK constraint."""

    MANUAL_TEXT = "manual_text"
    FILE = "file"
    EMAIL = "email"
    CALENDAR_EVENT = "calendar_event"
    MEETING = "meeting"


class ArtifactAvailability(StrEnum):
    """Matches the `source_artifacts.availability` CHECK constraint."""

    AVAILABLE = "available"
    DELETED_EXTERNAL = "deleted_external"
    INACCESSIBLE = "inaccessible"
    UNASSIGNED = "unassigned"
    REJECTED = "rejected"


class AssignmentMethod(StrEnum):
    """Matches the `source_artifacts.assignment_method` CHECK constraint."""

    MANUAL = "manual"
    BOUNDARY_MATCH = "boundary_match"
    USER_ASSIGNED = "user_assigned"


class ParseStatus(StrEnum):
    """Matches the `source_contents.parse_status` CHECK constraint."""

    PENDING = "pending"
    PARSED = "parsed"
    EMPTY = "empty"
    OCR_REQUIRED = "ocr_required"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


#: Parse statuses that count as a genuine failure to show text, as
#: opposed to `PARSED`/`EMPTY` which are both "the parser ran and this is
#: what it honestly found."
FAILED_PARSE_STATUSES = (ParseStatus.OCR_REQUIRED, ParseStatus.UNSUPPORTED, ParseStatus.FAILED)


def _clean_required(value: str, field_name: str, max_length: int) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be empty or whitespace-only")
    if len(stripped) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return stripped


def _clean_optional(value: str | None, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return stripped


class _ManualEvidenceFields(BaseModel):
    """Fields shared by manual text and manual file-upload input."""

    title: str
    source_type: EvidenceSourceType
    occurred_at: datetime
    author: str | None = None
    external_url: str | None = None

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _clean_required(value, "title", TITLE_MAX_LENGTH)

    @field_validator("author")
    @classmethod
    def _validate_author(cls, value: str | None) -> str | None:
        return _clean_optional(value, "author", AUTHOR_MAX_LENGTH)

    @field_validator("external_url")
    @classmethod
    def _validate_external_url(cls, value: str | None) -> str | None:
        return _clean_optional(value, "external_url", EXTERNAL_URL_MAX_LENGTH)


class ManualTextInput(_ManualEvidenceFields):
    """Validated input for FR-005 (manual text ingestion)."""

    text: str

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _clean_required(value, "text", MANUAL_TEXT_MAX_LENGTH)


class ManualFileUploadInput(_ManualEvidenceFields):
    """Validated input for FR-006 (file upload).

    `data` is validated here only for presence/non-blank-filename; the
    configured maximum size (Section 11.1, default 25 MB) is a runtime
    setting, not a domain constant, so that check lives in
    `project_context.services.evidence` where `AppConfig` is available.
    """

    filename: str
    declared_mime_type: str | None = None
    data: bytes = Field(repr=False)

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, value: str) -> str:
        return _clean_required(value, "filename", FILENAME_MAX_LENGTH)

    @field_validator("data")
    @classmethod
    def _validate_data(cls, value: bytes) -> bytes:
        if not value:
            raise ValueError("uploaded file is empty")
        return value


class SourceArtifact(BaseModel):
    """A stable artifact identity, as stored. See Section 9,
    `source_artifacts`."""

    id: str
    project_id: str
    source_id: str
    external_id: str
    artifact_type: ArtifactType
    title: str | None
    author: str | None
    occurred_at: str | None
    external_url: str | None
    source_type: EvidenceSourceType | None
    assignment_method: AssignmentMethod
    availability: ArtifactAvailability
    current_content_id: str | None
    #: Which deterministic Calendar rule tier matched, and why (Section
    #: 11.4; Prompt 12) — `None`/`None` for every non-Calendar artifact,
    #: which has no equivalent concept (see migrations/0010).
    match_rule: str | None = None
    match_reason: str | None = None
    created_at: str
    updated_at: str


class SourceContent(BaseModel):
    """One immutable content version, as stored. See Section 9,
    `source_contents`."""

    id: str
    project_id: str
    artifact_id: str
    version_key: str
    sha256: str
    raw_storage_path: str | None
    mime_type: str | None
    byte_size: int | None
    normalized_text: str | None
    parser_name: str | None
    parser_version: str | None
    parse_status: ParseStatus
    location_map: dict[str, Any] | None
    original_filename: str | None
    created_at: str


class SourceChunk(BaseModel):
    """One extraction/retrieval chunk, as stored. See Section 9,
    `source_chunks`."""

    id: str
    project_id: str
    content_id: str
    ordinal: int
    text: str
    char_start: int
    char_end: int
    token_estimate: int | None
    section_path: str | None
    location: dict[str, Any] | None
    sha256: str
    created_at: str
