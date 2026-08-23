"""Project Context — local Streamlit shell.

Bootstrap-stage entry point: application title, a local-development /
privacy notice, and a configuration-health panel. It does not implement
project data, ingestion, the ledger, or LLM calls — see
docs/Project_Context_Product_Plan_v1.md for the full design and
src/project_context/ for the package this app is built on top of.

Run with: streamlit run app.py
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from project_context.config import ConfigurationError, load_config
from project_context.db.connection import connect
from project_context.db.health import check_database_health
from project_context.db.migrations import MigrationError, run_migrations
from project_context.observability import configure_logging, get_logger

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

st.set_page_config(page_title="Project Context", page_icon="🗂️", layout="wide")

st.title("Project Context")
st.caption("Local, single-user, evidence-backed project intelligence — prototype")

st.warning(
    "Local development build. Use only synthetic, personal, public, or "
    "explicitly authorized data — never employer or customer data without "
    "explicit written permission. Source content is stored on this device. "
    "Once extraction is implemented, selected text will be sent to the "
    "configured LLM provider; this build does not send anything anywhere.",
    icon="⚠️",
)

st.subheader("Application health")

config = None
config_error: ConfigurationError | None = None
try:
    config = load_config()
except ConfigurationError as exc:
    config_error = exc

if config_error is not None:
    st.error(f"Configuration failed to load:\n\n{config_error}")
else:
    configure_logging(config.log_level)
    logger = get_logger(__name__)
    logger.info("app_started", extra={"environment": config.environment.value})

    status_col, details_col = st.columns([1, 2])
    with status_col:
        st.success("Configuration loaded")
        st.metric("Data directory exists", "Yes" if config.data_dir.exists() else "No")
        st.metric("Evidence directory exists", "Yes" if config.evidence_dir.exists() else "No")
    with details_col:
        st.markdown(
            f"- **Environment:** `{config.environment.value}`\n"
            f"- **Data directory:** `{config.data_dir}`\n"
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

    st.markdown("**Database**")
    config.ensure_local_directories()
    db_error: str | None = None
    try:
        db_conn = connect(config.sqlite_path)
        try:
            run_migrations(db_conn, MIGRATIONS_DIR)
        finally:
            db_conn.close()
    except MigrationError as exc:
        db_error = str(exc)
        logger.error("migration_failed", extra={"error": db_error})

    if db_error is not None:
        st.error(f"Database migrations failed:\n\n{db_error}")
    else:
        health = check_database_health(config.sqlite_path, MIGRATIONS_DIR)
        if health.error is not None:
            st.error(f"Database health check failed:\n\n{health.error}")
        else:
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

st.divider()
st.info(
    "No projects exist yet. Project creation, ingestion, the ledger, and "
    "brief generation are not implemented in this build.",
    icon="ℹ️",
)
