"""Review repository: direct SQL access to `reviews` — record-keeping
only (Prompt 6: "do not implement the full review transaction until
Prompt 8"). `insert_review` writes one row; it does not touch
`proposed_mutations`, `ledger_items`, or `ledger_versions`. The
transactional "apply this review as a ledger transition, in one
database transaction" service is Prompt 8's job.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from project_context.domain.review import Review, ReviewAction
from project_context.ids import new_id
from project_context.timeutil import utc_now_iso


def _row_to_review(row: sqlite3.Row) -> Review:
    return Review(
        id=row["id"],
        project_id=row["project_id"],
        proposal_id=row["proposal_id"],
        action=ReviewAction(row["action"]),
        before=json.loads(row["before_json"]) if row["before_json"] else None,
        after=json.loads(row["after_json"]) if row["after_json"] else None,
        reason_code=row["reason_code"],
        note=row["note"],
        actor=row["actor"],
        reviewed_at=row["reviewed_at"],
        duration_ms=row["duration_ms"],
    )


def insert_review(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    proposal_id: str,
    action: ReviewAction,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason_code: str | None = None,
    note: str | None = None,
    actor: str = "local-user",
    duration_ms: int | None = None,
) -> Review:
    """Insert one review row. Raises `sqlite3.IntegrityError` if a
    review already exists for `proposal_id` (`uq_reviews_proposal`) — a
    review is a final decision act, not a queue."""
    review_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO reviews
            (id, project_id, proposal_id, action, before_json, after_json, reason_code, note,
             actor, reviewed_at, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            review_id,
            project_id,
            proposal_id,
            action.value,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
            reason_code,
            note,
            actor,
            now,
            duration_ms,
        ),
    )
    row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    assert row is not None
    return _row_to_review(row)


def get_review_for_proposal(
    conn: sqlite3.Connection, project_id: str, proposal_id: str
) -> Review | None:
    row = conn.execute(
        "SELECT * FROM reviews WHERE project_id = ? AND proposal_id = ?", (project_id, proposal_id)
    ).fetchone()
    return _row_to_review(row) if row is not None else None


def list_for_project(conn: sqlite3.Connection, project_id: str) -> list[Review]:
    rows = conn.execute(
        "SELECT * FROM reviews WHERE project_id = ? ORDER BY reviewed_at ASC", (project_id,)
    ).fetchall()
    return [_row_to_review(row) for row in rows]
