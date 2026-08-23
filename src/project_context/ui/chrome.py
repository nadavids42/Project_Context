"""Small pieces of UI shown consistently across pages: the privacy
banner and the "which project am I looking at" identity bar required by
Section 6 ("Everything else is scoped to one project and visibly
displays the project name")."""

from __future__ import annotations

import streamlit as st

from project_context.domain.projects import Project

_STATUS_LABELS = {
    "active": "Active",
    "on_hold": "On hold",
    "completed": "Completed",
    "archived": "Archived",
}


def status_label(status: str) -> str:
    """Human-readable label for a `ProjectStatus` value."""
    return _STATUS_LABELS.get(status, status)


def render_privacy_banner() -> None:
    st.warning(
        "Local development build. Use only synthetic, personal, public, or "
        "explicitly authorized data — never employer or customer data without "
        "explicit written permission. Source content is stored on this device. "
        "Once extraction is implemented, selected text will be sent to the "
        "configured LLM provider; this build does not send anything anywhere.",
        icon="⚠️",
    )


def render_project_identity_bar(project: Project | None) -> None:
    """Show which project a project-scoped page is currently operating on,
    or an honest notice that none is selected. Never invents a project."""
    if project is None:
        st.info(
            "No project selected. Choose a project from **Projects** to use this page.",
            icon="🧭",
        )
        return
    client_suffix = f" · {project.client_name}" if project.client_name else ""
    st.caption(f"PROJECT — {project.name}{client_suffix} · {status_label(project.status.value)}")
