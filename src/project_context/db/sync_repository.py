"""Sync repository: direct SQL access to `sync_runs` and `sync_items`
(Section 9; FR-009, FR-031; Prompt 10).

Pure data access — no orchestration, no connector calls, no
transaction management. `project_context.services.sync` owns the
orchestration; this module only reads/writes rows.
"""

from __future__ import annotations

import sqlite3

from project_context.domain.sync import (
    SyncErrorClass,
    SyncItem,
    SyncItemStage,
    SyncItemStatus,
    SyncRun,
    SyncRunStatus,
    SyncTrigger,
)
from project_context.ids import new_id
from project_context.timeutil import utc_now_iso


def _row_to_sync_run(row: sqlite3.Row) -> SyncRun:
    return SyncRun(
        id=row["id"],
        project_id=row["project_id"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        status=SyncRunStatus(row["status"]),
        trigger=SyncTrigger(row["trigger"]),
        discovered_count=row["discovered_count"],
        unchanged_count=row["unchanged_count"],
        downloaded_count=row["downloaded_count"],
        parsed_count=row["parsed_count"],
        extracted_count=row["extracted_count"],
        failed_count=row["failed_count"],
        proposed_count=row["proposed_count"],
        needs_assignment_count=row["needs_assignment_count"],
        correlation_id=row["correlation_id"],
        app_version=row["app_version"],
        updated_at=row["updated_at"],
    )


def _row_to_sync_item(row: sqlite3.Row) -> SyncItem:
    return SyncItem(
        id=row["id"],
        sync_run_id=row["sync_run_id"],
        source_id=row["source_id"],
        artifact_id=row["artifact_id"],
        external_id=row["external_id"],
        stage=SyncItemStage(row["stage"]),
        status=SyncItemStatus(row["status"]),
        attempt_count=row["attempt_count"],
        error_class=SyncErrorClass(row["error_class"]) if row["error_class"] else None,
        safe_error_message=row["safe_error_message"],
        duration_ms=row["duration_ms"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# --- sync_runs --------------------------------------------------------------


def insert_sync_run(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    correlation_id: str | None = None,
    app_version: str | None = None,
) -> SyncRun:
    run_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO sync_runs
            (id, project_id, started_at, status, trigger, correlation_id, app_version, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            project_id,
            now,
            SyncRunStatus.RUNNING.value,
            SyncTrigger.MANUAL.value,
            correlation_id,
            app_version,
            now,
        ),
    )
    run = get_sync_run(conn, project_id, run_id)
    assert run is not None
    return run


def get_sync_run(conn: sqlite3.Connection, project_id: str, run_id: str) -> SyncRun | None:
    row = conn.execute(
        "SELECT * FROM sync_runs WHERE id = ? AND project_id = ?", (run_id, project_id)
    ).fetchone()
    return _row_to_sync_run(row) if row is not None else None


def finalize_sync_run(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    *,
    status: SyncRunStatus,
    discovered_count: int,
    unchanged_count: int,
    downloaded_count: int = 0,
    parsed_count: int,
    extracted_count: int = 0,
    failed_count: int,
    proposed_count: int,
    needs_assignment_count: int,
) -> SyncRun:
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE sync_runs
        SET status = ?, ended_at = ?, discovered_count = ?, unchanged_count = ?,
            downloaded_count = ?, parsed_count = ?, extracted_count = ?, failed_count = ?,
            proposed_count = ?, needs_assignment_count = ?, updated_at = ?
        WHERE id = ? AND project_id = ?
        """,
        (
            status.value,
            now,
            discovered_count,
            unchanged_count,
            downloaded_count,
            parsed_count,
            extracted_count,
            failed_count,
            proposed_count,
            needs_assignment_count,
            now,
            run_id,
            project_id,
        ),
    )
    run = get_sync_run(conn, project_id, run_id)
    assert run is not None
    return run


def list_sync_runs_for_project(
    conn: sqlite3.Connection, project_id: str, *, limit: int = 20
) -> list[SyncRun]:
    rows = conn.execute(
        "SELECT * FROM sync_runs WHERE project_id = ? ORDER BY started_at DESC LIMIT ?",
        (project_id, limit),
    ).fetchall()
    return [_row_to_sync_run(row) for row in rows]


# --- sync_items ---------------------------------------------------------


def insert_sync_item(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    sync_run_id: str,
    source_id: str,
    artifact_id: str | None,
    external_id: str,
    stage: SyncItemStage,
    status: SyncItemStatus = SyncItemStatus.OK,
    attempt_count: int = 1,
    error_class: SyncErrorClass | None = None,
    safe_error_message: str | None = None,
    duration_ms: int | None = None,
) -> SyncItem:
    """`project_id` is accepted (and unused in the SQL below) only to
    keep this repository's call shape consistent with every other
    project-scoped insert in the codebase — `sync_items` itself has no
    `project_id` column (Section 9's table definition); it is always
    reached through `sync_run_id`/`source_id`, which are themselves
    project-scoped."""
    item_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO sync_items
            (id, sync_run_id, source_id, artifact_id, external_id, stage, status,
             attempt_count, error_class, safe_error_message, duration_ms, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            sync_run_id,
            source_id,
            artifact_id,
            external_id,
            stage.value,
            status.value,
            attempt_count,
            error_class.value if error_class else None,
            safe_error_message,
            duration_ms,
            now,
            now,
        ),
    )
    item = get_sync_item(conn, item_id)
    assert item is not None
    return item


def get_sync_item(conn: sqlite3.Connection, item_id: str) -> SyncItem | None:
    row = conn.execute("SELECT * FROM sync_items WHERE id = ?", (item_id,)).fetchone()
    return _row_to_sync_item(row) if row is not None else None


def list_sync_items_for_run(conn: sqlite3.Connection, sync_run_id: str) -> list[SyncItem]:
    rows = conn.execute(
        "SELECT * FROM sync_items WHERE sync_run_id = ? ORDER BY created_at ASC", (sync_run_id,)
    ).fetchall()
    return [_row_to_sync_item(row) for row in rows]


def list_failed_sync_items_for_source(
    conn: sqlite3.Connection, source_id: str, *, limit: int = 500
) -> list[SyncItem]:
    """The most recent failed attempt per external_id for one source —
    used to resume a partial sync by retrying only what previously
    failed (Section 8: "Preserve a per-item checkpoint; rerun only
    failed/new items")."""
    rows = conn.execute(
        """
        SELECT si.* FROM sync_items si
        JOIN (
            SELECT external_id, MAX(created_at) AS latest
            FROM sync_items WHERE source_id = ?
            GROUP BY external_id
        ) latest_per_item
            ON si.external_id = latest_per_item.external_id
            AND si.created_at = latest_per_item.latest
        WHERE si.source_id = ? AND si.status = 'error'
        ORDER BY si.created_at DESC
        LIMIT ?
        """,
        (source_id, source_id, limit),
    ).fetchall()
    return [_row_to_sync_item(row) for row in rows]
