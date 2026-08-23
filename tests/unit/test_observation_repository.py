"""Tests for the observation repository: immutability, exact-fingerprint
deduplication, status-only mutation, and project-isolated FTS5 search
(Prompt 6)."""

from __future__ import annotations

import hashlib

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import evidence_repository, observation_repository, sources_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.observations import ObservationStatus
from project_context.domain.projects import ProjectCreateInput
from project_context.services.projects import create_project


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


def _make_project(conn, name="Acme Rollout"):
    return create_project(conn, ProjectCreateInput(name=name, objective="Ship the pilot")).id


def _content_and_chunk(conn, project_id, *, text="Priya will send the report by Friday."):
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = evidence_repository.insert_artifact(
        conn,
        project_id,
        source.id,
        external_id=f"text:{project_id}:{text[:8]}",
        artifact_type=ArtifactType.MANUAL_TEXT,
        title="Notes",
        author=None,
        occurred_at=None,
        external_url=None,
        source_type=None,
    )
    content = evidence_repository.insert_content(
        conn,
        project_id,
        artifact.id,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        raw_storage_path=None,
        mime_type="text/plain",
        byte_size=len(text),
        normalized_text=text,
        parser_name="text",
        parser_version="1",
        parse_status=ParseStatus.PARSED,
        location_map=None,
        original_filename=None,
    )
    spec = ChunkSpec(
        ordinal=0,
        text=text,
        char_start=0,
        char_end=len(text),
        section_path=None,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        token_estimate=len(text) // 4,
    )
    (chunk,) = evidence_repository.insert_chunks(conn, project_id, content.id, [spec])
    return content, chunk


def _insert(conn, project_id, content, chunk, **overrides):
    kwargs = {
        "content_id": content.id,
        "chunk_id": chunk.id,
        "kind": "commitment",
        "subject": "Priya",
        "statement": chunk.text,
        "evidence_spans": [(chunk.id, 0, len(chunk.text))],
        "explicitness": "explicit",
    }
    kwargs.update(overrides)
    return observation_repository.insert_observation(conn, project_id, **kwargs)


@pytest.fixture
def project_and_chunk(conn):
    project_id = _make_project(conn)
    content, chunk = _content_and_chunk(conn, project_id)
    return project_id, content, chunk


# --- insert / round trip -----------------------------------------------


def test_insert_observation_round_trips(conn, project_and_chunk):
    project_id, content, chunk = project_and_chunk
    observation, created = _insert(conn, project_id, content, chunk, owner_text="Priya")

    assert created is True
    assert observation.project_id == project_id
    assert observation.content_id == content.id
    assert observation.chunk_id == chunk.id
    assert observation.owner_text == "Priya"
    assert observation.polarity == "positive"
    assert observation.status is ObservationStatus.VALID
    assert observation_repository.get_observation(conn, project_id, observation.id) == observation


# --- exact fingerprint deduplication ------------------------------------


def test_reinserting_an_identical_observation_returns_the_existing_row(conn, project_and_chunk):
    project_id, content, chunk = project_and_chunk
    first, first_created = _insert(conn, project_id, content, chunk)
    second, second_created = _insert(conn, project_id, content, chunk)

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    count = len(observation_repository.list_observations_for_project(conn, project_id))
    assert count == 1


def test_a_different_statement_is_not_deduplicated(conn, project_and_chunk):
    project_id, content, chunk = project_and_chunk
    _insert(conn, project_id, content, chunk, statement="Priya will send the report by Friday.")
    _, created = _insert(
        conn, project_id, content, chunk, statement="Priya sent the report already."
    )
    assert created is True
    assert len(observation_repository.list_observations_for_project(conn, project_id)) == 2


def test_get_observation_by_fingerprint(conn, project_and_chunk):
    project_id, content, chunk = project_and_chunk
    observation, _ = _insert(conn, project_id, content, chunk)
    found = observation_repository.get_observation_by_fingerprint(conn, observation.fingerprint)
    assert found == observation


# --- immutability ---------------------------------------------------------


def test_observation_repository_exposes_no_field_mutation_function():
    """Prompt 6: "Observations are immutable atomic propositions." The
    only public function that changes a stored row is `update_status`,
    and it is the only one whose name suggests mutation at all."""
    public_names = [name for name in dir(observation_repository) if not name.startswith("_")]
    mutating_names = [
        name
        for name in public_names
        if any(verb in name for verb in ("update", "set", "edit", "correct", "delete", "patch"))
    ]
    assert mutating_names == ["update_status"]


def test_update_status_changes_only_status_not_propositional_fields(conn, project_and_chunk):
    project_id, content, chunk = project_and_chunk
    observation, _ = _insert(conn, project_id, content, chunk, owner_text="Priya")

    updated = observation_repository.update_status(
        conn, project_id, observation.id, ObservationStatus.RECONCILED
    )

    assert updated.status is ObservationStatus.RECONCILED
    assert updated.statement == observation.statement
    assert updated.subject == observation.subject
    assert updated.owner_text == observation.owner_text
    assert updated.fingerprint == observation.fingerprint
    assert updated.created_at == observation.created_at


# --- FTS project isolation ------------------------------------------------


def test_search_observations_is_scoped_to_one_project(conn):
    project_a = _make_project(conn, name="Project A")
    project_b = _make_project(conn, name="Project B")
    content_a, chunk_a = _content_and_chunk(
        conn, project_a, text="The sentinel-alpha rollout will proceed as planned."
    )
    content_b, chunk_b = _content_and_chunk(
        conn, project_b, text="The sentinel-alpha rollout will proceed as planned."
    )
    _insert(conn, project_a, content_a, chunk_a, subject="Rollout", statement=chunk_a.text)
    _insert(conn, project_b, content_b, chunk_b, subject="Rollout", statement=chunk_b.text)

    results_a = observation_repository.search_observations(conn, project_a, "sentinel-alpha")
    results_b = observation_repository.search_observations(conn, project_b, "sentinel-alpha")

    assert len(results_a) == 1
    assert len(results_b) == 1
    obs_a = observation_repository.list_observations_for_project(conn, project_a)[0]
    obs_b = observation_repository.list_observations_for_project(conn, project_b)[0]
    assert results_a[0].observation_id == obs_a.id
    assert results_b[0].observation_id == obs_b.id


def test_search_observations_does_not_leak_a_unique_sentinel_across_projects(conn):
    project_a = _make_project(conn, name="Project A")
    project_b = _make_project(conn, name="Project B")
    content_a, chunk_a = _content_and_chunk(
        conn, project_a, text="Only project A discusses zzqx-unique-sentinel-19284 today."
    )
    content_b, chunk_b = _content_and_chunk(
        conn, project_b, text="Project B discusses nothing special."
    )
    _insert(conn, project_a, content_a, chunk_a, subject="Sentinel", statement=chunk_a.text)
    _insert(conn, project_b, content_b, chunk_b, subject="Other", statement=chunk_b.text)

    sentinel = "zzqx-unique-sentinel-19284"
    assert len(observation_repository.search_observations(conn, project_a, sentinel)) == 1
    assert observation_repository.search_observations(conn, project_b, sentinel) == []
