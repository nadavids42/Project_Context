"""Tests for the generic connector-source CRUD added to
`project_context.db.sources_repository` in Prompt 10: create, boundary/
credential/health/cursor updates, enable/disable, and project-scoped
listing/lookup by kind. Manual-source get-or-create is already covered
indirectly by the evidence-ingestion tests."""

from __future__ import annotations

import sqlite3

import pytest

from project_context.db import sources_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.sources import SourceHealthStatus, SourceKind
from project_context.services.projects import create_project


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def project_id(conn):
    return create_project(
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
    ).id


def test_insert_source_defaults_to_unconfigured_and_enabled(conn, project_id):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Project Drive folder"
    )
    assert source.health_status is SourceHealthStatus.UNCONFIGURED
    assert source.enabled is True
    assert source.credential_ref is None


def test_insert_source_rejects_duplicate_kind_and_display_name(conn, project_id):
    sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Project Drive folder"
    )
    with pytest.raises(sqlite3.IntegrityError):
        sources_repository.insert_source(
            conn, project_id, kind=SourceKind.DRIVE, display_name="Project Drive folder"
        )


def test_list_sources_for_project_filters_by_kind(conn, project_id):
    sources_repository.ensure_manual_source(conn, project_id)
    drive = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    only_drive = sources_repository.list_sources_for_project(
        conn, project_id, kind=SourceKind.DRIVE
    )
    assert [s.id for s in only_drive] == [drive.id]

    all_sources = sources_repository.list_sources_for_project(conn, project_id)
    assert len(all_sources) == 2


def test_get_source_by_kind_returns_the_oldest(conn, project_id):
    first = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="First"
    )
    sources_repository.insert_source(conn, project_id, kind=SourceKind.DRIVE, display_name="Second")
    found = sources_repository.get_source_by_kind(conn, project_id, SourceKind.DRIVE)
    assert found.id == first.id


def test_get_source_by_kind_returns_none_when_absent(conn, project_id):
    assert sources_repository.get_source_by_kind(conn, project_id, SourceKind.DRIVE) is None


def test_update_boundary_round_trips(conn, project_id):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    updated = sources_repository.update_boundary(
        conn, project_id, source.id, boundary_json='{"folder_id": "abc123"}'
    )
    assert updated.boundary_json == '{"folder_id": "abc123"}'


def test_update_credential_ref_round_trips_and_clears(conn, project_id):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    with_ref = sources_repository.update_credential_ref(
        conn, project_id, source.id, credential_ref="keyring:abc"
    )
    assert with_ref.credential_ref == "keyring:abc"

    cleared = sources_repository.update_credential_ref(
        conn, project_id, source.id, credential_ref=None
    )
    assert cleared.credential_ref is None


def test_update_external_account_id(conn, project_id):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    updated = sources_repository.update_external_account_id(
        conn, project_id, source.id, external_account_id="user@example.com"
    )
    assert updated.external_account_id == "user@example.com"


def test_update_health_sets_status_and_error_code(conn, project_id):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    updated = sources_repository.update_health(
        conn, project_id, source.id,
        health_status=SourceHealthStatus.REAUTH_REQUIRED, last_error_code="auth",
    )
    assert updated.health_status is SourceHealthStatus.REAUTH_REQUIRED
    assert updated.last_error_code == "auth"


def test_update_last_success_and_last_cursor(conn, project_id):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    with_success = sources_repository.update_last_success(
        conn, project_id, source.id, last_success_at="2026-08-01T00:00:00Z"
    )
    assert with_success.last_success_at == "2026-08-01T00:00:00Z"

    with_cursor = sources_repository.update_last_cursor(
        conn, project_id, source.id, last_cursor='{"folder_queue": []}'
    )
    assert with_cursor.last_cursor == '{"folder_queue": []}'


def test_set_enabled_toggles(conn, project_id):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    disabled = sources_repository.set_enabled(conn, project_id, source.id, enabled=False)
    assert disabled.enabled is False
    reenabled = sources_repository.set_enabled(conn, project_id, source.id, enabled=True)
    assert reenabled.enabled is True


def test_source_updates_are_project_scoped(conn, project_id):
    other_id = create_project(conn, ProjectCreateInput(name="Other", objective="Other work")).id
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    # The WHERE clause matches no row for a foreign project_id, so the
    # update is a no-op and the subsequent re-fetch (asserted internally
    # to have found the just-updated row) fails loudly rather than
    # silently succeeding against the wrong project's row.
    with pytest.raises(AssertionError):
        sources_repository.update_health(
            conn, other_id, source.id, health_status=SourceHealthStatus.DISABLED
        )
    unchanged = sources_repository.get_source(conn, project_id, source.id)
    assert unchanged.health_status is SourceHealthStatus.UNCONFIGURED
