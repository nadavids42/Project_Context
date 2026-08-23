"""Ledger repository: direct SQL access to `ledger_items`,
`ledger_versions`, and `ledger_items_fts`.

Pure data access — the mutual-FK two-step insert (an item's
`current_version_id` cannot be set until its first version row exists,
and a version's `ledger_item_id` requires the item to exist first) and
the "close the prior version, bump version_no, repoint current_version_id"
sequence that keeps them consistent are orchestrated by
`project_context.services.ledger`, one transaction per operation — the
same split already used for `source_artifacts`/`source_contents` between
`evidence_repository` (pure access) and `services.evidence`
(transaction/orchestration).

No function here deletes a `ledger_versions` row, and none ever will —
Prompt 6: "Direct hard deletion of accepted ledger history is not
exposed."
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from project_context.domain.ledger import (
    ConfidenceBand,
    LedgerItem,
    LedgerItemKind,
    LedgerItemStatus,
    LedgerTransitionType,
    LedgerVersion,
)
from project_context.ids import new_id
from project_context.timeutil import utc_now_iso


def _row_to_item(row: sqlite3.Row) -> LedgerItem:
    return LedgerItem(
        id=row["id"],
        project_id=row["project_id"],
        kind=LedgerItemKind(row["kind"]),
        canonical_title=row["canonical_title"],
        canonical_description=row["canonical_description"],
        status=LedgerItemStatus(row["status"]),
        owner_person_id=row["owner_person_id"],
        due_date=row["due_date"],
        effective_at=row["effective_at"],
        current_version_id=row["current_version_id"],
        confidence_band=ConfidenceBand(row["confidence_band"]) if row["confidence_band"] else None,
        user_corrected=bool(row["user_corrected"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_version(row: sqlite3.Row) -> LedgerVersion:
    return LedgerVersion(
        id=row["id"],
        project_id=row["project_id"],
        ledger_item_id=row["ledger_item_id"],
        version_no=row["version_no"],
        canonical_title=row["canonical_title"],
        canonical_description=row["canonical_description"],
        status=LedgerItemStatus(row["status"]),
        owner_person_id=row["owner_person_id"],
        due_date=row["due_date"],
        effective_at=row["effective_at"],
        confidence_band=ConfidenceBand(row["confidence_band"]) if row["confidence_band"] else None,
        transition_type=LedgerTransitionType(row["transition_type"]),
        from_version_id=row["from_version_id"],
        observation_id=row["observation_id"],
        review_id=row["review_id"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        superseded_by_version_id=row["superseded_by_version_id"],
        created_at=row["created_at"],
    )


# --- ledger_items -----------------------------------------------------------


def insert_item(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    kind: LedgerItemKind,
    canonical_title: str,
    canonical_description: str | None,
    status: LedgerItemStatus,
    owner_person_id: str | None = None,
    due_date: str | None = None,
    effective_at: str | None = None,
    confidence_band: ConfidenceBand | None = None,
) -> LedgerItem:
    """Insert a bare item row with `current_version_id=NULL`. Callers
    (`project_context.services.ledger.create_ledger_item`) insert the
    first `ledger_versions` row next, then call `set_current_version`,
    all inside one transaction."""
    item_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO ledger_items
            (id, project_id, kind, canonical_title, canonical_description, status,
             owner_person_id, due_date, effective_at, current_version_id, confidence_band,
             user_corrected, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, ?, ?)
        """,
        (
            item_id,
            project_id,
            kind.value,
            canonical_title,
            canonical_description,
            status.value,
            owner_person_id,
            due_date,
            effective_at,
            confidence_band.value if confidence_band else None,
            now,
            now,
        ),
    )
    item = get_item(conn, project_id, item_id)
    assert item is not None
    return item


def get_item(conn: sqlite3.Connection, project_id: str, item_id: str) -> LedgerItem | None:
    row = conn.execute(
        "SELECT * FROM ledger_items WHERE id = ? AND project_id = ?", (item_id, project_id)
    ).fetchone()
    return _row_to_item(row) if row is not None else None


def list_items_for_project(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    kind: LedgerItemKind | None = None,
    status: LedgerItemStatus | None = None,
) -> list[LedgerItem]:
    query = "SELECT * FROM ledger_items WHERE project_id = ?"
    params: list[object] = [project_id]
    if kind is not None:
        query += " AND kind = ?"
        params.append(kind.value)
    if status is not None:
        query += " AND status = ?"
        params.append(status.value)
    query += " ORDER BY updated_at DESC"
    rows = conn.execute(query, params).fetchall()
    return [_row_to_item(row) for row in rows]


def update_projection(
    conn: sqlite3.Connection,
    project_id: str,
    item_id: str,
    *,
    canonical_title: str,
    canonical_description: str | None,
    status: LedgerItemStatus,
    owner_person_id: str | None,
    due_date: str | None,
    effective_at: str | None,
    confidence_band: ConfidenceBand | None,
    current_version_id: str,
    user_corrected: bool | None = None,
) -> LedgerItem | None:
    """Overwrite the current-projection fields and repoint
    `current_version_id` at the new current version. `user_corrected` is
    left unchanged (`None`) unless explicitly passed — an ordinary
    version append must not clear a prior correction flag."""
    now = utc_now_iso()
    if user_corrected is None:
        conn.execute(
            """
            UPDATE ledger_items
            SET canonical_title = ?, canonical_description = ?, status = ?, owner_person_id = ?,
                due_date = ?, effective_at = ?, confidence_band = ?, current_version_id = ?,
                updated_at = ?
            WHERE id = ? AND project_id = ?
            """,
            (
                canonical_title,
                canonical_description,
                status.value,
                owner_person_id,
                due_date,
                effective_at,
                confidence_band.value if confidence_band else None,
                current_version_id,
                now,
                item_id,
                project_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE ledger_items
            SET canonical_title = ?, canonical_description = ?, status = ?, owner_person_id = ?,
                due_date = ?, effective_at = ?, confidence_band = ?, current_version_id = ?,
                user_corrected = ?, updated_at = ?
            WHERE id = ? AND project_id = ?
            """,
            (
                canonical_title,
                canonical_description,
                status.value,
                owner_person_id,
                due_date,
                effective_at,
                confidence_band.value if confidence_band else None,
                current_version_id,
                1 if user_corrected else 0,
                now,
                item_id,
                project_id,
            ),
        )
    return get_item(conn, project_id, item_id)


# --- ledger_versions ----------------------------------------------------


def next_version_no(conn: sqlite3.Connection, ledger_item_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS max_no FROM ledger_versions "
        "WHERE ledger_item_id = ?",
        (ledger_item_id,),
    ).fetchone()
    return row["max_no"] + 1


def insert_version(
    conn: sqlite3.Connection,
    project_id: str,
    ledger_item_id: str,
    *,
    version_no: int,
    canonical_title: str,
    canonical_description: str | None,
    status: LedgerItemStatus,
    owner_person_id: str | None,
    due_date: str | None,
    effective_at: str | None,
    confidence_band: ConfidenceBand | None,
    transition_type: LedgerTransitionType,
    from_version_id: str | None,
    observation_id: str | None,
    review_id: str | None,
    valid_from: str | None = None,
) -> LedgerVersion:
    version_id = new_id()
    now = valid_from or utc_now_iso()
    conn.execute(
        """
        INSERT INTO ledger_versions
            (id, project_id, ledger_item_id, version_no, canonical_title, canonical_description,
             status, owner_person_id, due_date, effective_at, confidence_band, transition_type,
             from_version_id, observation_id, review_id, valid_from, valid_to,
             superseded_by_version_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        """,
        (
            version_id,
            project_id,
            ledger_item_id,
            version_no,
            canonical_title,
            canonical_description,
            status.value,
            owner_person_id,
            due_date,
            effective_at,
            confidence_band.value if confidence_band else None,
            transition_type.value,
            from_version_id,
            observation_id,
            review_id,
            now,
            now,
        ),
    )
    version = get_version(conn, project_id, version_id)
    assert version is not None
    return version


def get_version(conn: sqlite3.Connection, project_id: str, version_id: str) -> LedgerVersion | None:
    row = conn.execute(
        "SELECT * FROM ledger_versions WHERE id = ? AND project_id = ?", (version_id, project_id)
    ).fetchone()
    return _row_to_version(row) if row is not None else None


def close_version(
    conn: sqlite3.Connection,
    project_id: str,
    version_id: str,
    *,
    superseded_by_version_id: str,
    valid_to: str | None = None,
) -> LedgerVersion | None:
    """Set `valid_to`/`superseded_by_version_id` on a version that a new
    version just replaced. Never deletes it (Prompt 6: accepted history
    has no hard-delete path)."""
    now = valid_to or utc_now_iso()
    conn.execute(
        "UPDATE ledger_versions SET valid_to = ?, superseded_by_version_id = ? "
        "WHERE id = ? AND project_id = ?",
        (now, superseded_by_version_id, version_id, project_id),
    )
    return get_version(conn, project_id, version_id)


def list_versions_for_item(
    conn: sqlite3.Connection, project_id: str, ledger_item_id: str
) -> list[LedgerVersion]:
    """Full version history for one item, oldest first — mirrors
    `evidence_repository.list_contents_for_artifact`."""
    rows = conn.execute(
        "SELECT * FROM ledger_versions WHERE ledger_item_id = ? AND project_id = ? "
        "ORDER BY version_no ASC",
        (ledger_item_id, project_id),
    ).fetchall()
    return [_row_to_version(row) for row in rows]


# --- full-text search -------------------------------------------------------


@dataclass(frozen=True)
class LedgerItemSearchResult:
    ledger_item_id: str
    title_snippet: str
    description_snippet: str


def search_ledger_items(
    conn: sqlite3.Connection, project_id: str, query_text: str, *, limit: int = 20
) -> list[LedgerItemSearchResult]:
    """Full-text search over one project's current ledger projection.
    `project_id` is mandatory and applied as a relational filter *after*
    the FTS match (same rule as `evidence_repository.search_chunks`)."""
    phrase = '"' + query_text.replace('"', '""') + '"'
    rows = conn.execute(
        """
        SELECT li.id AS ledger_item_id,
               snippet(ledger_items_fts, 0, '[', ']', '...', 6) AS title_snippet,
               snippet(ledger_items_fts, 1, '[', ']', '...', 10) AS description_snippet
        FROM ledger_items_fts
        JOIN ledger_items li ON li.rowid = ledger_items_fts.rowid
        WHERE ledger_items_fts MATCH ? AND li.project_id = ?
        ORDER BY rank
        LIMIT ?
        """,
        (phrase, project_id, limit),
    ).fetchall()
    return [
        LedgerItemSearchResult(
            ledger_item_id=row["ledger_item_id"],
            title_snippet=row["title_snippet"],
            description_snippet=row["description_snippet"],
        )
        for row in rows
    ]
