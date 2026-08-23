"""UI test for the manually-triggered 'Extract observations' action on
the Evidence page (Section 12: "Required UI integration"). Uses
``FakeLLMProvider`` monkeypatched in for
``project_context.services.extraction.build_default_provider`` — no
network call, matching every other extraction test in this repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from fixtures.fake_llm_provider import FakeLLMProvider
from project_context.config import load_config
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.projects import ProjectCreateInput
from project_context.llm.provider import LLMClientError
from project_context.llm.schemas import EvidenceSpan, ExtractedObservation, ExtractionBatch
from project_context.services import evidence as evidence_service
from project_context.services import extraction as extraction_service
from project_context.services.projects import create_project

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROJECT_CONTEXT_ENVIRONMENT", "test")
    config = load_config()
    config.ensure_local_directories()
    conn = connect(config.sqlite_path)
    run_migrations(conn, REPO_ROOT / "migrations")
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


def _render_evidence_page() -> AppTest:
    def script():
        from project_context.ui.pages.evidence import render

        render()

    return AppTest.from_function(script)


def _at_for_project(project_id: str) -> AppTest:
    at = _render_evidence_page()
    at.session_state["selected_project_id"] = project_id
    return at


def _add_text_and_open_viewer(at: AppTest, *, title: str, text: str) -> AppTest:
    at.run()
    [ti for ti in at.text_input if ti.key == "text-title"][0].set_value(title)
    [ta for ta in at.text_area if ta.key == "text-body"][0].set_value(text)
    [b for b in at.button if b.label == "Add text evidence"][0].click().run()
    [b for b in at.button if b.label == "View"][0].click().run()
    return at


def _only_chunk(isolated_config, project_id: str):
    conn = connect(isolated_config.sqlite_path)
    try:
        artifact = evidence_service.list_evidence(conn, project_id)[0].artifact
        detail = evidence_service.get_evidence_detail(conn, project_id, artifact.id)
        return detail.chunks[0]
    finally:
        conn.close()


def test_extraction_button_absent_for_unparsed_evidence(isolated_config, project_id):
    at = _at_for_project(project_id)
    at.run()
    uploader = at.file_uploader[0]
    # Whitespace-only content parses to ParseStatus.EMPTY (not PARSED)
    # with a non-blank `normalized_text` (the raw whitespace itself), so
    # the viewer renders through to the extraction section — the
    # reachable "no chunks to extract from" case through the real UI,
    # as opposed to an unsupported/mismatched file, which the viewer
    # short-circuits earlier for lack of any text to display at all.
    uploader.set_value(("notes.txt", b"   ", "text/plain"))
    at.selectbox(key="file-source-type").set_value("Other")
    [b for b in at.button if b.label == "Upload file evidence"][0].click().run()
    [b for b in at.button if b.label == "View"][0].click().run()

    assert not at.exception
    assert not any(b.label == "Extract observations" for b in at.button)
    assert any("requires parsed evidence" in c.value for c in at.caption)


def test_extraction_shows_valid_observation_and_telemetry(isolated_config, project_id, monkeypatch):
    at = _at_for_project(project_id)
    text = "Priya will send the report by Friday."
    _add_text_and_open_viewer(at, title="Kickoff notes", text=text)

    chunk = _only_chunk(isolated_config, project_id)
    batch = ExtractionBatch(
        observations=[
            ExtractedObservation(
                kind="commitment",
                subject="Priya",
                statement="Priya will send the report by Friday.",
                owner_name="Priya",
                explicitness="explicit",
                evidence=[
                    EvidenceSpan(
                        chunk_id=chunk.id,
                        char_start=0,
                        char_end=len(chunk.text),
                        quote=chunk.text,
                    )
                ],
            )
        ],
        source_contains_no_material_updates=False,
    )
    fake_provider = FakeLLMProvider(responses=[batch])
    monkeypatch.setattr(extraction_service, "build_default_provider", lambda **_: fake_provider)

    extract_buttons = [b for b in at.button if b.label == "Extract observations"]
    assert len(extract_buttons) == 1
    extract_buttons[0].click().run()

    assert not at.exception
    assert any("Extraction completed" in s.value for s in at.success)
    assert any("commitment" in e.label and "Priya" in e.label for e in at.expander)
    assert any("Priya" in c.value for c in at.caption)
    assert len(fake_provider.calls) == 1


def test_extraction_shows_rejected_reason_for_invented_owner(
    isolated_config, project_id, monkeypatch
):
    at = _at_for_project(project_id)
    text = "The team agreed to send the report by Friday."
    _add_text_and_open_viewer(at, title="Kickoff notes", text=text)

    chunk = _only_chunk(isolated_config, project_id)
    batch = ExtractionBatch(
        observations=[
            ExtractedObservation(
                kind="commitment",
                subject="The team",
                statement="The team agreed to send the report by Friday.",
                owner_name="Zoe Xanthos",  # invented — not present in the evidence
                explicitness="explicit",
                evidence=[
                    EvidenceSpan(
                        chunk_id=chunk.id,
                        char_start=0,
                        char_end=len(chunk.text),
                        quote=chunk.text,
                    )
                ],
            )
        ],
        source_contains_no_material_updates=False,
    )
    fake_provider = FakeLLMProvider(responses=[batch])
    monkeypatch.setattr(extraction_service, "build_default_provider", lambda **_: fake_provider)

    [b for b in at.button if b.label == "Extract observations"][0].click().run()

    assert not at.exception
    rendered_warnings = " ".join(w.value for w in at.warning)
    assert "owner not supported" in rendered_warnings.lower()
    rendered_metrics = str(at.metric)
    assert "Rejected" in rendered_metrics


def test_extraction_failure_shows_safe_error_not_a_crash(isolated_config, project_id, monkeypatch):
    at = _at_for_project(project_id)
    _add_text_and_open_viewer(at, title="Kickoff notes", text="Some project evidence text.")

    fake_provider = FakeLLMProvider(responses=[LLMClientError("bad request")])
    monkeypatch.setattr(extraction_service, "build_default_provider", lambda **_: fake_provider)

    [b for b in at.button if b.label == "Extract observations"][0].click().run()

    assert not at.exception
    assert any("failed" in e.value.lower() for e in at.error)


def test_extraction_without_configured_provider_shows_actionable_error(
    isolated_config, project_id, monkeypatch
):
    at = _at_for_project(project_id)
    _add_text_and_open_viewer(at, title="Kickoff notes", text="Some project evidence text.")

    monkeypatch.setattr(extraction_service, "build_default_provider", lambda **_: None)

    [b for b in at.button if b.label == "Extract observations"][0].click().run()

    assert not at.exception
    assert any("OPENAI_API_KEY" in e.value for e in at.error)


def test_extraction_does_not_mutate_ledger_state(isolated_config, project_id, monkeypatch):
    """Section 12: 'Do not allow observations to update project state
    yet' — there is no ledger table this could write to at all in this
    step; this test documents that guarantee at the UI-integration layer
    by asserting the run only ever returns an in-memory result."""
    at = _at_for_project(project_id)
    text = "Priya will send the report by Friday."
    _add_text_and_open_viewer(at, title="Kickoff notes", text=text)

    chunk = _only_chunk(isolated_config, project_id)
    batch = ExtractionBatch(
        observations=[
            ExtractedObservation(
                kind="commitment",
                subject="Priya",
                statement="Priya will send the report by Friday.",
                owner_name="Priya",
                explicitness="explicit",
                evidence=[
                    EvidenceSpan(
                        chunk_id=chunk.id, char_start=0, char_end=len(chunk.text), quote=chunk.text
                    )
                ],
            )
        ],
        source_contains_no_material_updates=False,
    )
    fake_provider = FakeLLMProvider(responses=[batch])
    monkeypatch.setattr(extraction_service, "build_default_provider", lambda **_: fake_provider)
    [b for b in at.button if b.label == "Extract observations"][0].click().run()

    assert not at.exception
    conn = connect(isolated_config.sqlite_path)
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "observations" not in tables
    assert "ledger_items" not in tables
