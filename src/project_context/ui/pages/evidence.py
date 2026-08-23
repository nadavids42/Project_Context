"""Evidence page (Section 6). Not built in this step — see
docs/Project_Context_Product_Plan_v1.md FR-005 through FR-009 for the
manual ingestion and evidence-span behavior this page will eventually
implement."""

from __future__ import annotations

from project_context.ui.pages.not_built import render_not_built


def render() -> None:
    render_not_built(
        page_title="Evidence",
        description=(
            "Manually entered text, uploaded files, and connector-sourced "
            "artifacts will appear here once evidence ingestion is implemented."
        ),
    )
