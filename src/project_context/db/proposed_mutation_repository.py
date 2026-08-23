"""Proposed-mutation repository: direct SQL access to
`proposed_mutations` — pending/reviewed queue *state* only (Prompt 6).

No candidate scoring or action selection happens here or anywhere in
this repository — that is the reconciliation engine's job (Section 10),
explicitly out of scope for this prompt. `insert_proposal` takes an
already-decided `action`/`target_ledger_item_id`/confidence as given.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from project_context.domain.review import (
    ProposedMutation,
    ProposedMutationAction,
    ProposedMutationStatus,
)
from project_context.ids import new_id
from project_context.timeutil import utc_now_iso


def _row_to_proposal(row: sqlite3.Row) -> ProposedMutation:
    return ProposedMutation(
        id=row["id"],
        project_id=row["project_id"],
        observation_id=row["observation_id"],
        action=ProposedMutationAction(row["action"]),
        target_ledger_item_id=row["target_ledger_item_id"],
        proposed_patch=(
            json.loads(row["proposed_patch_json"]) if row["proposed_patch_json"] else None
        ),
        candidate_features=(
            json.loads(row["candidate_features_json"]) if row["candidate_features_json"] else None
        ),
        confidence_score=row["confidence_score"],
        confidence_band=row["confidence_band"],
        status=ProposedMutationStatus(row["status"]),
        created_at=row["created_at"],
        reviewed_at=row["reviewed_at"],
    )


def insert_proposal(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    observation_id: str,
    action: ProposedMutationAction,
    target_ledger_item_id: str | None = None,
    proposed_patch: dict[str, Any] | None = None,
    candidate_features: dict[str, Any] | None = None,
    confidence_score: float | None = None,
    confidence_band: str | None = None,
) -> ProposedMutation:
    """Insert one pending proposal. Raises `sqlite3.IntegrityError` if a
    pending proposal already exists for this `observation_id`
    (`uq_proposed_mutations_one_pending_per_observation`)."""
    proposal_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO proposed_mutations
            (id, project_id, observation_id, action, target_ledger_item_id, proposed_patch_json,
             candidate_features_json, confidence_score, confidence_band, status, created_at,
             reviewed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
        """,
        (
            proposal_id,
            project_id,
            observation_id,
            action.value,
            target_ledger_item_id,
            json.dumps(proposed_patch) if proposed_patch is not None else None,
            json.dumps(candidate_features) if candidate_features is not None else None,
            confidence_score,
            confidence_band,
            now,
        ),
    )
    proposal = get_proposal(conn, project_id, proposal_id)
    assert proposal is not None
    return proposal


def get_proposal(
    conn: sqlite3.Connection, project_id: str, proposal_id: str
) -> ProposedMutation | None:
    row = conn.execute(
        "SELECT * FROM proposed_mutations WHERE id = ? AND project_id = ?",
        (proposal_id, project_id),
    ).fetchone()
    return _row_to_proposal(row) if row is not None else None


def list_pending_for_project(conn: sqlite3.Connection, project_id: str) -> list[ProposedMutation]:
    rows = conn.execute(
        "SELECT * FROM proposed_mutations WHERE project_id = ? AND status = 'pending' "
        "ORDER BY created_at ASC",
        (project_id,),
    ).fetchall()
    return [_row_to_proposal(row) for row in rows]


def list_for_observation(
    conn: sqlite3.Connection, project_id: str, observation_id: str
) -> list[ProposedMutation]:
    rows = conn.execute(
        "SELECT * FROM proposed_mutations WHERE project_id = ? AND observation_id = ? "
        "ORDER BY created_at ASC",
        (project_id, observation_id),
    ).fetchall()
    return [_row_to_proposal(row) for row in rows]


def set_status(
    conn: sqlite3.Connection,
    project_id: str,
    proposal_id: str,
    status: ProposedMutationStatus,
    *,
    reviewed_at: str | None = None,
) -> ProposedMutation | None:
    """Move a proposal out of `pending` (or, in principle, back into it —
    the review transaction that will call this, Prompt 8, decides what
    is legal). `reviewed_at` defaults to now for any non-pending status."""
    stamp = reviewed_at
    if stamp is None and status is not ProposedMutationStatus.PENDING:
        stamp = utc_now_iso()
    conn.execute(
        "UPDATE proposed_mutations SET status = ?, reviewed_at = ? WHERE id = ? AND project_id = ?",
        (status.value, stamp, proposal_id, project_id),
    )
    return get_proposal(conn, project_id, proposal_id)
