"""Session-state helpers for the currently selected project.

Centralized so every page agrees on one key name and one accessor
pattern instead of touching `st.session_state` ad hoc.
"""

from __future__ import annotations

import streamlit as st

_SELECTED_PROJECT_ID_KEY = "selected_project_id"


def get_selected_project_id() -> str | None:
    """The `project_id` chosen on the Projects page, or None if none is
    selected yet (or the app just started)."""
    return st.session_state.get(_SELECTED_PROJECT_ID_KEY)


def set_selected_project_id(project_id: str | None) -> None:
    """Select a project (or clear the selection with None)."""
    st.session_state[_SELECTED_PROJECT_ID_KEY] = project_id
