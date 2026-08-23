"""Tests for the evidence-foundation schema: FKs, uniqueness, status checks.

These run the real `migrations/0001_evidence_foundation.sql` against a
temporary database, then exercise constraints directly with `sqlite3` —
no repository layer exists yet (out of scope for this prompt).
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
    conn.execute(
        f"INSERT INTO projects ({columns}) VALUES ({placeholders})",
        list(fields.values()),
    )
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
    conn.execute(
        f"INSERT INTO sources ({columns}) VALUES ({placeholders})",
        list(fields.values()),
    )
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
        f"INSERT INTO source_artifacts ({columns}) VALUES ({placeholders})",
        list(fields.values()),
    )
    return artifact_id


# --- foreign keys -----------------------------------------------------


def test_foreign_keys_are_enforced_on_insert(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_source(conn, project_id="does-not-exist")


def test_foreign_keys_prevent_deleting_a_referenced_project(conn):
    project_id = _insert_project(conn)
    _insert_source(conn, project_id)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def test_circular_reference_between_artifacts_and_contents_is_enforced(conn):
    project_id = _insert_project(conn)
    source_id = _insert_source(conn, project_id)
    artifact_id = _insert_artifact(conn, project_id, source_id)

    # A content row must reference a real artifact.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_contents "
            "(id, project_id, artifact_id, version_key, sha256, parse_status, created_at) "
            "VALUES (?, ?, 'nonexistent-artifact', 'v1', ?, 'pending', ?)",
            (new_id(), project_id, "a" * 64, utc_now_iso()),
        )

    # A real content row can then be pointed back at by the artifact.
    content_id = new_id()
    conn.execute(
        "INSERT INTO source_contents "
        "(id, project_id, artifact_id, version_key, sha256, parse_status, created_at) "
        "VALUES (?, ?, ?, 'v1', ?, 'pending', ?)",
        (content_id, project_id, artifact_id, "a" * 64, utc_now_iso()),
    )
    conn.execute(
        "UPDATE source_artifacts SET current_content_id = ? WHERE id = ?",
        (content_id, artifact_id),
    )

    row = conn.execute(
        "SELECT current_content_id FROM source_artifacts WHERE id = ?", (artifact_id,)
    ).fetchone()
    assert row["current_content_id"] == content_id


# --- uniqueness ---------------------------------------------------------


def test_source_artifact_external_identity_is_unique_per_source(conn):
    project_id = _insert_project(conn)
    source_id = _insert_source(conn, project_id)
    _insert_artifact(conn, project_id, source_id, external_id="msg-123")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_artifact(conn, project_id, source_id, external_id="msg-123")


def test_same_external_id_is_allowed_across_different_sources(conn):
    project_id = _insert_project(conn)
    source_a = _insert_source(conn, project_id, kind="manual", display_name="Manual A")
    source_b = _insert_source(conn, project_id, kind="drive", display_name="Drive A")

    _insert_artifact(conn, project_id, source_a, external_id="shared-id")
    _insert_artifact(conn, project_id, source_b, external_id="shared-id")  # must not raise


def test_content_version_key_is_unique_per_artifact(conn):
    project_id = _insert_project(conn)
    source_id = _insert_source(conn, project_id)
    artifact_id = _insert_artifact(conn, project_id, source_id)

    conn.execute(
        "INSERT INTO source_contents "
        "(id, project_id, artifact_id, version_key, sha256, parse_status, created_at) "
        "VALUES (?, ?, ?, 'v1', ?, 'pending', ?)",
        (new_id(), project_id, artifact_id, "a" * 64, utc_now_iso()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_contents "
            "(id, project_id, artifact_id, version_key, sha256, parse_status, created_at) "
            "VALUES (?, ?, ?, 'v1', ?, 'pending', ?)",
            (new_id(), project_id, artifact_id, "b" * 64, utc_now_iso()),
        )


def test_content_sha256_is_deduplicated_per_artifact(conn):
    """Re-ingesting byte-identical content for the same artifact must not
    create a second content row — this is the idempotence guarantee
    behind FR-005/FR-009 at the storage layer."""
    project_id = _insert_project(conn)
    source_id = _insert_source(conn, project_id)
    artifact_id = _insert_artifact(conn, project_id, source_id)
    same_hash = "c" * 64

    conn.execute(
        "INSERT INTO source_contents "
        "(id, project_id, artifact_id, version_key, sha256, parse_status, created_at) "
        "VALUES (?, ?, ?, 'v1', ?, 'pending', ?)",
        (new_id(), project_id, artifact_id, same_hash, utc_now_iso()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_contents "
            "(id, project_id, artifact_id, version_key, sha256, parse_status, created_at) "
            "VALUES (?, ?, ?, 'v2', ?, 'pending', ?)",
            (new_id(), project_id, artifact_id, same_hash, utc_now_iso()),
        )


def test_source_display_name_is_unique_per_project_and_kind(conn):
    project_id = _insert_project(conn)
    _insert_source(conn, project_id, kind="drive", display_name="Client Folder")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_source(conn, project_id, kind="drive", display_name="Client Folder")


# --- status / CHECK constraints -----------------------------------------


def test_project_status_rejects_unknown_value(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_project(conn, status="not_a_real_status")


def test_project_requires_non_blank_name(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_project(conn, name="   ")


def test_source_kind_rejects_unknown_connector(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_source(conn, project_id, kind="carrier_pigeon")


def test_source_health_status_rejects_unknown_value(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_source(conn, project_id, health_status="on_fire")


def test_content_sha256_must_be_64_characters(conn):
    project_id = _insert_project(conn)
    source_id = _insert_source(conn, project_id)
    artifact_id = _insert_artifact(conn, project_id, source_id)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_contents "
            "(id, project_id, artifact_id, version_key, sha256, parse_status, created_at) "
            "VALUES (?, ?, ?, 'v1', 'too-short', 'pending', ?)",
            (new_id(), project_id, artifact_id, utc_now_iso()),
        )


def test_chunk_char_end_must_exceed_char_start(conn):
    project_id = _insert_project(conn)
    source_id = _insert_source(conn, project_id)
    artifact_id = _insert_artifact(conn, project_id, source_id)
    content_id = new_id()
    conn.execute(
        "INSERT INTO source_contents "
        "(id, project_id, artifact_id, version_key, sha256, parse_status, created_at) "
        "VALUES (?, ?, ?, 'v1', ?, 'pending', ?)",
        (content_id, project_id, artifact_id, "d" * 64, utc_now_iso()),
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_chunks "
            "(id, project_id, content_id, ordinal, text, char_start, char_end, sha256, created_at) "
            "VALUES (?, ?, ?, 0, 'hello', 10, 5, ?, ?)",
            (new_id(), project_id, content_id, "e" * 64, utc_now_iso()),
        )


def test_sync_run_status_rejects_unknown_value(conn):
    project_id = _insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sync_runs (id, project_id, started_at, status, updated_at) "
            "VALUES (?, ?, ?, 'bogus_status', ?)",
            (new_id(), project_id, utc_now_iso(), utc_now_iso()),
        )


def test_sync_item_error_class_rejects_unknown_value(conn):
    project_id = _insert_project(conn)
    source_id = _insert_source(conn, project_id)
    run_id = new_id()
    conn.execute(
        "INSERT INTO sync_runs (id, project_id, started_at, updated_at) VALUES (?, ?, ?, ?)",
        (run_id, project_id, utc_now_iso(), utc_now_iso()),
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sync_items "
            "(id, sync_run_id, source_id, external_id, stage, error_class, created_at, updated_at) "
            "VALUES (?, ?, ?, 'ext-1', 'failed', 'not_a_real_class', ?, ?)",
            (new_id(), run_id, source_id, utc_now_iso(), utc_now_iso()),
        )


def test_credential_ref_is_the_only_credential_shaped_column_on_sources(conn):
    """Guard against secrets sneaking into `sources` under a new column
    name later: only `credential_ref` (an opaque pointer) is permitted."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
    credential_shaped = {
        c
        for c in columns
        if any(m in c.lower() for m in ("token", "secret", "api_key", "password"))
    }
    assert credential_shaped == set()
    assert "credential_ref" in columns
