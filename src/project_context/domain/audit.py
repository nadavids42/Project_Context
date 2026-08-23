"""Minimal audit-entry domain model.

See migrations/0002_project_audit.sql and
docs/Project_Context_Product_Plan_v1.md FR-001 ("edits create an audit
entry"). Deliberately narrow: this records *that* a project-owned entity
changed, who changed it, and its before/after snapshot — not the richer
`reviews`/`corrections` provenance model (Section 9) that later
ledger-review work will need.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    ARCHIVE = "archive"
    RESTORE = "restore"


class AuditEntry(BaseModel):
    """An immutable record of one change to a project-owned entity."""

    id: str
    project_id: str
    entity_type: str
    entity_id: str
    action: AuditAction
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    actor: str
    created_at: str
