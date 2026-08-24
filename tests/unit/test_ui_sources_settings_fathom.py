"""UI tests for the Sources & Settings page's Fathom section (Section 6;
FR-002, FR-030; Prompt 13). Every connector HTTP call is monkeypatched
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
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_FATHOM_ENABLED", "true")
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


def _connected_fathom_source(isolated_config, project):
    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.credentials.service import CredentialService
        from project_context.credentials.store import CredentialStore
        from project_context.db import sources_repository

        source = sources_repository.insert_source(
            conn, project.id, kind=SourceKind.FATHOM, display_name="Fathom API key"
        )
        store = CredentialStore(
            credentials_dir=isolated_config.credentials_dir, prefer_keyring=False
        )
        CredentialService(store).connect(conn, project.id, source.id, secret="fake-api-key")
        conn.commit()
        return source
    finally:
        conn.close()


def test_fathom_disabled_by_default_shows_the_feature_flag_hint(isolated_config, monkeypatch):
    monkeypatch.delenv("PROJECT_CONTEXT_FEATURE_FATHOM_ENABLED", raising=False)
    monkeypatch.setenv("PROJECT_CONTEXT_FEATURE_FATHOM_ENABLED", "false")
    project = _seed_project(isolated_config)
    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any("fathom integration is disabled" in i.value.lower() for i in at.info)


def test_fathom_enabled_shows_connect_form_and_no_oauth_language(isolated_config):
    project = _seed_project(isolated_config)
    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any(b.label == "Connect Fathom" for b in at.button)
    caption_text = " ".join(c.value.lower() for c in at.caption)
    assert "x-api-key" in caption_text
    assert "never fathom oauth" in caption_text or "never" in caption_text


def test_fathom_connect_form_requires_a_key(isolated_config):
    project = _seed_project(isolated_config)
    at = _at_for(isolated_config, project.id)
    at.run()
    connect_buttons = [b for b in at.button if b.label == "Connect Fathom"]
    assert connect_buttons
    connect_buttons[0].click().run()

    assert not at.exception
    assert any("enter an api key" in e.value.lower() for e in at.error)


def test_fathom_connect_form_stores_a_credential_and_flashes_success(isolated_config):
    project = _seed_project(isolated_config)
    at = _at_for(isolated_config, project.id)
    at.run()

    key_inputs = [ti for ti in at.text_input if "fathom api key" in ti.label.lower()]
    assert key_inputs
    key_inputs[0].set_value("test-fathom-api-key")
    connect_buttons = [b for b in at.button if b.label == "Connect Fathom"]
    connect_buttons[0].click().run()

    assert not at.exception
    assert any("connected to fathom" in s.value.lower() for s in at.success)

    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.db import sources_repository

        source = sources_repository.get_source_by_kind(conn, project.id, SourceKind.FATHOM)
        assert source is not None
        assert source.credential_ref is not None
    finally:
        conn.close()


def test_connected_fathom_source_renders_rule_form_and_sync_button(isolated_config):
    project = _seed_project(isolated_config)
    _connected_fathom_source(isolated_config, project)

    at = _at_for(isolated_config, project.id)
    at.run()

    assert not at.exception
    assert any("client email domain" in ti.label.lower() for ti in at.text_input)
    assert any(b.label == "Sync Project" for b in at.button)
    assert any(b.label == "Disconnect" for b in at.button)


def test_fathom_save_rules_requires_at_least_one_rule(isolated_config):
    project = _seed_project(isolated_config)
    _connected_fathom_source(isolated_config, project)

    at = _at_for(isolated_config, project.id)
    at.run()
    save_buttons = [b for b in at.button if b.label == "Save rules"]
    assert save_buttons
    save_buttons[0].click().run()

    assert not at.exception
    assert any("configure at least one assignment rule" in e.value.lower() for e in at.error)


def test_fathom_save_rules_persists_the_boundary(isolated_config):
    project = _seed_project(isolated_config)
    _connected_fathom_source(isolated_config, project)

    at = _at_for(isolated_config, project.id)
    at.run()
    domain_inputs = [ti for ti in at.text_input if "client email domain" in ti.label.lower()]
    domain_inputs[0].set_value("acme.com")
    save_buttons = [b for b in at.button if b.label == "Save rules"]
    save_buttons[0].click().run()

    assert not at.exception
    assert any("fathom rules saved" in s.value.lower() for s in at.success)

    conn = connect(isolated_config.sqlite_path)
    try:
        import json

        from project_context.db import sources_repository

        source = sources_repository.get_source_by_kind(conn, project.id, SourceKind.FATHOM)
        boundary = json.loads(source.boundary_json)
        assert boundary["client_domain"] == "acme.com"
    finally:
        conn.close()


def test_fathom_disconnect_deletes_the_credential(isolated_config):
    project = _seed_project(isolated_config)
    _connected_fathom_source(isolated_config, project)

    at = _at_for(isolated_config, project.id)
    at.run()
    disconnect_buttons = [b for b in at.button if b.label == "Disconnect"]
    disconnect_buttons[0].click().run()

    assert not at.exception
    conn = connect(isolated_config.sqlite_path)
    try:
        from project_context.db import sources_repository

        source = sources_repository.get_source_by_kind(conn, project.id, SourceKind.FATHOM)
        assert source.credential_ref is None
        assert source.enabled is False
    finally:
        conn.close()


def test_fathom_sync_button_runs_sync_and_displays_counts(isolated_config, monkeypatch):
    project = _seed_project(isolated_config)
    _connected_fathom_source(isolated_config, project)

    import project_context.ui.pages.sources_settings as page
    from project_context.domain.sync import SyncRunStatus

    def fake_sync_fathom_project(conn, project_id, source_id, **kwargs):
        conn2 = connect(isolated_config.sqlite_path)
        try:
            from project_context.db import sync_repository

            run = sync_repository.insert_sync_run(conn2, project_id)
            finalized = sync_repository.finalize_sync_run(
                conn2, project_id, run.id, status=SyncRunStatus.COMPLETED,
                discovered_count=1, unchanged_count=0, downloaded_count=1, parsed_count=1,
                extracted_count=1, failed_count=0, proposed_count=1, needs_assignment_count=0,
            )
            conn2.commit()
            return finalized
        finally:
            conn2.close()

    monkeypatch.setattr(page.sync_service, "sync_fathom_project", fake_sync_fathom_project)
    monkeypatch.setattr(page.extraction_service, "build_default_provider", lambda **_: None)

    at = _at_for(isolated_config, project.id)
    at.run()
    at.button(key="sync-fathom-project").click().run()

    assert not at.exception
    assert any("sync completed" in s.value.lower() for s in at.success)
    metric_labels = {m.label for m in at.metric}
    expected = {
        "Discovered", "Unchanged", "Downloaded", "Parsed", "Extracted", "Failed", "Unassigned",
    }
    assert expected <= metric_labels


def test_fathom_preview_flags_scheduled_event_only_matches_as_unassigned(
    isolated_config, monkeypatch
):
    project = _seed_project(isolated_config)
    _connected_fathom_source(isolated_config, project)

    from project_context.connectors.fathom import FathomPreview
    from project_context.connectors.protocol import ArtifactMetadata
    from project_context.domain.evidence import ArtifactType

    def fake_connector(**kwargs):
        class _FakeConnector:
            def preview_detailed(self, boundary, *, limit):
                matched = ArtifactMetadata(
                    external_id="rec1", title="Weekly slot", artifact_type=ArtifactType.MEETING,
                    version_marker="v1",
                    extra={"match_rule": "scheduled_event", "match_reason": "time window match"},
                )
                return FathomPreview(matched=[matched], unmatched_sample=[])

        return _FakeConnector()

    # `_render_fathom_preview` imports `FathomConnector` locally at call
    # time (mirrors Calendar/Gmail's own preview functions), so patching
    # the name on its defining module — not on the `page` module — is
    # what actually takes effect.
    monkeypatch.setattr("project_context.connectors.fathom.FathomConnector", fake_connector)

    at = _at_for(isolated_config, project.id)
    at.run()

    windows_inputs = [
        ta for ta in at.text_area if "bounded time windows" in ta.label.lower()
    ]
    windows_inputs[0].set_value("2026-06-01T00:00:00Z,2026-06-02T00:00:00Z")
    preview_buttons = [b for b in at.button if b.label == "Preview"]
    preview_buttons[0].click().run()

    assert not at.exception
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "weekly slot" in markdown_text.lower() or "Weekly slot" in markdown_text
    assert any("unassigned evidence" in m.value.lower() for m in at.markdown)


def test_readme_documents_fathom_and_zoom_drive_compatibility():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "x-api-key" in readme.lower()
    assert "zoom" in readme.lower()
