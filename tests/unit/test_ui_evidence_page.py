"""Lightweight UI tests for the Evidence page, driven through
`streamlit.testing.v1.AppTest`.

Unlike the Projects page's New/Edit dialogs, these forms are plain
`st.form`s (not `st.dialog`-wrapped), which — verified directly — DO
propagate widget interactions correctly through `AppTest`. So these
tests exercise real submit round-trips: add manual text, upload a file,
open the viewer, and highlight a span.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from project_context.config import load_config
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.projects import ProjectCreateInput
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


# --- empty state / identity --------------------------------------------


def test_empty_state_is_honest(isolated_config, project_id):
    at = _at_for_project(project_id)
    at.run()

    assert not at.exception
    assert any("No evidence yet" in info.value for info in at.info)
    assert any("Acme Rollout" in c.value for c in at.caption)


# --- manual text submission ----------------------------------------------


def test_add_manual_text_creates_evidence_and_shows_flash(isolated_config, project_id):
    at = _at_for_project(project_id)
    at.run()

    [ti for ti in at.text_input if ti.key == "text-title"][0].set_value("Kickoff notes")
    [ta for ta in at.text_area if ta.key == "text-body"][0].set_value(
        "We discussed scope.\n\nNext steps: send proposal by Friday."
    )
    [b for b in at.button if b.label == "Add text evidence"][0].click().run()

    assert not at.exception
    assert any("Kickoff notes" in s.value for s in at.success)
    rendered = " ".join(m.value for m in at.markdown)
    assert "Kickoff notes" in rendered
    assert "Parsed" in rendered


def test_resubmitting_identical_text_shows_no_change_message(isolated_config, project_id):
    at = _at_for_project(project_id)
    at.run()
    [ti for ti in at.text_input if ti.key == "text-title"][0].set_value("Note")
    [ta for ta in at.text_area if ta.key == "text-body"][0].set_value("Same content.")
    [b for b in at.button if b.label == "Add text evidence"][0].click().run()

    [ti for ti in at.text_input if ti.key == "text-title"][0].set_value("Note")
    [ta for ta in at.text_area if ta.key == "text-body"][0].set_value("Same content.")
    [b for b in at.button if b.label == "Add text evidence"][0].click().run()

    assert not at.exception
    assert any("no change recorded" in i.value for i in at.info)


def test_blank_title_shows_validation_error(isolated_config, project_id):
    at = _at_for_project(project_id)
    at.run()
    [ta for ta in at.text_area if ta.key == "text-body"][0].set_value("Some text.")
    [b for b in at.button if b.label == "Add text evidence"][0].click().run()

    assert not at.exception
    assert any(e.value for e in at.error)


# --- file upload -----------------------------------------------------------


def test_evidence_list_shows_required_columns_after_text_submission(isolated_config, project_id):
    at = _at_for_project(project_id)
    at.run()
    [ti for ti in at.text_input if ti.key == "text-title"][0].set_value("Weekly Sync")
    [ta for ta in at.text_area if ta.key == "text-body"][0].set_value("Some notes from the sync.")
    at.selectbox(key="text-source-type").set_value("Meeting notes")
    [b for b in at.button if b.label == "Add text evidence"][0].click().run()

    rendered = " ".join(m.value for m in at.markdown)
    assert "Weekly Sync" in rendered
    assert "Meeting notes" in rendered
    assert "1" in rendered  # version count column


# --- viewer and span highlight ----------------------------------------------


def test_view_button_opens_viewer_with_metadata_and_text(isolated_config, project_id):
    at = _at_for_project(project_id)
    at.run()
    [ti for ti in at.text_input if ti.key == "text-title"][0].set_value("Kickoff notes")
    [ta for ta in at.text_area if ta.key == "text-body"][0].set_value("The quick brown fox.")
    [b for b in at.button if b.label == "Add text evidence"][0].click().run()

    view_buttons = [b for b in at.button if b.label == "View"]
    assert len(view_buttons) == 1
    view_buttons[0].click().run()

    assert not at.exception
    assert any("Viewing: Kickoff notes" in s.value for s in at.subheader)
    assert any("The quick brown fox." in t.value for t in at.text)


def test_valid_span_highlight_renders_without_error(isolated_config, project_id):
    at = _at_for_project(project_id)
    at.run()
    [ti for ti in at.text_input if ti.key == "text-title"][0].set_value("Note")
    [ta for ta in at.text_area if ta.key == "text-body"][0].set_value("The quick brown fox.")
    [b for b in at.button if b.label == "Add text evidence"][0].click().run()
    [b for b in at.button if b.label == "View"][0].click().run()

    start_input = [ni for ni in at.number_input if ni.label == "Start"][0]
    end_input = [ni for ni in at.number_input if ni.label == "End"][0]
    start_input.set_value(4).run()
    end_input.set_value(9).run()

    assert not at.exception
    assert not any(e for e in at.error)
    markdown_html = " ".join(m.value for m in at.markdown)
    assert "<mark>quick</mark>" in markdown_html


def test_invalid_span_shows_error_not_blank_content(isolated_config, project_id):
    at = _at_for_project(project_id)
    at.run()
    [ti for ti in at.text_input if ti.key == "text-title"][0].set_value("Note")
    [ta for ta in at.text_area if ta.key == "text-body"][0].set_value("Short text.")
    [b for b in at.button if b.label == "Add text evidence"][0].click().run()
    [b for b in at.button if b.label == "View"][0].click().run()

    start_input = [ni for ni in at.number_input if ni.label == "Start"][0]
    end_input = [ni for ni in at.number_input if ni.label == "End"][0]
    start_input.set_value(5).run()
    end_input.set_value(3).run()  # end before start -> invalid

    assert not at.exception
    assert any(e for e in at.error)
    assert any(t.value == "Short text." for t in at.text)  # falls back to plain text, not blank


# --- parser status visibility -----------------------------------------------


def test_content_extension_mismatch_upload_is_reported_visibly(isolated_config, project_id):
    # Streamlit's own file_uploader widget enforces its `type=[...]`
    # allow-list before any of this app's code runs, so a wholly
    # unknown extension can't reach the UI at all — the real "do not
    # trust extension alone" case reachable through this widget is a
    # content/extension *mismatch*: an allowed extension whose bytes
    # don't match it.
    at = _at_for_project(project_id)
    at.run()

    uploader = at.file_uploader[0]
    uploader.set_value(("fake.pdf", b"just plain text, not a real pdf at all", "application/pdf"))
    at.selectbox(key="file-source-type").set_value("Other")
    [b for b in at.button if b.label == "Upload file evidence"][0].click().run()

    assert not at.exception
    assert any("parsing reported" in w.value.lower() for w in at.warning)
    rendered = " ".join(m.value for m in at.markdown)
    assert "Unsupported file" in rendered
