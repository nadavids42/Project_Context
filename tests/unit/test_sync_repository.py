"""Tests for the sync repository: `sync_runs`/`sync_items` CRUD
(Section 9; FR-009, FR-031; Prompt 10)."""

from __future__ import annotations

import pytest

from project_context.db import sources_repository, sync_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.sync import (
    SyncErrorClass,
    SyncItemStage,
    SyncItemStatus,
    SyncRunStatus,
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


@pytest.fixture
def source_id(conn, project_id):
    return sources_repository.ensure_manual_source(conn, project_id).id


def test_insert_sync_run_starts_running(conn, project_id):
    run = sync_repository.insert_sync_run(conn, project_id, correlation_id="corr-1")
    assert run.status is SyncRunStatus.RUNNING
    assert run.ended_at is None
    assert run.correlation_id == "corr-1"


def test_get_sync_run_is_project_scoped(conn, project_id):
    other_id = create_project(conn, ProjectCreateInput(name="Other", objective="Other work")).id
    run = sync_repository.insert_sync_run(conn, project_id)
    assert sync_repository.get_sync_run(conn, other_id, run.id) is None
    assert sync_repository.get_sync_run(conn, project_id, run.id) is not None


def test_finalize_sync_run_sets_status_counts_and_ended_at(conn, project_id):
    run = sync_repository.insert_sync_run(conn, project_id)
    finalized = sync_repository.finalize_sync_run(
        conn,
        project_id,
        run.id,
        status=SyncRunStatus.COMPLETED,
        discovered_count=5,
        unchanged_count=2,
        parsed_count=3,
        failed_count=0,
        proposed_count=1,
        needs_assignment_count=0,
    )
    assert finalized.status is SyncRunStatus.COMPLETED
    assert finalized.ended_at is not None
    assert finalized.discovered_count == 5
    assert finalized.parsed_count == 3


def test_list_sync_runs_for_project_orders_newest_first(conn, project_id):
    first = sync_repository.insert_sync_run(conn, project_id)
    second = sync_repository.insert_sync_run(conn, project_id)
    listed = sync_repository.list_sync_runs_for_project(conn, project_id)
    assert [r.id for r in listed] == [second.id, first.id]


def test_insert_and_list_sync_items_for_run(conn, project_id, source_id):
    run = sync_repository.insert_sync_run(conn, project_id)
    sync_repository.insert_sync_item(
        conn,
        project_id,
        sync_run_id=run.id,
        source_id=source_id,
        artifact_id=None,
        external_id="drive:file-1",
        stage=SyncItemStage.DISCOVERED,
    )
    sync_repository.insert_sync_item(
        conn,
        project_id,
        sync_run_id=run.id,
        source_id=source_id,
        artifact_id=None,
        external_id="drive:file-2",
        stage=SyncItemStage.FAILED,
        status=SyncItemStatus.ERROR,
        error_class=SyncErrorClass.NOT_FOUND,
        safe_error_message="file not found",
    )
    items = sync_repository.list_sync_items_for_run(conn, run.id)
    assert [i.external_id for i in items] == ["drive:file-1", "drive:file-2"]
    assert items[1].error_class is SyncErrorClass.NOT_FOUND
    assert items[1].status is SyncItemStatus.ERROR


def test_list_failed_sync_items_for_source_returns_latest_failure_only(conn, project_id, source_id):
    run1 = sync_repository.insert_sync_run(conn, project_id)
    sync_repository.insert_sync_item(
        conn,
        project_id,
        sync_run_id=run1.id,
        source_id=source_id,
        artifact_id=None,
        external_id="drive:file-1",
        stage=SyncItemStage.FAILED,
        status=SyncItemStatus.ERROR,
        error_class=SyncErrorClass.RATE_LIMIT,
    )
    run2 = sync_repository.insert_sync_run(conn, project_id)
    sync_repository.insert_sync_item(
        conn,
        project_id,
        sync_run_id=run2.id,
        source_id=source_id,
        artifact_id=None,
        external_id="drive:file-1",
        stage=SyncItemStage.PARSED,
        status=SyncItemStatus.OK,
    )
    failed = sync_repository.list_failed_sync_items_for_source(conn, source_id)
    assert failed == []  # the latest attempt for file-1 succeeded


def test_list_failed_sync_items_for_source_surfaces_current_failures(conn, project_id, source_id):
    run = sync_repository.insert_sync_run(conn, project_id)
    sync_repository.insert_sync_item(
        conn,
        project_id,
        sync_run_id=run.id,
        source_id=source_id,
        artifact_id=None,
        external_id="drive:file-1",
        stage=SyncItemStage.FAILED,
        status=SyncItemStatus.ERROR,
        error_class=SyncErrorClass.PROVIDER,
    )
    failed = sync_repository.list_failed_sync_items_for_source(conn, source_id)
    assert [i.external_id for i in failed] == ["drive:file-1"]
