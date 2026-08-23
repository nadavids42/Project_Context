"""Tests for the numbered SQL migration runner."""

from __future__ import annotations

import sqlite3

import pytest

from project_context.db.connection import connect
from project_context.db.migrations import (
    MigrationError,
    applied_versions,
    apply_migration,
    discover_migrations,
    run_migrations,
)


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "app.db")
    yield connection
    connection.close()


def _write_migration(directory, filename: str, sql: str):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(sql, encoding="utf-8")


def test_run_migrations_on_empty_database_applies_all_real_migrations(conn, migrations_dir):
    applied = run_migrations(conn, migrations_dir)

    assert [m.version for m in applied] == [m.version for m in discover_migrations(migrations_dir)]
    for table in (
        "projects",
        "sources",
        "source_artifacts",
        "source_contents",
        "source_chunks",
        "sync_runs",
        "sync_items",
    ):
        conn.execute(f"SELECT * FROM {table} LIMIT 0")  # raises if missing


def test_second_run_against_up_to_date_database_is_a_no_op(conn, migrations_dir):
    first = run_migrations(conn, migrations_dir)
    assert len(first) > 0

    second = run_migrations(conn, migrations_dir)

    assert second == []


def test_repeated_runs_do_not_duplicate_migration_records(conn, migrations_dir):
    run_migrations(conn, migrations_dir)
    run_migrations(conn, migrations_dir)

    versions = [
        row["version"]
        for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    ]
    assert versions == sorted(set(versions))


def test_applied_versions_reflects_what_was_recorded(conn, migrations_dir):
    assert applied_versions(conn) == set()
    run_migrations(conn, migrations_dir)
    assert applied_versions(conn) == {m.version for m in discover_migrations(migrations_dir)}


def test_failed_migration_is_not_recorded_as_applied(conn, tmp_path):
    bad_dir = tmp_path / "bad_migrations"
    _write_migration(bad_dir, "0001_broken.sql", "CREATE TABLE t (id TEXT);\nTHIS IS NOT SQL;")

    with pytest.raises(MigrationError):
        run_migrations(conn, bad_dir)

    assert applied_versions(conn) == set()
    # The valid statement before the broken one must also have been
    # rolled back, since the whole migration is one transaction.
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT * FROM t")


def test_failed_migration_is_retried_on_next_run_not_silently_skipped(conn, tmp_path):
    migrations_dir_local = tmp_path / "migrations"
    _write_migration(
        migrations_dir_local, "0001_broken.sql", "CREATE TABLE t (id TEXT);\nTHIS IS NOT SQL;"
    )

    with pytest.raises(MigrationError):
        run_migrations(conn, migrations_dir_local)

    # Fix the file and confirm the runner still attempts (and this time
    # succeeds at) version 1 — it was never marked applied.
    _write_migration(migrations_dir_local, "0001_broken.sql", "CREATE TABLE t (id TEXT);")
    applied = run_migrations(conn, migrations_dir_local)

    assert [m.version for m in applied] == [1]
    assert applied_versions(conn) == {1}


def test_partial_progress_across_calls_is_preserved(conn, tmp_path):
    migrations_dir_local = tmp_path / "migrations"
    _write_migration(migrations_dir_local, "0001_first.sql", "CREATE TABLE first (id TEXT);")
    _write_migration(migrations_dir_local, "0002_second.sql", "CREATE TABLE second (id TEXT);")

    run_migrations(conn, migrations_dir_local)

    # A third, broken migration must not affect the two already applied.
    _write_migration(migrations_dir_local, "0003_broken.sql", "NOT VALID SQL AT ALL;")
    with pytest.raises(MigrationError):
        run_migrations(conn, migrations_dir_local)

    assert applied_versions(conn) == {1, 2}
    conn.execute("SELECT * FROM first LIMIT 0")
    conn.execute("SELECT * FROM second LIMIT 0")


def test_discover_migrations_orders_by_version_not_filename_string(tmp_path):
    _write_migration(tmp_path, "0002_second.sql", "SELECT 1;")
    _write_migration(tmp_path, "0010_tenth.sql", "SELECT 1;")
    _write_migration(tmp_path, "0001_first.sql", "SELECT 1;")

    migrations = discover_migrations(tmp_path)

    assert [m.version for m in migrations] == [1, 2, 10]


def test_discover_migrations_rejects_malformed_filenames(tmp_path):
    _write_migration(tmp_path, "not_numbered.sql", "SELECT 1;")

    with pytest.raises(MigrationError):
        discover_migrations(tmp_path)


def test_discover_migrations_rejects_duplicate_versions(tmp_path):
    _write_migration(tmp_path, "0001_first.sql", "SELECT 1;")
    _write_migration(tmp_path, "0001_first_again.sql", "SELECT 1;")

    with pytest.raises(MigrationError):
        discover_migrations(tmp_path)


def test_apply_migration_rejects_empty_migration_file(conn, tmp_path):
    _write_migration(tmp_path, "0001_empty.sql", "-- just a comment, no statements\n")
    (migration,) = discover_migrations(tmp_path)

    with pytest.raises(MigrationError):
        apply_migration(conn, migration)
