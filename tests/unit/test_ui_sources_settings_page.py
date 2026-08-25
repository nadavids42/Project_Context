"""UI tests for the Sources & Settings page's Google Drive section
(Section 6; Prompt 10). Every OAuth/sync call is monkeypatched to a
fake — nothing here opens a browser or touches the network (Section 15:
"Keep live credential setup outside automated tests")."""

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


def test_drive_disabled_by_default_shows_the_feature_flag_hint(isolated_config):
    project = _seed_project(isolated_config)

    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any("drive integration is disabled" in i.value.lower() for i in at.info)


def test_drive_enabled_without_oauth_client_shows_a_clear_warning(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_DRIVE_ENABLED", "true")
    project = _seed_project(isolated_config)

    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any("no google oauth client is configured" in w.value.lower() for w in at.warning)


def test_drive_enabled_and_configured_but_not_connected_shows_connect_button(
    isolated_config, monkeypatch
):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_DRIVE_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)

    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any(b.label == "Connect Google Drive" for b in at.button)


def test_connect_button_stores_a_credential_and_flashes_success(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_DRIVE_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)

    import project_context.ui.pages.sources_settings as page

    def fake_connect(
        conn,
        project_id,
        source_id,
        *,
        credential_service,
        client_id,
        client_secret,
        redirect_port,
    ):
        return credential_service.connect(conn, project_id, source_id, secret="fake-refresh-token")

    monkeypatch.setattr(page, "connect_google_drive", fake_connect)

    at = _at_for(isolated_config, project.id)
    at.run()
    at.button(key="connect-google-drive").click().run()

    assert not at.exception
    assert any("connected to google drive" in s.value.lower() for s in at.success)

    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.db import sources_repository

        source = sources_repository.get_source_by_kind(conn, project.id, SourceKind.DRIVE)
        assert source is not None
        assert source.credential_ref is not None
    finally:
        conn.close()


def test_connected_source_renders_folder_form_and_sync_button(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_DRIVE_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)

    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.credentials.service import CredentialService
        from project_context.credentials.store import CredentialStore
        from project_context.db import sources_repository

        source = sources_repository.insert_source(
            conn, project.id, kind=SourceKind.DRIVE, display_name="Drive folder"
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
    assert any("drive folder id" in ti.label.lower() for ti in at.text_input)
    assert any(b.label == "Sync Project" for b in at.button)
    assert any(b.label == "Disconnect" for b in at.button)


def test_sync_button_runs_sync_and_displays_counts(isolated_config, monkeypatch):
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_DRIVE_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    project = _seed_project(isolated_config)

    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.credentials.service import CredentialService
        from project_context.credentials.store import CredentialStore
        from project_context.db import sources_repository

        source = sources_repository.insert_source(
            conn, project.id, kind=SourceKind.DRIVE, display_name="Drive folder"
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

    captured_kwargs: dict = {}

    def fake_sync_drive_project(conn, project_id, source_id, **kwargs):
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
                discovered_count=3,
                unchanged_count=1,
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

    monkeypatch.setattr(page.sync_service, "sync_drive_project", fake_sync_drive_project)

    at = _at_for(isolated_config, project.id)
    at.run()
    at.button(key="sync-drive-project").click().run()

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
    # Prompt-16-follow-up regression: the Sync Project button must never
    # pass a constructed LLM provider through — ingestion only.
    assert captured_kwargs.get("extraction_provider") is None


def test_sync_button_never_constructs_an_llm_provider_even_with_api_key_set(
    isolated_config, monkeypatch
):
    """The exact bug this correction fixes: the Sync Project button used
    to call `extraction_service.build_default_provider()` (reading
    `OPENAI_API_KEY`) and pass the result straight into
    `sync_drive_project`. Sync must never do that, regardless of whether
    a key is configured."""
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_DRIVE_ENABLED", "true")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-not-a-real-key")
    project = _seed_project(isolated_config)

    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.credentials.service import CredentialService
        from project_context.credentials.store import CredentialStore
        from project_context.db import sources_repository

        source = sources_repository.insert_source(
            conn, project.id, kind=SourceKind.DRIVE, display_name="Drive folder"
        )
        store = CredentialStore(
            credentials_dir=isolated_config.credentials_dir, prefer_keyring=False
        )
        CredentialService(store).connect(conn, project.id, source.id, secret="refresh-token")
        conn.commit()
    finally:
        conn.close()

    import project_context.services.extraction as extraction_service
    import project_context.ui.pages.sources_settings as page
    from project_context.domain.sync import SyncRunStatus

    captured_kwargs: dict = {}

    def fake_sync_drive_project(conn, project_id, source_id, **kwargs):
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

    monkeypatch.setattr(page.sync_service, "sync_drive_project", fake_sync_drive_project)
    monkeypatch.setattr(extraction_service, "build_default_provider", _fail_if_called)

    at = _at_for(isolated_config, project.id)
    at.run()
    at.button(key="sync-drive-project").click().run()

    assert not at.exception
    assert any("sync completed" in s.value.lower() for s in at.success)
    assert captured_kwargs.get("extraction_provider") is None
