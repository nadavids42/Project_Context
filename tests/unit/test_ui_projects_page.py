"""Lightweight UI tests driving Streamlit pages through
`streamlit.testing.v1.AppTest`.

Most tests here run the real app (`app.py`), which is what exercises the
actual `st.navigation` wiring — "Open" really switches to Project
Overview, filters really change what's listed, Restore really updates
the database. A few target one page's `render()` directly (via a thin
wrapper function AppTest can execute) to check the honest-empty-state
behavior of pages that aren't reachable except by clicking through a
selected project first.

Dialog-based forms (New Project, Edit Project, Archive confirmation) are
checked only for the content they render, not for full submit
round-trips: at the pinned Streamlit version, `AppTest` does not
propagate a widget interaction's effect out of an `st.dialog`-decorated
callback (verified directly against a minimal repro — even a plain
`st.button` inside `st.dialog` behaves this way here). Dialog
*submission* is covered where the actual logic lives: the service-layer
tests in test_projects_service.py, which is exactly why business logic
is kept out of Streamlit callbacks in the first place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from project_context.config import load_config
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.projects import ProjectCreateInput, ProjectStatus
from project_context.services.projects import archive_project, create_project

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_PATH = str(REPO_ROOT / "app.py")


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point the app at an isolated, migrated SQLite database."""
    monkeypatch.setenv("PROJECT_CONTEXT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROJECT_CONTEXT_ENVIRONMENT", "test")
    config = load_config()
    config.ensure_local_directories()
    conn = connect(config.sqlite_path)
    run_migrations(conn, REPO_ROOT / "migrations")
    conn.close()
    return config


def _seed_project(config, **overrides):
    conn = connect(config.sqlite_path)
    try:
        fields = {"name": "Acme Rollout", "objective": "Ship the pilot", **overrides}
        return create_project(conn, ProjectCreateInput(**fields))
    finally:
        conn.close()


def _run_app() -> AppTest:
    return AppTest.from_file(APP_PATH, default_timeout=30)


def _render_page(module_name: str) -> AppTest:
    """Run one `project_context.ui.pages.<module_name>.render()` in
    isolation, outside the full multipage app.

    `AppTest.from_function` re-executes only the extracted *source text*
    of `script` in a blank namespace — it does not carry over closures —
    so `module_name` is passed through `kwargs` instead of a closure.
    """

    def script(module_name):
        import importlib

        page = importlib.import_module(f"project_context.ui.pages.{module_name}")
        page.render()

    return AppTest.from_function(script, kwargs={"module_name": module_name})


# --- Projects list / filter -----------------------------------------------


def test_empty_active_state_is_honest(isolated_config):
    at = _run_app()
    at.run()

    assert not at.exception
    assert any("No active projects yet" in info.value for info in at.info)


def test_active_projects_are_listed_with_required_columns(isolated_config):
    project = _seed_project(isolated_config, client_name="Acme Corp")

    at = _run_app()
    at.run()

    rendered = " ".join(m.value for m in at.markdown)
    assert project.name in rendered
    assert "Acme Corp" in rendered
    assert "Active" in rendered
    assert project.updated_at in rendered


def test_archived_filter_shows_only_archived_projects(isolated_config):
    active = _seed_project(isolated_config, name="Active Project")
    to_archive = _seed_project(isolated_config, name="Archived Project")
    conn = connect(isolated_config.sqlite_path)
    archive_project(conn, to_archive.id)
    conn.close()

    at = _run_app()
    at.run()
    [r for r in at.radio if r.label == "Show"][0].set_value("Archived").run()

    assert not at.exception
    assert any(b.label == "Restore" for b in at.button)
    assert not any(b.key == f"open-{active.id}" for b in at.button if b.key)
    rendered = " ".join(m.value for m in at.markdown)
    assert to_archive.name in rendered


def test_active_filter_never_shows_archive_buttons_for_archived_projects(isolated_config):
    project = _seed_project(isolated_config)
    conn = connect(isolated_config.sqlite_path)
    archive_project(conn, project.id)
    conn.close()

    at = _run_app()
    at.run()

    assert not any(b.key == f"archive-{project.id}" for b in at.button if b.key)
    assert any("No active projects yet" in info.value for info in at.info)


# --- Open -> Project Overview navigation, with visible project identity ----


def test_open_selects_project_and_navigates_to_overview(isolated_config):
    project = _seed_project(isolated_config, client_name="Acme Corp")

    at = _run_app()
    at.run()
    [b for b in at.button if b.key == f"open-{project.id}"][0].click().run()

    assert not at.exception
    assert at.title[0].value == "Project Overview"
    assert at.session_state["selected_project_id"] == project.id
    assert any(project.name in c.value for c in at.caption)


def test_restore_reactivates_an_archived_project(isolated_config):
    project = _seed_project(isolated_config)
    conn = connect(isolated_config.sqlite_path)
    archive_project(conn, project.id)
    conn.close()

    at = _run_app()
    at.run()
    [r for r in at.radio if r.label == "Show"][0].set_value("Archived").run()
    [b for b in at.button if b.key == f"restore-{project.id}"][0].click().run()

    assert not at.exception
    conn = connect(isolated_config.sqlite_path)
    status = conn.execute("SELECT status FROM projects WHERE id = ?", (project.id,)).fetchone()[0]
    conn.close()
    assert status == ProjectStatus.ACTIVE.value


# --- Dialogs render their expected content ---------------------------------


def test_new_project_dialog_renders_required_fields(isolated_config):
    at = _run_app()
    at.run()
    [b for b in at.button if b.label == "New Project"][0].click().run()

    assert not at.exception
    assert any(ti.label == "Name*" for ti in at.text_input)
    assert any(ta.label == "Objective*" for ta in at.text_area)


def test_edit_project_dialog_prefills_current_values(isolated_config):
    project = _seed_project(isolated_config, stage="Discovery", client_name="Acme Corp")

    at = _run_app()
    at.run()
    [b for b in at.button if b.key == f"edit-{project.id}"][0].click().run()

    assert not at.exception
    name_input = [ti for ti in at.text_input if ti.label == "Name*"][0]
    assert name_input.value == project.name


def test_archive_confirmation_dialog_names_the_project(isolated_config):
    project = _seed_project(isolated_config)

    at = _run_app()
    at.run()
    [b for b in at.button if b.key == f"archive-{project.id}"][0].click().run()

    assert not at.exception
    assert any(project.name in w.value for w in at.markdown)


# --- Project-scoped pages: honest empty state and visible identity --------


@pytest.mark.parametrize("module_name", ["activity", "ledger", "briefs", "sources_settings"])
def test_project_scoped_page_without_selection_is_honest(isolated_config, module_name):
    at = _render_page(module_name)
    at.run()

    assert not at.exception
    assert any("No project selected" in info.value for info in at.info)
    assert not any("not built" in info.value.lower() for info in at.info)


def test_sources_settings_with_selection_and_drive_disabled_is_honest(isolated_config):
    """Drive is a real, built feature since Prompt 10, but stays fully
    gated behind `feature_drive_enabled` (default False) — this proves
    the page renders cleanly with the flag off rather than crashing or
    showing a stale "not built" placeholder."""
    project = _seed_project(isolated_config, client_name="Acme Corp")

    at = _render_page("sources_settings")
    at.session_state["selected_project_id"] = project.id
    at.run()

    assert not at.exception
    assert any(project.name in c.value for c in at.caption)
    assert any("drive integration is disabled" in info.value.lower() for info in at.info)


def test_project_overview_shows_objective_and_honest_empty_areas(isolated_config):
    project = _seed_project(isolated_config, stage="Discovery", client_name="Acme Corp")

    at = _render_page("project_overview")
    at.session_state["selected_project_id"] = project.id
    at.run()

    assert not at.exception
    assert any(project.name == s.value for s in at.subheader)
    assert "Ship the pilot" in " ".join(m.value for m in at.markdown)
    captions = [c.value for c in at.caption]
    assert any("Not built in this step" in c for c in captions)


def test_project_overview_without_selection_does_not_invent_a_project(isolated_config):
    at = _render_page("project_overview")
    at.run()

    assert not at.exception
    assert any("No project selected" in info.value for info in at.info)
    assert not at.subheader
