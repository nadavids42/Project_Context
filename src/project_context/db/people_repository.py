"""People/alias/stakeholder repository: direct SQL access to `people`,
`person_aliases`, and `project_people`.

Pure data access, like the other repositories — no transaction
management (callers use `project_context.db.connection.transaction`).
`people`/`person_aliases` are not project-scoped (see
`project_context.domain.people`); every `project_people` function
requires `project_id` explicitly.

`resolve_person` is Prompt 6's required "exact-email resolution first,
explicit ambiguous-name result" behavior: an email match (against either
`people.primary_email` or a known `email` alias) is always tried first
and, if found, is unambiguous by construction — a normalized email alias
is unique per `uq_person_aliases_email_normalized`. A name/speaker-label
match may legitimately return more than one distinct person; that is
reported as `"ambiguous"`, never silently resolved to the first match.
"""

from __future__ import annotations

import sqlite3

from project_context.domain.people import (
    AliasType,
    Person,
    PersonAlias,
    PersonResolution,
    PersonStatus,
    ProjectPerson,
    ProjectPersonStatus,
)
from project_context.ids import new_id
from project_context.spans import normalize_whitespace
from project_context.timeutil import utc_now_iso


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _normalize_name(name: str) -> str:
    return normalize_whitespace(name).lower()


def _row_to_person(row: sqlite3.Row) -> Person:
    return Person(
        id=row["id"],
        display_name=row["display_name"],
        primary_email=row["primary_email"],
        organization=row["organization"],
        status=PersonStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_alias(row: sqlite3.Row) -> PersonAlias:
    return PersonAlias(
        id=row["id"],
        person_id=row["person_id"],
        alias_type=AliasType(row["alias_type"]),
        alias_value=row["alias_value"],
        normalized_value=row["normalized_value"],
        source=row["source"],
        created_at=row["created_at"],
    )


def _row_to_project_person(row: sqlite3.Row) -> ProjectPerson:
    return ProjectPerson(
        id=row["id"],
        project_id=row["project_id"],
        person_id=row["person_id"],
        role=row["role"],
        organization=row["organization"],
        is_internal=bool(row["is_internal"]),
        status=ProjectPersonStatus(row["status"]),
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        evidence_link_id=row["evidence_link_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# --- people ---------------------------------------------------------------


def create_person(
    conn: sqlite3.Connection,
    *,
    display_name: str,
    primary_email: str | None = None,
    organization: str | None = None,
    status: PersonStatus = PersonStatus.ACTIVE,
) -> Person:
    person_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO people (id, display_name, primary_email, organization, status,
                             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (person_id, display_name, primary_email, organization, status.value, now, now),
    )
    person = get_person(conn, person_id)
    assert person is not None
    return person


def get_person(conn: sqlite3.Connection, person_id: str) -> Person | None:
    row = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    return _row_to_person(row) if row is not None else None


def get_person_by_email(conn: sqlite3.Connection, email: str) -> Person | None:
    """Exact-match lookup against `people.primary_email` only (normalized).
    Use `resolve_person` for the full email-then-alias resolution flow."""
    normalized = _normalize_email(email)
    row = conn.execute(
        "SELECT * FROM people WHERE lower(trim(primary_email)) = ?", (normalized,)
    ).fetchone()
    return _row_to_person(row) if row is not None else None


# --- aliases ----------------------------------------------------------------


def add_alias(
    conn: sqlite3.Connection,
    person_id: str,
    *,
    alias_type: AliasType,
    alias_value: str,
    source: str | None = None,
) -> PersonAlias:
    """Attach one alias to a person. Raises `sqlite3.IntegrityError` if
    `alias_type='email'` and the normalized value is already claimed by
    a different person (`uq_person_aliases_email_normalized`) — an email
    alias must be unambiguous by construction."""
    if alias_type is AliasType.EMAIL:
        normalized = _normalize_email(alias_value)
    else:
        normalized = _normalize_name(alias_value)
    alias_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO person_aliases
            (id, person_id, alias_type, alias_value, normalized_value, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (alias_id, person_id, alias_type.value, alias_value, normalized, source, now),
    )
    row = conn.execute("SELECT * FROM person_aliases WHERE id = ?", (alias_id,)).fetchone()
    assert row is not None
    return _row_to_alias(row)


def list_aliases_for_person(conn: sqlite3.Connection, person_id: str) -> list[PersonAlias]:
    rows = conn.execute(
        "SELECT * FROM person_aliases WHERE person_id = ? ORDER BY created_at ASC", (person_id,)
    ).fetchall()
    return [_row_to_alias(row) for row in rows]


# --- resolution -------------------------------------------------------------


def resolve_person(
    conn: sqlite3.Connection, *, email: str | None = None, name: str | None = None
) -> PersonResolution:
    """Resolve an email and/or name to a canonical person. Email is
    always tried first; name is only consulted if no email was given or
    the email did not match anything known."""
    if email:
        by_email = _resolve_by_email(conn, email)
        if by_email.outcome != "unknown":
            return by_email
    if name:
        return _resolve_by_name(conn, name)
    return PersonResolution(outcome="unknown")


def _resolve_by_email(conn: sqlite3.Connection, email: str) -> PersonResolution:
    person = get_person_by_email(conn, email)
    if person is not None:
        return PersonResolution(outcome="resolved", person_id=person.id)

    normalized = _normalize_email(email)
    row = conn.execute(
        "SELECT person_id FROM person_aliases WHERE alias_type = 'email' AND normalized_value = ?",
        (normalized,),
    ).fetchone()
    if row is not None:
        return PersonResolution(outcome="resolved", person_id=row["person_id"])
    return PersonResolution(outcome="unknown")


def _resolve_by_name(conn: sqlite3.Connection, name: str) -> PersonResolution:
    normalized = _normalize_name(name)
    rows = conn.execute(
        "SELECT DISTINCT person_id FROM person_aliases "
        "WHERE alias_type IN ('name', 'speaker_label') AND normalized_value = ?",
        (normalized,),
    ).fetchall()
    person_ids = [row["person_id"] for row in rows]
    if len(person_ids) == 1:
        return PersonResolution(outcome="resolved", person_id=person_ids[0])
    if len(person_ids) > 1:
        return PersonResolution(outcome="ambiguous", candidate_person_ids=tuple(person_ids))
    return PersonResolution(outcome="unknown")


# --- project_people ----------------------------------------------------------


def upsert_project_person(
    conn: sqlite3.Connection,
    project_id: str,
    person_id: str,
    *,
    role: str | None = None,
    organization: str | None = None,
    is_internal: bool = False,
    status: ProjectPersonStatus = ProjectPersonStatus.ACTIVE,
    evidence_link_id: str | None = None,
    seen_at: str | None = None,
) -> ProjectPerson:
    """Create the (project, person, role) stakeholder row, or update
    `last_seen_at`/status/organization if it already exists
    (`uq_project_people_project_person_role`)."""
    now = seen_at or utc_now_iso()
    existing = get_project_person(conn, project_id, person_id, role)
    if existing is not None:
        conn.execute(
            """
            UPDATE project_people
            SET organization = ?, is_internal = ?, status = ?, last_seen_at = ?,
                evidence_link_id = COALESCE(?, evidence_link_id), updated_at = ?
            WHERE id = ?
            """,
            (
                organization,
                1 if is_internal else 0,
                status.value,
                now,
                evidence_link_id,
                now,
                existing.id,
            ),
        )
        updated = _get_project_person_by_id(conn, existing.id)
        assert updated is not None
        return updated

    row_id = new_id()
    conn.execute(
        """
        INSERT INTO project_people
            (id, project_id, person_id, role, organization, is_internal, status,
             first_seen_at, last_seen_at, evidence_link_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id,
            project_id,
            person_id,
            role,
            organization,
            1 if is_internal else 0,
            status.value,
            now,
            now,
            evidence_link_id,
            now,
            now,
        ),
    )
    created = _get_project_person_by_id(conn, row_id)
    assert created is not None
    return created


def _get_project_person_by_id(conn: sqlite3.Connection, row_id: str) -> ProjectPerson | None:
    row = conn.execute("SELECT * FROM project_people WHERE id = ?", (row_id,)).fetchone()
    return _row_to_project_person(row) if row is not None else None


def get_project_person(
    conn: sqlite3.Connection, project_id: str, person_id: str, role: str | None
) -> ProjectPerson | None:
    row = conn.execute(
        "SELECT * FROM project_people WHERE project_id = ? AND person_id = ? AND role IS ?",
        (project_id, person_id, role),
    ).fetchone()
    return _row_to_project_person(row) if row is not None else None


def list_project_people(conn: sqlite3.Connection, project_id: str) -> list[ProjectPerson]:
    rows = conn.execute(
        "SELECT * FROM project_people WHERE project_id = ? ORDER BY first_seen_at ASC",
        (project_id,),
    ).fetchall()
    return [_row_to_project_person(row) for row in rows]
