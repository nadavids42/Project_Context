"""Tests for `project_context.db.maintenance` (Section 16: "explicit
optional local secure-delete maintenance path using `PRAGMA
secure_delete=ON` and `VACUUM`")."""

from __future__ import annotations

from project_context.db.connection import connect
from project_context.db.maintenance import (
    run_secure_delete_maintenance,
    secure_delete_is_active,
)
from project_context.db.migrations import run_migrations
from project_context.domain.projects import ProjectCreateInput
from project_context.services.projects import create_project


def test_secure_delete_is_off_by_default_on_a_fresh_connection(tmp_path):
    conn = connect(tmp_path / "app.db")
    try:
        assert secure_delete_is_active(conn) is False
    finally:
        conn.close()


def test_run_secure_delete_maintenance_reduces_or_maintains_file_size_after_bulk_delete(
    tmp_path, migrations_dir
):
    sqlite_path = tmp_path / "app.db"
    conn = connect(sqlite_path)
    run_migrations(conn, migrations_dir)
    project = create_project(conn, ProjectCreateInput(name="Acme", objective="Ship"))
    conn.execute("BEGIN IMMEDIATE")
    for i in range(2000):
        conn.execute(
            "INSERT INTO audit_entries (id, project_id, entity_type, entity_id, action, actor, "
            "created_at) VALUES (?, ?, 'project', ?, 'create', 'local-user', '2026-01-01')",
            (f"row-{i}", project.id, project.id),
        )
    conn.commit()
    conn.execute("DELETE FROM audit_entries")
    conn.commit()
    conn.close()

    result = run_secure_delete_maintenance(sqlite_path)

    assert result.sqlite_path == sqlite_path
    assert result.duration_ms >= 0
    # VACUUM reclaims the space the bulk delete freed — the file must
    # not have grown, and in practice shrinks noticeably.
    assert result.size_after_bytes <= result.size_before_bytes


def test_run_secure_delete_maintenance_leaves_the_database_fully_usable(tmp_path, migrations_dir):
    sqlite_path = tmp_path / "app.db"
    conn = connect(sqlite_path)
    run_migrations(conn, migrations_dir)
    project = create_project(conn, ProjectCreateInput(name="Acme", objective="Ship"))
    conn.execute(
        "INSERT INTO audit_entries (id, project_id, entity_type, entity_id, action, actor, "
        "created_at) VALUES ('a1', ?, 'project', ?, 'create', 'local-user', '2026-01-01')",
        (project.id, project.id),
    )
    conn.commit()
    conn.close()

    run_secure_delete_maintenance(sqlite_path)

    conn = connect(sqlite_path)
    try:
        row = conn.execute("SELECT id FROM audit_entries WHERE id = 'a1'").fetchone()
        assert row["id"] == "a1"
    finally:
        conn.close()
