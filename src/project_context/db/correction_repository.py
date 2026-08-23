"""Correction repository: direct SQL access to `corrections` —
record-keeping only (Prompt 6: "do not implement the full review
transaction until Prompt 8"). `insert_correction` writes one immutable
row and computes its `error_signature`
(`project_context.domain.review.compute_error_signature`); it does not
apply the correction to whatever it targets.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from project_context.domain.review import (
    Correction,
    CorrectionMateriality,
    CorrectionReasonCode,
    CorrectionTargetType,
    compute_error_signature,
)
from project_context.ids import new_id
from project_context.timeutil import utc_now_iso


def _row_to_correction(row: sqlite3.Row) -> Correction:
    return Correction(
        id=row["id"],
        project_id=row["project_id"],
        target_type=CorrectionTargetType(row["target_type"]),
        target_id=row["target_id"],
        review_id=row["review_id"],
        field_name=row["field_name"],
        original=json.loads(row["original_json"]) if row["original_json"] else None,
        corrected=json.loads(row["corrected_json"]) if row["corrected_json"] else None,
        reason_code=CorrectionReasonCode(row["reason_code"]),
        materiality=CorrectionMateriality(row["materiality"]),
        error_signature=row["error_signature"],
        model_id=row["model_id"],
        prompt_version=row["prompt_version"],
        actor=row["actor"],
        created_at=row["created_at"],
    )


def insert_correction(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    target_type: CorrectionTargetType,
    target_id: str,
    field_name: str,
    reason_code: CorrectionReasonCode,
    materiality: CorrectionMateriality,
    original: dict[str, Any] | None = None,
    corrected: dict[str, Any] | None = None,
    review_id: str | None = None,
    model_id: str | None = None,
    prompt_version: str | None = None,
    actor: str = "local-user",
) -> Correction:
    correction_id = new_id()
    now = utc_now_iso()
    error_signature = compute_error_signature(
        target_type=target_type, field_name=field_name, reason_code=reason_code
    )
    conn.execute(
        """
        INSERT INTO corrections
            (id, project_id, target_type, target_id, review_id, field_name, original_json,
             corrected_json, reason_code, materiality, error_signature, model_id, prompt_version,
             actor, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            correction_id,
            project_id,
            target_type.value,
            target_id,
            review_id,
            field_name,
            json.dumps(original) if original is not None else None,
            json.dumps(corrected) if corrected is not None else None,
            reason_code.value,
            materiality.value,
            error_signature,
            model_id,
            prompt_version,
            actor,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM corrections WHERE id = ?", (correction_id,)).fetchone()
    assert row is not None
    return _row_to_correction(row)


def list_for_project(conn: sqlite3.Connection, project_id: str) -> list[Correction]:
    rows = conn.execute(
        "SELECT * FROM corrections WHERE project_id = ? ORDER BY created_at ASC", (project_id,)
    ).fetchall()
    return [_row_to_correction(row) for row in rows]


def list_by_error_signature(
    conn: sqlite3.Connection, project_id: str, error_signature: str
) -> list[Correction]:
    rows = conn.execute(
        "SELECT * FROM corrections WHERE project_id = ? AND error_signature = ? "
        "ORDER BY created_at ASC",
        (project_id, error_signature),
    ).fetchall()
    return [_row_to_correction(row) for row in rows]
