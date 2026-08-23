"""Source domain model: connector/boundary configuration rows.

Minimal for this step — only what manual evidence ingestion needs (get
or create the one `kind='manual'` source per project). Boundary
configuration, health transitions, and the other connector kinds are a
later step's concern.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class SourceKind(StrEnum):
    """Matches the `sources.kind` CHECK constraint exactly."""

    MANUAL = "manual"
    DRIVE = "drive"
    GMAIL = "gmail"
    CALENDAR = "calendar"
    FATHOM = "fathom"


class SourceHealthStatus(StrEnum):
    """Matches the `sources.health_status` CHECK constraint exactly."""

    UNCONFIGURED = "unconfigured"
    READY = "ready"
    SYNCING = "syncing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    REAUTH_REQUIRED = "reauth_required"
    DISABLED = "disabled"


#: Display name of the one manual source every project gets, lazily,
#: on its first manual evidence submission. Unique per
#: `(project_id, kind, display_name)` — see migrations/0001.
MANUAL_SOURCE_DISPLAY_NAME = "Manual uploads"


class Source(BaseModel):
    """A source row as stored. See Section 9, table `sources`."""

    id: str
    project_id: str
    kind: SourceKind
    display_name: str
    external_account_id: str | None
    boundary_json: str | None
    credential_ref: str | None
    enabled: bool
    last_success_at: str | None
    last_cursor: str | None
    health_status: SourceHealthStatus
    last_error_code: str | None
    created_at: str
    updated_at: str
