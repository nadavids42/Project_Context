"""Activity & Review page (Section 6; FR-017 through FR-021; Prompt 8).

Two views, as tabs (Section 6 lists "Activity since last review" and
"Review queue" as separate screens; this build keeps one nav entry and
renders both as tabs rather than adding a second navigation destination):

- **Activity since last review**: what changed and what's waiting —
  pending-proposal counts by action, and recently completed reviews.
- **Review queue**: one card per pending proposal (Section 6's "Review
  card anatomy"), grouped by evidence source and sorted
  conflicts/material changes before low-risk creates
  (`project_context.services.review.list_review_queue` already returns
  them in that order).

All business logic — resolving a review action, validating edits,
building the review-card read model — lives in
`project_context.services.review`; this module only collects form input,
calls it, and renders the result (callbacks stay thin, per Prompt 8).
No bulk-accept control exists anywhere on this page, deliberately —
every action here targets exactly one proposal.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import streamlit as st

from project_context.db import (
    evidence_repository,
    observation_repository,
    people_repository,
    proposed_mutation_repository,
    review_repository,
)
from project_context.domain.ledger import LedgerItem, LedgerItemKind, LedgerItemStatus
from project_context.domain.observations import ObservationStatus
from project_context.domain.review import ProposalEdit, ProposedMutationAction
from project_context.services import reconciliation as reconciliation_service
from project_context.services import review as review_service
from project_context.services.review import EvidenceCitation, ReviewCard
from project_context.ui.chrome import render_highlighted_text
from project_context.ui.db import project_context_connection
from project_context.ui.project_scope import require_selected_project

_FLASH_MESSAGE_KEY = "activity_flash_message"
_NOTE_KEY_PREFIX = "activity_note_"
_UNCHANGED_STATUS_LABEL = "(use the proposal's own action)"
_MATCHED_TARGET_LABEL = "(use the proposal's matched item)"
_UNASSIGNED_OWNER_LABEL = "Unassigned"

_ACTION_LABELS = {
    "create": "CREATE",
    "no_op": "NO CHANGE",
    "add_evidence": "ADD EVIDENCE",
    "update": "UPDATE",
    "complete": "COMPLETE",
    "cancel": "CANCEL",
    "supersede": "SUPERSEDE",
    "conflict": "CONFLICT",
}


def _set_flash(kind: str, text: str) -> None:
    st.session_state[_FLASH_MESSAGE_KEY] = (kind, text)


def _render_flash() -> None:
    flash = st.session_state.pop(_FLASH_MESSAGE_KEY, None)
    if flash is None:
        return
    kind, text = flash
    getattr(st, kind)(text)


def render() -> None:
    st.title("Activity & Review")
    with project_context_connection() as conn:
        project = require_selected_project(conn)
        if project is None:
            return

        _render_flash()

        activity_tab, queue_tab = st.tabs(["Activity since last review", "Review queue"])
        with activity_tab:
            _render_activity_tab(conn, project.id)
        with queue_tab:
            _render_review_queue_tab(conn, project.id)


# ---------------------------------------------------------------------------
# Activity since last review.
# ---------------------------------------------------------------------------


def _render_reconcile_section(conn: sqlite3.Connection, project_id: str) -> None:
    """FR-011/Section 10: turn every saved-but-not-yet-reconciled
    observation (Evidence page's "Extract observations" step) into a
    reviewable proposal. `reconcile_pending_observations` is idempotent —
    an observation that already has a proposal from an earlier run is
    left untouched — so this button is safe to press repeatedly."""
    pending_observations = observation_repository.list_observations_for_project(
        conn, project_id, status=ObservationStatus.VALID
    )
    if not pending_observations:
        return
    st.info(
        f"{len(pending_observations)} observation(s) saved from extraction are "
        "awaiting reconciliation into review proposals.",
        icon="🔄",
    )
    if st.button("Reconcile pending observations", key="reconcile-pending"):
        with st.spinner("Reconciling…"):
            reconciliation_service.reconcile_pending_observations(conn, project_id)
        _set_flash("success", "Reconciliation complete.")
        st.rerun()
    st.divider()


def _render_activity_tab(conn: sqlite3.Connection, project_id: str) -> None:
    _render_reconcile_section(conn, project_id)

    pending = proposed_mutation_repository.list_pending_for_project(conn, project_id)
    if not pending:
        st.info("No pending proposals.", icon="🧭")
    else:
        counts: dict[str, int] = {}
        for proposal in pending:
            counts[proposal.action.value] = counts.get(proposal.action.value, 0) + 1
        cols = st.columns(len(counts) or 1)
        for column, (action, count) in zip(cols, sorted(counts.items()), strict=False):
            column.metric(_ACTION_LABELS.get(action, action), count)

    st.divider()
    st.subheader("Recently reviewed")
    reviews = list(reversed(review_repository.list_for_project(conn, project_id)))[:10]
    if not reviews:
        st.caption("No reviews recorded yet.")
        return
    for review in reviews:
        st.markdown(
            f"**{review.action.value.replace('_', ' ').upper()}** · {review.reviewed_at} "
            f"· by {review.actor}"
        )
        if review.note:
            st.caption(review.note)

    st.divider()
    st.caption(
        "Sync failures and unassigned-evidence counts are not shown here — see "
        "the Sources & Settings page's per-sync counts instead. There is no "
        "dedicated unassigned-evidence review queue yet (a known, tracked "
        "limitation, not an oversight — see docs/RELEASE_NOTES.md); ambiguous "
        "matches are stored, visible in the Evidence list, and simply excluded "
        "from automatic assignment."
    )


# ---------------------------------------------------------------------------
# Review queue.
# ---------------------------------------------------------------------------


def _render_review_queue_tab(conn: sqlite3.Connection, project_id: str) -> None:
    cards = review_service.list_review_queue(conn, project_id)
    if not cards:
        st.info("Nothing pending review.", icon="🧭")
        return

    groups: dict[str, list[ReviewCard]] = {}
    for card in cards:
        key = "Unknown source"
        if card.evidence and card.evidence[0].source is not None:
            key = card.evidence[0].source.title or "Untitled source"
        groups.setdefault(key, []).append(card)

    for source_title, group_cards in groups.items():
        st.subheader(source_title)
        for card in group_cards:
            _render_review_card(conn, project_id, card)


def _person_label(conn: sqlite3.Connection, person_id: str | None) -> str:
    if person_id is None:
        return "Not stated"
    person = people_repository.get_person(conn, person_id)
    return person.display_name if person is not None else "Not stated"


def _render_item_summary(conn: sqlite3.Connection, item: LedgerItem) -> None:
    st.write(item.canonical_title)
    st.caption(item.canonical_description or "Not stated")
    st.markdown(
        f"Status: **{item.status.value}** · Owner: {_person_label(conn, item.owner_person_id)} "
        f"· Due: {item.due_date or 'Not stated'}"
    )


def _render_proposed_patch(
    conn: sqlite3.Connection, proposal, target_item: LedgerItem | None
) -> None:
    patch = proposal.proposed_patch or {}
    if not patch:
        if target_item is not None:
            st.write("No field changes proposed — adds supporting evidence to the current item.")
        else:
            st.write("Not applicable.")
        return
    patch_fields = (
        "kind",
        "canonical_title",
        "canonical_description",
        "owner_person_id",
        "due_date",
    )
    for field_name in patch_fields:
        if field_name not in patch:
            continue
        value = patch[field_name]
        if field_name == "owner_person_id":
            value = _person_label(conn, value)
        current = getattr(target_item, field_name, None) if target_item is not None else None
        if field_name == "owner_person_id" and target_item is not None:
            current = _person_label(conn, target_item.owner_person_id)
        changed = target_item is not None and current != value
        label = field_name.replace("_", " ")
        if changed:
            st.markdown(f"**{label}: {value}**  \n_(was: {current})_")
        else:
            st.markdown(f"{label}: {value}")


def _render_citation(conn: sqlite3.Connection, project_id: str, citation: EvidenceCitation) -> None:
    text = citation.quote
    start, end = 0, len(citation.quote)
    if citation.chunk_id is not None:
        chunk = evidence_repository.get_chunk(conn, project_id, citation.chunk_id)
        if chunk is not None:
            text, start, end = chunk.text, citation.char_start, citation.char_end
    render_highlighted_text(text, start, end)
    if citation.source is not None:
        meta = f"{citation.source.title or 'Untitled'}"
        if citation.source.author:
            meta += f" · {citation.source.author}"
        if citation.source.occurred_at:
            meta += f" · {citation.source.occurred_at}"
        st.caption(meta)
        if citation.source.external_url:
            st.caption(citation.source.external_url)


def _render_why_matched(proposal) -> None:
    features = proposal.candidate_features or {}
    reasons = features.get("action_reasons") or []
    escalation_reasons = features.get("escalation_reasons") or []
    with st.expander("Why matched"):
        tier = features.get("match_tier", "n/a")
        margin = features.get("margin")
        st.caption(f"Match tier: {tier} · margin: {margin}")
        for reason in reasons:
            st.write(f"- {reason}")
        if escalation_reasons:
            st.warning("Escalated: " + ", ".join(escalation_reasons))


def _render_review_card(conn: sqlite3.Connection, project_id: str, card: ReviewCard) -> None:
    proposal = card.proposal
    action_label = _ACTION_LABELS.get(proposal.action.value, proposal.action.value.upper())
    kind_label = card.target_item.kind.value if card.target_item else "new item"
    with st.container(border=True):
        st.markdown(f"#### {action_label} · {kind_label}")
        band = (proposal.confidence_band or "not scored").upper()
        st.caption(f"Confidence: {band} · proposal `{proposal.id}`")

        current_col, proposed_col = st.columns(2)
        with current_col:
            st.markdown("**Current**")
            if card.target_item is not None:
                _render_item_summary(conn, card.target_item)
            else:
                st.write("No existing item — accepting this proposes a new one.")
        with proposed_col:
            st.markdown("**Proposed**")
            _render_proposed_patch(conn, proposal, card.target_item)

        st.markdown("**Evidence**")
        for citation in card.evidence:
            _render_citation(conn, project_id, citation)

        _render_why_matched(proposal)

        note_key = f"{_NOTE_KEY_PREFIX}{proposal.id}"
        note = st.text_input("Note (optional)", key=note_key)
        expected_version = card.target_item.current_version_id if card.target_item else None

        _render_quick_actions(conn, project_id, card, note, expected_version)
        with st.expander("Edit & accept"):
            _render_edit_and_accept_form(conn, project_id, card, note, expected_version)


def _run_action(action_callable, *, success_message: str) -> None:
    try:
        action_callable()
    except review_service.StaleReviewError as exc:
        st.error(f"This review card is out of date — reload the page. ({exc})")
        return
    except (
        review_service.ReviewValidationError,
        review_service.ReviewActionNotApplicableError,
        review_service.CrossProjectReferenceError,
    ) as exc:
        st.error(str(exc))
        return
    except (LookupError, ValueError) as exc:
        st.error(str(exc))
        return
    _set_flash("success", success_message)
    st.rerun()


def _render_quick_actions(
    conn: sqlite3.Connection,
    project_id: str,
    card: ReviewCard,
    note: str,
    expected_version: str | None,
) -> None:
    proposal = card.proposal
    cols = st.columns(4)
    can_accept = proposal.action is not ProposedMutationAction.CONFLICT
    if cols[0].button("Accept", key=f"accept-{proposal.id}", disabled=not can_accept):
        _run_action(
            lambda: review_service.accept_proposal(
                conn,
                project_id,
                proposal.id,
                note=note or None,
                expected_target_version_id=expected_version,
            ),
            success_message="Proposal accepted.",
        )
    if cols[1].button("Reject", key=f"reject-{proposal.id}"):
        _run_action(
            lambda: review_service.reject_proposal(
                conn, project_id, proposal.id, note=note or None
            ),
            success_message="Proposal rejected.",
        )
    no_target = card.target_item is None
    if cols[2].button("Mark complete", key=f"complete-{proposal.id}", disabled=no_target):
        _run_action(
            lambda: review_service.mark_complete(
                conn,
                project_id,
                proposal.id,
                note=note or None,
                expected_target_version_id=expected_version,
            ),
            success_message="Marked complete.",
        )
    if cols[3].button("Treat as new item", key=f"new-{proposal.id}"):
        _run_action(
            lambda: review_service.treat_as_new(conn, project_id, proposal.id, note=note or None),
            success_message="Created as a new item.",
        )


def _render_edit_and_accept_form(
    conn: sqlite3.Connection,
    project_id: str,
    card: ReviewCard,
    note: str,
    expected_version: str | None,
) -> None:
    proposal = card.proposal
    patch = proposal.proposed_patch or {}
    target = card.target_item

    default_title = patch.get("canonical_title") or (target.canonical_title if target else "") or ""
    default_description = (
        patch.get("canonical_description") or (target.canonical_description if target else "") or ""
    )
    default_kind = (
        LedgerItemKind(patch["kind"]) if patch.get("kind") else (target.kind if target else None)
    )
    kind_options = list(LedgerItemKind)
    kind_index = kind_options.index(default_kind) if default_kind in kind_options else 0

    project_people = people_repository.list_project_people(conn, project_id)
    owner_options = [_UNASSIGNED_OWNER_LABEL]
    owner_by_label: dict[str, str] = {}
    for pp in project_people:
        person = people_repository.get_person(conn, pp.person_id)
        if person is None:
            continue
        owner_options.append(person.display_name)
        owner_by_label[person.display_name] = pp.person_id
    default_owner_id = patch.get("owner_person_id") or (target.owner_person_id if target else None)
    default_owner_label = (
        _person_label(conn, default_owner_id) if default_owner_id else _UNASSIGNED_OWNER_LABEL
    )
    if default_owner_label not in owner_options:
        owner_options.append(default_owner_label)
        owner_by_label[default_owner_label] = default_owner_id

    default_due_date = patch.get("due_date") or (target.due_date if target else None)

    status_options = [_UNCHANGED_STATUS_LABEL] + [s.value for s in LedgerItemStatus]

    candidates = (proposal.candidate_features or {}).get("candidates") or []
    other_candidate_ids = [
        c["ledger_item_id"]
        for c in candidates
        if c["ledger_item_id"] != proposal.target_ledger_item_id
    ]
    target_options = [_MATCHED_TARGET_LABEL, *other_candidate_ids]

    with st.form(key=f"edit-accept-{proposal.id}"):
        title = st.text_input("Title", value=default_title, key=f"title-{proposal.id}")
        description = st.text_area(
            "Description", value=default_description, key=f"desc-{proposal.id}"
        )
        kind_label = st.selectbox(
            "Kind",
            options=[k.value for k in kind_options],
            index=kind_index,
            key=f"kind-{proposal.id}",
        )
        owner_label = st.selectbox(
            "Owner",
            options=owner_options,
            index=owner_options.index(default_owner_label),
            key=f"owner-{proposal.id}",
        )
        has_due_date = st.checkbox(
            "Set due date", value=bool(default_due_date), key=f"has-due-{proposal.id}"
        )
        due_date_value = st.date_input(
            "Due date",
            value=date.fromisoformat(default_due_date) if default_due_date else date.today(),
            key=f"due-{proposal.id}",
            disabled=not has_due_date,
        )
        status_label_choice = st.selectbox(
            "Resulting status", options=status_options, key=f"status-{proposal.id}"
        )
        target_choice = None
        if len(target_options) > 1:
            target_choice = st.selectbox(
                "Target item",
                options=target_options,
                key=f"target-{proposal.id}",
                help="Redirect this proposal to a different close candidate.",
            )
        evidence_options = {f"{c.link_id[:8]}… — {c.quote[:80]}": c.link_id for c in card.evidence}
        selected_evidence_labels = st.multiselect(
            "Evidence to cite",
            options=list(evidence_options.keys()),
            default=list(evidence_options.keys()),
            key=f"evidence-{proposal.id}",
        )
        submitted = st.form_submit_button("Save edit & accept")

    if not submitted:
        return

    owner_person_id = None
    if owner_label != _UNASSIGNED_OWNER_LABEL:
        owner_person_id = owner_by_label.get(owner_label)
    status = None
    if status_label_choice != _UNCHANGED_STATUS_LABEL:
        status = LedgerItemStatus(status_label_choice)
    evidence_link_ids = tuple(evidence_options[label] for label in selected_evidence_labels)

    edits = ProposalEdit(
        target_ledger_item_id=(
            target_choice if target_choice and target_choice != _MATCHED_TARGET_LABEL else None
        ),
        kind=LedgerItemKind(kind_label),
        canonical_title=title or None,
        canonical_description=description or None,
        owner_person_id=owner_person_id,
        due_date=due_date_value.isoformat() if has_due_date else None,
        status=status,
        evidence_link_ids=evidence_link_ids or None,
    )
    _run_action(
        lambda: review_service.edit_and_accept_proposal(
            conn,
            project_id,
            proposal.id,
            edits,
            note=note or None,
            expected_target_version_id=expected_version,
        ),
        success_message="Edited and accepted.",
    )
