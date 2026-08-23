"""Ledger Views page (Section 6, Section 9): one tab per item kind
(commitments, decisions, risks/blockers, milestones, open questions,
stakeholders), plus a history/evidence drawer for whichever item is
selected — versions, transitions, reviews, and citations (Prompt 8).

Read-only: nothing on this page writes to the ledger. Corrections and
transitions happen through the Activity & Review page's review actions
(`project_context.services.review`) — FR-012's "the only ledger mutation
service requires a reviewed proposal or explicit manual action" applies
here too, so this page has no edit controls of its own.
"""

from __future__ import annotations

import sqlite3

import streamlit as st

from project_context.db import (
    correction_repository,
    evidence_link_repository,
    ledger_repository,
    people_repository,
    review_repository,
)
from project_context.domain.evidence_links import EvidenceLinkTargetType
from project_context.domain.ledger import LedgerItem, LedgerItemKind
from project_context.domain.review import CorrectionTargetType
from project_context.ui.chrome import render_highlighted_text
from project_context.ui.db import project_context_connection
from project_context.ui.project_scope import require_selected_project

_SELECTED_ITEM_KEY = "ledger_selected_item_id"

_TAB_SPECS: tuple[tuple[str, tuple[LedgerItemKind, ...]], ...] = (
    ("Commitments", (LedgerItemKind.COMMITMENT,)),
    ("Decisions", (LedgerItemKind.DECISION,)),
    ("Risks & Blockers", (LedgerItemKind.RISK, LedgerItemKind.BLOCKER)),
    ("Milestones", (LedgerItemKind.MILESTONE,)),
    ("Open Questions", (LedgerItemKind.OPEN_QUESTION,)),
    ("Stakeholders", (LedgerItemKind.STAKEHOLDER,)),
)


def render() -> None:
    st.title("Ledger Views")
    with project_context_connection() as conn:
        project = require_selected_project(conn)
        if project is None:
            return

        selected_id = st.session_state.get(_SELECTED_ITEM_KEY)
        if selected_id:
            _render_history_drawer(conn, project.id, selected_id)
            st.divider()

        tabs = st.tabs([label for label, _kinds in _TAB_SPECS])
        for tab, (_label, kinds) in zip(tabs, _TAB_SPECS, strict=True):
            with tab:
                _render_kind_list(conn, project.id, kinds)


def _person_label(conn: sqlite3.Connection, person_id: str | None) -> str:
    if person_id is None:
        return "Not stated"
    person = people_repository.get_person(conn, person_id)
    return person.display_name if person is not None else "Not stated"


def _render_kind_list(
    conn: sqlite3.Connection, project_id: str, kinds: tuple[LedgerItemKind, ...]
) -> None:
    items: list[LedgerItem] = []
    for kind in kinds:
        items.extend(ledger_repository.list_items_for_project(conn, project_id, kind=kind))
    items.sort(key=lambda item: item.updated_at, reverse=True)

    if not items:
        st.info("No items of this kind yet.", icon="🧭")
        return

    header_cols = st.columns([3, 2, 2, 2, 1, 1])
    for column, label in zip(
        header_cols, ["Title", "Owner", "Due/target date", "Status", "Evidence", ""], strict=True
    ):
        column.markdown(f"**{label}**")

    for item in items:
        cols = st.columns([3, 2, 2, 2, 1, 1])
        cols[0].write(item.canonical_title)
        if item.canonical_description:
            cols[0].caption(item.canonical_description)
        cols[1].write(_person_label(conn, item.owner_person_id))
        cols[2].write(item.due_date or "Not stated")
        status_text = item.status.value
        if item.status.value in ("canceled", "superseded"):
            cols[3].markdown(f":red[{status_text}]")
        elif item.status.value in ("completed", "resolved"):
            cols[3].markdown(f":green[{status_text}]")
        else:
            cols[3].write(status_text)
        evidence_count = len(
            evidence_link_repository.list_for_target(
                conn, project_id, EvidenceLinkTargetType.LEDGER_ITEM, item.id
            )
        )
        cols[4].write(str(evidence_count))
        if cols[5].button("History", key=f"history-{item.id}"):
            st.session_state[_SELECTED_ITEM_KEY] = item.id
            st.rerun()
        if item.superseded_by_item_id:
            successor = ledger_repository.get_item(conn, project_id, item.superseded_by_item_id)
            if successor is not None:
                st.caption(f"Superseded by: {successor.canonical_title}")
        if item.supersedes_item_id:
            predecessor = ledger_repository.get_item(conn, project_id, item.supersedes_item_id)
            if predecessor is not None:
                st.caption(f"Supersedes: {predecessor.canonical_title}")


# ---------------------------------------------------------------------------
# History/evidence drawer.
# ---------------------------------------------------------------------------


def _render_history_drawer(conn: sqlite3.Connection, project_id: str, item_id: str) -> None:
    item = ledger_repository.get_item(conn, project_id, item_id)
    if item is None:
        st.warning("That item no longer exists in this project.")
        st.session_state.pop(_SELECTED_ITEM_KEY, None)
        return

    header_col, close_col = st.columns([5, 1])
    with header_col:
        st.subheader(f"History — {item.canonical_title}")
    with close_col:
        if st.button("Close", key="close-history"):
            st.session_state.pop(_SELECTED_ITEM_KEY, None)
            st.rerun()

    st.markdown(
        f"**Kind:** {item.kind.value} · **Status:** {item.status.value} · "
        f"**Owner:** {_person_label(conn, item.owner_person_id)} · "
        f"**Due:** {item.due_date or 'Not stated'}"
    )
    if item.superseded_by_item_id:
        successor = ledger_repository.get_item(conn, project_id, item.superseded_by_item_id)
        if successor is not None and st.button(
            f"→ View successor: {successor.canonical_title}", key="goto-successor"
        ):
            st.session_state[_SELECTED_ITEM_KEY] = successor.id
            st.rerun()
    if item.supersedes_item_id:
        predecessor = ledger_repository.get_item(conn, project_id, item.supersedes_item_id)
        if predecessor is not None and st.button(
            f"→ View predecessor: {predecessor.canonical_title}", key="goto-predecessor"
        ):
            st.session_state[_SELECTED_ITEM_KEY] = predecessor.id
            st.rerun()

    version_tab, corrections_tab = st.tabs(["Versions & evidence", "Corrections"])
    with version_tab:
        _render_versions(conn, project_id, item_id)
    with corrections_tab:
        _render_corrections(conn, project_id, item_id)


def _render_versions(conn: sqlite3.Connection, project_id: str, item_id: str) -> None:
    versions = ledger_repository.list_versions_for_item(conn, project_id, item_id)
    for version in reversed(versions):
        label = f"v{version.version_no} · {version.transition_type.value} · {version.status.value}"
        with st.expander(label, expanded=version.valid_to is None):
            st.markdown(f"**Title:** {version.canonical_title}")
            st.markdown(f"**Description:** {version.canonical_description or 'Not stated'}")
            st.markdown(
                f"**Owner:** {_person_label(conn, version.owner_person_id)} · "
                f"**Due:** {version.due_date or 'Not stated'}"
            )
            st.caption(
                f"Valid from {version.valid_from} to {version.valid_to or '(current)'} · "
                f"confidence: {version.confidence_band.value if version.confidence_band else 'n/a'}"
            )
            if version.review_id:
                review = review_repository.get_review(conn, project_id, version.review_id)
                if review is not None:
                    st.markdown(
                        f"**Review:** {review.action.value} by {review.actor} "
                        f"at {review.reviewed_at}"
                    )
                    if review.note:
                        st.caption(f"Note: {review.note}")
            if version.superseded_by_version_id:
                st.caption(f"Superseded by version id: {version.superseded_by_version_id}")

            st.markdown("**Citations**")
            links = evidence_link_repository.list_for_target(
                conn, project_id, EvidenceLinkTargetType.LEDGER_VERSION, version.id
            )
            if not links:
                st.caption("No evidence linked to this specific version.")
            for link in links:
                render_highlighted_text(link.quote, 0, len(link.quote))
                st.caption(f"Role: {link.support_role.value}")


def _render_corrections(conn: sqlite3.Connection, project_id: str, item_id: str) -> None:
    all_corrections = correction_repository.list_for_project(conn, project_id)
    item_corrections = [
        c
        for c in all_corrections
        if c.target_type is CorrectionTargetType.LEDGER_ITEM and c.target_id == item_id
    ]
    if not item_corrections:
        st.caption("No corrections recorded for this item.")
        return
    for correction in item_corrections:
        st.markdown(
            f"**{correction.field_name}** — {correction.reason_code.value} "
            f"({correction.materiality.value})"
        )
        original = (correction.original or {}).get("value")
        corrected = (correction.corrected or {}).get("value")
        st.caption(f"Was: {original!r} → Now: {corrected!r} · by {correction.actor}")
