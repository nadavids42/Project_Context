"""Observation repository: direct SQL access to `observations` and its
FTS5 index.

Pure data access, like the other repositories. Enforces the two Prompt 6
invariants that matter at this layer:

- **Immutability**: no function in this module updates any propositional
  field (`kind`, `subject`, `statement`, `owner_text`, `date_value`,
  `date_text`, `polarity`, `explicitness`, or evidence). `update_status`
  is the one narrow exception — Section 9's own status vocabulary
  (`valid`/`invalid`/`duplicate`/`rejected`/`reconciled`) requires a
  mutable projection field even though "Never mutate proposition" (that
  status column is not part of the proposition itself).
- **Exact-fingerprint deduplication**: `insert_observation` is
  idempotent — re-inserting an observation whose
  `project_context.domain.observations.compute_fingerprint` matches an
  existing row returns that row rather than creating a duplicate,
  mirroring the same check-then-insert idempotence pattern
  `project_context.db.evidence_repository` uses for content SHA-256.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from project_context.domain.observations import Observation, ObservationStatus, compute_fingerprint
from project_context.ids import new_id
from project_context.timeutil import utc_now_iso


def _row_to_observation(row: sqlite3.Row) -> Observation:
    return Observation(
        id=row["id"],
        project_id=row["project_id"],
        content_id=row["content_id"],
        chunk_id=row["chunk_id"],
        kind=row["kind"],
        subject=row["subject"],
        statement=row["statement"],
        owner_text=row["owner_text"],
        owner_person_id=row["owner_person_id"],
        date_value=row["date_value"],
        date_text=row["date_text"],
        polarity=row["polarity"],
        explicitness=row["explicitness"],
        model_id=row["model_id"],
        prompt_version=row["prompt_version"],
        schema_version=row["schema_version"],
        fingerprint=row["fingerprint"],
        status=ObservationStatus(row["status"]),
        created_at=row["created_at"],
    )


def get_observation(
    conn: sqlite3.Connection, project_id: str, observation_id: str
) -> Observation | None:
    row = conn.execute(
        "SELECT * FROM observations WHERE id = ? AND project_id = ?",
        (observation_id, project_id),
    ).fetchone()
    return _row_to_observation(row) if row is not None else None


def get_observation_by_fingerprint(
    conn: sqlite3.Connection, fingerprint: str
) -> Observation | None:
    row = conn.execute(
        "SELECT * FROM observations WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    return _row_to_observation(row) if row is not None else None


def insert_observation(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    content_id: str,
    chunk_id: str,
    kind: str,
    subject: str,
    statement: str,
    evidence_spans: list[tuple[str, int, int]],
    owner_text: str | None = None,
    owner_person_id: str | None = None,
    date_value: str | None = None,
    date_text: str | None = None,
    polarity: str = "positive",
    explicitness: str,
    model_id: str | None = None,
    prompt_version: str | None = None,
    schema_version: str | None = None,
    status: ObservationStatus = ObservationStatus.VALID,
) -> tuple[Observation, bool]:
    """Insert one observation, or return the existing row if an
    identical one (by exact fingerprint) already exists.

    Returns `(observation, created)` — `created` is `False` when an
    existing fingerprint match was returned instead of inserting.
    `evidence_spans` are `(chunk_id, char_start, char_end)` triples used
    only to compute the fingerprint here; persisting them as
    `evidence_links` rows is the caller's job (see
    `project_context.services.observations`).
    """
    fingerprint = compute_fingerprint(
        content_id=content_id, kind=kind, statement=statement, evidence_spans=evidence_spans
    )
    existing = get_observation_by_fingerprint(conn, fingerprint)
    if existing is not None:
        return existing, False

    observation_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO observations
            (id, project_id, content_id, chunk_id, kind, subject, statement, owner_text,
             owner_person_id, date_value, date_text, polarity, explicitness, model_id,
             prompt_version, schema_version, fingerprint, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            project_id,
            content_id,
            chunk_id,
            kind,
            subject,
            statement,
            owner_text,
            owner_person_id,
            date_value,
            date_text,
            polarity,
            explicitness,
            model_id,
            prompt_version,
            schema_version,
            fingerprint,
            status.value,
            now,
        ),
    )
    created = get_observation(conn, project_id, observation_id)
    assert created is not None
    return created, True


def update_status(
    conn: sqlite3.Connection, project_id: str, observation_id: str, status: ObservationStatus
) -> Observation | None:
    """Change only `status` — every propositional field is write-once
    (see module docstring)."""
    conn.execute(
        "UPDATE observations SET status = ? WHERE id = ? AND project_id = ?",
        (status.value, observation_id, project_id),
    )
    return get_observation(conn, project_id, observation_id)


def list_observations_for_project(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    kind: str | None = None,
    status: ObservationStatus | None = None,
) -> list[Observation]:
    query = "SELECT * FROM observations WHERE project_id = ?"
    params: list[object] = [project_id]
    if kind is not None:
        query += " AND kind = ?"
        params.append(kind)
    if status is not None:
        query += " AND status = ?"
        params.append(status.value)
    query += " ORDER BY created_at ASC"
    rows = conn.execute(query, params).fetchall()
    return [_row_to_observation(row) for row in rows]


# --- full-text search -------------------------------------------------------


@dataclass(frozen=True)
class ObservationSearchResult:
    observation_id: str
    subject_snippet: str
    statement_snippet: str


def search_observations(
    conn: sqlite3.Connection, project_id: str, query_text: str, *, limit: int = 20
) -> list[ObservationSearchResult]:
    """Full-text search over one project's observations. `project_id` is
    mandatory and applied as a relational filter *after* the FTS match —
    FTS result rows carry no project_id of their own (same rule as
    `evidence_repository.search_chunks`)."""
    phrase = '"' + query_text.replace('"', '""') + '"'
    rows = conn.execute(
        """
        SELECT o.id AS observation_id,
               snippet(observations_fts, 0, '[', ']', '...', 6) AS subject_snippet,
               snippet(observations_fts, 1, '[', ']', '...', 10) AS statement_snippet
        FROM observations_fts
        JOIN observations o ON o.rowid = observations_fts.rowid
        WHERE observations_fts MATCH ? AND o.project_id = ?
        ORDER BY rank
        LIMIT ?
        """,
        (phrase, project_id, limit),
    ).fetchall()
    return [
        ObservationSearchResult(
            observation_id=row["observation_id"],
            subject_snippet=row["subject_snippet"],
            statement_snippet=row["statement_snippet"],
        )
        for row in rows
    ]
