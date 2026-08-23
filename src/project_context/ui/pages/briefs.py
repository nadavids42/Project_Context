"""Briefs page (Section 6). Not built in this step — see
docs/Project_Context_Product_Plan_v1.md FR-024 through FR-026 for the
Current Project Brief and Meeting Preparation Brief behavior this page
will eventually implement."""

from __future__ import annotations

from project_context.ui.pages.not_built import render_not_built


def render() -> None:
    render_not_built(
        page_title="Briefs",
        description=(
            "The Current Project Brief and Meeting Preparation Brief will be "
            "generated here once ledger state and brief generation are "
            "implemented."
        ),
    )
