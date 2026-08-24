"""Tests for `project_context.backup` (Section 17: "daily timestamped
local backup to an encrypted location") — create/verify/restore, all
against `tmp_path`-only temporary directories (Section 16: "tests use
temporary directories"; never the real configured data directory).

Includes the required restore smoke test: back up a populated database,
restore it into a fresh temporary target, and confirm the restored
database and evidence are actually usable/intact — not merely that the
files exist.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from project_context.backup import (
    BackupError,
    create_backup,
    read_manifest,
    restore_backup,
    verify_backup,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import EvidenceSourceType, ManualTextInput
from project_context.domain.projects import ProjectCreateInput
from project_context.services import evidence as evidence_service
from project_context.services.projects import create_project, list_projects


@pytest.fixture
def source_data_dir(tmp_path, migrations_dir):
    data_dir = tmp_path / "source-data"
    sqlite_path = data_dir / "project_context.db"
    evidence_dir = data_dir / "evidence"
    evidence_dir.mkdir(parents=True)

    conn = connect(sqlite_path)
    run_migrations(conn, migrations_dir)
    project = create_project(conn, ProjectCreateInput(name="Acme Rollout", objective="Ship it"))
    evidence_service.submit_manual_text(
        conn,
        project.id,
        ManualTextInput(
            title="Kickoff notes",
            text="Backed-up evidence text.",
            source_type=EvidenceSourceType.MEETING_NOTES,
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    conn.close()
    return data_dir, sqlite_path, evidence_dir, project.id


def test_create_backup_produces_a_verifiable_manifest(tmp_path, source_data_dir):
    _data_dir, sqlite_path, evidence_dir, _project_id = source_data_dir
    dest = tmp_path / "backups"

    backup_dir = create_backup(sqlite_path=sqlite_path, evidence_dir=evidence_dir, dest=dest)

    assert backup_dir.parent == dest
    assert (backup_dir / "project_context.db").exists()
    manifest = read_manifest(backup_dir)
    assert manifest.content_object_sha256s
    assert manifest.content_object_total_bytes > 0
    assert verify_backup(backup_dir) is True


def test_create_backup_rejects_destination_inside_source_data_dir(tmp_path, source_data_dir):
    _data_dir, sqlite_path, evidence_dir, _project_id = source_data_dir

    with pytest.raises(BackupError):
        create_backup(sqlite_path=sqlite_path, evidence_dir=evidence_dir, dest=sqlite_path.parent)


def test_create_backup_raises_for_missing_database(tmp_path):
    with pytest.raises(BackupError):
        create_backup(
            sqlite_path=tmp_path / "nope" / "project_context.db",
            evidence_dir=tmp_path / "nope-evidence",
            dest=tmp_path / "backups",
        )


def test_two_backups_land_in_distinct_timestamped_directories(tmp_path, source_data_dir):
    _data_dir, sqlite_path, evidence_dir, _project_id = source_data_dir
    dest = tmp_path / "backups"

    first = create_backup(sqlite_path=sqlite_path, evidence_dir=evidence_dir, dest=dest)
    second = create_backup(sqlite_path=sqlite_path, evidence_dir=evidence_dir, dest=dest)

    assert first != second
    assert first.exists() and second.exists()


# ---------------------------------------------------------------------------
# Restore smoke test (Section 16: "Add a restore smoke-test path against
# temporary data").
# ---------------------------------------------------------------------------


def test_restore_smoke_test_round_trips_a_usable_database(tmp_path, source_data_dir):
    _data_dir, sqlite_path, evidence_dir, project_id = source_data_dir
    dest = tmp_path / "backups"
    backup_dir = create_backup(sqlite_path=sqlite_path, evidence_dir=evidence_dir, dest=dest)

    restore_target = tmp_path / "restored-data"
    restore_backup(
        backup_dir=backup_dir,
        target_sqlite_path=restore_target / "project_context.db",
        target_evidence_dir=restore_target / "evidence",
    )

    # The restored database is a real, queryable SQLite file with the
    # same application data — not just a byte-identical blob sitting
    # unopened on disk.
    restored_conn = connect(restore_target / "project_context.db")
    try:
        projects = list_projects(restored_conn)
        assert [p.id for p in projects] == [project_id]
        assert projects[0].name == "Acme Rollout"

        from project_context.db import evidence_repository

        artifacts = evidence_repository.list_artifacts(restored_conn, project_id)
        assert len(artifacts) == 1
        content = evidence_repository.get_content(
            restored_conn, project_id, artifacts[0].current_content_id
        )
        assert content.normalized_text == "Backed-up evidence text."

        # And the referenced evidence *bytes* were actually restored,
        # not just database rows pointing at nothing.
        from project_context import evidence_store

        assert evidence_store.content_exists(restore_target / "evidence", content.sha256)
        assert (
            evidence_store.read_bytes(restore_target / "evidence", content.sha256)
            == b"Backed-up evidence text."
        )
    finally:
        restored_conn.close()


def test_restore_refuses_to_overwrite_an_existing_database_without_force(tmp_path, source_data_dir):
    _data_dir, sqlite_path, evidence_dir, _project_id = source_data_dir
    dest = tmp_path / "backups"
    backup_dir = create_backup(sqlite_path=sqlite_path, evidence_dir=evidence_dir, dest=dest)

    restore_target = tmp_path / "already-has-a-database"
    restore_target.mkdir()
    (restore_target / "project_context.db").write_bytes(b"pretend this is a live database")

    with pytest.raises(BackupError):
        restore_backup(
            backup_dir=backup_dir,
            target_sqlite_path=restore_target / "project_context.db",
            target_evidence_dir=restore_target / "evidence",
        )
    # Untouched — the refusal happened before any file was overwritten.
    assert (restore_target / "project_context.db").read_bytes() == (
        b"pretend this is a live database"
    )


def test_restore_with_force_overwrites_an_existing_database(tmp_path, source_data_dir):
    _data_dir, sqlite_path, evidence_dir, project_id = source_data_dir
    dest = tmp_path / "backups"
    backup_dir = create_backup(sqlite_path=sqlite_path, evidence_dir=evidence_dir, dest=dest)

    restore_target = tmp_path / "already-has-a-database"
    restore_target.mkdir()
    (restore_target / "project_context.db").write_bytes(b"pretend this is a live database")

    restore_backup(
        backup_dir=backup_dir,
        target_sqlite_path=restore_target / "project_context.db",
        target_evidence_dir=restore_target / "evidence",
        force=True,
    )

    restored_conn = connect(restore_target / "project_context.db")
    try:
        assert [p.id for p in list_projects(restored_conn)] == [project_id]
    finally:
        restored_conn.close()


def test_verify_backup_detects_tampering(tmp_path, source_data_dir):
    _data_dir, sqlite_path, evidence_dir, _project_id = source_data_dir
    dest = tmp_path / "backups"
    backup_dir = create_backup(sqlite_path=sqlite_path, evidence_dir=evidence_dir, dest=dest)
    assert verify_backup(backup_dir) is True

    (backup_dir / "project_context.db").write_bytes(b"corrupted")

    assert verify_backup(backup_dir) is False


def test_read_manifest_raises_for_a_directory_that_is_not_a_backup(tmp_path):
    not_a_backup = tmp_path / "random-dir"
    not_a_backup.mkdir()
    with pytest.raises(BackupError):
        read_manifest(not_a_backup)
