"""The scripted, ground-truth-driven reviewer (Section 13.4: "review
proposals using a predefined reviewer protocol"; Section 13.7 step 4:
"Reviewer follows a written policy... no ad hoc prompt editing mid-run").

This is deliberately **not** "accept whatever reconciliation proposed."
A real reviewer looks at the evidence and the candidate, decides what
actually happened, and applies the correct review action — including
correcting a wrong action/target/kind the system proposed. This module
is that reviewer, made deterministic by consulting ground truth (which a
real reviewer would effectively be reconstructing from their own
judgment) instead of a human.

One concrete, load-bearing case this protects against: reconciliation's
own `classify_action` (`project_context.domain.reconciliation`) always
proposes a plain `COMPLETE` action for a risk/blocker/open_question
observation that uses completion language, because
`ProposedMutationAction` has no `resolve` value — but `COMPLETED` is not
a legal status for those kinds
(`project_context.domain.ledger.VALID_STATUSES_BY_KIND`). Accepting such
a proposal as-is would raise `LedgerStatusError`. A correct reviewer
recognizes "this risk was resolved," not "this risk was completed," and
edits the status accordingly — exactly what
`ProposalEdit(status=LedgerItemStatus.RESOLVED)` does here. Every other
branch below applies the same principle: derive the *correct* ledger
transition from ground truth, and use
`project_context.services.review.edit_and_accept_proposal` to apply it
regardless of which action/target reconciliation itself guessed. This
also makes the protocol usable, unchanged, against live-model extraction
(Section 13.4's live-model mode) where reconciliation's guess is far
less reliable than in scripted/fake mode.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus
from project_context.domain.review import ProposalEdit, ProposedMutation, ProposedMutationAction
from project_context.evaluation.schema import TransitionType
from project_context.services import review as review_service
from project_context.services.review import ReviewOutcome


@dataclass(frozen=True)
class ExpectedOutcome:
    """What a correct reviewer must do with the proposal for one
    scripted observation, derived from ground truth (never from the
    proposal itself)."""

    item_id: str
    kind: LedgerItemKind
    canonical_title: str
    transition_type: TransitionType
    owner_person_id: str | None
    due_date: str | None
    status: LedgerItemStatus | None
    #: SUPERSEDE only.
    predecessor_item_id: str | None = None
    #: True for a repeated-wording mention of an already-created item
    #: (Section 13.2's trap) — the correct action is ADD_EVIDENCE, never
    #: a second CREATE.
    is_repeat_mention: bool = False


def review_scripted_proposal(
    conn: sqlite3.Connection,
    project_id: str,
    proposal: ProposedMutation,
    *,
    expected: ExpectedOutcome,
    item_ledger_ids: dict[str, str],
) -> ReviewOutcome:
    """Apply the one correct review action for `proposal`, given
    `expected` (this observation's ground-truth-derived outcome) and
    `item_ledger_ids` (item_id -> real `ledger_items.id`, mutated in
    place as items are created so later transitions on the same item can
    find their target)."""
    if expected.is_repeat_mention:
        target_ledger_item_id = item_ledger_ids[expected.item_id]
        edits = ProposalEdit(
            action=ProposedMutationAction.ADD_EVIDENCE,
            target_ledger_item_id=target_ledger_item_id,
        )
        return review_service.edit_and_accept_proposal(conn, project_id, proposal.id, edits)

    if expected.transition_type is TransitionType.CREATE:
        if proposal.action is ProposedMutationAction.CREATE:
            outcome = review_service.accept_proposal(conn, project_id, proposal.id)
        else:
            outcome = review_service.treat_as_new(
                conn,
                project_id,
                proposal.id,
                kind=expected.kind,
                canonical_title=expected.canonical_title,
                owner_person_id=expected.owner_person_id,
                due_date=expected.due_date,
            )
        assert outcome.ledger_item is not None
        item_ledger_ids[expected.item_id] = outcome.ledger_item.id
        return outcome

    if expected.transition_type is TransitionType.SUPERSEDE:
        assert expected.predecessor_item_id is not None
        predecessor_ledger_id = item_ledger_ids[expected.predecessor_item_id]
        edits = ProposalEdit(
            target_ledger_item_id=predecessor_ledger_id,
            status=LedgerItemStatus.SUPERSEDED,
            kind=expected.kind,
            canonical_title=expected.canonical_title,
            owner_person_id=expected.owner_person_id,
            due_date=expected.due_date,
        )
        outcome = review_service.edit_and_accept_proposal(conn, project_id, proposal.id, edits)
        assert outcome.ledger_item is not None
        item_ledger_ids[expected.item_id] = outcome.ledger_item.id
        return outcome

    # UPDATE_OWNER / UPDATE_DATE / UPDATE_STATUS: all target an existing item.
    target_ledger_item_id = item_ledger_ids[expected.item_id]
    if expected.transition_type is TransitionType.UPDATE_STATUS:
        edits = ProposalEdit(target_ledger_item_id=target_ledger_item_id, status=expected.status)
    else:
        edits = ProposalEdit(
            target_ledger_item_id=target_ledger_item_id,
            action=ProposedMutationAction.UPDATE,
            owner_person_id=expected.owner_person_id,
            due_date=expected.due_date,
        )
    return review_service.edit_and_accept_proposal(conn, project_id, proposal.id, edits)
