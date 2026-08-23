"""Tests for the ledger kind/status/transition invariants (Prompt 6:
"Status transitions must be constrained by item kind at the
domain-service layer"). Pure domain logic — no database."""

from __future__ import annotations

import pytest

from project_context.domain.ledger import (
    INITIAL_STATUS_BY_KIND,
    VALID_STATUSES_BY_KIND,
    LedgerItemKind,
    LedgerItemStatus,
    LedgerStatusError,
    LedgerTransitionError,
    LedgerTransitionType,
    is_valid_status_for_kind,
    require_valid_status_for_kind,
    validate_transition,
)

# --- per-kind status compatibility ------------------------------------


def test_every_kind_has_a_non_empty_valid_status_set():
    for kind in LedgerItemKind:
        assert VALID_STATUSES_BY_KIND[kind]
        assert INITIAL_STATUS_BY_KIND[kind] in VALID_STATUSES_BY_KIND[kind]


def test_commitment_cannot_be_resolved():
    assert not is_valid_status_for_kind(LedgerItemKind.COMMITMENT, LedgerItemStatus.RESOLVED)


def test_risk_can_be_resolved_but_not_completed():
    assert is_valid_status_for_kind(LedgerItemKind.RISK, LedgerItemStatus.RESOLVED)
    assert not is_valid_status_for_kind(LedgerItemKind.RISK, LedgerItemStatus.COMPLETED)


def test_decision_has_no_open_status():
    assert not is_valid_status_for_kind(LedgerItemKind.DECISION, LedgerItemStatus.OPEN)
    assert is_valid_status_for_kind(LedgerItemKind.DECISION, LedgerItemStatus.ACTIVE)


def test_require_valid_status_for_kind_raises_with_allowed_list_in_message():
    with pytest.raises(LedgerStatusError, match="commitment"):
        require_valid_status_for_kind(LedgerItemKind.COMMITMENT, LedgerItemStatus.RESOLVED)


# --- transitions --------------------------------------------------------


def test_create_lands_on_the_kind_initial_status():
    validate_transition(
        kind=LedgerItemKind.COMMITMENT,
        from_status=None,
        to_status=LedgerItemStatus.OPEN,
        transition_type=LedgerTransitionType.CREATE,
    )
    validate_transition(
        kind=LedgerItemKind.DECISION,
        from_status=None,
        to_status=LedgerItemStatus.ACTIVE,
        transition_type=LedgerTransitionType.CREATE,
    )


def test_create_rejects_a_non_initial_status():
    with pytest.raises(LedgerTransitionError):
        validate_transition(
            kind=LedgerItemKind.COMMITMENT,
            from_status=None,
            to_status=LedgerItemStatus.COMPLETED,
            transition_type=LedgerTransitionType.CREATE,
        )


def test_create_rejects_a_non_none_from_status():
    with pytest.raises(LedgerTransitionError):
        validate_transition(
            kind=LedgerItemKind.COMMITMENT,
            from_status=LedgerItemStatus.OPEN,
            to_status=LedgerItemStatus.OPEN,
            transition_type=LedgerTransitionType.CREATE,
        )


def test_complete_from_open_is_legal_for_commitment():
    validate_transition(
        kind=LedgerItemKind.COMMITMENT,
        from_status=LedgerItemStatus.OPEN,
        to_status=LedgerItemStatus.COMPLETED,
        transition_type=LedgerTransitionType.COMPLETE,
    )


def test_complete_is_not_legal_for_a_kind_without_completed_status():
    with pytest.raises(LedgerStatusError):
        validate_transition(
            kind=LedgerItemKind.RISK,
            from_status=LedgerItemStatus.OPEN,
            to_status=LedgerItemStatus.COMPLETED,
            transition_type=LedgerTransitionType.COMPLETE,
        )


def test_resolve_from_open_is_legal_for_risk():
    validate_transition(
        kind=LedgerItemKind.RISK,
        from_status=LedgerItemStatus.OPEN,
        to_status=LedgerItemStatus.RESOLVED,
        transition_type=LedgerTransitionType.RESOLVE,
    )


def test_cancel_from_active_is_legal():
    validate_transition(
        kind=LedgerItemKind.MILESTONE,
        from_status=LedgerItemStatus.ACTIVE,
        to_status=LedgerItemStatus.CANCELED,
        transition_type=LedgerTransitionType.CANCEL,
    )


def test_fr014_canceled_to_open_without_reopen_is_rejected():
    """FR-014's explicit example: "Invalid transitions (for example
    canceled -> open without reopen review) are rejected."""
    with pytest.raises(LedgerTransitionError):
        validate_transition(
            kind=LedgerItemKind.COMMITMENT,
            from_status=LedgerItemStatus.CANCELED,
            to_status=LedgerItemStatus.OPEN,
            transition_type=LedgerTransitionType.UPDATE,
        )


def test_reopen_from_canceled_is_legal_and_targets_initial_status():
    validate_transition(
        kind=LedgerItemKind.COMMITMENT,
        from_status=LedgerItemStatus.CANCELED,
        to_status=LedgerItemStatus.OPEN,
        transition_type=LedgerTransitionType.REOPEN,
    )


def test_reopen_from_a_non_terminal_status_is_rejected():
    with pytest.raises(LedgerTransitionError):
        validate_transition(
            kind=LedgerItemKind.COMMITMENT,
            from_status=LedgerItemStatus.OPEN,
            to_status=LedgerItemStatus.OPEN,
            transition_type=LedgerTransitionType.REOPEN,
        )


def test_update_must_stay_in_a_non_terminal_status():
    with pytest.raises(LedgerTransitionError):
        validate_transition(
            kind=LedgerItemKind.COMMITMENT,
            from_status=LedgerItemStatus.OPEN,
            to_status=LedgerItemStatus.COMPLETED,
            transition_type=LedgerTransitionType.UPDATE,
        )


def test_update_between_open_and_active_is_legal():
    validate_transition(
        kind=LedgerItemKind.COMMITMENT,
        from_status=LedgerItemStatus.OPEN,
        to_status=LedgerItemStatus.ACTIVE,
        transition_type=LedgerTransitionType.UPDATE,
    )


def test_correct_is_legal_from_any_status_including_terminal_ones():
    validate_transition(
        kind=LedgerItemKind.COMMITMENT,
        from_status=LedgerItemStatus.COMPLETED,
        to_status=LedgerItemStatus.COMPLETED,
        transition_type=LedgerTransitionType.CORRECT,
    )


def test_supersede_from_active_is_legal():
    validate_transition(
        kind=LedgerItemKind.DECISION,
        from_status=LedgerItemStatus.ACTIVE,
        to_status=LedgerItemStatus.SUPERSEDED,
        transition_type=LedgerTransitionType.SUPERSEDE,
    )


def test_supersede_from_a_terminal_status_is_rejected():
    with pytest.raises(LedgerTransitionError):
        validate_transition(
            kind=LedgerItemKind.DECISION,
            from_status=LedgerItemStatus.SUPERSEDED,
            to_status=LedgerItemStatus.SUPERSEDED,
            transition_type=LedgerTransitionType.SUPERSEDE,
        )
