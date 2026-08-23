"""Tests for the audit repository directly (below the service layer)."""

from __future__ import annotations

import pytest

from project_context.db import audit_repository, projects_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.audit import AuditAction
from project_context.domain.projects import ProjectCreateInput


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def project_id(conn):
    project = projects_repository.insert_project(
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
    )
    return project.id


def test_record_entry_round_trips_before_and_after(conn, project_id):
    entry = audit_repository.record_entry(
        conn,
        project_id=project_id,
        entity_type="project",
        entity_id=project_id,
        action=AuditAction.UPDATE,
        before={"name": "Old"},
        after={"name": "New"},
        actor="local-user",
    )

    assert entry.before == {"name": "Old"}
    assert entry.after == {"name": "New"}
    assert entry.action is AuditAction.UPDATE
    assert entry.actor == "local-user"


def test_record_entry_accepts_none_before(conn, project_id):
    entry = audit_repository.record_entry(
        conn,
        project_id=project_id,
        entity_type="project",
        entity_id=project_id,
        action=AuditAction.CREATE,
        before=None,
        after={"name": "New"},
        actor="local-user",
    )

    assert entry.before is None


def test_list_for_project_orders_oldest_first(conn, project_id):
    audit_repository.record_entry(
        conn,
        project_id=project_id,
        entity_type="project",
        entity_id=project_id,
        action=AuditAction.CREATE,
        before=None,
        after={"n": 1},
        actor="local-user",
    )
    audit_repository.record_entry(
        conn,
        project_id=project_id,
        entity_type="project",
        entity_id=project_id,
        action=AuditAction.UPDATE,
        before={"n": 1},
        after={"n": 2},
        actor="local-user",
    )

    entries = audit_repository.list_for_project(conn, project_id)

    assert [e.action for e in entries] == [AuditAction.CREATE, AuditAction.UPDATE]


def test_list_for_project_is_scoped_to_the_given_project(conn):
    project_a = projects_repository.insert_project(
        conn, ProjectCreateInput(name="Project A", objective="Objective A")
    )
    project_b = projects_repository.insert_project(
        conn, ProjectCreateInput(name="Project B", objective="Objective B")
    )
    audit_repository.record_entry(
        conn,
        project_id=project_a.id,
        entity_type="project",
        entity_id=project_a.id,
        action=AuditAction.CREATE,
        before=None,
        after={"name": "Project A"},
        actor="local-user",
    )
    audit_repository.record_entry(
        conn,
        project_id=project_b.id,
        entity_type="project",
        entity_id=project_b.id,
        action=AuditAction.CREATE,
        before=None,
        after={"name": "Project B"},
        actor="local-user",
    )

    entries_a = audit_repository.list_for_project(conn, project_a.id)

    assert len(entries_a) == 1
    assert entries_a[0].project_id == project_a.id


def test_entity_type_check_constraint_rejects_unknown_entity_type(conn, project_id):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        audit_repository.record_entry(
            conn,
            project_id=project_id,
            entity_type="not_a_real_entity",
            entity_id=project_id,
            action=AuditAction.CREATE,
            before=None,
            after={},
            actor="local-user",
        )


def test_action_check_constraint_rejects_unknown_action(conn, project_id):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        audit_repository.record_entry(
            conn,
            project_id=project_id,
            entity_type="project",
            entity_id=project_id,
            action="not_a_real_action",
            before=None,
            after={},
            actor="local-user",
        )
