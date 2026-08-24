"""UI tests for the Briefs page's Meeting Preparation Brief section
(Section 6; FR-025; Prompt 14). LLM calls are monkeypatched to a fake —
nothing here touches the network (Section 15)."""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.projects import ProjectCreateInput
from project_context.services.projects import create_project

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    from project_context.config import load_config

    monkeypatch.setenv("PROJECT_CONTEXT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROJECT_CONTEXT_ENVIRONMENT", "test")
    config = load_config()
    config.ensure_local_directories()
    conn = connect(config.sqlite_path)
    run_migrations(conn, REPO_ROOT / "migrations")
    conn.close()
    return config


def _seed_project(config):
    conn = connect(config.sqlite_path)
    try:
        return create_project(
            conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
        )
    finally:
        conn.close()


def _seed_calendar_artifact(
    config, project_id, *, title="Kickoff", occurred_at="2026-08-01T10:00:00Z"
):
    conn = connect(config.sqlite_path)
    try:
        from project_context.db import evidence_repository, sources_repository
        from project_context.domain.evidence import ArtifactType

        source = sources_repository.ensure_manual_source(conn, project_id)
        artifact = evidence_repository.insert_artifact(
            conn,
            project_id,
            source.id,
            external_id=f"evt-{title}",
            artifact_type=ArtifactType.CALENDAR_EVENT,
            title=title,
            author=None,
            occurred_at=occurred_at,
            external_url="https://example.com/evt",
            source_type=None,
        )
        conn.commit()
        return artifact
    finally:
        conn.close()


def _seed_evidenced_risk(config, project_id, *, title="Vendor delay"):
    conn = connect(config.sqlite_path)
    try:
        import hashlib

        from project_context.chunking import ChunkSpec
        from project_context.db import (
            evidence_link_repository,
            evidence_repository,
            sources_repository,
        )
        from project_context.domain.evidence import ArtifactType, ParseStatus
        from project_context.domain.evidence_links import (
            EvidenceLinkSupportRole,
            EvidenceLinkTargetType,
        )
        from project_context.domain.ledger import LedgerItemKind
        from project_context.services.ledger import create_ledger_item

        item, _v1 = create_ledger_item(
            conn, project_id, kind=LedgerItemKind.RISK, canonical_title=title
        )
        source = sources_repository.ensure_manual_source(conn, project_id)
        text = f"{title} evidence."
        artifact = evidence_repository.insert_artifact(
            conn,
            project_id,
            source.id,
            external_id=f"t-{title}",
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
            sha256=hashlib.sha256((text + "c").encode()).hexdigest(),
            token_estimate=len(text) // 4,
        )
        (chunk,) = evidence_repository.insert_chunks(conn, project_id, content.id, [spec])
        evidence_link_repository.insert_link(
            conn,
            project_id,
            target_type=EvidenceLinkTargetType.LEDGER_ITEM,
            target_id=item.id,
            content_id=content.id,
            chunk_id=chunk.id,
            char_start=0,
            char_end=len(chunk.text),
            quote=chunk.text,
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )
        conn.commit()
        return item
    finally:
        conn.close()


def _render_page() -> AppTest:
    def script():
        import importlib

        page = importlib.import_module("project_context.ui.pages.briefs")
        page.render()

    return AppTest.from_function(script)


def _at_for(config, project_id) -> AppTest:
    at = _render_page()
    at.session_state["selected_project_id"] = project_id
    return at


def _fake_provider_returning_empty_composition():
    from project_context.llm.provider import StructuredResult, estimate_cost_usd
    from project_context.llm.schemas import BriefComposition

    class _FakeProvider:
        def generate_structured(self, *, task, system, input_text, response_model, config):
            return StructuredResult(
                parsed=BriefComposition(sections=[]),
                provider="fake",
                model=config.model,
                request_id=None,
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
                estimated_cost_usd=estimate_cost_usd(config.model, 1, 1),
            )

    return _FakeProvider()


def test_meeting_prep_section_renders_with_no_candidates(isolated_config):
    project = _seed_project(isolated_config)
    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any("Generate Meeting Preparation Brief" in h.value for h in at.subheader)
    # No Calendar/Fathom meeting exists yet — manual is the only mode.
    radios = [r for r in at.radio if r.label == "Meeting source"]
    assert radios
    assert radios[0].options == ["Enter manually"]


def test_meeting_prep_preview_from_an_existing_calendar_event(isolated_config):
    project = _seed_project(isolated_config)
    _seed_calendar_artifact(isolated_config, project.id, title="Acme Kickoff")

    at = _at_for(isolated_config, project.id)
    at.run()

    radios = [r for r in at.radio if r.label == "Meeting source"]
    assert "Select an existing meeting" in radios[0].options
    radios[0].set_value("Select an existing meeting").run()

    preview_buttons = [b for b in at.button if b.label == "Preview meeting & facts"]
    assert preview_buttons
    preview_buttons[0].click().run()

    assert not at.exception
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Acme Kickoff" in markdown_text


def test_manual_entry_fully_works_with_no_calendar_data(isolated_config):
    """Calendar-disabled manual fallback (Prompt 14)."""
    project = _seed_project(isolated_config)
    at = _at_for(isolated_config, project.id)
    at.run()

    title_inputs = [ti for ti in at.text_input if ti.label == "Meeting title*"]
    assert title_inputs
    title_inputs[0].set_value("Manual Weekly Sync")

    date_inputs = [d for d in at.date_input if d.label == "Scheduled date*"]
    time_inputs = [t for t in at.time_input if t.label == "Scheduled time*"]
    date_inputs[0].set_value(date(2026, 8, 25))
    time_inputs[0].set_value(time(15, 0))

    preview_buttons = [b for b in at.button if b.label == "Preview meeting & facts"]
    preview_buttons[0].click().run()

    assert not at.exception
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Manual Weekly Sync" in markdown_text


def test_stale_source_warning_shown_before_generation(isolated_config):
    project = _seed_project(isolated_config)
    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.db import sources_repository
        from project_context.domain.sources import SourceKind

        sources_repository.insert_source(
            conn, project.id, kind=SourceKind.DRIVE, display_name="Drive folder"
        )
        conn.commit()
    finally:
        conn.close()

    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any("stale" in w.value.lower() or "never synced" in w.value.lower() for w in at.warning)


def test_include_exclude_and_generate_meeting_prep_brief(isolated_config, monkeypatch):
    project = _seed_project(isolated_config)
    item = _seed_evidenced_risk(isolated_config, project.id, title="Vendor delay")

    import project_context.ui.pages.briefs as page

    monkeypatch.setattr(
        page.extraction_service,
        "build_default_provider",
        lambda **_: _fake_provider_returning_empty_composition(),
    )

    at = _at_for(isolated_config, project.id)
    at.run()

    title_inputs = [ti for ti in at.text_input if ti.label == "Meeting title*"]
    title_inputs[0].set_value("Sync")
    date_inputs = [d for d in at.date_input if d.label == "Scheduled date*"]
    time_inputs = [t for t in at.time_input if t.label == "Scheduled time*"]
    date_inputs[0].set_value(date(2026, 8, 25))
    time_inputs[0].set_value(time(15, 0))

    preview_buttons = [b for b in at.button if b.label == "Preview meeting & facts"]
    preview_buttons[0].click().run()
    assert not at.exception

    generate_buttons = [b for b in at.button if b.label == "Generate Meeting Preparation Brief"]
    assert generate_buttons
    generate_buttons[0].click().run()

    assert not at.exception
    assert any("meeting preparation brief generated" in s.value.lower() for s in at.success)
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Vendor delay" in markdown_text
    del item


def test_meeting_prep_history_shows_download_button_after_generation(isolated_config, monkeypatch):
    project = _seed_project(isolated_config)
    _seed_evidenced_risk(isolated_config, project.id, title="Vendor delay")

    import project_context.ui.pages.briefs as page

    monkeypatch.setattr(
        page.extraction_service,
        "build_default_provider",
        lambda **_: _fake_provider_returning_empty_composition(),
    )

    at = _at_for(isolated_config, project.id)
    at.run()
    title_inputs = [ti for ti in at.text_input if ti.label == "Meeting title*"]
    title_inputs[0].set_value("Sync")
    date_inputs = [d for d in at.date_input if d.label == "Scheduled date*"]
    time_inputs = [t for t in at.time_input if t.label == "Scheduled time*"]
    date_inputs[0].set_value(date(2026, 8, 25))
    time_inputs[0].set_value(time(15, 0))
    [b for b in at.button if b.label == "Preview meeting & facts"][0].click().run()
    [b for b in at.button if b.label == "Generate Meeting Preparation Brief"][0].click().run()

    assert not at.exception
    download_buttons = [b for b in at.download_button if "Download Markdown" in b.label]
    assert download_buttons
