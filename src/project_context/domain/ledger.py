"""Ledger domain model: the current-projection item, its append-only
version history, and the kind/status/transition invariants Prompt 6
requires be "constrained by item kind at the domain-service layer."

See Section 9 (tables `ledger_items`, `ledger_versions`) and Section 10
(status/transition vocabulary — reconciliation's *use* of these rules is
explicitly out of scope here; only the rules themselves are needed to
validate any future write path, including Prompt 8's review transaction).

Neither the plan's Section 9 table listing nor Section 10's narrative
fully enumerates which statuses are valid for which item kind, or which
(from_status, transition_type) pairs are legal — this module is the
single source of truth for both, chosen to be consistent with every
concrete example the plan does give (FR-014's "canceled -> open without
reopen review is rejected"; Section 10.6's completion language; Section
10.7's cancellation language; Section 10.10's supersession). The
per-kind status CHECK constraint in
migrations/0005_ledger_and_review.sql mirrors `VALID_STATUSES_BY_KIND`
exactly and must be kept in sync by hand if either changes.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class LedgerItemKind(StrEnum):
    """Matches the `ledger_items.kind` CHECK constraint exactly.

    Deliberately the eight `project_context.llm.schemas.ObservationKind`
    values minus `UPDATE` — an `update` observation always targets an
    *existing* item of one of these seven kinds (Section 10.3: "update |
    all, but only if it explicitly references the item") and never
    creates a ledger item on its own.
    """

    DECISION = "decision"
    COMMITMENT = "commitment"
    MILESTONE = "milestone"
    RISK = "risk"
    BLOCKER = "blocker"
    OPEN_QUESTION = "open_question"
    STAKEHOLDER = "stakeholder"


class LedgerItemStatus(StrEnum):
    """Matches the `ledger_items.status`/`ledger_versions.status` CHECK
    constraints exactly (Section 9's "common" status list)."""

    OPEN = "open"
    ACTIVE = "active"
    COMPLETED = "completed"
    RESOLVED = "resolved"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"


class LedgerTransitionType(StrEnum):
    """Matches the `ledger_versions.transition_type` CHECK constraint
    exactly (Section 9)."""

    CREATE = "create"
    UPDATE = "update"
    COMPLETE = "complete"
    RESOLVE = "resolve"
    CANCEL = "cancel"
    SUPERSEDE = "supersede"
    REOPEN = "reopen"
    CORRECT = "correct"


class ConfidenceBand(StrEnum):
    """Matches the `confidence_band` CHECK constraints on `ledger_items`
    and `proposed_mutations` (Section 10.12)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: Which statuses are legal for each item kind. A "commitment" is never
#: "resolved" (that word is reserved for risks/blockers/open questions);
#: a "decision" has no "open"/"resolved" state at all — extraction only
#: proposes a decision once it was actually made (Section 12.3: "The
#: model is explicitly told... source summaries/action items are
#: evidence... not automatically authoritative truth" applies equally to
#: treating a *discussed* decision as made).
VALID_STATUSES_BY_KIND: dict[LedgerItemKind, frozenset[LedgerItemStatus]] = {
    LedgerItemKind.COMMITMENT: frozenset(
        {
            LedgerItemStatus.OPEN,
            LedgerItemStatus.ACTIVE,
            LedgerItemStatus.COMPLETED,
            LedgerItemStatus.CANCELED,
            LedgerItemStatus.SUPERSEDED,
        }
    ),
    LedgerItemKind.DECISION: frozenset(
        {LedgerItemStatus.ACTIVE, LedgerItemStatus.CANCELED, LedgerItemStatus.SUPERSEDED}
    ),
    LedgerItemKind.MILESTONE: frozenset(
        {
            LedgerItemStatus.OPEN,
            LedgerItemStatus.ACTIVE,
            LedgerItemStatus.COMPLETED,
            LedgerItemStatus.CANCELED,
            LedgerItemStatus.SUPERSEDED,
        }
    ),
    LedgerItemKind.RISK: frozenset(
        {
            LedgerItemStatus.OPEN,
            LedgerItemStatus.ACTIVE,
            LedgerItemStatus.RESOLVED,
            LedgerItemStatus.CANCELED,
            LedgerItemStatus.SUPERSEDED,
        }
    ),
    LedgerItemKind.BLOCKER: frozenset(
        {
            LedgerItemStatus.OPEN,
            LedgerItemStatus.ACTIVE,
            LedgerItemStatus.RESOLVED,
            LedgerItemStatus.CANCELED,
            LedgerItemStatus.SUPERSEDED,
        }
    ),
    LedgerItemKind.OPEN_QUESTION: frozenset(
        {
            LedgerItemStatus.OPEN,
            LedgerItemStatus.RESOLVED,
            LedgerItemStatus.CANCELED,
            LedgerItemStatus.SUPERSEDED,
        }
    ),
    LedgerItemKind.STAKEHOLDER: frozenset(
        {LedgerItemStatus.ACTIVE, LedgerItemStatus.CANCELED, LedgerItemStatus.SUPERSEDED}
    ),
}

#: The status a brand-new item of each kind starts in (the `create`
#: transition's fixed target, and what `reopen` returns an item to).
INITIAL_STATUS_BY_KIND: dict[LedgerItemKind, LedgerItemStatus] = {
    LedgerItemKind.COMMITMENT: LedgerItemStatus.OPEN,
    LedgerItemKind.DECISION: LedgerItemStatus.ACTIVE,
    LedgerItemKind.MILESTONE: LedgerItemStatus.OPEN,
    LedgerItemKind.RISK: LedgerItemStatus.OPEN,
    LedgerItemKind.BLOCKER: LedgerItemStatus.OPEN,
    LedgerItemKind.OPEN_QUESTION: LedgerItemStatus.OPEN,
    LedgerItemKind.STAKEHOLDER: LedgerItemStatus.ACTIVE,
}

#: Non-terminal statuses an item can sit in while still open for
#: ordinary field updates (Section 10.8/10.9: due-date/owner changes).
_NON_TERMINAL_STATUSES = frozenset({LedgerItemStatus.OPEN, LedgerItemStatus.ACTIVE})

#: Which `from_status` values (None means "no prior version" — item
#: creation) each transition type may legally start from.
_ALLOWED_FROM_STATUSES: dict[LedgerTransitionType, frozenset[LedgerItemStatus | None]] = {
    LedgerTransitionType.CREATE: frozenset({None}),
    LedgerTransitionType.UPDATE: _NON_TERMINAL_STATUSES,
    LedgerTransitionType.COMPLETE: _NON_TERMINAL_STATUSES,
    LedgerTransitionType.RESOLVE: _NON_TERMINAL_STATUSES,
    LedgerTransitionType.CANCEL: _NON_TERMINAL_STATUSES,
    LedgerTransitionType.SUPERSEDE: _NON_TERMINAL_STATUSES,
    # FR-014's explicit example: "canceled -> open without reopen review
    # is rejected" — i.e. only `reopen` may leave a terminal status.
    LedgerTransitionType.REOPEN: frozenset(
        {
            LedgerItemStatus.COMPLETED,
            LedgerItemStatus.RESOLVED,
            LedgerItemStatus.CANCELED,
            LedgerItemStatus.SUPERSEDED,
        }
    ),
    # A correction can fix a wrongly-recorded field from any status,
    # including a wrongly-recorded status itself.
    LedgerTransitionType.CORRECT: frozenset(LedgerItemStatus) | {None},
}

#: The fixed resulting status for transition types whose target does not
#: depend on kind or caller input. `create`, `update`, `reopen`, and
#: `correct` are intentionally absent — their target status is
#: caller-supplied and validated by `validate_transition` instead (see
#: that function for the per-transition-type rule each applies).
_FIXED_TARGET_STATUS: dict[LedgerTransitionType, LedgerItemStatus] = {
    LedgerTransitionType.COMPLETE: LedgerItemStatus.COMPLETED,
    LedgerTransitionType.RESOLVE: LedgerItemStatus.RESOLVED,
    LedgerTransitionType.CANCEL: LedgerItemStatus.CANCELED,
    LedgerTransitionType.SUPERSEDE: LedgerItemStatus.SUPERSEDED,
}


class LedgerStatusError(ValueError):
    """Raised when a status is not valid for a given item kind."""


class LedgerTransitionError(ValueError):
    """Raised when a (from_status, transition_type, to_status) combination
    is not a legal ledger transition."""


def is_valid_status_for_kind(kind: LedgerItemKind, status: LedgerItemStatus) -> bool:
    return status in VALID_STATUSES_BY_KIND[kind]


def require_valid_status_for_kind(kind: LedgerItemKind, status: LedgerItemStatus) -> None:
    """Raise `LedgerStatusError` unless `status` is valid for `kind`."""
    if not is_valid_status_for_kind(kind, status):
        allowed = ", ".join(sorted(s.value for s in VALID_STATUSES_BY_KIND[kind]))
        raise LedgerStatusError(
            f"status {status.value!r} is not valid for kind {kind.value!r}; allowed: {allowed}"
        )


def validate_transition(
    *,
    kind: LedgerItemKind,
    from_status: LedgerItemStatus | None,
    to_status: LedgerItemStatus,
    transition_type: LedgerTransitionType,
) -> None:
    """Raise `LedgerStatusError`/`LedgerTransitionError` unless this
    transition is legal; otherwise return None. `from_status=None` means
    this is the item's first version (only `create` may use it).

    This validates the *rule*, not any live row — Prompt 8's review
    transaction is the (not-yet-built) caller that will apply an
    accepted transition to real `ledger_items`/`ledger_versions` rows
    through this check.
    """
    require_valid_status_for_kind(kind, to_status)

    allowed_from = _ALLOWED_FROM_STATUSES[transition_type]
    if from_status not in allowed_from:
        from_label = from_status.value if from_status is not None else "none (new item)"
        raise LedgerTransitionError(
            f"transition {transition_type.value!r} is not legal from status {from_label!r}"
        )

    if transition_type is LedgerTransitionType.CREATE:
        expected = INITIAL_STATUS_BY_KIND[kind]
        if to_status is not expected:
            raise LedgerTransitionError(
                f"kind {kind.value!r} must be created with status {expected.value!r}, "
                f"got {to_status.value!r}"
            )
        return

    if transition_type is LedgerTransitionType.REOPEN:
        expected = INITIAL_STATUS_BY_KIND[kind]
        if to_status is not expected:
            raise LedgerTransitionError(
                f"reopening kind {kind.value!r} must return it to status {expected.value!r}, "
                f"got {to_status.value!r}"
            )
        return

    if transition_type is LedgerTransitionType.UPDATE and to_status not in _NON_TERMINAL_STATUSES:
        raise LedgerTransitionError(
            f"update must leave the item in an open/active status, got {to_status.value!r}"
        )

    fixed_target = _FIXED_TARGET_STATUS.get(transition_type)
    if fixed_target is not None and to_status is not fixed_target:
        raise LedgerTransitionError(
            f"transition {transition_type.value!r} must result in status "
            f"{fixed_target.value!r}, got {to_status.value!r}"
        )
    # `correct` has no further constraint beyond kind/status compatibility
    # and the allowed-from-status check already applied above.


class LedgerItem(BaseModel):
    """The current-projection row, as stored. See Section 9, table
    `ledger_items`. `current_version_id` is `None` only for the instant
    between the two inserts a creation transaction performs — see
    migrations/0005_ledger_and_review.sql."""

    id: str
    project_id: str
    kind: LedgerItemKind
    canonical_title: str
    canonical_description: str | None
    status: LedgerItemStatus
    owner_person_id: str | None
    due_date: str | None
    effective_at: str | None
    current_version_id: str | None
    confidence_band: ConfidenceBand | None
    user_corrected: bool
    created_at: str
    updated_at: str


class LedgerVersion(BaseModel):
    """One append-only version row, as stored. See Section 9, table
    `ledger_versions`."""

    id: str
    project_id: str
    ledger_item_id: str
    version_no: int
    canonical_title: str
    canonical_description: str | None
    status: LedgerItemStatus
    owner_person_id: str | None
    due_date: str | None
    effective_at: str | None
    confidence_band: ConfidenceBand | None
    transition_type: LedgerTransitionType
    from_version_id: str | None
    observation_id: str | None
    review_id: str | None
    valid_from: str
    valid_to: str | None
    superseded_by_version_id: str | None
    created_at: str
