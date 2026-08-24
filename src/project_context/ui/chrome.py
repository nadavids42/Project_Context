"""Small pieces of UI shown consistently across pages: the privacy
banner, the "which project am I looking at" identity bar required by
Section 6 ("Everything else is scoped to one project and visibly
displays the project name"), and the evidence-span highlighter shared by
the Evidence page and the Activity & Review page (Section 6's review
card anatomy: "Exact quote/span...accessible without leaving the review
card")."""

from __future__ import annotations

import html

import streamlit as st

from project_context.db.connection import DatabaseBusyError
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
    """The exact disclosure Section 16 ("Local vs hosted processing")
    requires the UI to state — kept in sync with what this build
    actually does, not what an earlier build did. Update this text in
    the same change that changes what data leaves this device."""
    st.warning(
        "Local, single-user prototype — not a secured, multi-tenant product. "
        "Use only synthetic, personal, public, or explicitly authorized data — "
        "never employer or customer data without explicit written permission. "
        "Source content is stored on this device. When you click **Extract "
        "observations** (or generate a brief), that chunk's text is sent to "
        "the configured LLM provider (`store: false`, no provider-hosted "
        "retention) — nothing is sent automatically or in the background, and "
        "extraction stays disabled until an API key is configured. Enabled "
        "connectors (Drive/Gmail/Calendar/Fathom) send normal authenticated, "
        "read-only requests to their own providers when you click Sync.",
        icon="⚠️",
    )


def render_database_busy_error(exc: DatabaseBusyError) -> None:
    """The "clear UI" half of Section 15's SQLite busy/locked
    requirement — a friendly, actionable message instead of a raw
    traceback. `app.build_navigation`-driven pages reach this through
    `project_context.ui.navigation.run_navigation_with_busy_guard`,
    which is the one place every page's rendering funnels through, so
    no individual page needs its own `except DatabaseBusyError`."""
    st.error(f"⏳ {exc.safe_message}")


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


def render_highlighted_text(text: str, char_start: int, char_end: int) -> None:
    """Render `text` with `[char_start, char_end)` visually highlighted —
    the one way this app shows an exact evidence span (FR-008; Section
    6's review card "Evidence" region and the Evidence page's viewer)."""
    before = html.escape(text[:char_start])
    middle = html.escape(text[char_start:char_end])
    after = html.escape(text[char_end:])
    markup = (
        "<div style='white-space: pre-wrap; font-family: monospace;'>"
        f"{before}<mark>{middle}</mark>{after}"
        "</div>"
    )
    st.markdown(markup, unsafe_allow_html=True)
