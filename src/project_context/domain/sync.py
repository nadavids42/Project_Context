"""Sync orchestration domain model: `sync_runs`/`sync_items` rows (Section
9; FR-009, FR-031; Prompt 10).

Enum members match the `sync_runs.status`, `sync_items.stage`,
`sync_items.status`, and `sync_items.error_class` CHECK constraints in
migrations/0001_evidence_foundation.sql exactly — that migration
predates this module (it was written ahead of connector work) so no
schema change was needed to support Prompt 10.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class SyncTrigger(StrEnum):
    """Matches the `sync_runs.trigger` CHECK constraint. Only `manual`
    exists — Prompt 10 explicitly excludes background/webhook sync."""

    MANUAL = "manual"


class SyncRunStatus(StrEnum):
    """Matches the `sync_runs.status` CHECK constraint exactly."""

    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"


class SyncItemStage(StrEnum):
    """Matches the `sync_items.stage` CHECK constraint exactly — the
    furthest pipeline stage one artifact reached in one sync run."""

    DISCOVERED = "discovered"
    UNCHANGED = "unchanged"
    DOWNLOADED = "downloaded"
    PARSED = "parsed"
    EXTRACTED = "extracted"
    FAILED = "failed"
    SKIPPED = "skipped"


class SyncItemStatus(StrEnum):
    """Matches the `sync_items.status` CHECK constraint exactly — the
    outcome marker for whatever `stage` an item reached."""

    PENDING = "pending"
    OK = "ok"
    ERROR = "error"


class SyncErrorClass(StrEnum):
    """Matches the `sync_items.error_class` CHECK constraint exactly
    (Section 8, "Error handling: Classify errors")."""

    AUTH = "auth"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    NOT_FOUND = "not_found"
    PARSE = "parse"
    SCHEMA = "schema"
    PROVIDER = "provider"
    DATABASE = "database"
    ASSIGNMENT = "assignment"


class SyncRun(BaseModel):
    """A `sync_runs` row as stored. See Section 9, table `sync_runs`."""

    id: str
    project_id: str
    started_at: str
    ended_at: str | None
    status: SyncRunStatus
    trigger: SyncTrigger
    discovered_count: int
    unchanged_count: int
    downloaded_count: int
    parsed_count: int
    extracted_count: int
    failed_count: int
    proposed_count: int
    needs_assignment_count: int
    correlation_id: str | None
    app_version: str | None
    updated_at: str


class SyncItem(BaseModel):
    """A `sync_items` row as stored. See Section 9, table `sync_items`.

    Carries no source body (Section 9: "contains no source body") — only
    IDs, a stage/status pair, and a safe, non-quoting error message.
    """

    id: str
    sync_run_id: str
    source_id: str
    artifact_id: str | None
    external_id: str
    stage: SyncItemStage
    status: SyncItemStatus
    attempt_count: int
    error_class: SyncErrorClass | None
    safe_error_message: str | None
    duration_ms: int | None
    created_at: str
    updated_at: str
