"""Project repository: direct SQL access to the `projects` table.

Pure data access — no business-rule validation (that lives in
`project_context.domain.projects`) and no audit-entry writing or
transaction management (that is `project_context.services.projects`'s
job). UI code must never execute SQL against `projects` directly; it
goes through the service layer, which is the only caller of this
module.
"""

from __future__ import annotations

import sqlite3

from project_context.domain.projects import (
    Project,
    ProjectCreateInput,
    ProjectStatus,
    ProjectUpdateInput,
)
from project_context.ids import new_id
from project_context.timeutil import utc_now_iso


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        objective=row["objective"],
        description=row["description"],
        stage=row["stage"],
        client_name=row["client_name"],
        status=ProjectStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


def insert_project(conn: sqlite3.Connection, data: ProjectCreateInput) -> Project:
    """Create a new project row with a fresh, immutable id."""
    project_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO projects
            (id, name, objective, description, stage, client_name, status,
             created_at, updated_at, archived_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            project_id,
            data.name,
            data.objective,
            data.description,
            data.stage,
            data.client_name,
            data.status.value,
            now,
            now,
        ),
    )
    project = get_project(conn, project_id)
    assert project is not None  # just inserted, in the same connection
    return project


def get_project(conn: sqlite3.Connection, project_id: str) -> Project | None:
    """Fetch one project by id, or None if it does not exist."""
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _row_to_project(row) if row is not None else None


def list_projects(
    conn: sqlite3.Connection, *, statuses: tuple[ProjectStatus, ...]
) -> list[Project]:
    """List projects whose status is in `statuses`, most recently updated
    first. Always requires an explicit status set — there is no
    "list everything with no filter" call, so a caller can't accidentally
    forget to exclude archived projects."""
    if not statuses:
        return []
    placeholders = ", ".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT * FROM projects WHERE status IN ({placeholders}) ORDER BY updated_at DESC",
        tuple(s.value for s in statuses),
    ).fetchall()
    return [_row_to_project(row) for row in rows]


def update_project_fields(
    conn: sqlite3.Connection, project_id: str, data: ProjectUpdateInput
) -> Project | None:
    """Overwrite editable metadata fields. Never touches `id`,
    `created_at`, or `archived_at` — archiving/restoring is a separate
    operation (`set_archived`)."""
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE projects
        SET name = ?, objective = ?, description = ?, stage = ?, client_name = ?,
            status = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            data.name,
            data.objective,
            data.description,
            data.stage,
            data.client_name,
            data.status.value,
            now,
            project_id,
        ),
    )
    return get_project(conn, project_id)


def set_archived(conn: sqlite3.Connection, project_id: str, *, archived: bool) -> Project | None:
    """Archive (status -> archived, stamp `archived_at`) or restore
    (status -> active, clear `archived_at`) one project."""
    now = utc_now_iso()
    if archived:
        conn.execute(
            "UPDATE projects SET status = 'archived', archived_at = ?, updated_at = ? WHERE id = ?",
            (now, now, project_id),
        )
    else:
        conn.execute(
            "UPDATE projects SET status = 'active', archived_at = NULL, updated_at = ? "
            "WHERE id = ?",
            (now, project_id),
        )
    return get_project(conn, project_id)
