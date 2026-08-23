"""Read-only Google Drive folder connector (Section 11.2; FR-004,
FR-027; Prompt 10).

Implements `project_context.connectors.protocol.Connector`: recursively
enumerates one configured folder boundary, distinguishes new/changed/
unchanged children by `modifiedTime`, downloads supported binary files,
and exports Google Docs to plain text. Every call is `GET`/`files.export`
— never a write (FR-004: "No connector writes to an external service").

**Observed API limitation, documented per this prompt's instruction**:
Drive's `files.export` endpoint only works for native Google Workspace
document types, and only a fixed set of export MIME types is offered
per source type (`https://developers.google.com/workspace/drive/api/guides/ref-export-formats`).
This connector exports Google Docs (`application/vnd.google-apps.document`)
to `text/plain` only — Sheets, Slides, Forms, and Drawings are
recognized (their `application/vnd.google-apps.*` MIME type is
detected) but deliberately **skipped**, not fetched, because they need
a different, per-type export MIME type this prototype does not
implement (Section 11.2 only specifies Docs export). This is a
preserved manual fallback, not a scope expansion: a user can still
export such a file to a supported format themselves and upload it
through the existing manual-file path.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from typing import Any

from project_context.connectors.errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
)
from project_context.connectors.http import HttpTransport, RequestsHttpTransport, request_with_retry
from project_context.connectors.protocol import (
    ArtifactMetadata,
    ConnectorHealth,
    ConnectorHealthStatus,
    DiscoveryPage,
    RawArtifact,
)
from project_context.domain.evidence import ArtifactAvailability, ArtifactType, EvidenceSourceType

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_FILES_LIST_URL = f"{DRIVE_API_BASE}/files"

#: Section 11.2: Google Docs are exported through Drive rather than the
#: separate Docs API, to a plain-text/DOCX format. This connector uses
#: `text/plain` — simpler, and immediately consumable by the existing
#: TXT/MD parser with no new parser needed.
MIME_TYPE_FOLDER = "application/vnd.google-apps.folder"
MIME_TYPE_GOOGLE_DOC = "application/vnd.google-apps.document"
DOC_EXPORT_MIME_TYPE = "text/plain"
_GOOGLE_APPS_PREFIX = "application/vnd.google-apps."

#: Requested via `fields=` projection on every `files.list`/`files.get`
#: call (Section 11.2: "fields projection") — never the unrestricted
#: default field set.
_LIST_FIELDS = (
    "nextPageToken, files(id, name, mimeType, modifiedTime, size, "
    "md5Checksum, webViewLink, trashed)"
)
_GET_FIELDS = "id, name, mimeType, modifiedTime, size, md5Checksum, webViewLink, trashed"

_DEFAULT_PAGE_SIZE = 100


def _classify_mime_type(mime_type: str) -> str:
    """`"folder"` | `"google_doc"` | `"google_native_unsupported"` |
    `"downloadable"` — see the module docstring's documented limitation
    for why native Sheets/Slides/Forms/Drawings fall into the third
    bucket rather than being fetched."""
    if mime_type == MIME_TYPE_FOLDER:
        return "folder"
    if mime_type == MIME_TYPE_GOOGLE_DOC:
        return "google_doc"
    if mime_type.startswith(_GOOGLE_APPS_PREFIX):
        return "google_native_unsupported"
    return "downloadable"


class DriveConnector:
    """One configured Drive folder boundary (Section 11.2: "exactly one
    explicit Drive folder ID per source/project plus a human-readable
    display name"). `access_token` is a short-lived token the caller
    (sync orchestration) mints fresh from the stored refresh token
    before constructing this connector — nothing here persists or
    refreshes credentials itself."""

    def __init__(
        self,
        *,
        access_token: str,
        folder_id: str,
        http_transport: HttpTransport | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if not folder_id or not folder_id.strip():
            raise ConnectorConfigError("a Drive folder ID is required")
        self._access_token = access_token
        self._folder_id = folder_id.strip()
        self._transport = http_transport or RequestsHttpTransport()
        self._page_size = max(1, min(page_size, 1000))
        self._sleep = sleep
        self._rand = rand

    # --- Connector protocol -------------------------------------------

    def validate_config(self) -> ConnectorHealth:
        try:
            response = self._get_metadata(self._folder_id)
        except ConnectorAuthError as exc:
            return ConnectorHealth(status=ConnectorHealthStatus.AUTH_ERROR, detail=exc.safe_message)
        except ConnectorPermissionError as exc:
            return ConnectorHealth(
                status=ConnectorHealthStatus.PERMISSION_ERROR, detail=exc.safe_message
            )
        except ConnectorNotFoundError:
            return ConnectorHealth(
                status=ConnectorHealthStatus.CONFIG_ERROR,
                detail="configured folder was not found",
            )
        except ConnectorError as exc:
            return ConnectorHealth(status=ConnectorHealthStatus.ERROR, detail=exc.safe_message)

        if response.get("trashed"):
            return ConnectorHealth(
                status=ConnectorHealthStatus.CONFIG_ERROR, detail="configured folder is trashed"
            )
        if response.get("mimeType") != MIME_TYPE_FOLDER:
            return ConnectorHealth(
                status=ConnectorHealthStatus.CONFIG_ERROR,
                detail="configured ID is not a Drive folder",
            )
        return ConnectorHealth(status=ConnectorHealthStatus.OK)

    def preview(self, boundary: dict[str, Any], limit: int = 20) -> list[ArtifactMetadata]:
        """A dry run over `boundary["folder_id"]` (which may not yet be
        saved) rather than this connector's own configured folder — lets
        the UI show matched metadata before the user commits (FR-002)."""
        folder_id = boundary.get("folder_id")
        if not folder_id or not str(folder_id).strip():
            raise ConnectorConfigError("boundary must include a non-empty folder_id")
        folder_id = str(folder_id).strip()

        results: list[ArtifactMetadata] = []
        queue = [folder_id]
        page_token: str | None = None
        while queue and len(results) < limit:
            current_folder = queue[0]
            children, next_token = self._list_children(current_folder, page_token=page_token)
            artifacts, subfolder_ids = self._partition_children(children)
            queue.extend(subfolder_ids)
            results.extend(artifacts)
            if next_token:
                page_token = next_token
            else:
                queue.pop(0)
                page_token = None
        return results[:limit]

    def discover(self, checkpoint: dict[str, Any] | None) -> DiscoveryPage:
        """One `files.list` call — one page of one folder in the
        recursive walk. `checkpoint` carries the remaining folder queue
        plus the page token for the folder currently being listed;
        `next_checkpoint` is `None` only once every folder in the tree
        has been fully listed."""
        if checkpoint is None:
            folder_queue: list[str] = [self._folder_id]
            page_token: str | None = None
        else:
            folder_queue = list(checkpoint.get("folder_queue", []))
            page_token = checkpoint.get("page_token")

        if not folder_queue:
            return DiscoveryPage(artifacts=(), next_checkpoint=None)

        current_folder = folder_queue[0]
        children, next_token = self._list_children(current_folder, page_token=page_token)
        artifacts, subfolder_ids = self._partition_children(children)
        folder_queue = folder_queue + subfolder_ids

        if next_token:
            next_checkpoint: dict[str, Any] | None = {
                "folder_queue": folder_queue, "page_token": next_token,
            }
        else:
            remaining = folder_queue[1:]
            next_checkpoint = {"folder_queue": remaining, "page_token": None} if remaining else None

        return DiscoveryPage(artifacts=tuple(artifacts), next_checkpoint=next_checkpoint)

    def fetch(self, artifact: ArtifactMetadata) -> RawArtifact:
        mime_type = artifact.mime_type or ""
        if mime_type == MIME_TYPE_GOOGLE_DOC:
            response = self._request(
                "GET", f"{DRIVE_API_BASE}/files/{artifact.external_id}/export",
                params={"mimeType": DOC_EXPORT_MIME_TYPE},
            )
            return RawArtifact(
                metadata=artifact, data=response.content, mime_type=DOC_EXPORT_MIME_TYPE,
                filename=f"{artifact.title}.txt",
            )
        response = self._request(
            "GET", f"{DRIVE_API_BASE}/files/{artifact.external_id}", params={"alt": "media"}
        )
        return RawArtifact(
            metadata=artifact, data=response.content,
            mime_type=mime_type or "application/octet-stream", filename=artifact.title,
        )

    # --- Drive-specific extra: availability check (not part of the
    # generic Connector protocol; sync orchestration calls this only
    # for Drive sources, on artifacts no longer seen in a full
    # discover() pass) --------------------------------------------------

    def check_availability(self, external_id: str) -> ArtifactAvailability:
        """Distinguish trashed/deleted from merely-permission-revoked
        for one previously-known `external_id` no longer seen in the
        current recursive listing (FR-027: "deleted/trashed items are
        marked unavailable, not erased")."""
        try:
            response = self._get_metadata(external_id)
        except ConnectorNotFoundError:
            return ArtifactAvailability.DELETED_EXTERNAL
        except ConnectorPermissionError:
            return ArtifactAvailability.INACCESSIBLE
        if response.get("trashed"):
            return ArtifactAvailability.DELETED_EXTERNAL
        # Still exists and is accessible — most likely moved outside the
        # configured folder boundary rather than deleted. Folder mapping
        # is authoritative (Section 11.2), so it is no longer in scope
        # either way; treat it the same as "gone from this source."
        return ArtifactAvailability.DELETED_EXTERNAL

    # --- internals ---------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _request(self, method: str, url: str, *, params: dict[str, Any]):
        return request_with_retry(
            self._transport, method, url, params=params, headers=self._headers(),
            sleep=self._sleep, rand=self._rand,
        )

    def _list_children(
        self, folder_id: str, *, page_token: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": _LIST_FIELDS,
            "pageSize": self._page_size,
            "spaces": "drive",
        }
        if page_token:
            params["pageToken"] = page_token
        response = self._request("GET", DRIVE_FILES_LIST_URL, params=params)
        body = json.loads(response.content.decode("utf-8"))
        return body.get("files", []), body.get("nextPageToken")

    def _get_metadata(self, file_id: str) -> dict[str, Any]:
        response = self._request(
            "GET", f"{DRIVE_API_BASE}/files/{file_id}", params={"fields": _GET_FIELDS}
        )
        return json.loads(response.content.decode("utf-8"))

    def _partition_children(
        self, children: list[dict[str, Any]]
    ) -> tuple[list[ArtifactMetadata], list[str]]:
        artifacts: list[ArtifactMetadata] = []
        subfolder_ids: list[str] = []
        for child in children:
            classification = _classify_mime_type(child.get("mimeType", ""))
            if classification == "folder":
                subfolder_ids.append(child["id"])
            elif classification == "google_native_unsupported":
                continue  # see module docstring's documented limitation
            else:
                artifacts.append(self._to_artifact_metadata(child))
        return artifacts, subfolder_ids

    def _to_artifact_metadata(self, raw: dict[str, Any]) -> ArtifactMetadata:
        size = raw.get("size")
        extra: dict[str, Any] = {}
        if raw.get("md5Checksum"):
            extra["md5Checksum"] = raw["md5Checksum"]
        return ArtifactMetadata(
            external_id=raw["id"],
            title=raw.get("name") or raw["id"],
            artifact_type=ArtifactType.FILE,
            source_type=EvidenceSourceType.DOCUMENT,
            mime_type=raw.get("mimeType"),
            version_marker=raw.get("modifiedTime", ""),
            size_bytes=int(size) if size is not None else None,
            external_url=raw.get("webViewLink"),
            is_container=False,
            is_trashed=bool(raw.get("trashed", False)),
            extra=extra,
        )
