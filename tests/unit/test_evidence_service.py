"""Tests for the manual evidence ingestion service: idempotence,
versioning, size rejection, parser-status handling, and cross-project
isolation (FR-005 through FR-009)."""

from __future__ import annotations

from datetime import datetime

import pytest

from fixtures.docx_builder import build_docx_with_paragraphs_and_table
from fixtures.pdf_builder import build_minimal_pdf
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import (
    ArtifactType,
    EvidenceSourceType,
    ManualFileUploadInput,
    ManualTextInput,
    ParseStatus,
)
from project_context.domain.projects import ProjectCreateInput
from project_context.evidence_store import read_bytes
from project_context.services import evidence as evidence_service
from project_context.services.evidence import EvidenceTooLargeError
from project_context.services.projects import ProjectNotFoundError, create_project

_OCCURRED_AT = datetime(2026, 8, 20, 14, 30)
_CHUNK_KWARGS = {"chunk_target_chars": 4000, "chunk_overlap_ratio": 0.10}


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
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


def _text_input(**overrides):
    fields = {
        "title": "Kickoff notes",
        "source_type": EvidenceSourceType.MEETING_NOTES,
        "occurred_at": _OCCURRED_AT,
        "text": "We discussed scope.\n\nNext steps: send proposal by Friday.",
        **overrides,
    }
    return ManualTextInput(**fields)


def _file_input(**overrides):
    fields = {
        "title": "Contract draft",
        "source_type": EvidenceSourceType.DOCUMENT,
        "occurred_at": _OCCURRED_AT,
        "filename": "contract.txt",
        "data": b"This is the contract text.\n\nSection 2: payment terms apply.",
        **overrides,
    }
    return ManualFileUploadInput(**fields)


# --- manual text --------------------------------------------------------


def test_submit_manual_text_creates_artifact_and_content(conn, project_id, evidence_dir):
    result = evidence_service.submit_manual_text(
        conn, project_id, _text_input(), evidence_dir=evidence_dir, **_CHUNK_KWARGS
    )

    assert result.created_new_version is True
    assert result.artifact.artifact_type is ArtifactType.MANUAL_TEXT
    assert result.artifact.title == "Kickoff notes"
    assert result.content.parse_status is ParseStatus.PARSED
    assert (
        result.content.normalized_text
        == "We discussed scope.\n\nNext steps: send proposal by Friday."
    )
    assert len(result.chunks) >= 1


def test_submit_manual_text_stores_bytes_content_addressed(conn, project_id, evidence_dir):
    result = evidence_service.submit_manual_text(
        conn, project_id, _text_input(), evidence_dir=evidence_dir, **_CHUNK_KWARGS
    )

    stored = read_bytes(evidence_dir, result.content.sha256)
    assert stored.decode("utf-8") == result.content.normalized_text


def test_submit_manual_text_identical_resubmission_is_idempotent(conn, project_id, evidence_dir):
    data = _text_input()
    first = evidence_service.submit_manual_text(
        conn, project_id, data, evidence_dir=evidence_dir, **_CHUNK_KWARGS
    )
    second = evidence_service.submit_manual_text(
        conn, project_id, data, evidence_dir=evidence_dir, **_CHUNK_KWARGS
    )

    assert second.created_new_version is False
    assert second.artifact.id == first.artifact.id
    assert second.content.id == first.content.id
    from project_context.db import evidence_repository

    history = evidence_repository.list_contents_for_artifact(conn, project_id, first.artifact.id)
    assert len(history) == 1


def test_submit_manual_text_changed_text_creates_new_version(conn, project_id, evidence_dir):
    first = evidence_service.submit_manual_text(
        conn, project_id, _text_input(), evidence_dir=evidence_dir, **_CHUNK_KWARGS
    )
    changed = _text_input(text="We discussed scope.\n\nNext steps: send proposal by MONDAY.")
    second = evidence_service.submit_manual_text(
        conn, project_id, changed, evidence_dir=evidence_dir, **_CHUNK_KWARGS
    )

    assert second.created_new_version is True
    assert second.artifact.id == first.artifact.id
    assert second.content.id != first.content.id
    assert second.artifact.current_content_id == second.content.id

    from project_context.db import evidence_repository

    old_content = evidence_repository.get_content(conn, project_id, first.content.id)
    assert old_content is not None
    assert old_content.normalized_text == first.content.normalized_text  # old version unchanged


def test_submit_manual_text_raises_for_unknown_project(conn, evidence_dir):
    with pytest.raises(ProjectNotFoundError):
        evidence_service.submit_manual_text(
            conn, "does-not-exist", _text_input(), evidence_dir=evidence_dir, **_CHUNK_KWARGS
        )


def test_submit_manual_text_empty_after_strip_is_rejected_by_domain_validation():
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError, checked in test_domain_evidence.py
        _text_input(text="   ")


# --- file upload: txt/docx/pdf/vtt happy paths ------------------------------


def test_submit_file_upload_txt(conn, project_id, evidence_dir):
    result = evidence_service.submit_file_upload(
        conn,
        project_id,
        _file_input(),
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )

    assert result.content.parse_status is ParseStatus.PARSED
    assert result.content.mime_type == "text/plain"
    assert result.content.original_filename == "contract.txt"
    assert len(result.chunks) >= 1


def test_submit_file_upload_docx(conn, project_id, evidence_dir):
    data = _file_input(filename="notes.docx", data=build_docx_with_paragraphs_and_table())

    result = evidence_service.submit_file_upload(
        conn,
        project_id,
        data,
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )

    assert result.content.parse_status is ParseStatus.PARSED
    assert "First paragraph." in result.content.normalized_text
    assert result.content.mime_type.endswith("wordprocessingml.document")


def test_submit_file_upload_pdf_text(conn, project_id, evidence_dir):
    pdf_bytes = build_minimal_pdf(["This page has plenty of dense, real, extractable content."])
    data = _file_input(filename="doc.pdf", data=pdf_bytes)

    result = evidence_service.submit_file_upload(
        conn,
        project_id,
        data,
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )

    assert result.content.parse_status is ParseStatus.PARSED
    assert result.content.mime_type == "application/pdf"


def test_submit_file_upload_scanned_pdf_is_ocr_required_with_no_chunks(
    conn, project_id, evidence_dir
):
    pdf_bytes = build_minimal_pdf(["", ""])
    data = _file_input(filename="scan.pdf", data=pdf_bytes)

    result = evidence_service.submit_file_upload(
        conn,
        project_id,
        data,
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )

    assert result.content.parse_status is ParseStatus.OCR_REQUIRED
    assert result.chunks == ()
    assert result.content.normalized_text in (None, "")


def test_submit_file_upload_valid_vtt(conn, project_id, evidence_dir):
    vtt_bytes = (
        b"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n<v Alice>Hello there.</v>\n\n"
        b"00:00:02.500 --> 00:00:04.000\n<v Bob>Hi Alice.</v>\n"
    )
    data = _file_input(filename="meeting.vtt", data=vtt_bytes)

    result = evidence_service.submit_file_upload(
        conn,
        project_id,
        data,
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )

    assert result.content.parse_status is ParseStatus.PARSED
    assert "Alice" in result.content.normalized_text
    assert len(result.chunks) >= 1


def test_submit_file_upload_malformed_vtt_is_failed_but_still_stored(
    conn, project_id, evidence_dir
):
    malformed = b"WEBVTT\n\nnot-a-timestamp --> also-bad\nSome text\n"
    data = _file_input(filename="bad.vtt", data=malformed)

    result = evidence_service.submit_file_upload(
        conn,
        project_id,
        data,
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )

    assert result.content.parse_status is ParseStatus.FAILED
    assert result.chunks == ()
    # Still content-addressed and retrievable, per FR-006 ("SHA-256 are stored").
    assert read_bytes(evidence_dir, result.content.sha256) == malformed


def test_malformed_reupload_does_not_corrupt_the_previously_good_version(
    conn, project_id, evidence_dir
):
    """A later malformed/unsupported version becomes current (matching
    FR-007's unconditional "advances transactionally"), but the earlier
    good version's bytes and text must remain intact and addressable —
    "fail visibly without corrupting previously stored versions"."""
    good = evidence_service.submit_file_upload(
        conn,
        project_id,
        _file_input(filename="notes.txt", data=b"Good, well-formed evidence text."),
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )
    assert good.content.parse_status is ParseStatus.PARSED

    # Same filename (same artifact identity) but content that no longer
    # matches the .txt extension — a realistic "the file at this name
    # got overwritten with something else" scenario.
    malformed = evidence_service.submit_file_upload(
        conn,
        project_id,
        _file_input(filename="notes.txt", data=b"WEBVTT\n\nnot-a-timestamp --> bad\ntext\n"),
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )

    assert malformed.content.parse_status is ParseStatus.UNSUPPORTED
    assert malformed.artifact.id == good.artifact.id
    assert malformed.content.id != good.content.id
    assert malformed.artifact.current_content_id == malformed.content.id

    from project_context.db import evidence_repository

    history = evidence_repository.list_contents_for_artifact(conn, project_id, good.artifact.id)
    assert [c.id for c in history] == [good.content.id, malformed.content.id]

    still_good = evidence_repository.get_content(conn, project_id, good.content.id)
    assert still_good.parse_status is ParseStatus.PARSED
    assert still_good.normalized_text == "Good, well-formed evidence text."
    assert read_bytes(evidence_dir, still_good.sha256) == b"Good, well-formed evidence text."


def test_submit_file_upload_unsupported_type_is_recorded_visibly(conn, project_id, evidence_dir):
    data = _file_input(filename="data.xyz", data=b"totally unrecognized binary content" * 3)

    result = evidence_service.submit_file_upload(
        conn,
        project_id,
        data,
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )

    assert result.content.parse_status is ParseStatus.UNSUPPORTED
    assert result.chunks == ()
    warnings = (result.content.location_map or {}).get("warnings") or []
    assert any("unsupported" in w.lower() for w in warnings)


def test_submit_file_upload_content_mismatch_is_recorded_as_unsupported(
    conn, project_id, evidence_dir
):
    """FR-006: MIME/extension validated, not trusted alone — a .pdf
    filename with non-PDF bytes is not silently parsed as PDF."""
    data = _file_input(filename="fake.pdf", data=b"just plain text, not a real pdf at all")

    result = evidence_service.submit_file_upload(
        conn,
        project_id,
        data,
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )

    assert result.content.parse_status is ParseStatus.UNSUPPORTED


# --- idempotence and versioning for file uploads ----------------------------


def test_submit_file_upload_identical_resubmission_is_idempotent(conn, project_id, evidence_dir):
    data = _file_input()
    first = evidence_service.submit_file_upload(
        conn,
        project_id,
        data,
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )
    second = evidence_service.submit_file_upload(
        conn,
        project_id,
        data,
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )

    assert second.created_new_version is False
    assert second.content.id == first.content.id


def test_submit_file_upload_changed_bytes_creates_new_version(conn, project_id, evidence_dir):
    first = evidence_service.submit_file_upload(
        conn,
        project_id,
        _file_input(),
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )
    changed = _file_input(
        data=b"This is the REVISED contract text.\n\nSection 2: new payment terms."
    )
    second = evidence_service.submit_file_upload(
        conn,
        project_id,
        changed,
        evidence_dir=evidence_dir,
        max_upload_bytes=25 * 1024 * 1024,
        **_CHUNK_KWARGS,
    )

    assert second.created_new_version is True
    assert second.artifact.id == first.artifact.id
    assert second.content.id != first.content.id
    assert second.artifact.current_content_id == second.content.id


# --- size rejection -----------------------------------------------------


def test_submit_file_upload_rejects_oversized_file(conn, project_id, evidence_dir):
    data = _file_input(data=b"x" * 1000)

    with pytest.raises(EvidenceTooLargeError):
        evidence_service.submit_file_upload(
            conn, project_id, data, evidence_dir=evidence_dir, max_upload_bytes=100, **_CHUNK_KWARGS
        )


def test_submit_file_upload_oversized_file_is_not_stored(conn, project_id, evidence_dir):
    data = _file_input(data=b"x" * 1000)

    with pytest.raises(EvidenceTooLargeError):
        evidence_service.submit_file_upload(
            conn, project_id, data, evidence_dir=evidence_dir, max_upload_bytes=100, **_CHUNK_KWARGS
        )

    assert evidence_service.list_evidence(conn, project_id) == []


def test_submit_file_upload_raises_for_unknown_project(conn, evidence_dir):
    with pytest.raises(ProjectNotFoundError):
        evidence_service.submit_file_upload(
            conn,
            "does-not-exist",
            _file_input(),
            evidence_dir=evidence_dir,
            max_upload_bytes=25 * 1024 * 1024,
            **_CHUNK_KWARGS,
        )


# --- list / detail read paths -----------------------------------------------


def test_list_evidence_reflects_status_and_version_count(conn, project_id, evidence_dir):
    evidence_service.submit_manual_text(
        conn, project_id, _text_input(), evidence_dir=evidence_dir, **_CHUNK_KWARGS
    )
    evidence_service.submit_manual_text(
        conn,
        project_id,
        _text_input(text="Different content now."),
        evidence_dir=evidence_dir,
        **_CHUNK_KWARGS,
    )

    items = evidence_service.list_evidence(conn, project_id)

    assert len(items) == 1
    assert items[0].version_count == 2
    assert items[0].current_parse_status is ParseStatus.PARSED


def test_get_evidence_detail_returns_none_for_missing_artifact(conn, project_id):
    assert evidence_service.get_evidence_detail(conn, project_id, "does-not-exist") is None


def test_get_evidence_detail_includes_text_and_chunks(conn, project_id, evidence_dir):
    result = evidence_service.submit_manual_text(
        conn, project_id, _text_input(), evidence_dir=evidence_dir, **_CHUNK_KWARGS
    )

    detail = evidence_service.get_evidence_detail(conn, project_id, result.artifact.id)

    assert detail is not None
    assert detail.content.normalized_text == result.content.normalized_text
    assert len(detail.chunks) == len(result.chunks)
    assert detail.version_count == 1


# --- cross-project isolation --------------------------------------------


def test_evidence_is_not_readable_across_projects(conn, project_id, evidence_dir):
    other_project = create_project(
        conn, ProjectCreateInput(name="Other Project", objective="Obj")
    ).id
    result = evidence_service.submit_manual_text(
        conn, project_id, _text_input(), evidence_dir=evidence_dir, **_CHUNK_KWARGS
    )

    # Attempting to read project A's artifact while scoped to project B
    # must fail closed, even with a fully valid artifact_id.
    assert evidence_service.get_evidence_detail(conn, other_project, result.artifact.id) is None


def test_evidence_lists_do_not_leak_across_projects_with_similar_titles(
    conn, project_id, evidence_dir
):
    other_project = create_project(
        conn, ProjectCreateInput(name="Other Project", objective="Obj")
    ).id

    evidence_service.submit_manual_text(
        conn,
        project_id,
        _text_input(title="Weekly Sync", text="sentinelalpha content here"),
        evidence_dir=evidence_dir,
        **_CHUNK_KWARGS,
    )
    evidence_service.submit_manual_text(
        conn,
        other_project,
        _text_input(title="Weekly Sync", text="sentinelbeta content here"),
        evidence_dir=evidence_dir,
        **_CHUNK_KWARGS,
    )

    project_a_items = evidence_service.list_evidence(conn, project_id)
    project_b_items = evidence_service.list_evidence(conn, other_project)

    assert len(project_a_items) == 1
    assert len(project_b_items) == 1
    detail_a = evidence_service.get_evidence_detail(
        conn, project_id, project_a_items[0].artifact.id
    )
    detail_b = evidence_service.get_evidence_detail(
        conn, other_project, project_b_items[0].artifact.id
    )
    assert "sentinelalpha" in detail_a.content.normalized_text
    assert "sentinelbeta" not in detail_a.content.normalized_text
    assert "sentinelbeta" in detail_b.content.normalized_text
    assert "sentinelalpha" not in detail_b.content.normalized_text
