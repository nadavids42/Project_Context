"""Tests for observation persistence orchestration: one accepted
`ExtractedObservation` becomes one `observations` row plus one
`evidence_links` row per cited span, atomically, idempotent by
fingerprint (Prompt 6)."""

from __future__ import annotations

import hashlib

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import (
    evidence_link_repository,
    evidence_repository,
    people_repository,
    sources_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.evidence_links import EvidenceLinkTargetType
from project_context.domain.projects import ProjectCreateInput
from project_context.llm.schemas import EvidenceSpan, ExtractedObservation
from project_context.services.observations import persist_observation
from project_context.services.projects import create_project


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def project_id(conn):
    return create_project(
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
    ).id


@pytest.fixture
def content_and_chunk(conn, project_id):
    text = "Priya will send the report by Friday."
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = evidence_repository.insert_artifact(
        conn,
        project_id,
        source.id,
        external_id="text:1",
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


def _extracted(chunk, **overrides):
    fields = {
        "kind": "commitment",
        "subject": "Priya",
        "statement": chunk.text,
        "owner_name": "Priya",
        "explicitness": "explicit",
        "evidence": [
            EvidenceSpan(
                chunk_id=chunk.id, char_start=0, char_end=len(chunk.text), quote=chunk.text
            )
        ],
    }
    fields.update(overrides)
    return ExtractedObservation.model_validate(fields)


def test_persist_observation_writes_observation_and_evidence_link(
    conn, project_id, content_and_chunk
):
    content, chunk = content_and_chunk
    extracted = _extracted(chunk)

    observation, links, created = persist_observation(
        conn,
        project_id,
        content_id=content.id,
        chunk_id=chunk.id,
        extracted=extracted,
        model_id="gpt-5.6-terra",
        prompt_version="extraction_v1",
        schema_version="extraction_batch_v1",
    )

    assert created is True
    assert observation.owner_text == "Priya"
    assert observation.model_id == "gpt-5.6-terra"
    assert len(links) == 1
    assert links[0].target_type is EvidenceLinkTargetType.OBSERVATION
    assert links[0].target_id == observation.id
    assert links[0].quote == chunk.text

    stored_links = evidence_link_repository.list_for_target(
        conn, project_id, EvidenceLinkTargetType.OBSERVATION, observation.id
    )
    assert len(stored_links) == 1


def test_persist_observation_is_idempotent_by_fingerprint(conn, project_id, content_and_chunk):
    content, chunk = content_and_chunk
    extracted = _extracted(chunk)

    first_obs, first_links, first_created = persist_observation(
        conn, project_id, content_id=content.id, chunk_id=chunk.id, extracted=extracted
    )
    second_obs, second_links, second_created = persist_observation(
        conn, project_id, content_id=content.id, chunk_id=chunk.id, extracted=extracted
    )

    assert first_created is True
    assert second_created is False
    assert second_obs.id == first_obs.id
    assert [link.id for link in second_links] == [link.id for link in first_links]


def test_persist_observation_rejects_evidence_citing_a_different_chunk(
    conn, project_id, content_and_chunk
):
    content, chunk = content_and_chunk
    mismatched = _extracted(
        chunk,
        evidence=[
            EvidenceSpan(chunk_id="some-other-chunk", char_start=0, char_end=5, quote="hello")
        ],
    )
    with pytest.raises(ValueError, match="expected"):
        persist_observation(
            conn, project_id, content_id=content.id, chunk_id=chunk.id, extracted=mismatched
        )


def test_persist_observation_resolves_owner_person_id_when_given(
    conn, project_id, content_and_chunk
):
    content, chunk = content_and_chunk
    person = people_repository.create_person(
        conn, display_name="Priya", primary_email="p@example.com"
    )
    extracted = _extracted(chunk)

    observation, _links, _created = persist_observation(
        conn,
        project_id,
        content_id=content.id,
        chunk_id=chunk.id,
        extracted=extracted,
        owner_person_id=person.id,
    )
    assert observation.owner_person_id == person.id
