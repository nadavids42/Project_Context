"""Tests for ledger write orchestration: version-number ordering,
current-pointer consistency, and transition validation (Prompt 6:
"Ledger version ordering/current-pointer consistency")."""

from __future__ import annotations

import pytest

from project_context.db import ledger_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.ledger import (
    LedgerItemKind,
    LedgerItemStatus,
    LedgerStatusError,
    LedgerTransitionError,
    LedgerTransitionType,
)
from project_context.domain.projects import ProjectCreateInput
from project_context.services.ledger import (
    LedgerItemNotFoundError,
    append_ledger_version,
    create_ledger_item,
)
from project_context.services.projects import create_project


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def project_id(conn):
    return create_project(
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
    ).id


# --- creation -----------------------------------------------------------


def test_create_ledger_item_starts_at_kind_initial_status_and_version_1(conn, project_id):
    item, version = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report"
    )

    assert item.status is LedgerItemStatus.OPEN
    assert item.current_version_id == version.id
    assert version.version_no == 1
    assert version.transition_type is LedgerTransitionType.CREATE
    assert version.from_version_id is None
    assert version.valid_to is None
    assert version.superseded_by_version_id is None


def test_create_ledger_item_for_a_decision_starts_active(conn, project_id):
    item, version = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.DECISION, canonical_title="Use vendor X"
    )
    assert item.status is LedgerItemStatus.ACTIVE
    assert version.status is LedgerItemStatus.ACTIVE


# --- version ordering / current-pointer consistency -----------------------


def test_appending_a_version_increments_version_no_and_moves_the_current_pointer(conn, project_id):
    item, v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report"
    )

    item, v2 = append_ledger_version(
        conn,
        project_id,
        item.id,
        transition_type=LedgerTransitionType.UPDATE,
        status=LedgerItemStatus.ACTIVE,
        due_date="2026-09-04",
    )

    assert v2.version_no == 2
    assert v2.from_version_id == v1.id
    assert item.current_version_id == v2.id
    assert item.status is LedgerItemStatus.ACTIVE
    assert item.due_date == "2026-09-04"

    # The prior version is closed out, never deleted.
    closed_v1 = ledger_repository.get_version(conn, project_id, v1.id)
    assert closed_v1.valid_to is not None
    assert closed_v1.superseded_by_version_id == v2.id


def test_three_successive_versions_are_strictly_ordered(conn, project_id):
    item, v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.MILESTONE, canonical_title="Launch"
    )
    item, v2 = append_ledger_version(
        conn,
        project_id,
        item.id,
        transition_type=LedgerTransitionType.UPDATE,
        status=LedgerItemStatus.ACTIVE,
    )
    item, v3 = append_ledger_version(
        conn,
        project_id,
        item.id,
        transition_type=LedgerTransitionType.COMPLETE,
        status=LedgerItemStatus.COMPLETED,
    )

    history = ledger_repository.list_versions_for_item(conn, project_id, item.id)
    assert [v.version_no for v in history] == [1, 2, 3]
    assert [v.id for v in history] == [v1.id, v2.id, v3.id]
    assert item.current_version_id == v3.id
    assert item.status is LedgerItemStatus.COMPLETED

    # Every non-current version is closed; only the last one is open.
    assert history[0].valid_to is not None
    assert history[1].valid_to is not None
    assert history[2].valid_to is None
    assert history[0].superseded_by_version_id == v2.id
    assert history[1].superseded_by_version_id == v3.id
    assert history[2].superseded_by_version_id is None


def test_fields_not_passed_to_append_carry_forward_from_the_current_projection(conn, project_id):
    item, _v1 = create_ledger_item(
        conn,
        project_id,
        kind=LedgerItemKind.COMMITMENT,
        canonical_title="Send the report",
        owner_person_id=None,
        due_date="2026-08-28",
    )

    item, v2 = append_ledger_version(
        conn,
        project_id,
        item.id,
        transition_type=LedgerTransitionType.UPDATE,
        status=LedgerItemStatus.OPEN,
    )

    assert item.due_date == "2026-08-28"
    assert v2.canonical_title == "Send the report"


def test_append_ledger_version_rejects_an_illegal_transition(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report"
    )
    append_ledger_version(
        conn,
        project_id,
        item.id,
        transition_type=LedgerTransitionType.CANCEL,
        status=LedgerItemStatus.CANCELED,
    )

    with pytest.raises(LedgerTransitionError):
        # FR-014: canceled -> open requires `reopen`, not `update`.
        append_ledger_version(
            conn,
            project_id,
            item.id,
            transition_type=LedgerTransitionType.UPDATE,
            status=LedgerItemStatus.OPEN,
        )


def test_append_ledger_version_rejects_status_invalid_for_kind(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.RISK, canonical_title="Vendor delay risk"
    )
    with pytest.raises(LedgerStatusError):
        append_ledger_version(
            conn,
            project_id,
            item.id,
            transition_type=LedgerTransitionType.COMPLETE,
            status=LedgerItemStatus.COMPLETED,
        )


def test_append_ledger_version_on_unknown_item_raises(conn, project_id):
    with pytest.raises(LedgerItemNotFoundError):
        append_ledger_version(
            conn,
            project_id,
            "nonexistent-item",
            transition_type=LedgerTransitionType.UPDATE,
            status=LedgerItemStatus.OPEN,
        )


def test_reopen_after_cancel_returns_to_initial_status(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report"
    )
    item, _v2 = append_ledger_version(
        conn,
        project_id,
        item.id,
        transition_type=LedgerTransitionType.CANCEL,
        status=LedgerItemStatus.CANCELED,
    )
    item, v3 = append_ledger_version(
        conn,
        project_id,
        item.id,
        transition_type=LedgerTransitionType.REOPEN,
        status=LedgerItemStatus.OPEN,
    )
    assert item.status is LedgerItemStatus.OPEN
    assert v3.transition_type is LedgerTransitionType.REOPEN


def test_user_corrected_flag_persists_across_ordinary_updates(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report"
    )
    item, _v2 = append_ledger_version(
        conn,
        project_id,
        item.id,
        transition_type=LedgerTransitionType.CORRECT,
        status=LedgerItemStatus.OPEN,
        owner_person_id=None,
        user_corrected=True,
    )
    assert item.user_corrected is True

    item, _v3 = append_ledger_version(
        conn,
        project_id,
        item.id,
        transition_type=LedgerTransitionType.UPDATE,
        status=LedgerItemStatus.ACTIVE,
    )
    assert item.user_corrected is True
