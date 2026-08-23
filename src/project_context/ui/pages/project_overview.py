"""Project Overview page (Section 6, FR-001).

Shows the project's objective/stage and honestly empty areas for sync,
review, ledger, evidence, and briefs. This step implements project
metadata only — it must never invent counts, dates, or "smart" summaries
for functionality that doesn't exist yet (product principle 7,
"Uncertainty is visible").
"""

from __future__ import annotations

import streamlit as st

from project_context.ui.chrome import status_label
from project_context.ui.db import project_context_connection
from project_context.ui.project_scope import require_selected_project

#: (label, caption) for each not-yet-built area this overview links to.
_EMPTY_AREAS = (
    ("Sync", "Not built in this step. No sources are configured."),
    ("Activity & Review", "Not built in this step. No proposals to review."),
    ("Ledger", "Not built in this step. No ledger items recorded."),
    ("Evidence", "Not built in this step. No evidence stored."),
    ("Briefs", "Not built in this step. No briefs generated."),
    ("Sources & Settings", "Not built in this step. No source boundaries configured."),
)


def render() -> None:
    st.title("Project Overview")
    with project_context_connection() as conn:
        project = require_selected_project(conn)
    if project is None:
        return

    st.subheader(project.name)
    meta_col, detail_col = st.columns([1, 2])
    with meta_col:
        st.metric("Status", status_label(project.status.value))
        st.markdown(f"**Stage:** {project.stage or 'Not stated'}")
        st.markdown(f"**Client/organization:** {project.client_name or 'Not stated'}")
    with detail_col:
        st.markdown("**Objective**")
        st.write(project.objective)
        st.markdown("**Description**")
        st.write(project.description or "Not stated")

    st.divider()
    st.caption(
        "This project has no evidence, review activity, or ledger yet — "
        "nothing below reflects invented data."
    )

    for row in (_EMPTY_AREAS[:3], _EMPTY_AREAS[3:]):
        columns = st.columns(len(row))
        for column, (label, caption) in zip(columns, row, strict=True):
            with column:
                st.markdown(f"**{label}**")
                st.caption(caption)
