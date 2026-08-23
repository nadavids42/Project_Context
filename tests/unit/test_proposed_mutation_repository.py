"""Tests for the proposed-mutation repository: pending/reviewed queue
state (Prompt 6). No candidate scoring or action selection is exercised
here — that is reconciliation, out of scope."""

from __future__ import annotations

import hashlib

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import (
    evidence_repository,
    observation_repository,
    proposed_mutation_repository,
    sources_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.review import ProposedMutationAction, ProposedMutationStatus
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
def observation_id(conn, project_id):
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
    observation, _ = observation_repository.insert_observation(
        conn,
        project_id,
        content_id=content.id,
        chunk_id=chunk.id,
        kind="commitment",
        subject="Priya",
        statement=text,
        evidence_spans=[(chunk.id, 0, len(text))],
        explicitness="explicit",
    )
    return observation.id


def test_insert_proposal_defaults_to_pending(conn, project_id, observation_id):
    proposal = proposed_mutation_repository.insert_proposal(
        conn, project_id, observation_id=observation_id, action=ProposedMutationAction.CREATE
    )
    assert proposal.status is ProposedMutationStatus.PENDING
    assert proposal.reviewed_at is None
    assert proposed_mutation_repository.get_proposal(conn, project_id, proposal.id) == proposal


def test_list_pending_for_project_excludes_resolved_proposals(conn, project_id, observation_id):
    proposal = proposed_mutation_repository.insert_proposal(
        conn, project_id, observation_id=observation_id, action=ProposedMutationAction.CREATE
    )
    pending = proposed_mutation_repository.list_pending_for_project(conn, project_id)
    assert [p.id for p in pending] == [proposal.id]

    proposed_mutation_repository.set_status(
        conn, project_id, proposal.id, ProposedMutationStatus.ACCEPTED
    )
    assert proposed_mutation_repository.list_pending_for_project(conn, project_id) == []


def test_set_status_stamps_reviewed_at(conn, project_id, observation_id):
    proposal = proposed_mutation_repository.insert_proposal(
        conn, project_id, observation_id=observation_id, action=ProposedMutationAction.CREATE
    )
    updated = proposed_mutation_repository.set_status(
        conn, project_id, proposal.id, ProposedMutationStatus.REJECTED
    )
    assert updated.status is ProposedMutationStatus.REJECTED
    assert updated.reviewed_at is not None


def test_list_for_observation_returns_every_proposal_for_it(conn, project_id, observation_id):
    proposal = proposed_mutation_repository.insert_proposal(
        conn, project_id, observation_id=observation_id, action=ProposedMutationAction.CREATE
    )
    proposed_mutation_repository.set_status(
        conn, project_id, proposal.id, ProposedMutationStatus.REJECTED
    )
    second = proposed_mutation_repository.insert_proposal(
        conn, project_id, observation_id=observation_id, action=ProposedMutationAction.CREATE
    )
    matches = proposed_mutation_repository.list_for_observation(conn, project_id, observation_id)
    assert {p.id for p in matches} == {proposal.id, second.id}


def test_confidence_and_patch_round_trip(conn, project_id, observation_id):
    proposal = proposed_mutation_repository.insert_proposal(
        conn,
        project_id,
        observation_id=observation_id,
        action=ProposedMutationAction.UPDATE,
        target_ledger_item_id=None,
        proposed_patch={"due_date": "2026-09-04"},
        candidate_features={"subject_token_similarity": 0.9},
        confidence_score=0.87,
        confidence_band="high",
    )
    assert proposal.proposed_patch == {"due_date": "2026-09-04"}
    assert proposal.candidate_features == {"subject_token_similarity": 0.9}
    assert proposal.confidence_score == 0.87
