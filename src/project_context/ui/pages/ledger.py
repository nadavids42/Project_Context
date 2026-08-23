"""Ledger Views page (Section 6). Not built in this step — see
docs/Project_Context_Product_Plan_v1.md Section 9 for the
`ledger_items`/`ledger_versions` tables this page will eventually read."""

from __future__ import annotations

from project_context.ui.pages.not_built import render_not_built


def render() -> None:
    render_not_built(
        page_title="Ledger Views",
        description=(
            "Commitments, decisions, risks, blockers, milestones, and open "
            "questions will appear here once the ledger is implemented."
        ),
    )
