"""Shared "resolve the selected project or show an honest empty state"
helper for every project-scoped page (Project Overview, Activity &
Review, Ledger Views, Evidence, Briefs, Sources & Settings)."""

from __future__ import annotations

import sqlite3

import streamlit as st

from project_context.domain.projects import Project
from project_context.services.projects import ProjectNotFoundError, get_project
from project_context.ui.chrome import render_project_identity_bar
from project_context.ui.session import get_selected_project_id, set_selected_project_id


def require_selected_project(conn: sqlite3.Connection) -> Project | None:
    """Resolve the selected project for a project-scoped page.

    Always renders the project identity bar (or an honest "no project
    selected" notice) as a side effect. Returns the project, or None if
    the caller should stop rendering anything further this run.
    """
    project_id = get_selected_project_id()
    if project_id is None:
        render_project_identity_bar(None)
        return None

    try:
        project = get_project(conn, project_id)
    except ProjectNotFoundError:
        set_selected_project_id(None)
        st.warning("The previously selected project no longer exists.", icon="⚠️")
        render_project_identity_bar(None)
        return None

    render_project_identity_bar(project)
    return project
