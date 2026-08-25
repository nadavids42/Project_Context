"""UI tests for the Sources & Settings page's Calendar section
(Section 6; FR-002; Prompt 12). Every OAuth/sync call is monkeypatched
to a fake — nothing here opens a browser or touches the network
(Section 15)."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.sources import SourceKind
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


def _render_page() -> AppTest:
    def script():
        import importlib

        page = importlib.import_module("project_context.ui.pages.sources_settings")
        page.render()

    return AppTest.from_function(script)


def _at_for(config, project_id) -> AppTest:
    at = _render_page()
    at.session_state["selected_project_id"] = project_id
    return at


def _connected_calendar_source(isolated_config, project):
    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.credentials.service import CredentialService
        from project_context.credentials.store import CredentialStore
        from project_context.db import sources_repository

        source = sources_repository.insert_source(
            conn, project.id, kind=SourceKind.CALENDAR, display_name="Calendar rules"
        )
        store = CredentialStore(
            credentials_dir=isolated_config.credentials_dir, prefer_keyring=False
        )
        CredentialService(store).connect(conn, project.id, source.id, secret="refresh-token")
        conn.commit()
        return source
    finally:
        conn.close()


def test_calendar_disabled_by_default_shows_the_feature_flag_hint(isolated_config):
    project = _seed_project(isolated_config)
    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any("calendar integration is disabled" in i.value.lower() for i in at.info)


def test_calendar_enabled_without_oauth_client_shows_a_clear_warning(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_CALENDAR_ENABLED", "true")
    project = _seed_project(isolated_config)

    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any("no google oauth client is configured" in w.value.lower() for w in at.warning)


def test_calendar_enabled_and_configured_shows_connect_button_and_readonly_scope_caption(
    isolated_config, monkeypatch
):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_CALENDAR_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)

    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any(b.label == "Connect Calendar" for b in at.button)
    caption_text = " ".join(c.value.lower() for c in at.caption)
    assert "calendar.events.readonly" in caption_text


def test_calendar_connect_button_stores_a_credential_and_flashes_success(
    isolated_config, monkeypatch
):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_CALENDAR_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)

    import project_context.ui.pages.sources_settings as page

    def fake_connect_calendar(
        conn,
        project_id,
        source_id,
        *,
        credential_service,
        client_id,
        client_secret,
        redirect_port,
    ):
        return credential_service.connect(
            conn, project_id, source_id, secret="fake-calendar-refresh-token"
        )

    monkeypatch.setattr(page, "connect_calendar", fake_connect_calendar)

    at = _at_for(isolated_config, project.id)
    at.run()
    at.button(key="connect-calendar").click().run()

    assert not at.exception
    assert any("connected to calendar" in s.value.lower() for s in at.success)

    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.db import sources_repository

        source = sources_repository.get_source_by_kind(conn, project.id, SourceKind.CALENDAR)
        assert source is not None
        assert source.credential_ref is not None
    finally:
        conn.close()


def test_connected_calendar_source_renders_rule_form_prefilled_with_project_name(
    isolated_config, monkeypatch
):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_CALENDAR_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)
    _connected_calendar_source(isolated_config, project)

    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any("project name terms" in ti.label.lower() for ti in at.text_input)
    term_inputs = [ti for ti in at.text_input if "project name terms" in ti.label.lower()]
    assert term_inputs[0].value == "Acme Rollout"
    assert any(b.label == "Sync Project" for b in at.button)
    assert any(b.label == "Disconnect" for b in at.button)


def test_calendar_save_rules_requires_at_least_one_rule(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_CALENDAR_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)
    _connected_calendar_source(isolated_config, project)

    at = _at_for(isolated_config, project.id)
    at.run()

    # Clear the pre-filled project-name-terms field before saving.
    term_inputs = [ti for ti in at.text_input if "project name terms" in ti.label.lower()]
    term_inputs[0].set_value("").run()
    save_buttons = [b for b in at.button if b.label == "Save rules"]
    assert save_buttons
    save_buttons[0].click().run()

    assert not at.exception
    assert any("configure at least one assignment rule" in e.value.lower() for e in at.error)


def test_calendar_save_rules_persists_the_boundary(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_CALENDAR_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)
    _connected_calendar_source(isolated_config, project)

    at = _at_for(isolated_config, project.id)
    at.run()
    save_buttons = [b for b in at.button if b.label == "Save rules"]
    save_buttons[0].click().run()

    assert not at.exception
    assert any("calendar rules saved" in s.value.lower() for s in at.success)

    conn = connect(isolated_config.sqlite_path)
    try:
        import json

        from project_context.db import sources_repository

        source = sources_repository.get_source_by_kind(conn, project.id, SourceKind.CALENDAR)
        boundary = json.loads(source.boundary_json)
        assert boundary["project_name_terms"] == ["Acme Rollout"]
    finally:
        conn.close()


def test_readme_documents_the_calendar_scope_caveat():
    """Prompt 12 requires the sensitive-scope/read-only caveat be
    documented — covers the README half; the UI half is covered by
    `test_calendar_enabled_and_configured_shows_connect_button_and_readonly_scope_caption`
    above."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "calendar.events.readonly" in readme
    assert "sensitive" in readme.lower()


def test_calendar_sync_button_runs_sync_and_displays_counts(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_CALENDAR_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)
    _connected_calendar_source(isolated_config, project)

    import project_context.ui.pages.sources_settings as page
    from project_context.domain.sync import SyncRunStatus

    captured_kwargs: dict = {}

    def fake_sync_calendar_project(conn, project_id, source_id, **kwargs):
        captured_kwargs.update(kwargs)
        conn2 = connect(isolated_config.sqlite_path)
        try:
            from project_context.db import sync_repository

            run = sync_repository.insert_sync_run(conn2, project_id)
            finalized = sync_repository.finalize_sync_run(
                conn2,
                project_id,
                run.id,
                status=SyncRunStatus.COMPLETED,
                discovered_count=2,
                unchanged_count=0,
                downloaded_count=2,
                parsed_count=2,
                extracted_count=1,
                failed_count=0,
                proposed_count=1,
                needs_assignment_count=0,
            )
            conn2.commit()
            return finalized
        finally:
            conn2.close()

    monkeypatch.setattr(page.sync_service, "sync_calendar_project", fake_sync_calendar_project)

    at = _at_for(isolated_config, project.id)
    at.run()
    at.button(key="sync-calendar-project").click().run()

    assert not at.exception
    assert any("sync completed" in s.value.lower() for s in at.success)
    metric_labels = {m.label for m in at.metric}
    expected = {
        "Discovered",
        "Unchanged",
        "Downloaded",
        "Parsed",
        "Extracted",
        "Failed",
        "Unassigned",
    }
    assert expected <= metric_labels
    assert captured_kwargs.get("extraction_provider") is None


def test_calendar_sync_button_never_constructs_an_llm_provider_even_with_api_key_set(
    isolated_config, monkeypatch
):
    """Same bug/fix as Drive's equivalent test — Calendar's Sync Project
    button must never build or pass an LLM provider, even with
    `OPENAI_API_KEY` set."""
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_CALENDAR_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-not-a-real-key")
    project = _seed_project(isolated_config)
    _connected_calendar_source(isolated_config, project)

    import project_context.services.extraction as extraction_service
    import project_context.ui.pages.sources_settings as page
    from project_context.domain.sync import SyncRunStatus

    captured_kwargs: dict = {}

    def fake_sync_calendar_project(conn, project_id, source_id, **kwargs):
        captured_kwargs.update(kwargs)
        conn2 = connect(isolated_config.sqlite_path)
        try:
            from project_context.db import sync_repository

            run = sync_repository.insert_sync_run(conn2, project_id)
            finalized = sync_repository.finalize_sync_run(
                conn2,
                project_id,
                run.id,
                status=SyncRunStatus.COMPLETED,
                discovered_count=1,
                unchanged_count=0,
                downloaded_count=1,
                parsed_count=1,
                extracted_count=0,
                failed_count=0,
                proposed_count=0,
                needs_assignment_count=0,
            )
            conn2.commit()
            return finalized
        finally:
            conn2.close()

    def _fail_if_called(**_kwargs):
        raise AssertionError("Sync Project must never construct an LLM provider")

    monkeypatch.setattr(page.sync_service, "sync_calendar_project", fake_sync_calendar_project)
    monkeypatch.setattr(extraction_service, "build_default_provider", _fail_if_called)

    at = _at_for(isolated_config, project.id)
    at.run()
    at.button(key="sync-calendar-project").click().run()

    assert not at.exception
    assert any("sync completed" in s.value.lower() for s in at.success)
    assert captured_kwargs.get("extraction_provider") is None
