"""Builds the app's Streamlit pages and navigation structure (Section 6,
"Navigation model") fresh on every script run.

`st.Page` binds to whichever `ScriptRunContext` is active when it's
constructed. Streamlit's entrypoint script (`app.py`) re-executes fully
on every rerun, but a plain Python *module* is imported — and therefore
executed — only once per process. If the `st.Page` objects were built at
this module's top level, the very first rerun would create them
correctly and every later rerun (and every other session) would keep
reusing those same, now-stale objects, which breaks `st.navigation()`
(observed directly: it raises inside Streamlit's own navigation code on
reuse). So navigation is built via `build_navigation()`, called anew
each run from `app.py`, rather than cached at import time.
"""

from __future__ import annotations

import streamlit as st

from project_context.ui.pages import (
    activity,
    briefs,
    evidence,
    ledger,
    project_overview,
    projects,
    sources_settings,
)

#: (title, icon, url_path) for Project Overview — shared between
#: `build_navigation()` and `overview_page()` so the two can't drift.
_OVERVIEW_SPEC = ("Project Overview", "🧭", "overview")


def _make_page(render, title: str, icon: str, url_path: str, *, default: bool = False) -> st.Page:
    return st.Page(render, title=title, icon=icon, url_path=url_path, default=default)


def build_navigation() -> dict[str, list[st.Page]]:
    """A fresh navigation structure for `st.navigation()`, matching the
    mermaid diagram in Section 6: Projects is global; everything else
    hangs off one selected project."""
    return {
        "Projects": [_make_page(projects.render, "Projects", "📁", "projects", default=True)],
        "Project": [
            _make_page(project_overview.render, *_OVERVIEW_SPEC),
            _make_page(activity.render, "Activity & Review", "🔎", "activity"),
            _make_page(ledger.render, "Ledger Views", "📒", "ledger"),
            _make_page(evidence.render, "Evidence", "📎", "evidence"),
            _make_page(briefs.render, "Briefs", "📝", "briefs"),
            _make_page(sources_settings.render, "Sources & Settings", "⚙️", "sources-settings"),
        ],
    }


def overview_page() -> st.Page:
    """A fresh `Page` for `st.switch_page`, matching the Project Overview
    entry `build_navigation()` registers for the current run. Streamlit
    matches a switch target by page source + `url_path`, not object
    identity, so building a new one here — rather than importing a
    cached instance — is both correct and necessary (see module
    docstring)."""
    return _make_page(project_overview.render, *_OVERVIEW_SPEC)
