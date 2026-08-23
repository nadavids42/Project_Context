"""Tests for the ledger repository's read paths and project-isolated
FTS5 search over `ledger_items` (Prompt 6)."""

from __future__ import annotations

import pytest

from project_context.db import ledger_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus, LedgerTransitionType
from project_context.domain.projects import ProjectCreateInput
from project_context.services.ledger import append_ledger_version, create_ledger_item
from project_context.services.projects import create_project


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


def _make_project(conn, name="Acme Rollout"):
    return create_project(conn, ProjectCreateInput(name=name, objective="Ship the pilot")).id


def test_list_items_for_project_filters_by_kind_and_status(conn):
    project_id = _make_project(conn)
    create_ledger_item(conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="A")
    create_ledger_item(conn, project_id, kind=LedgerItemKind.RISK, canonical_title="B")

    commitments = ledger_repository.list_items_for_project(
        conn, project_id, kind=LedgerItemKind.COMMITMENT
    )
    assert [i.canonical_title for i in commitments] == ["A"]

    open_items = ledger_repository.list_items_for_project(
        conn, project_id, status=LedgerItemStatus.OPEN
    )
    assert {i.canonical_title for i in open_items} == {"A", "B"}


def test_search_ledger_items_is_scoped_to_one_project(conn):
    project_a = _make_project(conn, name="Project A")
    project_b = _make_project(conn, name="Project B")
    create_ledger_item(
        conn,
        project_a,
        kind=LedgerItemKind.COMMITMENT,
        canonical_title="Send the vendor-delta report",
    )
    create_ledger_item(
        conn,
        project_b,
        kind=LedgerItemKind.COMMITMENT,
        canonical_title="Send the vendor-delta report",
    )

    results_a = ledger_repository.search_ledger_items(conn, project_a, "vendor-delta")
    results_b = ledger_repository.search_ledger_items(conn, project_b, "vendor-delta")

    assert len(results_a) == 1
    assert len(results_b) == 1
    item_a = ledger_repository.list_items_for_project(conn, project_a)[0]
    item_b = ledger_repository.list_items_for_project(conn, project_b)[0]
    assert results_a[0].ledger_item_id == item_a.id
    assert results_b[0].ledger_item_id == item_b.id


def test_search_ledger_items_does_not_leak_a_unique_sentinel_across_projects(conn):
    project_a = _make_project(conn, name="Project A")
    project_b = _make_project(conn, name="Project B")
    create_ledger_item(
        conn,
        project_a,
        kind=LedgerItemKind.RISK,
        canonical_title="Contains zzqx-unique-sentinel-77123 only here",
    )
    create_ledger_item(conn, project_b, kind=LedgerItemKind.RISK, canonical_title="Nothing special")

    assert (
        len(ledger_repository.search_ledger_items(conn, project_a, "zzqx-unique-sentinel-77123"))
        == 1
    )
    assert (
        ledger_repository.search_ledger_items(conn, project_b, "zzqx-unique-sentinel-77123") == []
    )


def test_fts_index_reflects_updates_to_the_current_projection(conn):
    """ledger_items is a mutable projection (unlike source_chunks); the
    FTS trigger set must resync on UPDATE, not just INSERT/DELETE."""
    project_id = _make_project(conn)
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.MILESTONE, canonical_title="Old title zzqx-before"
    )
    assert len(ledger_repository.search_ledger_items(conn, project_id, "zzqx-before")) == 1

    append_ledger_version(
        conn,
        project_id,
        item.id,
        transition_type=LedgerTransitionType.UPDATE,
        status=LedgerItemStatus.ACTIVE,
        canonical_title="New title zzqx-after",
    )

    assert ledger_repository.search_ledger_items(conn, project_id, "zzqx-before") == []
    assert len(ledger_repository.search_ledger_items(conn, project_id, "zzqx-after")) == 1
