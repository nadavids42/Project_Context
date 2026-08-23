"""Project Context — local Streamlit shell.

Bootstrap-stage entry point: application title, a local-development /
privacy notice, and a configuration-health panel. It does not implement
project data, ingestion, the ledger, or LLM calls — see
docs/Project_Context_Product_Plan_v1.md for the full design and
src/project_context/ for the package this app is built on top of.

Run with: streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from project_context.config import ConfigurationError, load_config
from project_context.observability import configure_logging, get_logger

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

st.divider()
st.info(
    "No projects exist yet. Project creation, ingestion, the ledger, and "
    "brief generation are not implemented in this build.",
    icon="ℹ️",
)
