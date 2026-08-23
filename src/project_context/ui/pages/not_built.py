"""Generic "not built in this step" page body for nav destinations that
exist in the navigation skeleton (Section 6) but have no functionality
yet. Never a dead link and never fake data: the project identity bar
still renders, and the page states plainly what isn't implemented."""

from __future__ import annotations

import streamlit as st

from project_context.ui.db import project_context_connection
from project_context.ui.project_scope import require_selected_project


def render_not_built(*, page_title: str, description: str) -> None:
    st.title(page_title)
    with project_context_connection() as conn:
        project = require_selected_project(conn)
    if project is None:
        return
    st.info(f"{page_title} is not built in this step. {description}", icon="🚧")
