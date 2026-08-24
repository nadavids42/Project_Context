"""Sources repository: direct SQL access to the `sources` table.

Started (Prompt 6) with only what manual evidence ingestion needs:
get-or-create the one `kind='manual'` source every project gets lazily,
on its first manual submission. Prompt 10 adds the generic CRUD every
connector source (Drive, and later Gmail/Calendar/Fathom) needs:
create, boundary/credential/health/cursor updates, enable/disable, and
project-scoped listing/lookup by kind."""

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


# --- generic connector source CRUD (Prompt 10) ------------------------


def insert_source(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    kind: SourceKind,
    display_name: str,
    external_account_id: str | None = None,
    boundary_json: str | None = None,
    credential_ref: str | None = None,
    health_status: SourceHealthStatus = SourceHealthStatus.UNCONFIGURED,
) -> Source:
    """Create one connector source. Raises `sqlite3.IntegrityError` if
    `(project_id, kind, display_name)` already exists — display names
    must be distinct per connector kind within a project (migrations/0001's
    `uq_sources_project_kind_display_name`)."""
    source_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO sources
            (id, project_id, kind, display_name, external_account_id, boundary_json,
             credential_ref, enabled, health_status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            source_id,
            project_id,
            kind.value,
            display_name,
            external_account_id,
            boundary_json,
            credential_ref,
            health_status.value,
            now,
            now,
        ),
    )
    created = get_source(conn, project_id, source_id)
    assert created is not None
    return created


def list_sources_for_project(
    conn: sqlite3.Connection, project_id: str, *, kind: SourceKind | None = None
) -> list[Source]:
    if kind is None:
        rows = conn.execute(
            "SELECT * FROM sources WHERE project_id = ? ORDER BY created_at ASC", (project_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sources WHERE project_id = ? AND kind = ? ORDER BY created_at ASC",
            (project_id, kind.value),
        ).fetchall()
    return [_row_to_source(row) for row in rows]


def get_source_by_kind(
    conn: sqlite3.Connection, project_id: str, kind: SourceKind
) -> Source | None:
    """The first (oldest) source of this kind — used where the UI treats
    a connector kind as "at most one configured source per project"
    (Section 11.2: "exactly one explicit Drive folder ID per source/
    project"), without a schema-level constraint forcing that in
    general."""
    row = conn.execute(
        "SELECT * FROM sources WHERE project_id = ? AND kind = ? ORDER BY created_at ASC LIMIT 1",
        (project_id, kind.value),
    ).fetchone()
    return _row_to_source(row) if row is not None else None


def update_boundary(
    conn: sqlite3.Connection, project_id: str, source_id: str, *, boundary_json: str | None
) -> Source:
    now = utc_now_iso()
    conn.execute(
        "UPDATE sources SET boundary_json = ?, updated_at = ? WHERE id = ? AND project_id = ?",
        (boundary_json, now, source_id, project_id),
    )
    updated = get_source(conn, project_id, source_id)
    assert updated is not None
    return updated


def update_credential_ref(
    conn: sqlite3.Connection, project_id: str, source_id: str, *, credential_ref: str | None
) -> Source:
    now = utc_now_iso()
    conn.execute(
        "UPDATE sources SET credential_ref = ?, updated_at = ? WHERE id = ? AND project_id = ?",
        (credential_ref, now, source_id, project_id),
    )
    updated = get_source(conn, project_id, source_id)
    assert updated is not None
    return updated


def update_external_account_id(
    conn: sqlite3.Connection, project_id: str, source_id: str, *, external_account_id: str | None
) -> Source:
    now = utc_now_iso()
    conn.execute(
        "UPDATE sources SET external_account_id = ?, updated_at = ? "
        "WHERE id = ? AND project_id = ?",
        (external_account_id, now, source_id, project_id),
    )
    updated = get_source(conn, project_id, source_id)
    assert updated is not None
    return updated


def update_health(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    *,
    health_status: SourceHealthStatus,
    last_error_code: str | None = None,
) -> Source:
    now = utc_now_iso()
    conn.execute(
        "UPDATE sources SET health_status = ?, last_error_code = ?, updated_at = ? "
        "WHERE id = ? AND project_id = ?",
        (health_status.value, last_error_code, now, source_id, project_id),
    )
    updated = get_source(conn, project_id, source_id)
    assert updated is not None
    return updated


def update_last_success(
    conn: sqlite3.Connection, project_id: str, source_id: str, *, last_success_at: str
) -> Source:
    now = utc_now_iso()
    conn.execute(
        "UPDATE sources SET last_success_at = ?, updated_at = ? WHERE id = ? AND project_id = ?",
        (last_success_at, now, source_id, project_id),
    )
    updated = get_source(conn, project_id, source_id)
    assert updated is not None
    return updated


def update_last_cursor(
    conn: sqlite3.Connection, project_id: str, source_id: str, *, last_cursor: str | None
) -> Source:
    now = utc_now_iso()
    conn.execute(
        "UPDATE sources SET last_cursor = ?, updated_at = ? WHERE id = ? AND project_id = ?",
        (last_cursor, now, source_id, project_id),
    )
    updated = get_source(conn, project_id, source_id)
    assert updated is not None
    return updated


def set_enabled(
    conn: sqlite3.Connection, project_id: str, source_id: str, *, enabled: bool
) -> Source:
    now = utc_now_iso()
    conn.execute(
        "UPDATE sources SET enabled = ?, updated_at = ? WHERE id = ? AND project_id = ?",
        (1 if enabled else 0, now, source_id, project_id),
    )
    updated = get_source(conn, project_id, source_id)
    assert updated is not None
    return updated
