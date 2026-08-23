"""Sources & Settings page (Section 6; FR-002, FR-004, FR-027, FR-031;
Prompt 10).

Google Drive connect/configure/sync now lives here, gated entirely
behind `AppConfig.feature_drive_enabled` and `google_oauth_is_configured`
— with Drive disabled (the default) this page renders exactly as it did
before Prompt 10, with no import error and no live network call
(Prompt 10: "Feature flag must allow the entire Drive integration to
remain disabled without import/startup failure"). This page also
carries forward the application-level configuration and database health
check that used to live at the app root.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from project_context.config import AppConfig, load_config
from project_context.connectors.errors import ConnectorError
from project_context.credentials.service import MASKED_SECRET_DISPLAY, CredentialService
from project_context.credentials.store import CredentialStore
from project_context.db import sources_repository, sync_repository
from project_context.db.health import check_database_health
from project_context.domain.sources import Source, SourceHealthStatus, SourceKind
from project_context.services import extraction as extraction_service
from project_context.services import sync as sync_service
from project_context.services.google_connect import GoogleConnectError, connect_google_drive
from project_context.ui.db import project_context_connection
from project_context.ui.project_scope import require_selected_project

#: src/project_context/ui/pages/sources_settings.py -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_DIR = _REPO_ROOT / "migrations"

_FLASH_KEY = "sources_settings_flash"
_LAST_SYNC_COUNTS_KEY = "sources_settings_last_sync_counts"

_HEALTH_RENDER = {
    SourceHealthStatus.UNCONFIGURED: ("info", "Not configured"),
    SourceHealthStatus.READY: ("info", "Ready — not yet synced"),
    SourceHealthStatus.SYNCING: ("info", "Syncing…"),
    SourceHealthStatus.HEALTHY: ("success", "Healthy"),
    SourceHealthStatus.DEGRADED: ("warning", "Degraded — see last error below"),
    SourceHealthStatus.REAUTH_REQUIRED: ("error", "Reauthorization required — reconnect below"),
    SourceHealthStatus.DISABLED: ("warning", "Disabled"),
}


def _set_flash(kind: str, text: str) -> None:
    st.session_state[_FLASH_KEY] = (kind, text)


def _render_flash() -> None:
    flash = st.session_state.pop(_FLASH_KEY, None)
    if flash is None:
        return
    kind, text = flash
    getattr(st, kind)(text)


def render() -> None:
    st.title("Sources & Settings")
    with project_context_connection() as conn:
        project = require_selected_project(conn)
        if project is not None:
            _render_flash()
            _render_drive_section(conn, project.id)

    st.divider()
    st.subheader("Application health")
    _render_application_health()


# ---------------------------------------------------------------------------
# Google Drive.
# ---------------------------------------------------------------------------


def _credential_service(config: AppConfig) -> CredentialService:
    return CredentialService(CredentialStore(credentials_dir=config.credentials_dir))


def _render_drive_section(conn, project_id: str) -> None:
    st.subheader("Google Drive")
    config = load_config()

    if not config.feature_drive_enabled:
        st.info(
            "Drive integration is disabled. Set `PROJECT_CONTEXT_FEATURE_DRIVE_ENABLED=true` "
            "to enable it (Google OAuth client credentials are also required below).",
            icon="🚧",
        )
        return
    if not config.google_oauth_is_configured:
        st.warning(
            "Drive is enabled but no Google OAuth client is configured. Set "
            "`PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID` and "
            "`PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET` from a Google Cloud **Desktop app** "
            "OAuth client, then restart."
        )
        return

    st.caption(
        "Requests `drive.readonly` only — a Google-classified **restricted** scope. This is "
        "appropriate for a private, local, single-user prototype and is **not suitable for a "
        "casual public launch** without Google's restricted-scope verification and a security "
        "review (see Sections 11.2 and 16 of the product plan)."
    )

    source = sources_repository.get_source_by_kind(conn, project_id, SourceKind.DRIVE)
    credential_service = _credential_service(config)

    if source is None or source.credential_ref is None:
        _render_connect_form(conn, project_id, source, credential_service, config)
        return

    _render_connected_drive(conn, project_id, source, credential_service, config)


def _render_connect_form(
    conn, project_id: str, source: Source | None, credential_service: CredentialService,
    config: AppConfig,
) -> None:
    st.write("Not connected.")
    if st.button("Connect Google Drive", key="connect-google-drive"):
        try:
            if source is None:
                source = sources_repository.insert_source(
                    conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
                )
            with st.spinner("Waiting for authorization in your browser…"):
                connect_google_drive(
                    conn, project_id, source.id, credential_service=credential_service,
                    client_id=config.google_oauth_client_id,
                    client_secret=config.google_oauth_client_secret,
                    redirect_port=config.google_oauth_redirect_port,
                )
        except GoogleConnectError as exc:
            st.error(str(exc))
            return
        _set_flash("success", "Connected to Google Drive.")
        st.rerun()


def _render_connected_drive(
    conn, project_id: str, source: Source, credential_service: CredentialService,
    config: AppConfig,
) -> None:
    kind, label = _HEALTH_RENDER.get(source.health_status, ("info", source.health_status.value))
    getattr(st, kind)(f"**{label}**  ·  secret: `{MASKED_SECRET_DISPLAY}`")

    freshness_col, error_col = st.columns(2)
    with freshness_col:
        st.metric("Last successful sync", source.last_success_at or "Never")
    with error_col:
        st.metric("Last safe error", source.last_error_code or "None")

    boundary = json.loads(source.boundary_json) if source.boundary_json else {}
    with st.form("drive-folder-form"):
        folder_id = st.text_input(
            "Drive folder ID", value=boundary.get("folder_id", ""),
            help="The ID from the folder's Drive URL: drive.google.com/drive/folders/<this part>",
        )
        preview_col, save_col = st.columns(2)
        preview_clicked = preview_col.form_submit_button("Preview")
        save_clicked = save_col.form_submit_button("Save folder")

    if preview_clicked:
        _render_preview(conn, project_id, source, credential_service, config, folder_id.strip())
    if save_clicked and folder_id.strip():
        sources_repository.update_boundary(
            conn, project_id, source.id, boundary_json=json.dumps({"folder_id": folder_id.strip()})
        )
        _set_flash("success", "Folder boundary saved.")
        st.rerun()

    enabled_col, disconnect_col = st.columns(2)
    with enabled_col:
        if source.enabled:
            if st.button("Disable", key="disable-drive"):
                sources_repository.set_enabled(conn, project_id, source.id, enabled=False)
                _set_flash("success", "Drive source disabled.")
                st.rerun()
        else:
            if st.button("Enable", key="enable-drive"):
                sources_repository.set_enabled(conn, project_id, source.id, enabled=True)
                _set_flash("success", "Drive source enabled.")
                st.rerun()
    with disconnect_col:
        if st.button("Disconnect", key="disconnect-drive"):
            credential_service.disconnect(conn, project_id, source.id)
            _set_flash("success", "Disconnected. Credential material was deleted.")
            st.rerun()

    st.divider()
    _render_sync_section(conn, project_id, source, credential_service, config)


def _render_preview(
    conn, project_id: str, source: Source, credential_service: CredentialService,
    config: AppConfig, folder_id: str,
) -> None:
    if not folder_id:
        st.error("Enter a folder ID first.")
        return
    access_token = sync_service.mint_drive_access_token(
        conn, project_id, source.id, credential_service=credential_service,
        google_client_id=config.google_oauth_client_id,
        google_client_secret=config.google_oauth_client_secret,
    )
    if access_token is None:
        st.error("Reauthorization required — disconnect and connect again.")
        return
    from project_context.connectors.drive import DriveConnector
    from project_context.connectors.http import RequestsHttpTransport

    connector = DriveConnector(
        access_token=access_token, folder_id=folder_id, http_transport=RequestsHttpTransport()
    )
    try:
        health = connector.validate_config()
        if health.status.value != "ok":
            st.error(f"Folder check failed: {health.detail or health.status.value}")
            return
        results = connector.preview({"folder_id": folder_id}, limit=20)
    except ConnectorError as exc:
        st.error(f"Preview failed: {exc.safe_message}")
        return
    if not results:
        st.info("No matching files found in this folder (recursively).")
        return
    st.markdown(f"**{len(results)} matching item(s)** (showing up to 20):")
    for item in results:
        st.write(f"- {item.title} · `{item.mime_type}` · modified {item.version_marker}")


def _render_sync_section(
    conn, project_id: str, source: Source, credential_service: CredentialService,
    config: AppConfig,
) -> None:
    st.subheader("Sync Project")
    if not source.enabled:
        st.caption("Enable this source to sync.")
        return
    if st.button("Sync Project", key="sync-drive-project"):
        provider = extraction_service.build_default_provider()
        with st.spinner("Syncing…"):
            run = sync_service.sync_drive_project(
                conn, project_id, source.id, credential_service=credential_service,
                google_client_id=config.google_oauth_client_id,
                google_client_secret=config.google_oauth_client_secret,
                evidence_dir=config.evidence_dir,
                chunk_target_chars=config.chunk_target_chars,
                chunk_overlap_ratio=config.chunk_overlap_ratio,
                extraction_provider=provider,
                extraction_model=config.openai_model,
            )
        st.session_state[_LAST_SYNC_COUNTS_KEY] = run
        st.rerun()

    last_run = st.session_state.get(_LAST_SYNC_COUNTS_KEY)
    if last_run is not None:
        _render_sync_result(last_run)

    _render_sync_history(conn, project_id, source.id)


def _render_sync_result(run) -> None:
    status_renderer = {"completed": st.success, "partial": st.warning, "failed": st.error}
    status_renderer.get(run.status.value, st.info)(f"Sync {run.status.value}.")
    cols = st.columns(7)
    labels = [
        ("Discovered", run.discovered_count), ("Unchanged", run.unchanged_count),
        ("Downloaded", run.downloaded_count), ("Parsed", run.parsed_count),
        ("Extracted", run.extracted_count), ("Failed", run.failed_count),
        ("Unassigned", run.needs_assignment_count),
    ]
    for column, (label, value) in zip(cols, labels, strict=False):
        column.metric(label, value)


def _render_sync_history(conn, project_id: str, source_id: str) -> None:
    """`sync_runs` is project-scoped, not source-scoped (Section 9) —
    with only one connector kind implemented so far, every run shown
    here is a Drive run, but a future second connector kind would need
    this to filter by `sync_items.source_id` instead."""
    with st.expander("Sync history"):
        runs = sync_repository.list_sync_runs_for_project(conn, project_id, limit=10)
        if not runs:
            st.caption("No syncs yet.")
            return
        for run in runs:
            st.markdown(
                f"**{run.status.value}** · {run.started_at} · "
                f"discovered {run.discovered_count} · failed {run.failed_count}"
            )


# ---------------------------------------------------------------------------
# Application health (unchanged from before Prompt 10).
# ---------------------------------------------------------------------------


def _render_application_health() -> None:
    config = load_config()

    status_col, details_col = st.columns([1, 2])
    with status_col:
        st.success("Configuration loaded")
        st.metric("Data directory exists", "Yes" if config.data_dir.exists() else "No")
        st.metric("Evidence directory exists", "Yes" if config.evidence_dir.exists() else "No")
    with details_col:
        st.markdown(
            f"- **Environment:** `{config.environment.value}`\n"
            f"- **SQLite path:** `{config.sqlite_path}`\n"
            f"- **Evidence directory:** `{config.evidence_dir}`\n"
            f"- **Log level:** `{config.log_level}`\n"
            f"- **OpenAI model:** `{config.openai_model}`\n"
        )

    st.markdown("**Connector feature flags**")
    flags = {
        "Google Drive": config.feature_drive_enabled,
        "Gmail": config.feature_gmail_enabled,
        "Calendar": config.feature_calendar_enabled,
        "Fathom": config.feature_fathom_enabled,
    }
    st.table({"Connector": list(flags.keys()), "Enabled": list(flags.values())})

    health = check_database_health(config.sqlite_path, MIGRATIONS_DIR)
    if health.error is not None:
        st.error(f"Database health check failed:\n\n{health.error}")
        return

    db_status_col, db_details_col = st.columns([1, 2])
    with db_status_col:
        if health.ok:
            st.success("Database ready")
        else:
            st.warning("Database has pending migrations")
        st.metric("Foreign keys enabled", "Yes" if health.foreign_keys_enabled else "No")
    with db_details_col:
        st.markdown(
            f"- **Journal mode:** `{health.journal_mode}`\n"
            f"- **Applied migrations:** `{health.applied_migration_count}`\n"
            f"- **Pending migrations:** `{health.pending_migration_count}`\n"
        )
