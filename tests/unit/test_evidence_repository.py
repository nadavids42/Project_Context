"""Tests for the sources/evidence repositories: artifact/content/chunk
CRUD, version history, current-pointer transitions, and FTS5
insert/update/delete synchronization with project isolation."""

from __future__ import annotations

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import evidence_repository, sources_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import (
    ArtifactAvailability,
    ArtifactType,
    AssignmentMethod,
    EvidenceSourceType,
    ParseStatus,
)
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.sources import SourceKind
from project_context.services.projects import create_project


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def project_id(conn):
    project = create_project(
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
    )
    return project.id


# --- sources ------------------------------------------------------------


def test_ensure_manual_source_creates_on_first_call(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)

    assert source.project_id == project_id
    assert source.kind.value == "manual"
    assert source.enabled is True


def test_ensure_manual_source_reuses_existing_source(conn, project_id):
    first = sources_repository.ensure_manual_source(conn, project_id)
    second = sources_repository.ensure_manual_source(conn, project_id)

    assert first.id == second.id


def test_manual_sources_are_isolated_per_project(conn, project_id):
    other_project = create_project(conn, ProjectCreateInput(name="Other Project", objective="Obj"))

    source_a = sources_repository.ensure_manual_source(conn, project_id)
    source_b = sources_repository.ensure_manual_source(conn, other_project.id)

    assert source_a.id != source_b.id


# --- artifacts ------------------------------------------------------------


def _insert_artifact(conn, project_id, source_id, **overrides):
    fields = {
        "external_id": "ext-1",
        "artifact_type": ArtifactType.MANUAL_TEXT,
        "title": "Note",
        "author": None,
        "occurred_at": "2026-08-20T14:30:00",
        "external_url": None,
        "source_type": EvidenceSourceType.DOCUMENT,
        **overrides,
    }
    return evidence_repository.insert_artifact(conn, project_id, source_id, **fields)


def test_insert_and_get_artifact_round_trip(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)

    artifact = _insert_artifact(conn, project_id, source.id)
    fetched = evidence_repository.get_artifact(conn, project_id, artifact.id)

    assert fetched == artifact
    assert fetched.availability.value == "available"
    assert fetched.assignment_method is AssignmentMethod.MANUAL
    assert fetched.current_content_id is None


def test_get_artifact_by_external_id_finds_existing(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id, external_id="text:abc")

    found = evidence_repository.get_artifact_by_external_id(conn, project_id, source.id, "text:abc")

    assert found.id == artifact.id


def test_get_artifact_by_external_id_returns_none_when_missing(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)

    assert (
        evidence_repository.get_artifact_by_external_id(conn, project_id, source.id, "nope") is None
    )


def test_external_id_is_unique_per_source(conn, project_id):
    import sqlite3

    source = sources_repository.ensure_manual_source(conn, project_id)
    _insert_artifact(conn, project_id, source.id, external_id="dup")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_artifact(conn, project_id, source.id, external_id="dup")


# --- contents ---------------------------------------------------------------


def _insert_content(conn, project_id, artifact_id, **overrides):
    fields = {
        "sha256": "a" * 64,
        "raw_storage_path": "objects/aa/" + "a" * 64,
        "mime_type": "text/plain",
        "byte_size": 10,
        "normalized_text": "hello world",
        "parser_name": "text",
        "parser_version": "1",
        "parse_status": ParseStatus.PARSED,
        "location_map": {"blocks": [], "warnings": []},
        "original_filename": None,
        **overrides,
    }
    return evidence_repository.insert_content(conn, project_id, artifact_id, **fields)


def test_insert_content_assigns_incrementing_version_keys(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)

    content1 = _insert_content(conn, project_id, artifact.id, sha256="a" * 64)
    content2 = _insert_content(conn, project_id, artifact.id, sha256="b" * 64)

    assert content1.version_key == "v1"
    assert content2.version_key == "v2"


def test_get_content_by_sha256_finds_matching_version(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)
    content = _insert_content(conn, project_id, artifact.id, sha256="c" * 64)

    found = evidence_repository.get_content_by_sha256(conn, artifact.id, "c" * 64)

    assert found.id == content.id


def test_get_content_by_sha256_returns_none_for_unknown_hash(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)

    assert evidence_repository.get_content_by_sha256(conn, artifact.id, "d" * 64) is None


def test_set_current_content_advances_pointer(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)
    content = _insert_content(conn, project_id, artifact.id, sha256="e" * 64)

    updated = evidence_repository.set_current_content(conn, project_id, artifact.id, content.id)

    assert updated.current_content_id == content.id


def test_list_contents_for_artifact_returns_full_history_oldest_first(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)
    content1 = _insert_content(conn, project_id, artifact.id, sha256="1" * 64)
    content2 = _insert_content(conn, project_id, artifact.id, sha256="2" * 64)

    history = evidence_repository.list_contents_for_artifact(conn, project_id, artifact.id)

    assert [c.id for c in history] == [content1.id, content2.id]


def test_old_content_remains_addressable_after_a_newer_version_is_current(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)
    old_content = _insert_content(
        conn, project_id, artifact.id, sha256="1" * 64, normalized_text="old"
    )
    new_content = _insert_content(
        conn, project_id, artifact.id, sha256="2" * 64, normalized_text="new"
    )
    evidence_repository.set_current_content(conn, project_id, artifact.id, new_content.id)

    fetched_old = evidence_repository.get_content(conn, project_id, old_content.id)

    assert fetched_old.normalized_text == "old"


# --- chunks -----------------------------------------------------------------


def test_insert_chunks_and_list_ordered_by_ordinal(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)
    content = _insert_content(conn, project_id, artifact.id)

    specs = [
        ChunkSpec(
            ordinal=0,
            text="first",
            char_start=0,
            char_end=5,
            section_path="p1",
            sha256="1" * 64,
            token_estimate=1,
        ),
        ChunkSpec(
            ordinal=1,
            text="second",
            char_start=6,
            char_end=12,
            section_path="p2",
            sha256="2" * 64,
            token_estimate=1,
        ),
    ]
    evidence_repository.insert_chunks(conn, project_id, content.id, specs)

    chunks = evidence_repository.list_chunks_for_content(conn, project_id, content.id)

    assert [c.text for c in chunks] == ["first", "second"]
    assert [c.ordinal for c in chunks] == [0, 1]


# --- full-text search / FTS sync --------------------------------------------


def test_search_chunks_finds_inserted_text(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)
    content = _insert_content(conn, project_id, artifact.id)
    evidence_repository.insert_chunks(
        conn,
        project_id,
        content.id,
        [
            ChunkSpec(
                ordinal=0,
                text="unique marker zzyzx",
                char_start=0,
                char_end=19,
                section_path=None,
                sha256="f" * 64,
                token_estimate=3,
            )
        ],
    )

    results = evidence_repository.search_chunks(conn, project_id, "zzyzx")

    assert len(results) == 1
    assert "zzyzx" in results[0].snippet


def test_search_chunks_reflects_updates(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)
    content = _insert_content(conn, project_id, artifact.id)
    chunk = evidence_repository.insert_chunks(
        conn,
        project_id,
        content.id,
        [
            ChunkSpec(
                ordinal=0,
                text="original marker alpha",
                char_start=0,
                char_end=21,
                section_path=None,
                sha256="1" * 64,
                token_estimate=3,
            )
        ],
    )[0]

    conn.execute(
        "UPDATE source_chunks SET text = ? WHERE id = ?", ("updated marker beta", chunk.id)
    )

    assert evidence_repository.search_chunks(conn, project_id, "alpha") == []
    updated_results = evidence_repository.search_chunks(conn, project_id, "beta")
    assert len(updated_results) == 1


def test_search_chunks_reflects_deletes(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)
    content = _insert_content(conn, project_id, artifact.id)
    chunk = evidence_repository.insert_chunks(
        conn,
        project_id,
        content.id,
        [
            ChunkSpec(
                ordinal=0,
                text="deleteme marker gamma",
                char_start=0,
                char_end=22,
                section_path=None,
                sha256="2" * 64,
                token_estimate=3,
            )
        ],
    )[0]
    assert evidence_repository.search_chunks(conn, project_id, "gamma") != []

    conn.execute("DELETE FROM source_chunks WHERE id = ?", (chunk.id,))

    assert evidence_repository.search_chunks(conn, project_id, "gamma") == []


def test_search_chunks_is_isolated_per_project(conn, project_id):
    other_project = create_project(conn, ProjectCreateInput(name="Other Project", objective="Obj"))
    source_a = sources_repository.ensure_manual_source(conn, project_id)
    source_b = sources_repository.ensure_manual_source(conn, other_project.id)
    artifact_a = _insert_artifact(conn, project_id, source_a.id, external_id="a1")
    artifact_b = _insert_artifact(conn, other_project.id, source_b.id, external_id="b1")
    content_a = _insert_content(conn, project_id, artifact_a.id, sha256="a" * 64)
    content_b = _insert_content(conn, other_project.id, artifact_b.id, sha256="b" * 64)

    evidence_repository.insert_chunks(
        conn,
        project_id,
        content_a.id,
        [
            ChunkSpec(
                ordinal=0,
                text="sentinelalphaproject",
                char_start=0,
                char_end=20,
                section_path=None,
                sha256="3" * 64,
                token_estimate=3,
            )
        ],
    )
    evidence_repository.insert_chunks(
        conn,
        other_project.id,
        content_b.id,
        [
            ChunkSpec(
                ordinal=0,
                text="sentinelbetaproject",
                char_start=0,
                char_end=19,
                section_path=None,
                sha256="4" * 64,
                token_estimate=3,
            )
        ],
    )

    assert len(evidence_repository.search_chunks(conn, project_id, "sentinelalphaproject")) == 1
    assert evidence_repository.search_chunks(conn, project_id, "sentinelbetaproject") == []
    assert (
        len(evidence_repository.search_chunks(conn, other_project.id, "sentinelbetaproject")) == 1
    )
    assert evidence_repository.search_chunks(conn, other_project.id, "sentinelalphaproject") == []


def test_search_chunks_treats_query_as_literal_phrase_not_fts_operators(conn, project_id):
    """A hyphen is an FTS5 NOT-operator in bare query syntax; callers must
    not need to know that."""
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)
    content = _insert_content(conn, project_id, artifact.id)
    evidence_repository.insert_chunks(
        conn,
        project_id,
        content.id,
        [
            ChunkSpec(
                ordinal=0,
                text="cross-project term appears here",
                char_start=0,
                char_end=32,
                section_path=None,
                sha256="5" * 64,
                token_estimate=5,
            )
        ],
    )

    results = evidence_repository.search_chunks(conn, project_id, "cross-project")

    assert len(results) == 1


# --- connector-sourced content: explicit version_key, availability (Prompt 10) --


def test_insert_content_accepts_an_explicit_version_key(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)

    content = _insert_content(
        conn, project_id, artifact.id, sha256="d" * 64,
        version_key="2026-08-01T00:00:00.000Z",
    )

    assert content.version_key == "2026-08-01T00:00:00.000Z"


def test_insert_content_explicit_version_key_still_unique_per_artifact(conn, project_id):
    import sqlite3

    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)
    _insert_content(conn, project_id, artifact.id, sha256="e" * 64, version_key="same-marker")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_content(conn, project_id, artifact.id, sha256="f" * 64, version_key="same-marker")


def test_list_artifacts_for_source_filters_to_that_source(conn, project_id):
    source_a = sources_repository.ensure_manual_source(conn, project_id)
    source_b = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    _insert_artifact(conn, project_id, source_a.id, external_id="a1")
    _insert_artifact(conn, project_id, source_b.id, external_id="b1")

    only_b = evidence_repository.list_artifacts_for_source(conn, project_id, source_b.id)

    assert [a.external_id for a in only_b] == ["b1"]


def test_update_availability_marks_deleted_external_without_removing_the_row(conn, project_id):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = _insert_artifact(conn, project_id, source.id)

    updated = evidence_repository.update_availability(
        conn, project_id, artifact.id, availability=ArtifactAvailability.DELETED_EXTERNAL
    )

    assert updated.availability is ArtifactAvailability.DELETED_EXTERNAL
    refetched = evidence_repository.get_artifact(conn, project_id, artifact.id)
    assert refetched is not None
    assert refetched.availability is ArtifactAvailability.DELETED_EXTERNAL
