"""Project lifecycle service — the only supported way to create, read,
list, edit, archive, or restore a project.

Owns transaction boundaries and audit-entry creation so that
`project_context.db.projects_repository` can stay a thin data-access
layer and UI code never executes ad hoc SQL (Section 8: "Application
services... Transactions, authorization boundary, orchestration, domain
rules"). See FR-001 for the acceptance criteria this module implements:
required-field validation, an audit entry on every edit/archive/restore,
archived projects excluded from the active default but retrievable, and
immutable project IDs.
"""

from __future__ import annotations

import sqlite3

from project_context.db import audit_repository, projects_repository
from project_context.db.connection import transaction
from project_context.domain.audit import AuditAction
from project_context.domain.projects import (
    Project,
    ProjectCreateInput,
    ProjectStatus,
    ProjectUpdateInput,
)

#: Actor recorded on audit entries until the application has a real
#: notion of "who". The product is single-user (Section 1); this is a
#: fixed placeholder, not a stand-in for multi-user auditing.
DEFAULT_ACTOR = "local-user"

_ENTITY_TYPE = "project"


class ProjectNotFoundError(LookupError):
    """Raised when a `project_id` does not identify an existing project."""


class ProjectStateError(ValueError):
    """Raised when a lifecycle action is invalid for the project's current
    state (e.g. editing an archived project, archiving twice, restoring a
    project that isn't archived)."""


def create_project(
    conn: sqlite3.Connection, data: ProjectCreateInput, *, actor: str = DEFAULT_ACTOR
) -> Project:
    """Create a project and record its creation audit entry."""
    with transaction(conn):
        project = projects_repository.insert_project(conn, data)
        audit_repository.record_entry(
            conn,
            project_id=project.id,
            entity_type=_ENTITY_TYPE,
            entity_id=project.id,
            action=AuditAction.CREATE,
            before=None,
            after=project.model_dump(mode="json"),
            actor=actor,
        )
    return project


def get_project(conn: sqlite3.Connection, project_id: str) -> Project:
    """Fetch one project. Raises `ProjectNotFoundError` if it does not
    exist. Requires `project_id` explicitly, per Section 9."""
    project = projects_repository.get_project(conn, project_id)
    if project is None:
        raise ProjectNotFoundError(project_id)
    return project


def list_projects(conn: sqlite3.Connection, *, include_archived: bool = False) -> list[Project]:
    """List projects, most recently updated first.

    By default only non-archived projects are returned — FR-001:
    "archived projects disappear from the active default". Pass
    `include_archived=True` to retrieve every project explicitly.
    """
    if include_archived:
        statuses = tuple(ProjectStatus)
    else:
        statuses = tuple(s for s in ProjectStatus if s is not ProjectStatus.ARCHIVED)
    return projects_repository.list_projects(conn, statuses=statuses)


def list_archived_projects(conn: sqlite3.Connection) -> list[Project]:
    """List only archived projects — the explicit retrieval path FR-001
    requires ("...and remain retrievable")."""
    return projects_repository.list_projects(conn, statuses=(ProjectStatus.ARCHIVED,))


def edit_project(
    conn: sqlite3.Connection,
    project_id: str,
    data: ProjectUpdateInput,
    *,
    actor: str = DEFAULT_ACTOR,
) -> Project:
    """Edit project metadata and record an audit entry.

    Raises `ProjectNotFoundError` if the project does not exist, and
    `ProjectStateError` if it is archived (restore it first — archived
    projects are frozen, not silently editable back to life).
    """
    with transaction(conn):
        before = projects_repository.get_project(conn, project_id)
        if before is None:
            raise ProjectNotFoundError(project_id)
        if before.status is ProjectStatus.ARCHIVED:
            raise ProjectStateError(f"project {project_id} is archived; restore it before editing")

        after = projects_repository.update_project_fields(conn, project_id, data)
        assert after is not None  # existence just confirmed, same transaction
        audit_repository.record_entry(
            conn,
            project_id=project_id,
            entity_type=_ENTITY_TYPE,
            entity_id=project_id,
            action=AuditAction.UPDATE,
            before=before.model_dump(mode="json"),
            after=after.model_dump(mode="json"),
            actor=actor,
        )
    return after


def archive_project(
    conn: sqlite3.Connection, project_id: str, *, actor: str = DEFAULT_ACTOR
) -> Project:
    """Archive a project (non-destructive: evidence and history are kept).

    Raises `ProjectNotFoundError` if the project does not exist, and
    `ProjectStateError` if it is already archived.
    """
    with transaction(conn):
        before = projects_repository.get_project(conn, project_id)
        if before is None:
            raise ProjectNotFoundError(project_id)
        if before.status is ProjectStatus.ARCHIVED:
            raise ProjectStateError(f"project {project_id} is already archived")

        after = projects_repository.set_archived(conn, project_id, archived=True)
        assert after is not None
        audit_repository.record_entry(
            conn,
            project_id=project_id,
            entity_type=_ENTITY_TYPE,
            entity_id=project_id,
            action=AuditAction.ARCHIVE,
            before=before.model_dump(mode="json"),
            after=after.model_dump(mode="json"),
            actor=actor,
        )
    return after


def restore_project(
    conn: sqlite3.Connection, project_id: str, *, actor: str = DEFAULT_ACTOR
) -> Project:
    """Restore an archived project to `active`.

    Raises `ProjectNotFoundError` if the project does not exist, and
    `ProjectStateError` if it is not currently archived.
    """
    with transaction(conn):
        before = projects_repository.get_project(conn, project_id)
        if before is None:
            raise ProjectNotFoundError(project_id)
        if before.status is not ProjectStatus.ARCHIVED:
            raise ProjectStateError(f"project {project_id} is not archived")

        after = projects_repository.set_archived(conn, project_id, archived=False)
        assert after is not None
        audit_repository.record_entry(
            conn,
            project_id=project_id,
            entity_type=_ENTITY_TYPE,
            entity_id=project_id,
            action=AuditAction.RESTORE,
            before=before.model_dump(mode="json"),
            after=after.model_dump(mode="json"),
            actor=actor,
        )
    return after
