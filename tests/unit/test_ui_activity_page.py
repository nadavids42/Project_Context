"""Lightweight UI tests for the Activity & Review page, driven through
`streamlit.testing.v1.AppTest` (Prompt 8). Business logic lives in
`project_context.services.review`/`reconciliation`, already covered by
`tests/unit/test_review_service.py`; these tests only check that the
page renders honestly and that its plain (non-dialog) buttons/forms
round-trip through the real service."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from project_context.chunking import ChunkSpec
from project_context.config import load_config
from project_context.db import evidence_repository, sources_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.projects import ProjectCreateInput
from project_context.llm.schemas import EvidenceSpan, ExtractedObservation, ObservationKind
from project_context.services.observations import persist_observation
from project_context.services.projects import create_project
from project_context.services.reconciliation import reconcile_observation

REPO_ROOT_MIGRATIONS = Path(__file__).resolve().parent.parent.parent / "migrations"


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROJECT_CONTEXT_ENVIRONMENT", "test")
    config = load_config()
    config.ensure_local_directories()
    conn = connect(config.sqlite_path)
    run_migrations(conn, REPO_ROOT_MIGRATIONS)
    conn.close()
    return config


@pytest.fixture
def project_id(isolated_config):
    conn = connect(isolated_config.sqlite_path)
    try:
        return create_project(
            conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
        ).id
    finally:
        conn.close()


def _seed_pending_proposal(config, project_id, *, statement="Priya will send the report."):
    conn = connect(config.sqlite_path)
    try:
        source = sources_repository.ensure_manual_source(conn, project_id)
        artifact = evidence_repository.insert_artifact(
            conn,
            project_id,
            source.id,
            external_id=f"t:{hash(statement)}",
            artifact_type=ArtifactType.MANUAL_TEXT,
            title="Kickoff notes",
            author="Priya",
            occurred_at=None,
            external_url=None,
            source_type=None,
        )
        content = evidence_repository.insert_content(
            conn,
            project_id,
            artifact.id,
            sha256=hashlib.sha256(statement.encode()).hexdigest(),
            raw_storage_path=None,
            mime_type="text/plain",
            byte_size=len(statement),
            normalized_text=statement,
            parser_name="text",
            parser_version="1",
            parse_status=ParseStatus.PARSED,
            location_map=None,
            original_filename=None,
        )
        spec = ChunkSpec(
            ordinal=0,
            text=statement,
            char_start=0,
            char_end=len(statement),
            section_path=None,
            sha256=hashlib.sha256((statement + "c").encode()).hexdigest(),
            token_estimate=len(statement) // 4,
        )
        (chunk,) = evidence_repository.insert_chunks(conn, project_id, content.id, [spec])
        extracted = ExtractedObservation(
            kind=ObservationKind.COMMITMENT,
            subject="Send the report",
            statement=statement,
            owner_name="Priya",
            explicitness="explicit",
            evidence=[
                EvidenceSpan(
                    chunk_id=chunk.id, char_start=0, char_end=len(statement), quote=statement
                )
            ],
        )
        observation, _links, _created = persist_observation(
            conn, project_id, content_id=content.id, chunk_id=chunk.id, extracted=extracted
        )
        result = reconcile_observation(conn, project_id, observation.id)
        return result.proposal.id
    finally:
        conn.close()


def _render_activity_page() -> AppTest:
    def script():
        from project_context.ui.pages.activity import render

        render()

    return AppTest.from_function(script)


def _at_for_project(project_id: str) -> AppTest:
    at = _render_activity_page()
    at.session_state["selected_project_id"] = project_id
    return at


def test_empty_state_is_honest(isolated_config, project_id):
    at = _at_for_project(project_id)
    at.run()

    assert not at.exception
    assert any("No pending proposals" in i.value for i in at.info)
    assert any("Nothing pending review" in i.value for i in at.info)


def test_review_card_shows_proposal_and_evidence(isolated_config, project_id):
    _seed_pending_proposal(isolated_config, project_id)
    at = _at_for_project(project_id)
    at.run()

    assert not at.exception
    rendered = " ".join(m.value for m in at.markdown)
    assert "Send the report" in rendered
    caption_text = " ".join(c.value for c in at.caption)
    assert "Kickoff notes" in caption_text


def test_accept_button_creates_ledger_item(isolated_config, project_id):
    _seed_pending_proposal(isolated_config, project_id)
    at = _at_for_project(project_id)
    at.run()

    accept_buttons = [b for b in at.button if b.label == "Accept"]
    assert len(accept_buttons) == 1
    accept_buttons[0].click().run()

    assert not at.exception
    assert any("accepted" in s.value.lower() for s in at.success)

    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.db import ledger_repository

        items = ledger_repository.list_items_for_project(conn, project_id)
        assert len(items) == 1
        assert items[0].canonical_title == "Send the report"
    finally:
        conn.close()


def test_reject_button_leaves_no_ledger_item(isolated_config, project_id):
    _seed_pending_proposal(isolated_config, project_id)
    at = _at_for_project(project_id)
    at.run()

    reject_buttons = [b for b in at.button if b.label == "Reject"]
    reject_buttons[0].click().run()

    assert not at.exception
    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.db import ledger_repository

        assert ledger_repository.list_items_for_project(conn, project_id) == []
    finally:
        conn.close()
