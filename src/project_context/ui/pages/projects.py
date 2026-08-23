"""Projects page (Section 6): the entry point of Project Context.

List, create, edit, and archive/restore projects. This is the only page
that is not project-scoped — it is how a project *becomes* selected for
every other page. All business logic (validation, transactions, audit
entries) lives in `project_context.services.projects`; this module only
collects form input and renders results.
"""

from __future__ import annotations

import sqlite3

import streamlit as st

from project_context.domain.projects import (
    EDITABLE_STATUSES,
    NAME_MAX_LENGTH,
    OBJECTIVE_MAX_LENGTH,
    Project,
    ProjectCreateInput,
    ProjectStatus,
    ProjectUpdateInput,
)
from project_context.services.projects import (
    ProjectNotFoundError,
    ProjectStateError,
    archive_project,
    create_project,
    edit_project,
    list_archived_projects,
    list_projects,
    restore_project,
)
from project_context.ui.chrome import status_label
from project_context.ui.db import project_context_connection
from project_context.ui.session import set_selected_project_id

_STATUS_OPTIONS = [status.value for status in EDITABLE_STATUSES]


def render() -> None:
    st.title("Projects")

    if st.button("New Project", type="primary", icon="➕"):
        _open_new_project_dialog()

    show_archived = st.radio("Show", options=["Active", "Archived"], horizontal=True) == "Archived"

    with project_context_connection() as conn:
        projects = list_archived_projects(conn) if show_archived else list_projects(conn)

        if not projects:
            st.info(
                "No archived projects."
                if show_archived
                else "No active projects yet. Use **New Project** to create one.",
                icon="🧭",
            )
            return

        header = st.columns([3, 1, 2, 2, 3])
        for column, label in zip(
            header, ["Name", "Status", "Client", "Updated", "Actions"], strict=True
        ):
            column.markdown(f"**{label}**")

        for project in projects:
            _render_project_row(conn, project)


def _render_project_row(conn: sqlite3.Connection, project: Project) -> None:
    with st.container(border=True):
        name_col, status_col, client_col, updated_col, actions_col = st.columns([3, 1, 2, 2, 3])
        name_col.write(project.name)
        status_col.write(status_label(project.status.value))
        client_col.write(project.client_name or "Not stated")
        updated_col.write(project.updated_at)

        with actions_col:
            button_cols = st.columns(3)
            if button_cols[0].button("Open", key=f"open-{project.id}"):
                _select_and_open(project.id)
            if project.status is ProjectStatus.ARCHIVED:
                if button_cols[1].button("Restore", key=f"restore-{project.id}"):
                    _restore(project.id)
            else:
                if button_cols[1].button("Edit", key=f"edit-{project.id}"):
                    _open_edit_project_dialog(project)
                if button_cols[2].button("Archive", key=f"archive-{project.id}"):
                    _open_archive_confirm_dialog(project)


def _select_and_open(project_id: str) -> None:
    from project_context.ui.navigation import overview_page

    set_selected_project_id(project_id)
    st.switch_page(overview_page())


def _restore(project_id: str) -> None:
    with project_context_connection() as conn:
        try:
            restore_project(conn, project_id)
        except (ProjectNotFoundError, ProjectStateError) as exc:
            st.error(str(exc))
            return
    st.rerun()


@st.dialog("New Project")
def _open_new_project_dialog() -> None:
    _render_project_form(mode="create")


@st.dialog("Edit Project")
def _open_edit_project_dialog(project: Project) -> None:
    _render_project_form(mode="edit", project=project)


@st.dialog("Archive project?")
def _open_archive_confirm_dialog(project: Project) -> None:
    st.write(
        f"Archive **{project.name}**? It will be hidden from the active project "
        "list. Evidence and history are not deleted, and it can be restored later."
    )
    confirm_col, cancel_col = st.columns(2)
    if confirm_col.button("Archive", type="primary", key=f"confirm-archive-{project.id}"):
        with project_context_connection() as conn:
            try:
                archive_project(conn, project.id)
            except (ProjectNotFoundError, ProjectStateError) as exc:
                st.error(str(exc))
                return
        st.rerun()
    if cancel_col.button("Cancel", key=f"cancel-archive-{project.id}"):
        st.rerun()


def _render_project_form(*, mode: str, project: Project | None = None) -> None:
    form_key = f"project-form-{mode}-{project.id if project else 'new'}"
    with st.form(key=form_key):
        name = st.text_input(
            "Name*", value=project.name if project else "", max_chars=NAME_MAX_LENGTH
        )
        objective = st.text_area(
            "Objective*",
            value=project.objective if project else "",
            max_chars=OBJECTIVE_MAX_LENGTH,
        )
        description = st.text_area(
            "Description", value=(project.description or "") if project else ""
        )
        stage = st.text_input("Stage", value=(project.stage or "") if project else "")
        client_name = st.text_input(
            "Client/organization", value=(project.client_name or "") if project else ""
        )
        default_status = project.status.value if project else ProjectStatus.ACTIVE.value
        status_value = st.selectbox(
            "Status",
            options=_STATUS_OPTIONS,
            index=_STATUS_OPTIONS.index(default_status) if default_status in _STATUS_OPTIONS else 0,
        )
        submitted = st.form_submit_button("Save")

    if not submitted:
        return

    try:
        if mode == "create":
            data = ProjectCreateInput(
                name=name,
                objective=objective,
                description=description or None,
                stage=stage or None,
                client_name=client_name or None,
                status=ProjectStatus(status_value),
            )
            with project_context_connection() as conn:
                create_project(conn, data)
        else:
            assert project is not None
            data = ProjectUpdateInput(
                name=name,
                objective=objective,
                description=description or None,
                stage=stage or None,
                client_name=client_name or None,
                status=ProjectStatus(status_value),
            )
            with project_context_connection() as conn:
                edit_project(conn, project.id, data)
    except (ValueError, ProjectNotFoundError, ProjectStateError) as exc:
        st.error(str(exc))
        return

    st.rerun()
