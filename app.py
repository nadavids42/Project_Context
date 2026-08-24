"""Project Context — Streamlit application entry point.

Loads configuration, applies pending database migrations, then hands off
to the real navigation skeleton (Section 6): Projects, Project Overview,
Activity & Review, Ledger Views, Evidence, Briefs, and Sources & Settings.
See docs/Project_Context_Product_Plan_v1.md for the full design and
src/project_context/ for the package this app is built on top of.

Run with: streamlit run app.py
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from project_context.config import ConfigurationError, load_config
from project_context.db.connection import connect
from project_context.db.migrations import MigrationError, run_migrations
from project_context.observability import configure_logging, get_logger
from project_context.ui.chrome import render_privacy_banner
from project_context.ui.navigation import build_navigation, run_navigation_with_busy_guard

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

st.set_page_config(page_title="Project Context", page_icon="🗂️", layout="wide")
render_privacy_banner()

config = None
config_error: ConfigurationError | None = None
try:
    config = load_config()
except ConfigurationError as exc:
    config_error = exc

if config_error is not None:
    st.title("Project Context")
    st.error(f"Configuration failed to load:\n\n{config_error}")
    st.stop()

configure_logging(config.log_level)
logger = get_logger(__name__)

config.ensure_local_directories()
db_error: str | None = None
try:
    bootstrap_conn = connect(config.sqlite_path)
    try:
        run_migrations(bootstrap_conn, MIGRATIONS_DIR)
    finally:
        bootstrap_conn.close()
except MigrationError as exc:
    db_error = str(exc)
    logger.error("migration_failed", extra={"error": db_error})

if db_error is not None:
    st.title("Project Context")
    st.error(f"Database migrations failed:\n\n{db_error}")
    st.info(
        "Application health and migration status are also available on the "
        "Sources & Settings page once this is resolved.",
        icon="ℹ️",
    )
    st.stop()

logger.info("app_started", extra={"environment": config.environment.value})

navigation = st.navigation(build_navigation())
run_navigation_with_busy_guard(navigation)
