"""Evidence repository: direct SQL access to `source_artifacts`,
`source_contents`, `source_chunks`, and the `source_chunks_fts` index.

Pure data access, like `project_context.db.projects_repository` — no
transaction management (callers use `project_context.db.connection.
transaction`) and no parsing/chunking logic (that's
`project_context.services.evidence`'s job).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from project_context.chunking import ChunkSpec
from project_context.domain.evidence import (
    ArtifactAvailability,
    ArtifactType,
    AssignmentMethod,
    EvidenceSourceType,
    ParseStatus,
    SourceArtifact,
    SourceChunk,
    SourceContent,
)
from project_context.ids import new_id
from project_context.timeutil import utc_now_iso


def _row_to_artifact(row: sqlite3.Row) -> SourceArtifact:
    return SourceArtifact(
        id=row["id"],
        project_id=row["project_id"],
        source_id=row["source_id"],
        external_id=row["external_id"],
        artifact_type=ArtifactType(row["artifact_type"]),
        title=row["title"],
        author=row["author"],
        occurred_at=row["occurred_at"],
        external_url=row["external_url"],
        source_type=EvidenceSourceType(row["source_type"]) if row["source_type"] else None,
        assignment_method=AssignmentMethod(row["assignment_method"]),
        availability=ArtifactAvailability(row["availability"]),
        current_content_id=row["current_content_id"],
        match_rule=row["match_rule"],
        match_reason=row["match_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_content(row: sqlite3.Row) -> SourceContent:
    return SourceContent(
        id=row["id"],
        project_id=row["project_id"],
        artifact_id=row["artifact_id"],
        version_key=row["version_key"],
        sha256=row["sha256"],
        raw_storage_path=row["raw_storage_path"],
        mime_type=row["mime_type"],
        byte_size=row["byte_size"],
        normalized_text=row["normalized_text"],
        parser_name=row["parser_name"],
        parser_version=row["parser_version"],
        parse_status=ParseStatus(row["parse_status"]),
        location_map=json.loads(row["location_map_json"]) if row["location_map_json"] else None,
        original_filename=row["original_filename"],
        created_at=row["created_at"],
    )


def _row_to_chunk(row: sqlite3.Row) -> SourceChunk:
    return SourceChunk(
        id=row["id"],
        project_id=row["project_id"],
        content_id=row["content_id"],
        ordinal=row["ordinal"],
        text=row["text"],
        char_start=row["char_start"],
        char_end=row["char_end"],
        token_estimate=row["token_estimate"],
        section_path=row["section_path"],
        location=json.loads(row["location_json"]) if row["location_json"] else None,
        sha256=row["sha256"],
        created_at=row["created_at"],
    )


# --- artifacts --------------------------------------------------------


def get_artifact(
    conn: sqlite3.Connection, project_id: str, artifact_id: str
) -> SourceArtifact | None:
    row = conn.execute(
        "SELECT * FROM source_artifacts WHERE id = ? AND project_id = ?",
        (artifact_id, project_id),
    ).fetchone()
    return _row_to_artifact(row) if row is not None else None


def get_artifact_by_external_id(
    conn: sqlite3.Connection, project_id: str, source_id: str, external_id: str
) -> SourceArtifact | None:
    row = conn.execute(
        "SELECT * FROM source_artifacts WHERE project_id = ? AND source_id = ? AND external_id = ?",
        (project_id, source_id, external_id),
    ).fetchone()
    return _row_to_artifact(row) if row is not None else None


def insert_artifact(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    *,
    external_id: str,
    artifact_type: ArtifactType,
    title: str | None,
    author: str | None,
    occurred_at: str | None,
    external_url: str | None,
    source_type: EvidenceSourceType | None,
    assignment_method: AssignmentMethod = AssignmentMethod.MANUAL,
    match_rule: str | None = None,
    match_reason: str | None = None,
    availability: ArtifactAvailability = ArtifactAvailability.AVAILABLE,
) -> SourceArtifact:
    artifact_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO source_artifacts
            (id, project_id, source_id, external_id, artifact_type, title, author,
             occurred_at, external_url, source_type, assignment_method, availability,
             current_content_id, match_rule, match_reason, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            project_id,
            source_id,
            external_id,
            artifact_type.value,
            title,
            author,
            occurred_at,
            external_url,
            source_type.value if source_type else None,
            assignment_method.value,
            availability.value,
            match_rule,
            match_reason,
            now,
            now,
        ),
    )
    artifact = get_artifact(conn, project_id, artifact_id)
    assert artifact is not None
    return artifact


def set_current_content(
    conn: sqlite3.Connection, project_id: str, artifact_id: str, content_id: str
) -> SourceArtifact:
    """Advance the artifact's current-version pointer. Caller wraps this
    with the content insert in one transaction (FR-007: "current version
    pointer advances transactionally")."""
    now = utc_now_iso()
    conn.execute(
        "UPDATE source_artifacts SET current_content_id = ?, updated_at = ? "
        "WHERE id = ? AND project_id = ?",
        (content_id, now, artifact_id, project_id),
    )
    artifact = get_artifact(conn, project_id, artifact_id)
    assert artifact is not None
    return artifact


def list_artifacts(conn: sqlite3.Connection, project_id: str) -> list[SourceArtifact]:
    """All artifacts for one project, most recently updated first."""
    rows = conn.execute(
        "SELECT * FROM source_artifacts WHERE project_id = ? ORDER BY updated_at DESC",
        (project_id,),
    ).fetchall()
    return [_row_to_artifact(row) for row in rows]


def list_artifacts_for_source(
    conn: sqlite3.Connection, project_id: str, source_id: str
) -> list[SourceArtifact]:
    rows = conn.execute(
        "SELECT * FROM source_artifacts WHERE project_id = ? AND source_id = ? "
        "ORDER BY updated_at DESC",
        (project_id, source_id),
    ).fetchall()
    return [_row_to_artifact(row) for row in rows]


def update_availability(
    conn: sqlite3.Connection,
    project_id: str,
    artifact_id: str,
    *,
    availability: ArtifactAvailability,
) -> SourceArtifact:
    """Mark an artifact unavailable (or available again) without ever
    erasing its already-imported evidence (FR-027: "deleted/trashed
    items are marked unavailable, not erased")."""
    now = utc_now_iso()
    conn.execute(
        "UPDATE source_artifacts SET availability = ?, updated_at = ? "
        "WHERE id = ? AND project_id = ?",
        (availability.value, now, artifact_id, project_id),
    )
    artifact = get_artifact(conn, project_id, artifact_id)
    assert artifact is not None
    return artifact


# --- contents -----------------------------------------------------------


def get_content(conn: sqlite3.Connection, project_id: str, content_id: str) -> SourceContent | None:
    row = conn.execute(
        "SELECT * FROM source_contents WHERE id = ? AND project_id = ?",
        (content_id, project_id),
    ).fetchone()
    return _row_to_content(row) if row is not None else None


def get_content_by_sha256(
    conn: sqlite3.Connection, artifact_id: str, sha256: str
) -> SourceContent | None:
    """Used to detect identical resubmission (FR-005/FR-006): if a
    content row with this exact hash already exists for this artifact,
    reuse it instead of creating a new version."""
    row = conn.execute(
        "SELECT * FROM source_contents WHERE artifact_id = ? AND sha256 = ?",
        (artifact_id, sha256),
    ).fetchone()
    return _row_to_content(row) if row is not None else None


def next_version_key(conn: sqlite3.Connection, artifact_id: str) -> str:
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM source_contents WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    return f"v{count + 1}"


def insert_content(
    conn: sqlite3.Connection,
    project_id: str,
    artifact_id: str,
    *,
    sha256: str,
    raw_storage_path: str | None,
    mime_type: str | None,
    byte_size: int,
    normalized_text: str | None,
    parser_name: str | None,
    parser_version: str | None,
    parse_status: ParseStatus,
    location_map: dict[str, Any] | None,
    original_filename: str | None,
    version_key: str | None = None,
) -> SourceContent:
    """`version_key` defaults to the next sequential `"v1"`, `"v2"`, ...
    (manual ingestion's convention). A connector with its own natural
    version marker (Drive's `modifiedTime`; Prompt 10) may pass one
    explicitly instead — still unique per `(artifact_id, version_key)`,
    just not sequential text. Either way this is the one fact a caller
    can compare against a freshly discovered marker *before* fetching,
    to decide "changed" without a schema column dedicated to it."""
    content_id = new_id()
    if version_key is None:
        version_key = next_version_key(conn, artifact_id)
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO source_contents
            (id, project_id, artifact_id, version_key, sha256, raw_storage_path, mime_type,
             byte_size, normalized_text, parser_name, parser_version, parse_status,
             location_map_json, original_filename, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            content_id,
            project_id,
            artifact_id,
            version_key,
            sha256,
            raw_storage_path,
            mime_type,
            byte_size,
            normalized_text,
            parser_name,
            parser_version,
            parse_status.value,
            json.dumps(location_map) if location_map is not None else None,
            original_filename,
            now,
        ),
    )
    content = get_content(conn, project_id, content_id)
    assert content is not None
    return content


def list_contents_for_artifact(
    conn: sqlite3.Connection, project_id: str, artifact_id: str
) -> list[SourceContent]:
    """Full version history for one artifact, oldest first — old bytes/
    text remain addressable (FR-007)."""
    rows = conn.execute(
        "SELECT * FROM source_contents WHERE artifact_id = ? AND project_id = ? "
        "ORDER BY created_at ASC",
        (artifact_id, project_id),
    ).fetchall()
    return [_row_to_content(row) for row in rows]


# --- chunks ---------------------------------------------------------------


def get_chunk(conn: sqlite3.Connection, project_id: str, chunk_id: str) -> SourceChunk | None:
    row = conn.execute(
        "SELECT * FROM source_chunks WHERE id = ? AND project_id = ?", (chunk_id, project_id)
    ).fetchone()
    return _row_to_chunk(row) if row is not None else None


def insert_chunks(
    conn: sqlite3.Connection, project_id: str, content_id: str, chunk_specs: list[ChunkSpec]
) -> list[SourceChunk]:
    now = utc_now_iso()
    chunks: list[SourceChunk] = []
    for spec in chunk_specs:
        chunk_id = new_id()
        location = {"section_path": spec.section_path} if spec.section_path else None
        conn.execute(
            """
            INSERT INTO source_chunks
                (id, project_id, content_id, ordinal, text, char_start, char_end,
                 token_estimate, section_path, location_json, sha256, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk_id,
                project_id,
                content_id,
                spec.ordinal,
                spec.text,
                spec.char_start,
                spec.char_end,
                spec.token_estimate,
                spec.section_path,
                json.dumps(location) if location else None,
                spec.sha256,
                now,
            ),
        )
        chunks.append(
            SourceChunk(
                id=chunk_id,
                project_id=project_id,
                content_id=content_id,
                ordinal=spec.ordinal,
                text=spec.text,
                char_start=spec.char_start,
                char_end=spec.char_end,
                token_estimate=spec.token_estimate,
                section_path=spec.section_path,
                location=location,
                sha256=spec.sha256,
                created_at=now,
            )
        )
    return chunks


def list_chunks_for_content(
    conn: sqlite3.Connection, project_id: str, content_id: str
) -> list[SourceChunk]:
    rows = conn.execute(
        "SELECT * FROM source_chunks WHERE content_id = ? AND project_id = ? ORDER BY ordinal ASC",
        (content_id, project_id),
    ).fetchall()
    return [_row_to_chunk(row) for row in rows]


# --- full-text search -------------------------------------------------------


@dataclass(frozen=True)
class ChunkSearchResult:
    chunk_id: str
    content_id: str
    section_path: str | None
    snippet: str


def _phrase_query(text: str) -> str:
    """Wrap arbitrary caller text as one FTS5 phrase literal, so callers
    never need to know FTS5's query-operator syntax (a bare `-` inside a
    query, for instance, is a NOT operator, not a literal hyphen)."""
    return '"' + text.replace('"', '""') + '"'


def search_chunks(
    conn: sqlite3.Connection, project_id: str, query_text: str, *, limit: int = 20
) -> list[ChunkSearchResult]:
    """Full-text search over one project's chunks. `project_id` is
    mandatory and always applied as a relational filter *after* the FTS
    match — FTS result rows carry no project_id of their own (Section
    9: "FTS result IDs are always joined back through relational tables
    with project_id = ?; do not rely on an FTS query string to enforce
    scope")."""
    rows = conn.execute(
        """
        SELECT sc.id AS chunk_id, sc.content_id, sc.section_path,
               snippet(source_chunks_fts, 0, '[', ']', '...', 10) AS snippet
        FROM source_chunks_fts
        JOIN source_chunks sc ON sc.rowid = source_chunks_fts.rowid
        WHERE source_chunks_fts MATCH ? AND sc.project_id = ?
        ORDER BY rank
        LIMIT ?
        """,
        (_phrase_query(query_text), project_id, limit),
    ).fetchall()
    return [
        ChunkSearchResult(
            chunk_id=row["chunk_id"],
            content_id=row["content_id"],
            section_path=row["section_path"],
            snippet=row["snippet"],
        )
        for row in rows
    ]
