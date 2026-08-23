"""Tests for the ledger/review schema (migrations 0005-0006): fresh and
repeated migration, foreign keys, uniqueness, and CHECK constraints.
Runs the real migrations against a temporary database and exercises
constraints directly with `sqlite3` — no repository layer, matching the
convention in test_db_schema.py.
"""

from __future__ import annotations

import sqlite3

import pytest

from project_context.db.connection import connect
from project_context.db.migrations import discover_migrations, run_migrations
from project_context.ids import new_id
from project_context.timeutil import utc_now_iso


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


def _insert_project(conn, **overrides):
    project_id = overrides.pop("id", new_id())
    now = utc_now_iso()
    fields = {
        "id": project_id,
        "name": "Acme Rollout",
        "objective": "Ship the pilot",
        "status": "active",
        "created_at": now,
        "updated_at": now,
        **overrides,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO projects ({columns}) VALUES ({placeholders})", list(fields.values()))
    return project_id


def _insert_source(conn, project_id, **overrides):
    source_id = overrides.pop("id", new_id())
    now = utc_now_iso()
    fields = {
        "id": source_id,
        "project_id": project_id,
        "kind": "manual",
        "display_name": "Manual uploads",
        "created_at": now,
        "updated_at": now,
        **overrides,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO sources ({columns}) VALUES ({placeholders})", list(fields.values()))
    return source_id


def _insert_artifact(conn, project_id, source_id, **overrides):
    artifact_id = overrides.pop("id", new_id())
    now = utc_now_iso()
    fields = {
        "id": artifact_id,
        "project_id": project_id,
        "source_id": source_id,
        "external_id": overrides.pop("external_id", new_id()),
        "artifact_type": "manual_text",
        "created_at": now,
        "updated_at": now,
        **overrides,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO source_artifacts ({columns}) VALUES ({placeholders})", list(fields.values())
    )
    return artifact_id


def _insert_content(conn, project_id, artifact_id, **overrides):
    content_id = overrides.pop("id", new_id())
    fields = {
        "id": content_id,
        "project_id": project_id,
        "artifact_id": artifact_id,
        "version_key": overrides.pop("version_key", "v1"),
        "sha256": overrides.pop("sha256", "a" * 64),
        "normalized_text": "Hello world.",
        "parse_status": "parsed",
        "created_at": utc_now_iso(),
        **overrides,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO source_contents ({columns}) VALUES ({placeholders})", list(fields.values())
    )
    return content_id


def _insert_chunk(conn, project_id, content_id, **overrides):
    chunk_id = overrides.pop("id", new_id())
    fields = {
        "id": chunk_id,
        "project_id": project_id,
        "content_id": content_id,
        "ordinal": 0,
        "text": "Hello world.",
        "char_start": 0,
        "char_end": 12,
        "sha256": overrides.pop("sha256", "b" * 64),
        "created_at": utc_now_iso(),
        **overrides,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO source_chunks ({columns}) VALUES ({placeholders})", list(fields.values())
    )
    return chunk_id


def _insert_observation(conn, project_id, content_id, chunk_id, **overrides):
    observation_id = overrides.pop("id", new_id())
    fields = {
        "id": observation_id,
        "project_id": project_id,
        "content_id": content_id,
        "chunk_id": chunk_id,
        "kind": "commitment",
        "subject": "Priya",
        "statement": "Priya will send the report by Friday.",
        "explicitness": "explicit",
        "fingerprint": overrides.pop("fingerprint", new_id()),
        "created_at": utc_now_iso(),
        **overrides,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO observations ({columns}) VALUES ({placeholders})", list(fields.values())
    )
    return observation_id


def _insert_ledger_item(conn, project_id, **overrides):
    item_id = overrides.pop("id", new_id())
    now = utc_now_iso()
    fields = {
        "id": item_id,
        "project_id": project_id,
        "kind": "commitment",
        "canonical_title": "Send the report",
        "status": "open",
        "created_at": now,
        "updated_at": now,
        **overrides,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO ledger_items ({columns}) VALUES ({placeholders})", list(fields.values())
    )
    return item_id


@pytest.fixture
def content_fixture(conn):
    """A full project -> source -> artifact -> content -> chunk chain,
    ready for an observation/evidence_link to reference."""
    project_id = _insert_project(conn)
    source_id = _insert_source(conn, project_id)
    artifact_id = _insert_artifact(conn, project_id, source_id)
    content_id = _insert_content(conn, project_id, artifact_id)
    chunk_id = _insert_chunk(conn, project_id, content_id)
    return project_id, content_id, chunk_id


# --- fresh / repeated migration ------------------------------------------


def test_fresh_migration_creates_every_new_table(conn):
    for table in (
        "people",
        "person_aliases",
        "project_people",
        "observations",
        "ledger_items",
        "ledger_versions",
        "evidence_links",
        "proposed_mutations",
        "reviews",
        "corrections",
        "ledger_items_fts",
        "observations_fts",
    ):
        conn.execute(f"SELECT * FROM {table} LIMIT 0")  # raises if missing


def test_repeated_migration_against_up_to_date_database_is_a_no_op(tmp_path, migrations_dir):
    connection = connect(tmp_path / "repeat.db")
    try:
        first = run_migrations(connection, migrations_dir)
        expected = [m.version for m in discover_migrations(migrations_dir)]
        assert [m.version for m in first] == expected
        second = run_migrations(connection, migrations_dir)
        assert second == []
        # Every new table must still be queryable after the no-op rerun.
        for table in ("people", "observations", "ledger_items", "ledger_versions", "reviews"):
            connection.execute(f"SELECT * FROM {table} LIMIT 0")
    finally:
        connection.close()


# --- foreign keys ---------------------------------------------------------


def test_observation_requires_a_real_content_and_chunk(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_observation(conn, project_id, "nonexistent-content", "nonexistent-chunk")


def test_ledger_item_owner_person_id_requires_a_real_person(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ledger_item(conn, project_id, owner_person_id="nonexistent-person")


def test_evidence_link_requires_real_content(conn, content_fixture):
    project_id, content_id, chunk_id = content_fixture
    observation_id = _insert_observation(conn, project_id, content_id, chunk_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO evidence_links "
            "(id, project_id, target_type, target_id, content_id, char_start, char_end, quote, "
            " support_role, created_at) "
            "VALUES (?, ?, 'observation', ?, 'nonexistent-content', 0, 5, 'hello', 'supports', ?)",
            (new_id(), project_id, observation_id, utc_now_iso()),
        )


def test_ledger_version_requires_a_real_ledger_item(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ledger_versions "
            "(id, project_id, ledger_item_id, version_no, canonical_title, status, "
            " transition_type, valid_from, created_at) "
            "VALUES (?, ?, 'nonexistent-item', 1, 'Title', 'open', 'create', ?, ?)",
            (new_id(), project_id, utc_now_iso(), utc_now_iso()),
        )


def test_proposed_mutation_requires_a_real_observation(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposed_mutations "
            "(id, project_id, observation_id, action, created_at) "
            "VALUES (?, ?, 'nonexistent-observation', 'create', ?)",
            (new_id(), project_id, utc_now_iso()),
        )


def test_review_requires_a_real_proposal(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reviews (id, project_id, proposal_id, action, actor, reviewed_at) "
            "VALUES (?, ?, 'nonexistent-proposal', 'accept', 'local-user', ?)",
            (new_id(), project_id, utc_now_iso()),
        )


def test_correction_requires_a_real_project(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO corrections "
            "(id, project_id, target_type, target_id, field_name, reason_code, materiality, "
            " error_signature, actor, created_at) "
            "VALUES (?, 'nonexistent-project', 'observation', 'x', 'owner_text', 'wrong_owner', "
            " 'minor', 'sig', 'local-user', ?)",
            (new_id(), utc_now_iso()),
        )


def test_person_alias_requires_a_real_person(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO person_aliases "
            "(id, person_id, alias_type, alias_value, normalized_value, created_at) "
            "VALUES (?, 'nonexistent-person', 'email', 'a@example.com', 'a@example.com', ?)",
            (new_id(), utc_now_iso()),
        )


def test_project_people_requires_a_real_person(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO project_people "
            "(id, project_id, person_id, is_internal, status, first_seen_at, last_seen_at, "
            " created_at, updated_at) "
            "VALUES (?, ?, 'nonexistent-person', 0, 'active', ?, ?, ?, ?)",
            (new_id(), project_id, utc_now_iso(), utc_now_iso(), utc_now_iso(), utc_now_iso()),
        )


# --- uniqueness -----------------------------------------------------------


def test_observation_fingerprint_is_unique(conn, content_fixture):
    project_id, content_id, chunk_id = content_fixture
    _insert_observation(conn, project_id, content_id, chunk_id, fingerprint="dupe-fp")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_observation(conn, project_id, content_id, chunk_id, fingerprint="dupe-fp")


def test_person_email_alias_is_unique_across_people(conn):
    conn.execute(
        "INSERT INTO people (id, display_name, status, created_at, updated_at) "
        "VALUES (?, 'Priya', 'active', ?, ?)",
        (new_id(), utc_now_iso(), utc_now_iso()),
    )
    person_a = conn.execute("SELECT id FROM people").fetchone()["id"]
    conn.execute(
        "INSERT INTO people (id, display_name, status, created_at, updated_at) "
        "VALUES (?, 'Priya B', 'active', ?, ?)",
        (new_id(), utc_now_iso(), utc_now_iso()),
    )
    person_b = conn.execute("SELECT id FROM people WHERE display_name = 'Priya B'").fetchone()["id"]

    conn.execute(
        "INSERT INTO person_aliases (id, person_id, alias_type, alias_value, normalized_value, "
        "created_at) VALUES (?, ?, 'email', 'p@example.com', 'p@example.com', ?)",
        (new_id(), person_a, utc_now_iso()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO person_aliases (id, person_id, alias_type, alias_value, "
            "normalized_value, created_at) "
            "VALUES (?, ?, 'email', 'p@example.com', 'p@example.com', ?)",
            (new_id(), person_b, utc_now_iso()),
        )


def test_person_name_alias_allows_ambiguity_across_people(conn):
    """Unlike email, two different people may share the same normalized
    name alias — ambiguity is resolved by
    project_context.db.people_repository.resolve_person, not a DB
    constraint."""
    for _ in range(2):
        conn.execute(
            "INSERT INTO people (id, display_name, status, created_at, updated_at) "
            "VALUES (?, 'Alex', 'active', ?, ?)",
            (new_id(), utc_now_iso(), utc_now_iso()),
        )
    person_ids = [row["id"] for row in conn.execute("SELECT id FROM people").fetchall()]
    for person_id in person_ids:
        conn.execute(
            "INSERT INTO person_aliases (id, person_id, alias_type, alias_value, "
            "normalized_value, created_at) VALUES (?, ?, 'name', 'Alex', 'alex', ?)",
            (new_id(), person_id, utc_now_iso()),
        )  # must not raise
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM person_aliases WHERE normalized_value = 'alex'"
    ).fetchone()["n"]
    assert count == 2


def test_project_people_role_is_unique_per_project_and_person(conn):
    project_id = _insert_project(conn)
    conn.execute(
        "INSERT INTO people (id, display_name, status, created_at, updated_at) "
        "VALUES (?, 'Priya', 'active', ?, ?)",
        (new_id(), utc_now_iso(), utc_now_iso()),
    )
    person_id = conn.execute("SELECT id FROM people").fetchone()["id"]
    fields = dict(
        id=new_id(),
        project_id=project_id,
        person_id=person_id,
        role="sponsor",
        is_internal=0,
        status="active",
        first_seen_at=utc_now_iso(),
        last_seen_at=utc_now_iso(),
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    insert_sql = f"INSERT INTO project_people ({columns}) VALUES ({placeholders})"
    conn.execute(insert_sql, list(fields.values()))
    with pytest.raises(sqlite3.IntegrityError):
        fields["id"] = new_id()
        conn.execute(insert_sql, list(fields.values()))


def test_only_one_pending_proposal_per_observation(conn, content_fixture):
    project_id, content_id, chunk_id = content_fixture
    observation_id = _insert_observation(conn, project_id, content_id, chunk_id)
    conn.execute(
        "INSERT INTO proposed_mutations (id, project_id, observation_id, action, status, "
        "created_at) VALUES (?, ?, ?, 'create', 'pending', ?)",
        (new_id(), project_id, observation_id, utc_now_iso()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposed_mutations (id, project_id, observation_id, action, status, "
            "created_at) VALUES (?, ?, ?, 'create', 'pending', ?)",
            (new_id(), project_id, observation_id, utc_now_iso()),
        )


def test_a_second_pending_proposal_is_allowed_once_the_first_is_resolved(conn, content_fixture):
    project_id, content_id, chunk_id = content_fixture
    observation_id = _insert_observation(conn, project_id, content_id, chunk_id)
    conn.execute(
        "INSERT INTO proposed_mutations (id, project_id, observation_id, action, status, "
        "created_at) VALUES (?, ?, ?, 'create', 'rejected', ?)",
        (new_id(), project_id, observation_id, utc_now_iso()),
    )
    conn.execute(
        "INSERT INTO proposed_mutations (id, project_id, observation_id, action, status, "
        "created_at) VALUES (?, ?, ?, 'create', 'pending', ?)",
        (new_id(), project_id, observation_id, utc_now_iso()),
    )  # must not raise


def test_only_one_review_per_proposal(conn, content_fixture):
    project_id, content_id, chunk_id = content_fixture
    observation_id = _insert_observation(conn, project_id, content_id, chunk_id)
    proposal_id = new_id()
    conn.execute(
        "INSERT INTO proposed_mutations (id, project_id, observation_id, action, status, "
        "created_at) VALUES (?, ?, ?, 'create', 'accepted', ?)",
        (proposal_id, project_id, observation_id, utc_now_iso()),
    )
    conn.execute(
        "INSERT INTO reviews (id, project_id, proposal_id, action, actor, reviewed_at) "
        "VALUES (?, ?, ?, 'accept', 'local-user', ?)",
        (new_id(), project_id, proposal_id, utc_now_iso()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reviews (id, project_id, proposal_id, action, actor, reviewed_at) "
            "VALUES (?, ?, ?, 'accept', 'local-user', ?)",
            (new_id(), project_id, proposal_id, utc_now_iso()),
        )


def test_ledger_version_no_is_unique_per_item(conn):
    project_id = _insert_project(conn)
    item_id = _insert_ledger_item(conn, project_id)
    conn.execute(
        "INSERT INTO ledger_versions (id, project_id, ledger_item_id, version_no, "
        "canonical_title, status, transition_type, valid_from, created_at) "
        "VALUES (?, ?, ?, 1, 'Title', 'open', 'create', ?, ?)",
        (new_id(), project_id, item_id, utc_now_iso(), utc_now_iso()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ledger_versions (id, project_id, ledger_item_id, version_no, "
            "canonical_title, status, transition_type, valid_from, created_at) "
            "VALUES (?, ?, ?, 1, 'Title v2', 'open', 'update', ?, ?)",
            (new_id(), project_id, item_id, utc_now_iso(), utc_now_iso()),
        )


# --- CHECK constraints ------------------------------------------------------


def test_ledger_item_rejects_unknown_kind(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ledger_item(conn, project_id, kind="not_a_real_kind")


def test_ledger_item_rejects_status_invalid_for_kind(conn):
    """Deviation 6 (migrations/0005 header): the compound CHECK mirrors
    project_context.domain.ledger.VALID_STATUSES_BY_KIND — a commitment
    can never be 'resolved'."""
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ledger_item(conn, project_id, kind="commitment", status="resolved")


def test_ledger_item_accepts_status_valid_for_kind(conn):
    project_id = _insert_project(conn)
    _insert_ledger_item(conn, project_id, kind="risk", status="resolved")  # must not raise


# --- migration 0007: item-level supersession links --------------------------


def test_ledger_item_superseded_by_item_id_requires_a_real_item(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ledger_item(conn, project_id, superseded_by_item_id="nonexistent-item")


def test_ledger_item_supersedes_item_id_requires_a_real_item(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ledger_item(conn, project_id, supersedes_item_id="nonexistent-item")


def test_ledger_item_supersession_links_accept_a_real_item_pair(conn):
    project_id = _insert_project(conn)
    old_id = _insert_ledger_item(conn, project_id, kind="decision", status="active")
    new_id_ = _insert_ledger_item(
        conn, project_id, kind="decision", status="active", supersedes_item_id=old_id
    )
    conn.execute(
        "UPDATE ledger_items SET superseded_by_item_id = ? WHERE id = ?", (new_id_, old_id)
    )
    row = conn.execute(
        "SELECT superseded_by_item_id FROM ledger_items WHERE id = ?", (old_id,)
    ).fetchone()
    assert row["superseded_by_item_id"] == new_id_


def test_observation_rejects_unknown_kind(conn, content_fixture):
    project_id, content_id, chunk_id = content_fixture
    with pytest.raises(sqlite3.IntegrityError):
        _insert_observation(conn, project_id, content_id, chunk_id, kind="not_a_real_kind")


def test_observation_rejects_unknown_explicitness(conn, content_fixture):
    project_id, content_id, chunk_id = content_fixture
    with pytest.raises(sqlite3.IntegrityError):
        _insert_observation(conn, project_id, content_id, chunk_id, explicitness="pretty_sure")


def test_observation_polarity_defaults_to_positive(conn, content_fixture):
    project_id, content_id, chunk_id = content_fixture
    observation_id = _insert_observation(conn, project_id, content_id, chunk_id)
    row = conn.execute(
        "SELECT polarity FROM observations WHERE id = ?", (observation_id,)
    ).fetchone()
    assert row["polarity"] == "positive"


def test_correction_rejects_unknown_reason_code(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO corrections "
            "(id, project_id, target_type, target_id, field_name, reason_code, materiality, "
            " error_signature, actor, created_at) "
            "VALUES (?, ?, 'observation', 'x', 'owner_text', 'not_a_real_reason', 'minor', "
            " 'sig', 'local-user', ?)",
            (new_id(), project_id, utc_now_iso()),
        )


def test_correction_rejects_unknown_materiality(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO corrections "
            "(id, project_id, target_type, target_id, field_name, reason_code, materiality, "
            " error_signature, actor, created_at) "
            "VALUES (?, ?, 'observation', 'x', 'owner_text', 'wrong_owner', 'severe', "
            " 'sig', 'local-user', ?)",
            (new_id(), project_id, utc_now_iso()),
        )


def test_evidence_link_rejects_reversed_span(conn, content_fixture):
    project_id, content_id, chunk_id = content_fixture
    observation_id = _insert_observation(conn, project_id, content_id, chunk_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO evidence_links "
            "(id, project_id, target_type, target_id, content_id, chunk_id, char_start, "
            " char_end, quote, support_role, created_at) "
            "VALUES (?, ?, 'observation', ?, ?, ?, 10, 5, 'hello', 'supports', ?)",
            (new_id(), project_id, observation_id, content_id, chunk_id, utc_now_iso()),
        )
