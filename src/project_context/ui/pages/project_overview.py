"""Project Overview page (Section 6, FR-001).

Shows the project's objective/stage, real pending-review and ledger
counts now that Activity & Review and Ledger Views are built (Prompt 8),
and honestly empty areas for the parts that still aren't (sync,
evidence, briefs). Must never invent counts, dates, or "smart" summaries
for functionality that doesn't exist yet (product principle 7,
"Uncertainty is visible") — the built areas show real query results,
never a placeholder claiming activity that didn't happen.
"""

from __future__ import annotations

import streamlit as st

from project_context.db import ledger_repository, proposed_mutation_repository
from project_context.ui.chrome import status_label
from project_context.ui.db import project_context_connection
from project_context.ui.project_scope import require_selected_project

#: (label, caption) for each area of this overview that is still not
#: built at all.
_EMPTY_AREAS = (
    ("Sync", "Not built in this step. No sources are configured."),
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
        pending = proposed_mutation_repository.list_pending_for_project(conn, project.id)
        items = ledger_repository.list_items_for_project(conn, project.id)
        metric_cols = st.columns(2)
        metric_cols[0].metric("Pending review", len(pending))
        metric_cols[1].metric("Ledger items", len(items))
        if pending:
            st.caption("See Activity & Review to act on pending proposals.")
        if items:
            st.caption("See Ledger Views for commitments, decisions, risks, and more.")

    st.divider()
    st.caption("This project has no evidence or briefs yet — nothing below reflects invented data.")

    columns = st.columns(len(_EMPTY_AREAS))
    for column, (label, caption) in zip(columns, _EMPTY_AREAS, strict=True):
        with column:
            st.markdown(f"**{label}**")
            st.caption(caption)
