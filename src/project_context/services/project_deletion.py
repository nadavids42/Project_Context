"""Project deletion service (Section 16, "Deletion and retention"):
irreversible, previewed, exactly-confirmed removal of one project and
everything it owns — distinct from `project_context.services.projects
.archive_project`, which is reversible and destroys nothing.

Three-step flow, matching the manual acceptance checklist (Section 15,
item 9 — "Delete a test project; confirm evidence/credentials/FTS
cleanup"):

1. :func:`preview_delete_project` — read-only row/orphaned-byte counts,
   safe to show the user any number of times before they commit.
2. :func:`delete_project` — requires the caller to pass the project's
   *exact current name* as `confirmation_text` (Section 16: "requires
   exact confirmation"); anything else raises
   :class:`DeletionConfirmationError` and deletes nothing.
3. The credential material for every one of this project's sources is
   disconnected *before* any row is deleted (Section 16: "Add
   credential disconnect/delete behavior separate from project
   archive") — `CredentialService.disconnect` is idempotent and safe to
   call even for a source that was never connected.

Content-addressed byte objects (`evidence_store`) are deleted only
*after* the database transaction commits, and only for digests
`db.project_deletion_repository.sha256_only_referenced_by_project`
identified as exclusively this project's before any row was removed —
so a mid-transaction failure (rolled back) never leaves an orphaned
byte deleted out from under a still-referenced row, and a project that
shares identical content with another project (the storage layer's own
cross-artifact dedup) never has that shared file deleted.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from project_context import evidence_store
from project_context.credentials.service import CredentialService
from project_context.db import project_deletion_repository
from project_context.db.connection import transaction
from project_context.db.project_deletion_repository import ProjectDeletionCounts
from project_context.db.sources_repository import list_sources_for_project
from project_context.observability import get_logger
from project_context.services.projects import get_project

logger = get_logger(__name__)


class DeletionConfirmationError(ValueError):
    """Raised when `confirmation_text` does not exactly match the
    project's current name — Section 16: "requires exact confirmation."
    Nothing is deleted."""


@dataclass(frozen=True)
class ProjectDeletionResult:
    project_id: str
    project_name: str
    counts: ProjectDeletionCounts
    deleted_content_objects: int


def preview_delete_project(conn: sqlite3.Connection, project_id: str) -> ProjectDeletionCounts:
    """Read-only counts of everything `delete_project` would remove.
    Raises `ProjectNotFoundError` if the project does not exist. Safe to
    call repeatedly — issues no writes and requires no confirmation."""
    get_project(conn, project_id)  # raises ProjectNotFoundError if missing
    return project_deletion_repository.count_project_rows(conn, project_id)


def delete_project(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    confirmation_text: str,
    evidence_dir: Path,
    credential_service: CredentialService,
) -> ProjectDeletionResult:
    """Permanently delete `project_id` and every row it owns.

    Raises `ProjectNotFoundError` if the project does not exist, and
    `DeletionConfirmationError` — before touching anything — if
    `confirmation_text` is not exactly the project's current `name`.

    Irreversible. Unlike `services.projects.archive_project`, this is
    not "hidden and restorable" — evidence, ledger history, briefs, FTS
    entries, and connector credentials are all actually removed. A
    caller that wants a reversible action wants `archive_project`
    instead.
    """
    project = get_project(conn, project_id)  # raises ProjectNotFoundError if missing
    if confirmation_text != project.name:
        raise DeletionConfirmationError(
            f"confirmation text must exactly match the project name {project.name!r} to delete it"
        )

    for source in list_sources_for_project(conn, project_id):
        if source.credential_ref is not None:
            credential_service.disconnect(conn, project_id, source.id)

    conn.execute("PRAGMA defer_foreign_keys = ON")
    with transaction(conn):
        counts = project_deletion_repository.count_project_rows(conn, project_id)
        orphaned_sha256 = project_deletion_repository.sha256_only_referenced_by_project(
            conn, project_id
        )
        project_deletion_repository.delete_project_rows(conn, project_id)

    deleted_content_objects = 0
    for sha256 in orphaned_sha256:
        evidence_store.delete_object(evidence_dir, sha256)
        deleted_content_objects += 1

    logger.info(
        "project_deleted",
        extra={
            "project_id": project_id,
            "total_rows_deleted": counts.total_rows,
            "content_objects_deleted": deleted_content_objects,
        },
    )
    return ProjectDeletionResult(
        project_id=project_id,
        project_name=project.name,
        counts=counts,
        deleted_content_objects=deleted_content_objects,
    )
