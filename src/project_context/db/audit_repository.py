"""Audit-entry repository: direct SQL access to `audit_entries`.

Pure data access, like `project_context.db.projects_repository` — no
transaction management; callers wrap multi-step operations in
`project_context.db.connection.transaction`.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from project_context.domain.audit import AuditAction, AuditEntry
from project_context.ids import new_id
from project_context.timeutil import utc_now_iso


def _row_to_entry(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        id=row["id"],
        project_id=row["project_id"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        action=AuditAction(row["action"]),
        before=json.loads(row["before_json"]) if row["before_json"] is not None else None,
        after=json.loads(row["after_json"]) if row["after_json"] is not None else None,
        actor=row["actor"],
        created_at=row["created_at"],
    )


def record_entry(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    entity_type: str,
    entity_id: str,
    action: AuditAction | str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    actor: str,
) -> AuditEntry:
    """Append one immutable audit entry. Never updated or deleted."""
    action_value = action.value if isinstance(action, AuditAction) else action
    entry_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO audit_entries
            (id, project_id, entity_type, entity_id, action, before_json, after_json,
             actor, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry_id,
            project_id,
            entity_type,
            entity_id,
            action_value,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
            actor,
            now,
        ),
    )
    return AuditEntry(
        id=entry_id,
        project_id=project_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=AuditAction(action_value),
        before=before,
        after=after,
        actor=actor,
        created_at=now,
    )


def list_for_project(conn: sqlite3.Connection, project_id: str) -> list[AuditEntry]:
    """All audit entries for one project, oldest first. Takes `project_id`
    explicitly — every project-owned repository method does, so isolation
    filters stay direct (Section 9)."""
    rows = conn.execute(
        "SELECT * FROM audit_entries WHERE project_id = ? ORDER BY created_at ASC",
        (project_id,),
    ).fetchall()
    return [_row_to_entry(row) for row in rows]


def list_for_entity(
    conn: sqlite3.Connection, *, project_id: str, entity_type: str, entity_id: str
) -> list[AuditEntry]:
    """Audit entries for one specific entity, oldest first, scoped to a
    project explicitly even though `entity_id` alone would already
    disambiguate it in this schema."""
    rows = conn.execute(
        "SELECT * FROM audit_entries "
        "WHERE project_id = ? AND entity_type = ? AND entity_id = ? "
        "ORDER BY created_at ASC",
        (project_id, entity_type, entity_id),
    ).fetchall()
    return [_row_to_entry(row) for row in rows]
