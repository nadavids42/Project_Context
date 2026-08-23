"""Ledger write orchestration: the transactional two-step insert a new
item requires, and the append-a-version sequence that keeps
`ledger_items` (current projection) and `ledger_versions` (append-only
history) consistent.

This is *not* the review transaction (Prompt 8) — nothing here decides
*whether* a mutation should happen or *what* it should be; both
functions take an already-decided target state and transition type and
only guarantee it is applied consistently, validated by
`project_context.domain.ledger.validate_transition`. No caller exists
yet in this step (reconciliation and the review UI are both out of
scope); these functions are exercised directly by
tests/unit/test_ledger_service.py to prove the version-history/
current-pointer invariants Prompt 6 requires.
"""

from __future__ import annotations

import sqlite3

from project_context.db import ledger_repository
from project_context.db.connection import transaction
from project_context.domain.ledger import (
    INITIAL_STATUS_BY_KIND,
    ConfidenceBand,
    LedgerItem,
    LedgerItemKind,
    LedgerItemStatus,
    LedgerTransitionType,
    LedgerVersion,
    validate_transition,
)


class LedgerItemNotFoundError(LookupError):
    """Raised when a `ledger_item_id` does not identify an existing item
    in the given project."""


def create_ledger_item(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    kind: LedgerItemKind,
    canonical_title: str,
    canonical_description: str | None = None,
    owner_person_id: str | None = None,
    due_date: str | None = None,
    effective_at: str | None = None,
    confidence_band: ConfidenceBand | None = None,
    observation_id: str | None = None,
    review_id: str | None = None,
) -> tuple[LedgerItem, LedgerVersion]:
    """Create a new ledger item at its kind's initial status, with
    version 1 (`transition_type='create'`) as its current version. One
    transaction: insert the bare item, insert version 1, repoint
    `current_version_id` — the same lazily-resolved mutual-FK pattern
    already used for `source_artifacts`/`source_contents`.
    """
    status = INITIAL_STATUS_BY_KIND[kind]
    validate_transition(
        kind=kind,
        from_status=None,
        to_status=status,
        transition_type=LedgerTransitionType.CREATE,
    )

    with transaction(conn):
        item = ledger_repository.insert_item(
            conn,
            project_id,
            kind=kind,
            canonical_title=canonical_title,
            canonical_description=canonical_description,
            status=status,
            owner_person_id=owner_person_id,
            due_date=due_date,
            effective_at=effective_at,
            confidence_band=confidence_band,
        )
        version = ledger_repository.insert_version(
            conn,
            project_id,
            item.id,
            version_no=1,
            canonical_title=canonical_title,
            canonical_description=canonical_description,
            status=status,
            owner_person_id=owner_person_id,
            due_date=due_date,
            effective_at=effective_at,
            confidence_band=confidence_band,
            transition_type=LedgerTransitionType.CREATE,
            from_version_id=None,
            observation_id=observation_id,
            review_id=review_id,
        )
        item = ledger_repository.update_projection(
            conn,
            project_id,
            item.id,
            canonical_title=canonical_title,
            canonical_description=canonical_description,
            status=status,
            owner_person_id=owner_person_id,
            due_date=due_date,
            effective_at=effective_at,
            confidence_band=confidence_band,
            current_version_id=version.id,
        )
    assert item is not None
    return item, version


def append_ledger_version(
    conn: sqlite3.Connection,
    project_id: str,
    ledger_item_id: str,
    *,
    transition_type: LedgerTransitionType,
    status: LedgerItemStatus,
    canonical_title: str | None = None,
    canonical_description: str | None = None,
    owner_person_id: str | None = None,
    due_date: str | None = None,
    effective_at: str | None = None,
    confidence_band: ConfidenceBand | None = None,
    observation_id: str | None = None,
    review_id: str | None = None,
    user_corrected: bool | None = None,
) -> tuple[LedgerItem, LedgerVersion]:
    """Append one new version to an existing item: validate the
    transition, insert version `N+1`, close the prior current version
    (`valid_to`/`superseded_by_version_id`), and repoint
    `current_version_id`. One transaction.

    Fields left as `None` carry forward the item's current value —
    ordinary updates only need to pass what changed.
    """
    with transaction(conn):
        item = ledger_repository.get_item(conn, project_id, ledger_item_id)
        if item is None:
            raise LedgerItemNotFoundError(ledger_item_id)

        validate_transition(
            kind=item.kind,
            from_status=item.status,
            to_status=status,
            transition_type=transition_type,
        )

        next_title = canonical_title if canonical_title is not None else item.canonical_title
        next_description = (
            canonical_description
            if canonical_description is not None
            else item.canonical_description
        )
        next_owner = owner_person_id if owner_person_id is not None else item.owner_person_id
        next_due_date = due_date if due_date is not None else item.due_date
        next_effective_at = effective_at if effective_at is not None else item.effective_at
        next_confidence_band = (
            confidence_band if confidence_band is not None else item.confidence_band
        )

        next_no = ledger_repository.next_version_no(conn, ledger_item_id)
        new_version = ledger_repository.insert_version(
            conn,
            project_id,
            ledger_item_id,
            version_no=next_no,
            canonical_title=next_title,
            canonical_description=next_description,
            status=status,
            owner_person_id=next_owner,
            due_date=next_due_date,
            effective_at=next_effective_at,
            confidence_band=next_confidence_band,
            transition_type=transition_type,
            from_version_id=item.current_version_id,
            observation_id=observation_id,
            review_id=review_id,
        )
        if item.current_version_id is not None:
            ledger_repository.close_version(
                conn,
                project_id,
                item.current_version_id,
                superseded_by_version_id=new_version.id,
                valid_to=new_version.valid_from,
            )
        item = ledger_repository.update_projection(
            conn,
            project_id,
            ledger_item_id,
            canonical_title=next_title,
            canonical_description=next_description,
            status=status,
            owner_person_id=next_owner,
            due_date=next_due_date,
            effective_at=next_effective_at,
            confidence_band=next_confidence_band,
            current_version_id=new_version.id,
            user_corrected=user_corrected,
        )
    assert item is not None
    return item, new_version
