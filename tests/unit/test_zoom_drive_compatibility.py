"""Zoom-to-Drive compatibility coverage (Section 11.6; Prompt 13): this
application never calls a Zoom API — it only ever discovers whatever
the user's existing Zoom-to-Drive workflow has already deposited in
their configured Drive folder, as ordinary evidence, through the
existing `DriveConnector`/`drive_ingestion` path unchanged. These tests
prove representative Zoom-shaped VTT transcripts, chat exports, and
meeting-summary documents (`.txt` and `.docx`) parse and ingest
correctly through that existing path, that filename hints are advisory
only, and that project assignment always comes from the configured
Drive folder — never from a filename, no matter how Zoom-shaped it
looks.

See `tests/fixtures/zoom_fixtures.py` for the exact fixture bytes and
the documented compatibility finding they surface (no `<v>` speaker
tags in Zoom's own VTT export, so per-speaker turn merging does not
apply the way it does for a `<v>`-tagged VTT source)."""

from __future__ import annotations

import json

import pytest

from fixtures.fake_drive_api import FakeDriveApi
from fixtures.zoom_fixtures import (
    ZOOM_CHAT_BYTES,
    ZOOM_CHAT_FILENAME,
    ZOOM_SUMMARY_DOCX_FILENAME,
    ZOOM_SUMMARY_TXT_FILENAME,
    ZOOM_SUMMARY_TXT_TEXT,
    ZOOM_VTT_FILENAME,
    ZOOM_VTT_TRANSCRIPT_BYTES,
    build_zoom_summary_docx,
)
from project_context.connectors.drive import DriveConnector
from project_context.db import evidence_repository, sources_repository
from project_context.db.connection import connect
from project_context.domain.evidence import ParseStatus
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.sources import SourceKind
from project_context.domain.sync import SyncRunStatus
from project_context.domain.zoom_hints import describe_filename_hint
from project_context.services import sync as sync_service
from project_context.services.projects import create_project

_ROOT = "root-folder"


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    from project_context.db.migrations import run_migrations

    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def evidence_dir(tmp_path):
    directory = tmp_path / "evidence"
    directory.mkdir()
    return directory


@pytest.fixture
def project_id(conn):
    return create_project(
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
    ).id


def _api(*, folder_id=_ROOT) -> FakeDriveApi:
    api = FakeDriveApi()
    api.files[folder_id] = {
        "id": folder_id,
        "mimeType": "application/vnd.google-apps.folder",
        "trashed": False,
    }
    return api


def _make_drive_source(conn, project_id, *, folder_id=_ROOT):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    return sources_repository.update_boundary(
        conn, project_id, source.id, boundary_json=json.dumps({"folder_id": folder_id})
    )


def _sync(conn, project_id, source_id, api, evidence_dir, *, folder_id=_ROOT):
    connector = DriveConnector(
        access_token="fake-token",
        folder_id=folder_id,
        http_transport=api,
        sleep=lambda _s: None,
        rand=lambda: 0.0,
    )
    return sync_service.sync_source(
        conn,
        project_id,
        source_id,
        connector=connector,
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )


# --- VTT transcript ----------------------------------------------------


def test_zoom_vtt_transcript_ingests_through_drive(conn, project_id, evidence_dir):
    source = _make_drive_source(conn, project_id)
    api = _api()
    api.add_folder(
        _ROOT,
        [
            api.add_file(
                "f1",
                name=ZOOM_VTT_FILENAME,
                mime_type="text/vtt",
                content=ZOOM_VTT_TRANSCRIPT_BYTES,
            )
        ],
    )

    run = _sync(conn, project_id, source.id, api, evidence_dir)
    assert run.status is SyncRunStatus.COMPLETED
    assert run.failed_count == 0

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert len(artifacts) == 1
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert content.parse_status is ParseStatus.PARSED
    assert "Alice Smith: Thanks everyone for joining today's kickoff." in content.normalized_text
    assert "Bob Jones: Happy to be here, excited to get started." in content.normalized_text
    assert "We decided to ship the Acme rollout in July." in content.normalized_text

    chunks = evidence_repository.list_chunks_for_content(conn, project_id, content.id)
    assert len(chunks) >= 1  # fully evidence-linkable regardless of turn granularity


def test_zoom_vtt_without_v_tags_merges_into_one_turn_documented_limitation(
    conn, project_id, evidence_dir
):
    """Documented finding (see `zoom_fixtures` module docstring): Zoom's
    real VTT export has no `<v Speaker>` tags, so
    `project_context.parsers.vtt_parser`'s speaker-based adjacent-cue
    merge treats every cue as the same (unlabeled) speaker and merges
    all three cues into a single block. No text is lost — every
    speaker's name and words are still present, verbatim, inside the
    merged block's own text — but per-speaker turn boundaries are not
    preserved the way they would be for a `<v>`-tagged VTT source. This
    test pins that actual behavior rather than an unimplemented ideal,
    so a future fix changes this assertion deliberately, not by
    accident."""
    source = _make_drive_source(conn, project_id)
    api = _api()
    api.add_folder(
        _ROOT,
        [
            api.add_file(
                "f1",
                name=ZOOM_VTT_FILENAME,
                mime_type="text/vtt",
                content=ZOOM_VTT_TRANSCRIPT_BYTES,
            )
        ],
    )
    _sync(conn, project_id, source.id, api, evidence_dir)

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    chunks = evidence_repository.list_chunks_for_content(conn, project_id, content.id)
    assert len(chunks) == 1
    assert "Alice Smith" in chunks[0].text
    assert "Bob Jones" in chunks[0].text


# --- chat export ---------------------------------------------------------


def test_zoom_chat_export_ingests_through_drive_as_text(conn, project_id, evidence_dir):
    source = _make_drive_source(conn, project_id)
    api = _api()
    api.add_folder(
        _ROOT,
        [
            api.add_file(
                "f1", name=ZOOM_CHAT_FILENAME, mime_type="text/plain", content=ZOOM_CHAT_BYTES
            )
        ],
    )
    run = _sync(conn, project_id, source.id, api, evidence_dir)
    assert run.status is SyncRunStatus.COMPLETED

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert content.parse_status is ParseStatus.PARSED
    assert "I'll drop the doc link here." in content.normalized_text


# --- meeting summary: txt and docx ------------------------------------


def test_zoom_summary_txt_ingests_through_drive(conn, project_id, evidence_dir):
    source = _make_drive_source(conn, project_id)
    api = _api()
    api.add_folder(
        _ROOT,
        [
            api.add_file(
                "f1",
                name=ZOOM_SUMMARY_TXT_FILENAME,
                mime_type="text/plain",
                content=ZOOM_SUMMARY_TXT_TEXT.encode("utf-8"),
            )
        ],
    )
    run = _sync(conn, project_id, source.id, api, evidence_dir)
    assert run.status is SyncRunStatus.COMPLETED

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert content.parse_status is ParseStatus.PARSED
    assert "Bob to send the requirements doc by Friday." in content.normalized_text
    chunks = evidence_repository.list_chunks_for_content(conn, project_id, content.id)
    assert len(chunks) >= 1


def test_zoom_summary_docx_ingests_through_drive(conn, project_id, evidence_dir):
    source = _make_drive_source(conn, project_id)
    api = _api()
    api.add_folder(
        _ROOT,
        [
            api.add_file(
                "f1",
                name=ZOOM_SUMMARY_DOCX_FILENAME,
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                content=build_zoom_summary_docx(),
            )
        ],
    )
    run = _sync(conn, project_id, source.id, api, evidence_dir)
    assert run.status is SyncRunStatus.COMPLETED

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert content.parse_status is ParseStatus.PARSED
    assert "Alice to schedule the next check-in." in content.normalized_text


# --- filename hints are advisory only; assignment stays folder-based -------


def test_filename_hints_never_influence_which_project_a_file_lands_in(conn, evidence_dir):
    """Two projects, each with its own Drive folder, each receiving an
    identically Zoom-shaped filename. Assignment must be determined
    solely by which project's configured folder the file was found in
    — never by the filename (Prompt 13: "do not assign solely from a
    filename when the configured Drive folder does not already
    establish the project")."""
    project_a = create_project(conn, ProjectCreateInput(name="Project A", objective="A")).id
    project_b = create_project(conn, ProjectCreateInput(name="Project B", objective="B")).id

    source_a = _make_drive_source(conn, project_a, folder_id="folder-a")
    api_a = _api(folder_id="folder-a")
    api_a.add_folder(
        "folder-a",
        [
            api_a.add_file(
                "f1",
                name=ZOOM_VTT_FILENAME,
                mime_type="text/vtt",
                content=(
                    b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nSecret sentinel Alpha content.\n"
                ),
            )
        ],
    )
    _sync(conn, project_a, source_a.id, api_a, evidence_dir, folder_id="folder-a")

    source_b = _make_drive_source(conn, project_b, folder_id="folder-b")
    api_b = _api(folder_id="folder-b")
    api_b.add_folder(
        "folder-b",
        [
            api_b.add_file(
                "f2",
                name=ZOOM_VTT_FILENAME,
                mime_type="text/vtt",  # identical filename
                content=b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nUnrelated Beta content.\n",
            )
        ],
    )
    _sync(conn, project_b, source_b.id, api_b, evidence_dir, folder_id="folder-b")

    artifacts_b = evidence_repository.list_artifacts_for_source(conn, project_b, source_b.id)
    content_b = evidence_repository.get_content(conn, project_b, artifacts_b[0].current_content_id)
    assert "Secret sentinel Alpha" not in content_b.normalized_text
    assert "Unrelated Beta content." in content_b.normalized_text

    artifacts_a = evidence_repository.list_artifacts_for_source(conn, project_a, source_a.id)
    assert evidence_repository.get_artifact(conn, project_b, artifacts_a[0].id) is None


def test_filename_hint_is_purely_advisory_text_not_a_project_id():
    """`describe_filename_hint` returns a display string or `None` —
    nothing an assignment path could ever mistake for a project/source
    decision."""
    hint = describe_filename_hint(ZOOM_VTT_FILENAME)
    assert isinstance(hint, str)
    assert hint  # non-empty


# --- manual ingestion (FR-005/FR-006) ---------------------------------


def test_zoom_vtt_transcript_ingests_through_manual_upload(conn, project_id, evidence_dir):
    """The same fixture bytes, this time through manual file upload
    (Prompt 13: "through Drive/manual ingestion") — proving the Zoom
    VTT shape works via the ordinary parser registry regardless of
    which connector (or no connector at all) delivered it."""
    from datetime import UTC, datetime

    from project_context.domain.evidence import EvidenceSourceType, ManualFileUploadInput
    from project_context.services import evidence as evidence_service

    result = evidence_service.submit_file_upload(
        conn, project_id,
        ManualFileUploadInput(
            title="Acme Kickoff transcript", source_type=EvidenceSourceType.CALL_RECORDING,
            occurred_at=datetime(2026, 6, 1, tzinfo=UTC), filename=ZOOM_VTT_FILENAME,
            data=ZOOM_VTT_TRANSCRIPT_BYTES,
        ),
        evidence_dir=evidence_dir, max_upload_bytes=25 * 1024 * 1024,
        chunk_target_chars=4000, chunk_overlap_ratio=0.0,
    )
    assert result.content.parse_status is ParseStatus.PARSED
    assert "We decided to ship the Acme rollout in July." in result.content.normalized_text


def test_zoom_summary_docx_ingests_through_manual_upload(conn, project_id, evidence_dir):
    from datetime import UTC, datetime

    from project_context.domain.evidence import EvidenceSourceType, ManualFileUploadInput
    from project_context.services import evidence as evidence_service

    result = evidence_service.submit_file_upload(
        conn, project_id,
        ManualFileUploadInput(
            title="Acme Kickoff summary", source_type=EvidenceSourceType.MEETING_NOTES,
            occurred_at=datetime(2026, 6, 1, tzinfo=UTC), filename=ZOOM_SUMMARY_DOCX_FILENAME,
            data=build_zoom_summary_docx(),
        ),
        evidence_dir=evidence_dir, max_upload_bytes=25 * 1024 * 1024,
        chunk_target_chars=4000, chunk_overlap_ratio=0.0,
    )
    assert result.content.parse_status is ParseStatus.PARSED
    assert "Alice to schedule the next check-in." in result.content.normalized_text
