"""Evidence-link repository: direct SQL access to `evidence_links`, with
the project- and span-validation Section 9 explicitly assigns to the
service/repository layer rather than SQL ("span within content check in
service").

`insert_link` enforces, before ever writing a row:

1. the target (`observation`/`ledger_item`/`ledger_version`) actually
   exists;
2. the target belongs to the *same* `project_id` as the link being
   created (Prompt 6: "a target and its evidence must belong to the same
   project") — checked directly against the target's own row, not
   inferred, so a cross-project link is rejected even if the caller
   passes a project_id that happens to match neither row;
3. the cited content (and chunk, if given) exists, belongs to the same
   project, and the span/quote are valid against its actual text
   (`project_context.spans.validate_span` / `normalize_whitespace` — the
   same rules `project_context.services.extraction` uses for evidence
   spans).

`target_type='brief_claim'` is accepted by the schema (migration 0005)
for forward compatibility but rejected here with a clear error, since no
`brief_claims` table exists yet to validate against.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from project_context.db import evidence_repository
from project_context.domain.evidence_links import (
    EvidenceLink,
    EvidenceLinkSupportRole,
    EvidenceLinkTargetType,
)
from project_context.ids import new_id
from project_context.spans import InvalidSpanError, normalize_whitespace, validate_span
from project_context.timeutil import utc_now_iso

_TARGET_TABLES = {
    EvidenceLinkTargetType.OBSERVATION: "observations",
    EvidenceLinkTargetType.LEDGER_ITEM: "ledger_items",
    EvidenceLinkTargetType.LEDGER_VERSION: "ledger_versions",
}


class EvidenceLinkError(ValueError):
    """Raised when an evidence link's target, content, or span/quote
    cannot be validated."""


def _row_to_link(row: sqlite3.Row) -> EvidenceLink:
    return EvidenceLink(
        id=row["id"],
        project_id=row["project_id"],
        target_type=EvidenceLinkTargetType(row["target_type"]),
        target_id=row["target_id"],
        content_id=row["content_id"],
        chunk_id=row["chunk_id"],
        char_start=row["char_start"],
        char_end=row["char_end"],
        quote=row["quote"],
        location=json.loads(row["location_json"]) if row["location_json"] else None,
        support_role=EvidenceLinkSupportRole(row["support_role"]),
        created_at=row["created_at"],
    )


def _target_project_id(
    conn: sqlite3.Connection, target_type: EvidenceLinkTargetType, target_id: str
) -> str | None:
    table = _TARGET_TABLES[target_type]
    row = conn.execute(f"SELECT project_id FROM {table} WHERE id = ?", (target_id,)).fetchone()
    return row["project_id"] if row is not None else None


def insert_link(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    target_type: EvidenceLinkTargetType,
    target_id: str,
    content_id: str,
    char_start: int,
    char_end: int,
    quote: str,
    support_role: EvidenceLinkSupportRole,
    chunk_id: str | None = None,
    location: dict[str, Any] | None = None,
) -> EvidenceLink:
    if target_type is EvidenceLinkTargetType.BRIEF_CLAIM:
        raise EvidenceLinkError(
            "target_type='brief_claim' is not yet supported: no brief_claims table exists "
            "to validate the target against"
        )

    target_project_id = _target_project_id(conn, target_type, target_id)
    if target_project_id is None:
        raise EvidenceLinkError(f"{target_type.value} {target_id!r} does not exist")
    if target_project_id != project_id:
        raise EvidenceLinkError(
            f"{target_type.value} {target_id!r} belongs to a different project than this link"
        )

    content = evidence_repository.get_content(conn, project_id, content_id)
    if content is None:
        raise EvidenceLinkError(f"content {content_id!r} does not exist in project {project_id!r}")

    if chunk_id is not None:
        chunk = evidence_repository.get_chunk(conn, project_id, chunk_id)
        if chunk is None:
            raise EvidenceLinkError(f"chunk {chunk_id!r} does not exist in project {project_id!r}")
        if chunk.content_id != content_id:
            raise EvidenceLinkError(f"chunk {chunk_id!r} does not belong to content {content_id!r}")
        source_text = chunk.text
    else:
        source_text = content.normalized_text or ""

    try:
        actual = validate_span(source_text, char_start, char_end)
    except InvalidSpanError as exc:
        raise EvidenceLinkError(str(exc)) from exc
    if normalize_whitespace(actual) != normalize_whitespace(quote):
        raise EvidenceLinkError("quote does not match the cited text at that span")

    link_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO evidence_links
            (id, project_id, target_type, target_id, content_id, chunk_id, char_start,
             char_end, quote, location_json, support_role, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            link_id,
            project_id,
            target_type.value,
            target_id,
            content_id,
            chunk_id,
            char_start,
            char_end,
            quote,
            json.dumps(location) if location is not None else None,
            support_role.value,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM evidence_links WHERE id = ?", (link_id,)).fetchone()
    assert row is not None
    return _row_to_link(row)


def list_for_target(
    conn: sqlite3.Connection, project_id: str, target_type: EvidenceLinkTargetType, target_id: str
) -> list[EvidenceLink]:
    rows = conn.execute(
        "SELECT * FROM evidence_links WHERE project_id = ? AND target_type = ? AND target_id = ? "
        "ORDER BY created_at ASC",
        (project_id, target_type.value, target_id),
    ).fetchall()
    return [_row_to_link(row) for row in rows]
