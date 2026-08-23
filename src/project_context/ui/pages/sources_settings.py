"""Sources & Settings page (Section 6).

Source/connector configuration (FR-002) is not built in this step. This
page also carries forward the application-level configuration and
database health check that used to live at the app root (app.py before
real navigation existed) — it fits naturally under "Settings" and stays
useful as a diagnostic regardless of which project is selected.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from project_context.config import load_config
from project_context.db.health import check_database_health
from project_context.ui.db import project_context_connection
from project_context.ui.project_scope import require_selected_project

#: src/project_context/ui/pages/sources_settings.py -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_DIR = _REPO_ROOT / "migrations"


def render() -> None:
    st.title("Sources & Settings")
    with project_context_connection() as conn:
        project = require_selected_project(conn)
    if project is not None:
        st.info(
            "Source & connector configuration is not built in this step. "
            "Manual evidence upload, and Drive, Gmail, calendar, and Fathom "
            "boundaries, will be configured here once connectors are "
            "implemented.",
            icon="🚧",
        )

    st.divider()
    st.subheader("Application health")
    _render_application_health()


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

    st.markdown("**Connector feature flags** (all disabled until connectors are implemented)")
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
