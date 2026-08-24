"""Tests for the evidence-link repository: target existence, same-project
enforcement, span/quote validation, and the `brief_claim` target type
(Prompt 6: "Evidence-link repository with project and span validation";
Prompt 9: `brief_claim` targets are now backed by a real `brief_claims`
table, migrations/0008_briefs.sql)."""

from __future__ import annotations

import hashlib

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import (
    brief_repository,
    evidence_link_repository,
    evidence_repository,
    observation_repository,
    sources_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.briefs import BriefType, ClaimType, ClaimValidationStatus
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.evidence_links import EvidenceLinkSupportRole, EvidenceLinkTargetType
from project_context.domain.ledger import LedgerItemKind
from project_context.domain.projects import ProjectCreateInput
from project_context.services.ledger import create_ledger_item
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


def _observation(conn, project_id, content, chunk):
    observation, _ = observation_repository.insert_observation(
        conn,
        project_id,
        content_id=content.id,
        chunk_id=chunk.id,
        kind="commitment",
        subject="Priya",
        statement=chunk.text,
        evidence_spans=[(chunk.id, 0, len(chunk.text))],
        explicitness="explicit",
    )
    return observation


# --- happy path -----------------------------------------------------------


def test_insert_link_round_trips(conn):
    project_id = _make_project(conn)
    content, chunk = _content_and_chunk(conn, project_id)
    observation = _observation(conn, project_id, content, chunk)

    link = evidence_link_repository.insert_link(
        conn,
        project_id,
        target_type=EvidenceLinkTargetType.OBSERVATION,
        target_id=observation.id,
        content_id=content.id,
        chunk_id=chunk.id,
        char_start=0,
        char_end=5,
        quote=chunk.text[:5],
        support_role=EvidenceLinkSupportRole.SUPPORTS,
    )

    assert link.project_id == project_id
    assert link.target_id == observation.id
    assert link.quote == chunk.text[:5]


def test_insert_link_accepts_a_ledger_item_target(conn):
    project_id = _make_project(conn)
    content, chunk = _content_and_chunk(conn, project_id)
    item, _version = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report"
    )

    link = evidence_link_repository.insert_link(
        conn,
        project_id,
        target_type=EvidenceLinkTargetType.LEDGER_ITEM,
        target_id=item.id,
        content_id=content.id,
        chunk_id=chunk.id,
        char_start=0,
        char_end=5,
        quote=chunk.text[:5],
        support_role=EvidenceLinkSupportRole.SUPPORTS,
    )
    assert link.target_type is EvidenceLinkTargetType.LEDGER_ITEM


def test_insert_link_without_chunk_id_validates_against_content_text(conn):
    project_id = _make_project(conn)
    content, chunk = _content_and_chunk(conn, project_id)
    observation = _observation(conn, project_id, content, chunk)

    link = evidence_link_repository.insert_link(
        conn,
        project_id,
        target_type=EvidenceLinkTargetType.OBSERVATION,
        target_id=observation.id,
        content_id=content.id,
        char_start=0,
        char_end=5,
        quote=content.normalized_text[:5],
        support_role=EvidenceLinkSupportRole.SUPPORTS,
    )
    assert link.chunk_id is None


# --- target validation ------------------------------------------------


def test_insert_link_rejects_a_nonexistent_target(conn):
    project_id = _make_project(conn)
    content, chunk = _content_and_chunk(conn, project_id)

    with pytest.raises(evidence_link_repository.EvidenceLinkError, match="does not exist"):
        evidence_link_repository.insert_link(
            conn,
            project_id,
            target_type=EvidenceLinkTargetType.OBSERVATION,
            target_id="nonexistent-observation",
            content_id=content.id,
            chunk_id=chunk.id,
            char_start=0,
            char_end=5,
            quote=chunk.text[:5],
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )


def test_insert_link_rejects_a_target_from_a_different_project(conn):
    project_a = _make_project(conn, name="Project A")
    project_b = _make_project(conn, name="Project B")
    content_a, chunk_a = _content_and_chunk(conn, project_a)
    observation_b = _observation(conn, project_b, *_content_and_chunk(conn, project_b))

    with pytest.raises(evidence_link_repository.EvidenceLinkError, match="different project"):
        evidence_link_repository.insert_link(
            conn,
            project_a,
            target_type=EvidenceLinkTargetType.OBSERVATION,
            target_id=observation_b.id,
            content_id=content_a.id,
            chunk_id=chunk_a.id,
            char_start=0,
            char_end=5,
            quote=chunk_a.text[:5],
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )


def test_insert_link_rejects_content_from_a_different_project(conn):
    project_a = _make_project(conn, name="Project A")
    project_b = _make_project(conn, name="Project B")
    content_a, chunk_a = _content_and_chunk(conn, project_a)
    observation_a = _observation(conn, project_a, content_a, chunk_a)
    content_b, _chunk_b = _content_and_chunk(conn, project_b)

    with pytest.raises(evidence_link_repository.EvidenceLinkError):
        evidence_link_repository.insert_link(
            conn,
            project_a,
            target_type=EvidenceLinkTargetType.OBSERVATION,
            target_id=observation_a.id,
            content_id=content_b.id,
            char_start=0,
            char_end=5,
            quote="hello",
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )


def test_insert_link_rejects_a_brief_claim_target_that_does_not_exist(conn):
    project_id = _make_project(conn)
    content, chunk = _content_and_chunk(conn, project_id)

    with pytest.raises(evidence_link_repository.EvidenceLinkError, match="brief_claim"):
        evidence_link_repository.insert_link(
            conn,
            project_id,
            target_type=EvidenceLinkTargetType.BRIEF_CLAIM,
            target_id="whatever",
            content_id=content.id,
            chunk_id=chunk.id,
            char_start=0,
            char_end=5,
            quote=chunk.text[:5],
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )


def test_insert_link_accepts_a_real_brief_claim_target(conn):
    project_id = _make_project(conn)
    content, chunk = _content_and_chunk(conn, project_id)
    brief = brief_repository.insert_brief(
        conn,
        project_id,
        brief_type=BriefType.CURRENT_PROJECT,
        cutoff_at="2026-08-23T00:00:00Z",
        input_snapshot={},
    )
    claim = brief_repository.insert_claim(
        conn,
        project_id,
        brief_id=brief.id,
        section="recent_changes",
        ordinal=0,
        claim_text="Something happened.",
        claim_type=ClaimType.FACT,
        cited_fact_ids=("fact-1",),
        validation_status=ClaimValidationStatus.VALID,
    )

    link = evidence_link_repository.insert_link(
        conn,
        project_id,
        target_type=EvidenceLinkTargetType.BRIEF_CLAIM,
        target_id=claim.id,
        content_id=content.id,
        chunk_id=chunk.id,
        char_start=0,
        char_end=5,
        quote=chunk.text[:5],
        support_role=EvidenceLinkSupportRole.SUPPORTS,
    )
    assert link.target_id == claim.id


# --- span / quote validation ---------------------------------------------


def test_insert_link_rejects_out_of_bounds_span(conn):
    project_id = _make_project(conn)
    content, chunk = _content_and_chunk(conn, project_id)
    observation = _observation(conn, project_id, content, chunk)

    with pytest.raises(evidence_link_repository.EvidenceLinkError):
        evidence_link_repository.insert_link(
            conn,
            project_id,
            target_type=EvidenceLinkTargetType.OBSERVATION,
            target_id=observation.id,
            content_id=content.id,
            chunk_id=chunk.id,
            char_start=0,
            char_end=len(chunk.text) + 50,
            quote=chunk.text,
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )


def test_insert_link_rejects_mismatched_quote(conn):
    project_id = _make_project(conn)
    content, chunk = _content_and_chunk(conn, project_id)
    observation = _observation(conn, project_id, content, chunk)

    with pytest.raises(evidence_link_repository.EvidenceLinkError, match="does not match"):
        evidence_link_repository.insert_link(
            conn,
            project_id,
            target_type=EvidenceLinkTargetType.OBSERVATION,
            target_id=observation.id,
            content_id=content.id,
            chunk_id=chunk.id,
            char_start=0,
            char_end=5,
            quote="wrong",
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )


def test_insert_link_accepts_quote_matching_after_whitespace_normalization(conn):
    project_id = _make_project(conn)
    text = "Priya will   send the\nreport by Friday."
    content, chunk = _content_and_chunk(conn, project_id, text=text)
    observation = _observation(conn, project_id, content, chunk)

    link = evidence_link_repository.insert_link(
        conn,
        project_id,
        target_type=EvidenceLinkTargetType.OBSERVATION,
        target_id=observation.id,
        content_id=content.id,
        chunk_id=chunk.id,
        char_start=0,
        char_end=len(text),
        quote="Priya will send the report by Friday.",
        support_role=EvidenceLinkSupportRole.SUPPORTS,
    )
    assert link is not None


def test_list_for_target_returns_only_that_targets_links(conn):
    project_id = _make_project(conn)
    content, chunk = _content_and_chunk(conn, project_id)
    observation = _observation(conn, project_id, content, chunk)
    evidence_link_repository.insert_link(
        conn,
        project_id,
        target_type=EvidenceLinkTargetType.OBSERVATION,
        target_id=observation.id,
        content_id=content.id,
        chunk_id=chunk.id,
        char_start=0,
        char_end=5,
        quote=chunk.text[:5],
        support_role=EvidenceLinkSupportRole.SUPPORTS,
    )
    links = evidence_link_repository.list_for_target(
        conn, project_id, EvidenceLinkTargetType.OBSERVATION, observation.id
    )
    assert len(links) == 1
