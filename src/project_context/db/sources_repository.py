"""Sources repository: direct SQL access to the `sources` table.

For this step, only what manual evidence ingestion needs: get-or-create
the one `kind='manual'` source every project gets lazily, on its first
manual submission. Boundary configuration and the other connector kinds
are a later step's concern.
"""

from __future__ import annotations

import sqlite3

from project_context.domain.sources import (
    MANUAL_SOURCE_DISPLAY_NAME,
    Source,
    SourceHealthStatus,
    SourceKind,
)
from project_context.ids import new_id
from project_context.timeutil import utc_now_iso


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"],
        project_id=row["project_id"],
        kind=SourceKind(row["kind"]),
        display_name=row["display_name"],
        external_account_id=row["external_account_id"],
        boundary_json=row["boundary_json"],
        credential_ref=row["credential_ref"],
        enabled=bool(row["enabled"]),
        last_success_at=row["last_success_at"],
        last_cursor=row["last_cursor"],
        health_status=SourceHealthStatus(row["health_status"]),
        last_error_code=row["last_error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_source(conn: sqlite3.Connection, project_id: str, source_id: str) -> Source | None:
    row = conn.execute(
        "SELECT * FROM sources WHERE id = ? AND project_id = ?", (source_id, project_id)
    ).fetchone()
    return _row_to_source(row) if row is not None else None


def get_manual_source(conn: sqlite3.Connection, project_id: str) -> Source | None:
    row = conn.execute(
        "SELECT * FROM sources WHERE project_id = ? AND kind = ? AND display_name = ?",
        (project_id, SourceKind.MANUAL.value, MANUAL_SOURCE_DISPLAY_NAME),
    ).fetchone()
    return _row_to_source(row) if row is not None else None


def ensure_manual_source(conn: sqlite3.Connection, project_id: str) -> Source:
    """Get the project's manual source, creating it if this is the first
    manual submission. Callers run this inside their own transaction."""
    existing = get_manual_source(conn, project_id)
    if existing is not None:
        return existing

    source_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO sources
            (id, project_id, kind, display_name, enabled, health_status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            source_id,
            project_id,
            SourceKind.MANUAL.value,
            MANUAL_SOURCE_DISPLAY_NAME,
            SourceHealthStatus.READY.value,
            now,
            now,
        ),
    )
    created = get_source(conn, project_id, source_id)
    assert created is not None
    return created
