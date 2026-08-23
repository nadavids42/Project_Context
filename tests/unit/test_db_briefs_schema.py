"""Tests for the briefs schema (migration 0008): foreign keys,
uniqueness, and CHECK constraints. Runs the real migrations against a
temporary database and exercises constraints directly with `sqlite3` —
no repository layer, matching the convention in test_db_ledger_schema.py.
"""

from __future__ import annotations

import sqlite3

import pytest

from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
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


def _insert_brief(conn, project_id, **overrides):
    brief_id = overrides.pop("id", new_id())
    fields = {
        "id": brief_id,
        "project_id": project_id,
        "brief_type": "current_project",
        "cutoff_at": utc_now_iso(),
        "input_snapshot_json": "{}",
        "status": "generating",
        "created_at": utc_now_iso(),
        **overrides,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO generated_briefs ({columns}) VALUES ({placeholders})", list(fields.values())
    )
    return brief_id


def _insert_claim(conn, project_id, brief_id, **overrides):
    claim_id = overrides.pop("id", new_id())
    fields = {
        "id": claim_id,
        "project_id": project_id,
        "brief_id": brief_id,
        "section": "open_commitments",
        "ordinal": overrides.pop("ordinal", 0),
        "claim_text": "Something is true.",
        "claim_type": "fact",
        "cited_fact_ids_json": "[]",
        "validation_status": "valid",
        "created_at": utc_now_iso(),
        **overrides,
    }
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO brief_claims ({columns}) VALUES ({placeholders})", list(fields.values())
    )
    return claim_id


def test_fresh_migration_creates_both_tables(conn):
    for table in ("generated_briefs", "brief_claims"):
        conn.execute(f"SELECT 1 FROM {table}")  # must not raise


def test_generated_brief_requires_a_real_project(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_brief(conn, "nonexistent-project")


def test_generated_brief_rejects_unknown_brief_type(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_brief(conn, project_id, brief_type="not_a_real_type")


def test_generated_brief_rejects_unknown_status(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_brief(conn, project_id, status="not_a_real_status")


def test_generated_brief_accepts_every_valid_status(conn):
    project_id = _insert_project(conn)
    for status in ("generating", "valid", "failed", "superseded"):
        _insert_brief(conn, project_id, status=status)  # must not raise


def test_generated_brief_rejects_negative_token_counts(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_brief(conn, project_id, input_tokens=-1)


def test_brief_claim_requires_a_real_brief(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_claim(conn, project_id, "nonexistent-brief")


def test_brief_claim_rejects_unknown_claim_type(conn):
    project_id = _insert_project(conn)
    brief_id = _insert_brief(conn, project_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_claim(conn, project_id, brief_id, claim_type="not_a_real_type")


def test_brief_claim_rejects_unknown_validation_status(conn):
    project_id = _insert_project(conn)
    brief_id = _insert_brief(conn, project_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_claim(conn, project_id, brief_id, validation_status="not_a_real_status")


def test_brief_claim_rejects_blank_claim_text(conn):
    project_id = _insert_project(conn)
    brief_id = _insert_brief(conn, project_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_claim(conn, project_id, brief_id, claim_text="   ")


def test_brief_claim_ordinal_is_unique_per_brief(conn):
    project_id = _insert_project(conn)
    brief_id = _insert_brief(conn, project_id)
    _insert_claim(conn, project_id, brief_id, ordinal=0)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_claim(conn, project_id, brief_id, ordinal=0)


def test_brief_claim_requires_a_real_ledger_item_when_given(conn):
    project_id = _insert_project(conn)
    brief_id = _insert_brief(conn, project_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_claim(conn, project_id, brief_id, ledger_item_id="nonexistent-item")


def test_brief_claim_ledger_item_is_optional(conn):
    project_id = _insert_project(conn)
    brief_id = _insert_brief(conn, project_id)
    _insert_claim(conn, project_id, brief_id, ledger_item_id=None)  # must not raise
