"""Tests for `project_context.connectors.drive.DriveConnector`:
pagination, recursive folder listing, Docs export, trashed/deleted
files, 403/404, and 429/5xx retry (Section 11.2; FR-004, FR-027;
Prompt 10). Every test uses `FakeDriveApi` (tests/fixtures/
fake_drive_api.py) — nothing here ever touches the network."""

from __future__ import annotations

import pytest

from fixtures.fake_drive_api import FakeDriveApi
from project_context.connectors.drive import (
    DOC_EXPORT_MIME_TYPE,
    MIME_TYPE_FOLDER,
    MIME_TYPE_GOOGLE_DOC,
    DriveConnector,
)
from project_context.connectors.errors import (
    ConnectorAuthError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
)
from project_context.connectors.protocol import ConnectorHealthStatus
from project_context.domain.evidence import ArtifactAvailability

_ROOT = "root-folder-id"


def _connector(api: FakeDriveApi, *, folder_id: str = _ROOT) -> DriveConnector:
    return DriveConnector(
        access_token="fake-access-token", folder_id=folder_id, http_transport=api,
        sleep=lambda _s: None, rand=lambda: 0.0,
    )


# --- validate_config ---------------------------------------------------


def test_validate_config_ok_for_an_accessible_folder():
    api = FakeDriveApi()
    api.files[_ROOT] = {"id": _ROOT, "mimeType": MIME_TYPE_FOLDER, "trashed": False}
    health = _connector(api).validate_config()
    assert health.status is ConnectorHealthStatus.OK


def test_validate_config_maps_401_to_auth_error():
    api = FakeDriveApi()
    api.fail_next(f"get:{_ROOT}", times=99, status_code=401)
    health = _connector(api).validate_config()
    assert health.status is ConnectorHealthStatus.AUTH_ERROR


def test_validate_config_maps_403_to_permission_error():
    api = FakeDriveApi()
    api.fail_next(f"get:{_ROOT}", times=99, status_code=403)
    health = _connector(api).validate_config()
    assert health.status is ConnectorHealthStatus.PERMISSION_ERROR


def test_validate_config_maps_404_to_config_error():
    api = FakeDriveApi()  # folder never registered -> 404
    health = _connector(api).validate_config()
    assert health.status is ConnectorHealthStatus.CONFIG_ERROR


def test_validate_config_rejects_a_trashed_folder():
    api = FakeDriveApi()
    api.files[_ROOT] = {"id": _ROOT, "mimeType": MIME_TYPE_FOLDER, "trashed": True}
    health = _connector(api).validate_config()
    assert health.status is ConnectorHealthStatus.CONFIG_ERROR


def test_validate_config_rejects_a_non_folder_id():
    api = FakeDriveApi()
    api.files[_ROOT] = {"id": _ROOT, "mimeType": "text/plain", "trashed": False}
    health = _connector(api).validate_config()
    assert health.status is ConnectorHealthStatus.CONFIG_ERROR


# --- discover: pagination, recursion, filtering -------------------------


def test_discover_returns_files_from_the_root_folder():
    api = FakeDriveApi()
    api.add_folder(
        _ROOT,
        [
            api.add_file("f1", name="Notes.txt", mime_type="text/plain"),
            api.add_file("f2", name="Report.pdf", mime_type="application/pdf"),
        ],
    )
    page = _connector(api).discover(None)
    assert {a.external_id for a in page.artifacts} == {"f1", "f2"}
    assert page.next_checkpoint is None


def test_discover_paginates_within_one_folder():
    api = FakeDriveApi()
    children = [api.add_file(f"f{i}", name=f"doc{i}.txt", mime_type="text/plain") for i in range(5)]
    api.add_folder(_ROOT, children)
    api.set_page_size(_ROOT, 2)

    connector = _connector(api)
    page1 = connector.discover(None)
    assert len(page1.artifacts) == 2
    assert page1.next_checkpoint is not None

    page2 = connector.discover(page1.next_checkpoint)
    assert len(page2.artifacts) == 2
    assert page2.next_checkpoint is not None

    page3 = connector.discover(page2.next_checkpoint)
    assert len(page3.artifacts) == 1
    assert page3.next_checkpoint is None

    all_ids = {a.external_id for a in page1.artifacts + page2.artifacts + page3.artifacts}
    assert all_ids == {f"f{i}" for i in range(5)}


def test_discover_recursively_walks_subfolders():
    api = FakeDriveApi()
    api.add_folder(
        _ROOT,
        [
            api.add_file("f-root", name="root.txt", mime_type="text/plain"),
            {"id": "sub-1", "name": "Sub", "mimeType": MIME_TYPE_FOLDER, "trashed": False},
        ],
    )
    api.add_folder(
        "sub-1",
        [api.add_file("f-sub", name="sub.txt", mime_type="text/plain")],
    )

    connector = _connector(api)
    page1 = connector.discover(None)
    assert {a.external_id for a in page1.artifacts} == {"f-root"}
    assert page1.next_checkpoint is not None  # sub-1 still queued

    page2 = connector.discover(page1.next_checkpoint)
    assert {a.external_id for a in page2.artifacts} == {"f-sub"}
    assert page2.next_checkpoint is None


def test_discover_skips_unsupported_google_native_types():
    api = FakeDriveApi()
    api.add_folder(
        _ROOT,
        [
            api.add_file("f1", name="Notes.txt", mime_type="text/plain"),
            api.add_file(
                "sheet1", name="Budget", mime_type="application/vnd.google-apps.spreadsheet"
            ),
        ],
    )
    page = _connector(api).discover(None)
    assert {a.external_id for a in page.artifacts} == {"f1"}


def test_discover_version_marker_reflects_modified_time_for_change_detection():
    api = FakeDriveApi()
    api.add_folder(
        _ROOT,
        [
            api.add_file(
                "f1", name="a.txt", mime_type="text/plain",
                modified_time="2026-08-01T00:00:00Z",
            )
        ],
    )
    first = _connector(api).discover(None).artifacts[0]
    assert first.version_marker == "2026-08-01T00:00:00Z"

    api.files["f1"]["modifiedTime"] = "2026-08-15T00:00:00Z"
    api.folders[_ROOT][0]["modifiedTime"] = "2026-08-15T00:00:00Z"
    second = _connector(api).discover(None).artifacts[0]
    assert second.version_marker == "2026-08-15T00:00:00Z"
    assert second.version_marker != first.version_marker


def test_list_call_uses_fields_projection_not_the_unrestricted_default():
    api = FakeDriveApi()
    api.add_folder(_ROOT, [])
    _connector(api).discover(None)
    assert api.list_calls
    fields = api.list_calls[0]["params"]["fields"]
    assert "nextPageToken" in fields
    assert "mimeType" in fields
    assert fields != "*"  # never the unrestricted default field set


# --- preview -------------------------------------------------------------


def test_preview_uses_the_passed_boundary_not_the_configured_folder():
    api = FakeDriveApi()
    api.add_folder("other-folder", [api.add_file("f1", name="a.txt", mime_type="text/plain")])
    connector = _connector(api, folder_id=_ROOT)  # configured for a different folder
    results = connector.preview({"folder_id": "other-folder"}, limit=20)
    assert [r.external_id for r in results] == ["f1"]


def test_preview_is_bounded_by_limit():
    api = FakeDriveApi()
    children = [api.add_file(f"f{i}", name=f"d{i}.txt", mime_type="text/plain") for i in range(10)]
    api.add_folder(_ROOT, children)
    results = _connector(api).preview({"folder_id": _ROOT}, limit=3)
    assert len(results) == 3


# --- fetch -----------------------------------------------------------------


def test_fetch_downloads_a_binary_file():
    api = FakeDriveApi()
    metadata = api.add_file(
        "f1", name="notes.pdf", mime_type="application/pdf", content=b"%PDF-1.4 fake pdf bytes"
    )
    api.add_folder(_ROOT, [metadata])
    connector = _connector(api)
    artifact = connector.discover(None).artifacts[0]
    raw = connector.fetch(artifact)
    assert raw.data == b"%PDF-1.4 fake pdf bytes"
    assert raw.mime_type == "application/pdf"
    assert raw.filename == "notes.pdf"


def test_fetch_exports_a_google_doc_to_text_plain():
    api = FakeDriveApi()
    metadata = api.add_file(
        "doc1", name="Kickoff notes", mime_type=MIME_TYPE_GOOGLE_DOC,
        export_content=b"Exported plain text content.",
    )
    api.add_folder(_ROOT, [metadata])
    connector = _connector(api)
    artifact = connector.discover(None).artifacts[0]
    raw = connector.fetch(artifact)
    assert raw.data == b"Exported plain text content."
    assert raw.mime_type == DOC_EXPORT_MIME_TYPE
    assert raw.filename == "Kickoff notes.txt"
    assert api.export_calls == ["doc1"]


def test_fetch_raises_not_found_for_a_deleted_file():
    api = FakeDriveApi()
    metadata = api.add_file("f1", name="gone.txt", mime_type="text/plain")  # no content registered
    api.add_folder(_ROOT, [metadata])
    connector = _connector(api)
    artifact = connector.discover(None).artifacts[0]
    with pytest.raises(ConnectorNotFoundError):
        connector.fetch(artifact)


# --- check_availability ----------------------------------------------------


def test_check_availability_detects_a_trashed_file():
    api = FakeDriveApi()
    api.add_file("f1", name="x.txt", mime_type="text/plain", trashed=True)
    result = _connector(api).check_availability("f1")
    assert result is ArtifactAvailability.DELETED_EXTERNAL


def test_check_availability_detects_a_deleted_file():
    api = FakeDriveApi()  # never registered -> 404
    result = _connector(api).check_availability("does-not-exist")
    assert result is ArtifactAvailability.DELETED_EXTERNAL


def test_check_availability_detects_permission_revoked():
    api = FakeDriveApi()
    api.fail_next("get:f1", times=99, status_code=403)
    result = _connector(api).check_availability("f1")
    assert result is ArtifactAvailability.INACCESSIBLE


# --- retry behavior is inherited from request_with_retry, exercised
# end-to-end through the connector here (unit coverage of the retry
# math itself lives in test_connectors_http.py) --------------------------


def test_discover_retries_transparently_on_429_with_retry_after():
    api = FakeDriveApi()
    api.add_folder(_ROOT, [api.add_file("f1", name="a.txt", mime_type="text/plain")])
    api.fail_next(f"list:{_ROOT}", times=1, status_code=429, headers={"Retry-After": "0"})
    page = _connector(api).discover(None)
    assert [a.external_id for a in page.artifacts] == ["f1"]


def test_discover_retries_transparently_on_5xx():
    api = FakeDriveApi()
    api.add_folder(_ROOT, [api.add_file("f1", name="a.txt", mime_type="text/plain")])
    api.fail_next(f"list:{_ROOT}", times=2, status_code=503)
    page = _connector(api).discover(None)
    assert [a.external_id for a in page.artifacts] == ["f1"]


def test_discover_raises_permission_error_for_a_403_folder():
    api = FakeDriveApi()
    api.fail_next(f"list:{_ROOT}", times=99, status_code=403)
    with pytest.raises(ConnectorPermissionError):
        _connector(api).discover(None)


def test_discover_raises_auth_error_for_a_401():
    api = FakeDriveApi()
    api.fail_next(f"list:{_ROOT}", times=99, status_code=401)
    with pytest.raises(ConnectorAuthError):
        _connector(api).discover(None)
