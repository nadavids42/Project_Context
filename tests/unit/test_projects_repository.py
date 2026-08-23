"""Tests for the projects repository: direct CRUD against `projects`,
with no business-rule validation or audit-entry writing (that belongs to
the service layer — see test_projects_service.py)."""

from __future__ import annotations

import pytest

from project_context.db import projects_repository
from project_context.db.connection import connect
from project_context.domain.projects import ProjectCreateInput, ProjectStatus, ProjectUpdateInput


@pytest.fixture
def conn(tmp_path, migrations_dir):
    from project_context.db.migrations import run_migrations

    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


def _create_input(**overrides):
    fields = {"name": "Acme Rollout", "objective": "Ship the pilot", **overrides}
    return ProjectCreateInput(**fields)


def test_insert_and_get_round_trip(conn):
    project = projects_repository.insert_project(conn, _create_input(client_name="Acme Corp"))

    fetched = projects_repository.get_project(conn, project.id)

    assert fetched == project
    assert fetched.name == "Acme Rollout"
    assert fetched.objective == "Ship the pilot"
    assert fetched.client_name == "Acme Corp"
    assert fetched.status is ProjectStatus.ACTIVE
    assert fetched.archived_at is None
    assert fetched.created_at == fetched.updated_at


def test_get_missing_project_returns_none(conn):
    assert projects_repository.get_project(conn, "does-not-exist") is None


def test_insert_generates_a_unique_id_each_time(conn):
    first = projects_repository.insert_project(conn, _create_input())
    second = projects_repository.insert_project(conn, _create_input())

    assert first.id != second.id


def test_list_projects_filters_by_requested_statuses(conn):
    active = projects_repository.insert_project(conn, _create_input(name="Active One"))
    on_hold = projects_repository.insert_project(
        conn, _create_input(name="On Hold One", status=ProjectStatus.ON_HOLD)
    )
    projects_repository.set_archived(conn, active.id, archived=False)  # no-op, still active

    only_active = projects_repository.list_projects(conn, statuses=(ProjectStatus.ACTIVE,))
    only_on_hold = projects_repository.list_projects(conn, statuses=(ProjectStatus.ON_HOLD,))
    both = projects_repository.list_projects(
        conn, statuses=(ProjectStatus.ACTIVE, ProjectStatus.ON_HOLD)
    )

    assert [p.id for p in only_active] == [active.id]
    assert [p.id for p in only_on_hold] == [on_hold.id]
    assert {p.id for p in both} == {active.id, on_hold.id}


def test_list_projects_with_no_statuses_returns_empty(conn):
    projects_repository.insert_project(conn, _create_input())

    assert projects_repository.list_projects(conn, statuses=()) == []


def test_list_projects_orders_most_recently_updated_first(conn):
    first = projects_repository.insert_project(conn, _create_input(name="First"))
    second = projects_repository.insert_project(conn, _create_input(name="Second"))

    # Touch `first` so it becomes the most recently updated.
    projects_repository.update_project_fields(
        conn, first.id, ProjectUpdateInput(name="First", objective="Ship the pilot")
    )

    ordered = projects_repository.list_projects(conn, statuses=(ProjectStatus.ACTIVE,))

    assert [p.id for p in ordered] == [first.id, second.id]


def test_update_project_fields_overwrites_editable_fields(conn):
    project = projects_repository.insert_project(conn, _create_input())

    updated = projects_repository.update_project_fields(
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
    assert updated.objective == "Ship phase 2"
    assert updated.description == "New description"
    assert updated.stage == "Design"
    assert updated.client_name == "Acme Corp"
    assert updated.status is ProjectStatus.ON_HOLD


def test_update_project_fields_never_changes_id_or_created_at(conn):
    project = projects_repository.insert_project(conn, _create_input())

    updated = projects_repository.update_project_fields(
        conn, project.id, ProjectUpdateInput(name="Renamed", objective="Ship the pilot")
    )

    assert updated.id == project.id
    assert updated.created_at == project.created_at


def test_update_project_fields_advances_updated_at(conn):
    project = projects_repository.insert_project(conn, _create_input())

    updated = projects_repository.update_project_fields(
        conn, project.id, ProjectUpdateInput(name="Renamed", objective="Ship the pilot")
    )

    assert updated.updated_at >= project.updated_at


def test_update_project_fields_on_missing_project_returns_none(conn):
    result = projects_repository.update_project_fields(
        conn, "does-not-exist", ProjectUpdateInput(name="Renamed", objective="Ship the pilot")
    )

    assert result is None


def test_set_archived_true_stamps_status_and_archived_at(conn):
    project = projects_repository.insert_project(conn, _create_input())

    archived = projects_repository.set_archived(conn, project.id, archived=True)

    assert archived.status is ProjectStatus.ARCHIVED
    assert archived.archived_at is not None
    assert archived.id == project.id


def test_set_archived_false_clears_status_and_archived_at(conn):
    project = projects_repository.insert_project(conn, _create_input())
    projects_repository.set_archived(conn, project.id, archived=True)

    restored = projects_repository.set_archived(conn, project.id, archived=False)

    assert restored.status is ProjectStatus.ACTIVE
    assert restored.archived_at is None


def test_insert_project_rejects_out_of_band_id_field():
    """Guard: ProjectCreateInput has no `id` field at all — a caller
    cannot influence the generated id even if it tried."""
    assert "id" not in ProjectCreateInput.model_fields
