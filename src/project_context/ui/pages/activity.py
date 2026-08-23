"""Activity & Review page (Section 6). Not built in this step — see
docs/Project_Context_Product_Plan_v1.md FR-017 through FR-021 for the
review-queue behavior this page will eventually implement."""

from __future__ import annotations

from project_context.ui.pages.not_built import render_not_built


def render() -> None:
    render_not_built(
        page_title="Activity & Review",
        description=(
            "New evidence, proposed ledger changes, sync failures, and "
            "unassigned items will appear here once sync and reconciliation "
            "are implemented."
        ),
    )
