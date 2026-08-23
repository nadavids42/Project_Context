"""Tests for the database health check used by the Streamlit panel."""

from __future__ import annotations

from project_context.db.connection import connect
from project_context.db.health import check_database_health
from project_context.db.migrations import run_migrations


def test_health_reports_pending_before_migrations_run(tmp_path, migrations_dir):
    sqlite_path = tmp_path / "app.db"
    conn = connect(sqlite_path)
    conn.close()

    health = check_database_health(sqlite_path, migrations_dir)

    assert health.error is None
    assert health.ok is False
    assert health.pending_migration_count > 0
    assert health.applied_migration_count == 0
    assert health.foreign_keys_enabled is True


def test_health_reports_ok_after_migrations_run(tmp_path, migrations_dir):
    sqlite_path = tmp_path / "app.db"
    conn = connect(sqlite_path)
    run_migrations(conn, migrations_dir)
    conn.close()

    health = check_database_health(sqlite_path, migrations_dir)

    assert health.error is None
    assert health.ok is True
    assert health.pending_migration_count == 0
    assert health.applied_migration_count > 0
    assert health.journal_mode.lower() == "wal"


def test_health_handles_missing_migrations_directory_gracefully(tmp_path):
    sqlite_path = tmp_path / "app.db"
    missing_dir = tmp_path / "no-such-migrations-dir"

    health = check_database_health(sqlite_path, missing_dir)

    assert health.error is None
    assert health.pending_migration_count == 0
