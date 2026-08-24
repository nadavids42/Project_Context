"""Project-deletion repository: counts and ordered row removal for
`project_context.services.project_deletion` (Section 16, "Deletion and
retention": "Delete project previews counts, requires exact
confirmation, and removes project rows, FTS entries, stored raw/parsed
content not shared by another project, briefs, and connector
configuration").

Pure data access, like every other `db/*_repository.py` module — no
transaction management (the service wraps calls here in
`project_context.db.connection.transaction`) and no confirmation-text
or credential logic (the service's job).

Deletion order and `PRAGMA defer_foreign_keys`
------------------------------------------------
migrations/0001's header states the schema's own design intent
explicitly: "Foreign keys use SQLite's default NO ACTION behavior —
nothing in this schema silently cascades a delete. Project deletion is
a later, explicit, previewed service" — this module is that service's
data-access half.

Several tables in this schema have *mutual* foreign keys within one
project's own rows (`ledger_items.current_version_id` <->
`ledger_versions.ledger_item_id`; `source_artifacts.current_content_id`
<-> `source_contents.artifact_id`; `ledger_items.superseded_by_item_id`/
`supersedes_item_id` <-> another `ledger_items` row). No single delete
order satisfies `PRAGMA foreign_keys = ON` immediate (per-statement)
enforcement for a mutual pair — one side must always be deleted before
the other exists to reference. `services.project_deletion` sets
`PRAGMA defer_foreign_keys = ON` before opening the deletion
transaction (a pragma that can only be changed outside an active
transaction), which defers every FK check to `COMMIT` instead of each
statement — so this module's `DELETE_ORDER` only needs to guarantee
that *by commit time* every project-owned row referencing another
project-owned row is gone, not that each individual statement leaves
the database momentarily consistent. `DELETE_ORDER` still follows a
sensible dependency-order-first shape (deepest dependents before their
parents) for readability and because it also happens to be correct
without deferral for every *non-circular* pair in this schema.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectDeletionCounts:
    """Row counts for every project-owned table, plus the number of
    content-addressed byte objects on disk that only this project
    references (and would therefore become orphaned/deleted). Shown to
    the user before they type the exact confirmation text (Section 16:
    "previews counts... requires exact confirmation")."""

    sources: int
    source_artifacts: int
    source_contents: int
    source_chunks: int
    sync_runs: int
    sync_items: int
    observations: int
    ledger_items: int
    ledger_versions: int
    evidence_links: int
    proposed_mutations: int
    reviews: int
    corrections: int
    project_people: int
    generated_briefs: int
    brief_claims: int
    audit_entries: int
    orphaned_content_objects: int

    @property
    def total_rows(self) -> int:
        return (
            self.sources
            + self.source_artifacts
            + self.source_contents
            + self.source_chunks
            + self.sync_runs
            + self.sync_items
            + self.observations
            + self.ledger_items
            + self.ledger_versions
            + self.evidence_links
            + self.proposed_mutations
            + self.reviews
            + self.corrections
            + self.project_people
            + self.generated_briefs
            + self.brief_claims
            + self.audit_entries
        )


def _count(conn: sqlite3.Connection, sql: str, project_id: str) -> int:
    (value,) = conn.execute(sql, (project_id,)).fetchone()
    return value


def count_project_rows(conn: sqlite3.Connection, project_id: str) -> ProjectDeletionCounts:
    """Everything a full purge of `project_id` would remove. Read-only —
    safe to call for a preview with no confirmation and no transaction."""
    sync_items = _count(
        conn,
        "SELECT COUNT(*) FROM sync_items WHERE sync_run_id IN "
        "(SELECT id FROM sync_runs WHERE project_id = ?)",
        project_id,
    )
    return ProjectDeletionCounts(
        sources=_count(conn, "SELECT COUNT(*) FROM sources WHERE project_id = ?", project_id),
        source_artifacts=_count(
            conn, "SELECT COUNT(*) FROM source_artifacts WHERE project_id = ?", project_id
        ),
        source_contents=_count(
            conn, "SELECT COUNT(*) FROM source_contents WHERE project_id = ?", project_id
        ),
        source_chunks=_count(
            conn, "SELECT COUNT(*) FROM source_chunks WHERE project_id = ?", project_id
        ),
        sync_runs=_count(conn, "SELECT COUNT(*) FROM sync_runs WHERE project_id = ?", project_id),
        sync_items=sync_items,
        observations=_count(
            conn, "SELECT COUNT(*) FROM observations WHERE project_id = ?", project_id
        ),
        ledger_items=_count(
            conn, "SELECT COUNT(*) FROM ledger_items WHERE project_id = ?", project_id
        ),
        ledger_versions=_count(
            conn, "SELECT COUNT(*) FROM ledger_versions WHERE project_id = ?", project_id
        ),
        evidence_links=_count(
            conn, "SELECT COUNT(*) FROM evidence_links WHERE project_id = ?", project_id
        ),
        proposed_mutations=_count(
            conn, "SELECT COUNT(*) FROM proposed_mutations WHERE project_id = ?", project_id
        ),
        reviews=_count(conn, "SELECT COUNT(*) FROM reviews WHERE project_id = ?", project_id),
        corrections=_count(
            conn, "SELECT COUNT(*) FROM corrections WHERE project_id = ?", project_id
        ),
        project_people=_count(
            conn, "SELECT COUNT(*) FROM project_people WHERE project_id = ?", project_id
        ),
        generated_briefs=_count(
            conn, "SELECT COUNT(*) FROM generated_briefs WHERE project_id = ?", project_id
        ),
        brief_claims=_count(
            conn, "SELECT COUNT(*) FROM brief_claims WHERE project_id = ?", project_id
        ),
        audit_entries=_count(
            conn, "SELECT COUNT(*) FROM audit_entries WHERE project_id = ?", project_id
        ),
        orphaned_content_objects=len(sha256_only_referenced_by_project(conn, project_id)),
    )


def sha256_only_referenced_by_project(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """SHA-256 digests that appear in this project's `source_contents`
    rows and *no other project's* — i.e. the on-disk content-addressed
    byte objects (Section 16: "Content-addressed bytes are deleted only
    when no reference remains") that will become orphaned once this
    project's rows are gone. Must be read before (or in the same
    transaction before) deleting this project's `source_contents` rows,
    since it compares against them."""
    rows = conn.execute(
        """
        SELECT DISTINCT sha256 FROM source_contents WHERE project_id = ?
        EXCEPT
        SELECT DISTINCT sha256 FROM source_contents WHERE project_id != ?
        """,
        (project_id, project_id),
    ).fetchall()
    return [row["sha256"] for row in rows]


#: Deepest dependents first (see module docstring for why exact order is
#: a secondary concern once `PRAGMA defer_foreign_keys = ON` is set —
#: this order is kept sensible anyway, for readability and so it also
#: works correctly if that pragma is ever removed for a non-circular
#: subset).
DELETE_ORDER: tuple[tuple[str, str], ...] = (
    ("brief_claims", "project_id"),
    ("generated_briefs", "project_id"),
    ("corrections", "project_id"),
    ("reviews", "project_id"),
    ("proposed_mutations", "project_id"),
    ("evidence_links", "project_id"),
    ("ledger_versions", "project_id"),
    ("ledger_items", "project_id"),
    ("observations", "project_id"),
    ("project_people", "project_id"),
    # sync_items has no project_id column of its own (see migrations/
    # 0001) — deleted via a subquery against sync_runs instead of the
    # (table, project_id_column) shape every other entry here uses.
    ("source_chunks", "project_id"),
    ("source_contents", "project_id"),
    ("source_artifacts", "project_id"),
    ("sources", "project_id"),
    ("audit_entries", "project_id"),
)


def delete_project_rows(conn: sqlite3.Connection, project_id: str) -> None:
    """Delete every project-owned row for `project_id`, in `DELETE_ORDER`,
    plus `sync_items` (joined through `sync_runs`) and finally the
    `projects` row itself. Callers MUST run this inside a transaction
    that has already set `PRAGMA defer_foreign_keys = ON` on `conn` —
    see the module docstring. `people`/`person_aliases` are
    deliberately never touched: they are canonical identity shared
    across projects (migrations/0005), not project-owned."""
    conn.execute(
        "DELETE FROM sync_items WHERE sync_run_id IN "
        "(SELECT id FROM sync_runs WHERE project_id = ?)",
        (project_id,),
    )
    conn.execute("DELETE FROM sync_runs WHERE project_id = ?", (project_id,))
    for table, column in DELETE_ORDER:
        conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (project_id,))  # noqa: S608
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
