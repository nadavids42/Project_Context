"""Tests for the project lifecycle service (FR-001): create/edit/archive/
list behavior, validation, audit-entry creation, archived-by-default
exclusion, immutable IDs, and cross-project isolation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from project_context.db import audit_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.audit import AuditAction
from project_context.domain.projects import ProjectCreateInput, ProjectStatus, ProjectUpdateInput
from project_context.services.projects import (
    ProjectNotFoundError,
    ProjectStateError,
    archive_project,
    create_project,
    edit_project,
    get_project,
    list_archived_projects,
    list_projects,
    restore_project,
)


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


def _create_input(**overrides):
    fields = {"name": "Acme Rollout", "objective": "Ship the pilot", **overrides}
    return ProjectCreateInput(**fields)


# --- create -----------------------------------------------------------


def test_create_project_returns_the_created_project(conn):
    project = create_project(conn, _create_input(client_name="Acme Corp", stage="Discovery"))

    assert project.name == "Acme Rollout"
    assert project.objective == "Ship the pilot"
    assert project.client_name == "Acme Corp"
    assert project.stage == "Discovery"
    assert project.status is ProjectStatus.ACTIVE
    assert project.archived_at is None


def test_create_project_is_retrievable_afterwards(conn):
    created = create_project(conn, _create_input())

    fetched = get_project(conn, created.id)

    assert fetched == created


def test_create_project_writes_a_create_audit_entry(conn):
    project = create_project(conn, _create_input())

    entries = audit_repository.list_for_project(conn, project.id)

    assert len(entries) == 1
    assert entries[0].action is AuditAction.CREATE
    assert entries[0].project_id == project.id
    assert entries[0].entity_type == "project"
    assert entries[0].entity_id == project.id
    assert entries[0].before is None
    assert entries[0].after["name"] == "Acme Rollout"


# --- get / not found ----------------------------------------------------


def test_get_project_raises_for_unknown_id(conn):
    with pytest.raises(ProjectNotFoundError):
        get_project(conn, "does-not-exist")


# --- validation ---------------------------------------------------------


def test_create_project_rejects_blank_name(conn):
    with pytest.raises(ValidationError):
        _create_input(name="   ")


def test_create_project_rejects_invalid_status(conn):
    with pytest.raises(ValidationError):
        ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot", status="bogus")


# --- list / archive default exclusion -----------------------------------


def test_list_projects_excludes_archived_by_default(conn):
    active = create_project(conn, _create_input(name="Active Project"))
    archived_source = create_project(conn, _create_input(name="Archived Project"))
    archive_project(conn, archived_source.id)

    listed = list_projects(conn)

    assert [p.id for p in listed] == [active.id]


def test_list_projects_include_archived_returns_everything(conn):
    active = create_project(conn, _create_input(name="Active Project"))
    archived_source = create_project(conn, _create_input(name="Archived Project"))
    archive_project(conn, archived_source.id)

    listed = list_projects(conn, include_archived=True)

    assert {p.id for p in listed} == {active.id, archived_source.id}


def test_list_archived_projects_returns_only_archived(conn):
    active = create_project(conn, _create_input(name="Active Project"))
    archived_source = create_project(conn, _create_input(name="Archived Project"))
    archive_project(conn, archived_source.id)

    listed = list_archived_projects(conn)

    assert [p.id for p in listed] == [archived_source.id]
    assert active.id not in [p.id for p in listed]


# --- edit -----------------------------------------------------------------


def test_edit_project_updates_fields(conn):
    project = create_project(conn, _create_input())

    updated = edit_project(
        conn,
        project.id,
        ProjectUpdateInput(
            name="Acme Rollout — Phase 2",
            objective="Ship phase 2",
            description="New description",
            stage="Design",
            client_name="Acme Corp",
            status=ProjectStatus.ON_HOLD,
        ),
    )

    assert updated.name == "Acme Rollout — Phase 2"
    assert updated.status is ProjectStatus.ON_HOLD
    assert updated.updated_at >= project.updated_at


def test_edit_project_writes_an_update_audit_entry_with_before_and_after(conn):
    project = create_project(conn, _create_input())

    edit_project(conn, project.id, ProjectUpdateInput(name="Renamed", objective="Ship the pilot"))

    entries = audit_repository.list_for_project(conn, project.id)
    update_entries = [e for e in entries if e.action is AuditAction.UPDATE]
    assert len(update_entries) == 1
    assert update_entries[0].before["name"] == "Acme Rollout"
    assert update_entries[0].after["name"] == "Renamed"


def test_edit_project_raises_for_unknown_id(conn):
    with pytest.raises(ProjectNotFoundError):
        edit_project(conn, "does-not-exist", ProjectUpdateInput(name="X", objective="Y"))


def test_edit_project_rejects_editing_an_archived_project(conn):
    project = create_project(conn, _create_input())
    archive_project(conn, project.id)

    with pytest.raises(ProjectStateError):
        edit_project(
            conn, project.id, ProjectUpdateInput(name="Renamed", objective="Ship the pilot")
        )


def test_edit_project_never_changes_id(conn):
    project = create_project(conn, _create_input())

    updated = edit_project(
        conn, project.id, ProjectUpdateInput(name="Renamed", objective="Ship the pilot")
    )

    assert updated.id == project.id


# --- archive ----------------------------------------------------------------


def test_archive_project_hides_it_from_default_list_and_sets_archived_at(conn):
    project = create_project(conn, _create_input())

    archived = archive_project(conn, project.id)

    assert archived.status is ProjectStatus.ARCHIVED
    assert archived.archived_at is not None
    assert project.id not in [p.id for p in list_projects(conn)]


def test_archive_project_writes_an_archive_audit_entry(conn):
    project = create_project(conn, _create_input())

    archive_project(conn, project.id)

    entries = audit_repository.list_for_project(conn, project.id)
    archive_entries = [e for e in entries if e.action is AuditAction.ARCHIVE]
    assert len(archive_entries) == 1
    assert archive_entries[0].before["status"] == "active"
    assert archive_entries[0].after["status"] == "archived"


def test_archive_project_raises_for_unknown_id(conn):
    with pytest.raises(ProjectNotFoundError):
        archive_project(conn, "does-not-exist")


def test_archive_project_rejects_double_archive(conn):
    project = create_project(conn, _create_input())
    archive_project(conn, project.id)

    with pytest.raises(ProjectStateError):
        archive_project(conn, project.id)


def test_archived_project_remains_explicitly_retrievable(conn):
    project = create_project(conn, _create_input())
    archive_project(conn, project.id)

    fetched = get_project(conn, project.id)

    assert fetched.status is ProjectStatus.ARCHIVED


def test_archive_project_never_changes_id(conn):
    project = create_project(conn, _create_input())

    archived = archive_project(conn, project.id)

    assert archived.id == project.id


# --- restore ------------------------------------------------------------


def test_restore_project_reactivates_and_clears_archived_at(conn):
    project = create_project(conn, _create_input())
    archive_project(conn, project.id)

    restored = restore_project(conn, project.id)

    assert restored.status is ProjectStatus.ACTIVE
    assert restored.archived_at is None
    assert restored.id in [p.id for p in list_projects(conn)]


def test_restore_project_writes_a_restore_audit_entry(conn):
    project = create_project(conn, _create_input())
    archive_project(conn, project.id)

    restore_project(conn, project.id)

    entries = audit_repository.list_for_project(conn, project.id)
    restore_entries = [e for e in entries if e.action is AuditAction.RESTORE]
    assert len(restore_entries) == 1
    assert restore_entries[0].before["status"] == "archived"
    assert restore_entries[0].after["status"] == "active"


def test_restore_project_raises_for_unknown_id(conn):
    with pytest.raises(ProjectNotFoundError):
        restore_project(conn, "does-not-exist")


def test_restore_project_rejects_restoring_a_non_archived_project(conn):
    project = create_project(conn, _create_input())

    with pytest.raises(ProjectStateError):
        restore_project(conn, project.id)


def test_full_audit_trail_reflects_lifecycle_in_order(conn):
    project = create_project(conn, _create_input())
    edit_project(conn, project.id, ProjectUpdateInput(name="Renamed", objective="Ship the pilot"))
    archive_project(conn, project.id)
    restore_project(conn, project.id)

    entries = audit_repository.list_for_project(conn, project.id)

    assert [e.action for e in entries] == [
        AuditAction.CREATE,
        AuditAction.UPDATE,
        AuditAction.ARCHIVE,
        AuditAction.RESTORE,
    ]


# --- project isolation ---------------------------------------------------


def test_similar_names_stay_isolated_across_operations(conn):
    """Two projects with near-identical names must never be confused by
    any lifecycle operation."""
    first = create_project(conn, _create_input(name="Acme Rollout", client_name="Acme Corp"))
    second = create_project(
        conn, _create_input(name="Acme Rollout - Phase 2", client_name="Acme Corp")
    )

    assert first.id != second.id

    edit_project(
        conn,
        first.id,
        ProjectUpdateInput(name="Acme Rollout", objective="Updated objective for first"),
    )

    # Editing `first` must not have touched `second`.
    untouched_second = get_project(conn, second.id)
    assert untouched_second.objective == "Ship the pilot"
    assert untouched_second.name == "Acme Rollout - Phase 2"

    archive_project(conn, first.id)

    # Archiving `first` must not archive `second`.
    still_active_second = get_project(conn, second.id)
    assert still_active_second.status is ProjectStatus.ACTIVE
    assert [p.id for p in list_projects(conn)] == [second.id]
    assert [p.id for p in list_archived_projects(conn)] == [first.id]

    # Each project's audit trail is its own.
    first_entries = audit_repository.list_for_project(conn, first.id)
    second_entries = audit_repository.list_for_project(conn, second.id)
    assert [e.action for e in first_entries] == [
        AuditAction.CREATE,
        AuditAction.UPDATE,
        AuditAction.ARCHIVE,
    ]
    assert [e.action for e in second_entries] == [AuditAction.CREATE]
    assert all(e.project_id == first.id for e in first_entries)
    assert all(e.project_id == second.id for e in second_entries)
