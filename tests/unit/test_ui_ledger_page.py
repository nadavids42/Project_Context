"""Lightweight UI tests for the Ledger Views page (Prompt 8), driven
through `streamlit.testing.v1.AppTest`. Seeds ledger state directly via
`project_context.services.ledger`/`review` rather than through the UI —
the write paths themselves are covered by
`tests/unit/test_ledger_service.py` and `test_review_service.py`."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from project_context.config import load_config
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.ledger import LedgerItemKind
from project_context.domain.projects import ProjectCreateInput
from project_context.services.ledger import create_ledger_item
from project_context.services.projects import create_project

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


def _render_ledger_page() -> AppTest:
    def script():
        from project_context.ui.pages.ledger import render

        render()

    return AppTest.from_function(script)


def _at_for_project(project_id: str) -> AppTest:
    at = _render_ledger_page()
    at.session_state["selected_project_id"] = project_id
    return at


def test_empty_state_is_honest(isolated_config, project_id):
    at = _at_for_project(project_id)
    at.run()

    assert not at.exception
    assert any("No items of this kind yet" in i.value for i in at.info)


def test_commitment_appears_in_its_own_tab(isolated_config, project_id):
    conn = connect(isolated_config.sqlite_path)
    try:
        create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Priya will send it.",
            due_date="2026-08-28",
        )
    finally:
        conn.close()

    at = _at_for_project(project_id)
    at.run()

    assert not at.exception
    rendered = " ".join(m.value for m in at.markdown)
    assert "Send the report" in rendered


def test_history_button_opens_the_drawer(isolated_config, project_id):
    conn = connect(isolated_config.sqlite_path)
    try:
        item, _v1 = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Priya will send it.",
        )
    finally:
        conn.close()

    at = _at_for_project(project_id)
    at.run()
    history_buttons = [b for b in at.button if b.key == f"history-{item.id}"]
    assert len(history_buttons) == 1
    history_buttons[0].click().run()

    assert not at.exception
    assert any("History — Send the report" in s.value for s in at.subheader)
    rendered = " ".join(e.label for e in at.expander)
    assert "v1" in rendered
