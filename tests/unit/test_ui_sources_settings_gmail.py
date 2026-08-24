"""UI tests for the Sources & Settings page's Gmail section (Section 6;
FR-002; Prompt 11). Every OAuth/sync call is monkeypatched to a fake —
nothing here opens a browser or touches the network (Section 15)."""

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


def test_gmail_disabled_by_default_shows_the_feature_flag_hint(isolated_config):
    project = _seed_project(isolated_config)
    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any("gmail integration is disabled" in i.value.lower() for i in at.info)


def test_gmail_enabled_without_oauth_client_shows_a_clear_warning(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_GMAIL_ENABLED", "true")
    project = _seed_project(isolated_config)

    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any("no google oauth client is configured" in w.value.lower() for w in at.warning)


def test_gmail_enabled_and_configured_shows_connect_button_and_restricted_scope_warning(
    isolated_config, monkeypatch
):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_GMAIL_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)

    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any(b.label == "Connect Gmail" for b in at.button)
    caption_text = " ".join(c.value.lower() for c in at.caption)
    assert "gmail.readonly" in caption_text
    assert "restricted" in caption_text


def test_gmail_connect_button_stores_a_credential_and_flashes_success(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_GMAIL_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)

    import project_context.ui.pages.sources_settings as page

    def fake_connect_gmail(
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
            conn, project_id, source_id, secret="fake-gmail-refresh-token"
        )

    monkeypatch.setattr(page, "connect_gmail", fake_connect_gmail)

    at = _at_for(isolated_config, project.id)
    at.run()
    at.button(key="connect-gmail").click().run()

    assert not at.exception
    assert any("connected to gmail" in s.value.lower() for s in at.success)

    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.db import sources_repository

        source = sources_repository.get_source_by_kind(conn, project.id, SourceKind.GMAIL)
        assert source is not None
        assert source.credential_ref is not None
    finally:
        conn.close()


def test_connected_gmail_source_renders_boundary_form_and_sync_button(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_GMAIL_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)

    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.credentials.service import CredentialService
        from project_context.credentials.store import CredentialStore
        from project_context.db import sources_repository

        source = sources_repository.insert_source(
            conn, project.id, kind=SourceKind.GMAIL, display_name="Gmail label/query"
        )
        store = CredentialStore(
            credentials_dir=isolated_config.credentials_dir, prefer_keyring=False
        )
        CredentialService(store).connect(conn, project.id, source.id, secret="refresh-token")
        conn.commit()
    finally:
        conn.close()

    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any("gmail label" in ti.label.lower() for ti in at.text_input)
    assert any("gmail search query" in ti.label.lower() for ti in at.text_input)
    assert any(b.label == "Sync Project" for b in at.button)
    assert any(b.label == "Disconnect" for b in at.button)


def test_gmail_sync_button_runs_sync_and_displays_counts(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_GMAIL_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)

    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.credentials.service import CredentialService
        from project_context.credentials.store import CredentialStore
        from project_context.db import sources_repository

        source = sources_repository.insert_source(
            conn, project.id, kind=SourceKind.GMAIL, display_name="Gmail label/query"
        )
        store = CredentialStore(
            credentials_dir=isolated_config.credentials_dir, prefer_keyring=False
        )
        CredentialService(store).connect(conn, project.id, source.id, secret="refresh-token")
        conn.commit()
    finally:
        conn.close()

    import project_context.ui.pages.sources_settings as page
    from project_context.domain.sync import SyncRunStatus

    def fake_sync_gmail_project(conn, project_id, source_id, **kwargs):
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

    monkeypatch.setattr(page.sync_service, "sync_gmail_project", fake_sync_gmail_project)
    monkeypatch.setattr(page.extraction_service, "build_default_provider", lambda **_: None)

    at = _at_for(isolated_config, project.id)
    at.run()
    at.button(key="sync-gmail-project").click().run()

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


def test_readme_documents_the_gmail_restricted_scope_caveat():
    """Prompt 11: "Document in UI/README that it is a restricted scope
    and a major commercialization constraint." Covers the README half —
    the UI half is covered by
    `test_gmail_enabled_and_configured_shows_connect_button_and_restricted_scope_warning`
    above."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "gmail.readonly" in readme
    assert "restricted" in readme.lower()
    assert "commercialization" in readme.lower()


def test_gmail_boundary_save_requires_a_label_or_query(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_GMAIL_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)

    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.credentials.service import CredentialService
        from project_context.credentials.store import CredentialStore
        from project_context.db import sources_repository

        source = sources_repository.insert_source(
            conn, project.id, kind=SourceKind.GMAIL, display_name="Gmail label/query"
        )
        store = CredentialStore(
            credentials_dir=isolated_config.credentials_dir, prefer_keyring=False
        )
        CredentialService(store).connect(conn, project.id, source.id, secret="refresh-token")
        conn.commit()
    finally:
        conn.close()

    at = _at_for(isolated_config, project.id)
    at.run()
    # form_submit_button keys are auto-generated by Streamlit; drive the
    # form via its submit button found by label instead.
    save_buttons = [b for b in at.button if b.label == "Save boundary"]
    assert save_buttons
    save_buttons[0].click().run()

    assert not at.exception
    assert any("enter a label" in e.value.lower() for e in at.error)
