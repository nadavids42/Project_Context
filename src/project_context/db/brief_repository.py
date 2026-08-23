"""Brief repository: direct SQL access to `generated_briefs` and
`brief_claims` (Section 9; migrations/0008_briefs.sql). Pure data
access — `project_context.services.briefs` owns transaction boundaries,
fact building, LLM composition, and claim validation.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from project_context.domain.briefs import (
    BriefClaimRecord,
    BriefStatus,
    BriefType,
    ClaimType,
    ClaimValidationStatus,
    GeneratedBrief,
)
from project_context.ids import new_id
from project_context.timeutil import utc_now_iso


def _row_to_brief(row: sqlite3.Row) -> GeneratedBrief:
    return GeneratedBrief(
        id=row["id"],
        project_id=row["project_id"],
        brief_type=BriefType(row["brief_type"]),
        meeting_artifact_id=row["meeting_artifact_id"],
        cutoff_at=row["cutoff_at"],
        input_snapshot=json.loads(row["input_snapshot_json"]),
        markdown=row["markdown"],
        model_id=row["model_id"],
        prompt_version=row["prompt_version"],
        schema_version=row["schema_version"],
        status=BriefStatus(row["status"]),
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        estimated_cost_usd=row["estimated_cost_usd"],
        latency_ms=row["latency_ms"],
        safe_error=row["safe_error"],
        created_at=row["created_at"],
    )


def _row_to_claim(row: sqlite3.Row) -> BriefClaimRecord:
    return BriefClaimRecord(
        id=row["id"],
        project_id=row["project_id"],
        brief_id=row["brief_id"],
        section=row["section"],
        ordinal=row["ordinal"],
        claim_text=row["claim_text"],
        claim_type=ClaimType(row["claim_type"]),
        ledger_item_id=row["ledger_item_id"],
        ledger_version_id=row["ledger_version_id"],
        cited_fact_ids=tuple(json.loads(row["cited_fact_ids_json"])),
        validation_status=ClaimValidationStatus(row["validation_status"]),
        created_at=row["created_at"],
    )


# --- generated_briefs ---------------------------------------------------


def insert_brief(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    brief_type: BriefType,
    cutoff_at: str,
    input_snapshot: dict[str, Any],
    meeting_artifact_id: str | None = None,
    status: BriefStatus = BriefStatus.GENERATING,
    brief_id: str | None = None,
) -> GeneratedBrief:
    """Insert one brief row, initially `generating` by default.
    `brief_id`, if given, is used as-is instead of generating a fresh one
    — so a caller can reference it (e.g. `brief_claims.brief_id`) before
    this row's own insert completes in the same transaction, the same
    pre-generated-id pattern `project_context.services.review` already
    uses for `reviews.id`."""
    brief_id = brief_id or new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO generated_briefs
            (id, project_id, brief_type, meeting_artifact_id, cutoff_at, input_snapshot_json,
             markdown, model_id, prompt_version, schema_version, status, input_tokens,
             output_tokens, estimated_cost_usd, latency_ms, safe_error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, NULL, NULL, NULL, NULL, NULL, ?)
        """,
        (
            brief_id,
            project_id,
            brief_type.value,
            meeting_artifact_id,
            cutoff_at,
            json.dumps(input_snapshot),
            status.value,
            now,
        ),
    )
    brief = get_brief(conn, project_id, brief_id)
    assert brief is not None
    return brief


def get_brief(conn: sqlite3.Connection, project_id: str, brief_id: str) -> GeneratedBrief | None:
    row = conn.execute(
        "SELECT * FROM generated_briefs WHERE id = ? AND project_id = ?", (brief_id, project_id)
    ).fetchone()
    return _row_to_brief(row) if row is not None else None


def list_briefs_for_project(
    conn: sqlite3.Connection, project_id: str, *, brief_type: BriefType | None = None
) -> list[GeneratedBrief]:
    query = "SELECT * FROM generated_briefs WHERE project_id = ?"
    params: list[object] = [project_id]
    if brief_type is not None:
        query += " AND brief_type = ?"
        params.append(brief_type.value)
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    return [_row_to_brief(row) for row in rows]


def finalize_brief(
    conn: sqlite3.Connection,
    project_id: str,
    brief_id: str,
    *,
    status: BriefStatus,
    markdown: str | None = None,
    model_id: str | None = None,
    prompt_version: str | None = None,
    schema_version: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    estimated_cost_usd: float | None = None,
    latency_ms: int | None = None,
    safe_error: str | None = None,
) -> GeneratedBrief | None:
    """Move a brief out of `generating` into its final `valid`/`failed`
    status, recording the Markdown (if any) and generation telemetry in
    one write. Every propositional field on `generated_briefs` is
    write-once in practice — the service never calls this twice for the
    same brief — but nothing here enforces that beyond the caller's own
    discipline, matching how `proposed_mutation_repository.set_status`
    is used."""
    conn.execute(
        """
        UPDATE generated_briefs
        SET status = ?, markdown = ?, model_id = ?, prompt_version = ?, schema_version = ?,
            input_tokens = ?, output_tokens = ?, estimated_cost_usd = ?, latency_ms = ?,
            safe_error = ?
        WHERE id = ? AND project_id = ?
        """,
        (
            status.value,
            markdown,
            model_id,
            prompt_version,
            schema_version,
            input_tokens,
            output_tokens,
            estimated_cost_usd,
            latency_ms,
            safe_error,
            brief_id,
            project_id,
        ),
    )
    return get_brief(conn, project_id, brief_id)


def supersede_previous_valid_briefs(
    conn: sqlite3.Connection, project_id: str, *, brief_type: BriefType, except_brief_id: str
) -> int:
    """Mark every other currently-`valid` brief of this type in this
    project as `superseded` (Section 9: "stored brief history" — exactly
    one brief counts as current at a time; older ones stay queryable).
    Returns the number of rows updated."""
    cursor = conn.execute(
        """
        UPDATE generated_briefs
        SET status = ?
        WHERE project_id = ? AND brief_type = ? AND status = ? AND id != ?
        """,
        (
            BriefStatus.SUPERSEDED.value,
            project_id,
            brief_type.value,
            BriefStatus.VALID.value,
            except_brief_id,
        ),
    )
    return cursor.rowcount


# --- brief_claims ---------------------------------------------------------


def insert_claim(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    brief_id: str,
    section: str,
    ordinal: int,
    claim_text: str,
    claim_type: ClaimType,
    cited_fact_ids: tuple[str, ...],
    validation_status: ClaimValidationStatus,
    ledger_item_id: str | None = None,
    ledger_version_id: str | None = None,
    claim_id: str | None = None,
) -> BriefClaimRecord:
    claim_id = claim_id or new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO brief_claims
            (id, project_id, brief_id, section, ordinal, claim_text, claim_type,
             ledger_item_id, ledger_version_id, cited_fact_ids_json, validation_status,
             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            claim_id,
            project_id,
            brief_id,
            section,
            ordinal,
            claim_text,
            claim_type.value,
            ledger_item_id,
            ledger_version_id,
            json.dumps(list(cited_fact_ids)),
            validation_status.value,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM brief_claims WHERE id = ?", (claim_id,)).fetchone()
    assert row is not None
    return _row_to_claim(row)


def list_claims_for_brief(
    conn: sqlite3.Connection, project_id: str, brief_id: str
) -> list[BriefClaimRecord]:
    rows = conn.execute(
        "SELECT * FROM brief_claims WHERE project_id = ? AND brief_id = ? ORDER BY ordinal ASC",
        (project_id, brief_id),
    ).fetchall()
    return [_row_to_claim(row) for row in rows]
