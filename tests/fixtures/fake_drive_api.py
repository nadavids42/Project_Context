"""An in-memory fake of the tiny slice of the Drive v3 REST API
`project_context.connectors.drive.DriveConnector` calls — `files.list`
(paginated), `files.get` (metadata and `alt=media` binary download),
and `files.export`. Implements `project_context.connectors.http.
HttpTransport`, so it plugs directly into `DriveConnector` in place of
`RequestsHttpTransport`; nothing in any test using this fixture ever
touches the network (Section 15: "Tests must not require live
credentials").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from project_context.connectors.http import HttpResponse

_PARENT_QUERY_RE = re.compile(r"'([^']+)' in parents")


@dataclass
class _FailurePlan:
    """How many times a given call should fail before succeeding, and
    with what status/headers."""

    remaining: int
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeDriveApi:
    """Build a small fake folder tree with `add_folder`/`add_file`, then
    pass this object as `http_transport=` to `DriveConnector`."""

    #: folder_id -> ordered list of raw Drive "files" child dicts
    #: (id/name/mimeType/... exactly as the real API would return them).
    folders: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: file_id -> (metadata dict, alt=media bytes or None, export bytes or None)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    file_bytes: dict[str, bytes] = field(default_factory=dict)
    export_bytes: dict[str, bytes] = field(default_factory=dict)
    #: folder_id -> page size to enforce for files.list pagination.
    page_sizes: dict[str, int] = field(default_factory=dict)
    #: A queued failure plan per exact URL+kind key, e.g. "list:<folder_id>".
    failures: dict[str, _FailurePlan] = field(default_factory=dict)

    list_calls: list[dict[str, Any]] = field(default_factory=list)
    get_calls: list[str] = field(default_factory=list)
    export_calls: list[str] = field(default_factory=list)

    # --- fixture-building helpers -------------------------------------

    def add_folder(self, folder_id: str, children: list[dict[str, Any]]) -> None:
        self.folders[folder_id] = children

    def add_file(
        self,
        file_id: str,
        *,
        name: str,
        mime_type: str,
        modified_time: str = "2026-08-01T00:00:00.000Z",
        size: int | None = None,
        trashed: bool = False,
        content: bytes | None = None,
        export_content: bytes | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Registers full metadata/content for `file_id` and returns the
        raw child dict callers pass to `add_folder`."""
        metadata = {
            "id": file_id,
            "name": name,
            "mimeType": mime_type,
            "modifiedTime": modified_time,
            "trashed": trashed,
            **extra,
        }
        if size is not None:
            metadata["size"] = str(size)
        self.files[file_id] = metadata
        if content is not None:
            self.file_bytes[file_id] = content
        if export_content is not None:
            self.export_bytes[file_id] = export_content
        return metadata

    def set_page_size(self, folder_id: str, size: int) -> None:
        self.page_sizes[folder_id] = size

    def fail_next(
        self, key: str, *, times: int, status_code: int, headers: dict[str, str] | None = None
    ) -> None:
        """`key` is `"list:<folder_id>"`, `"get:<file_id>"`, or
        `"export:<file_id>"` — the next `times` calls of that exact kind
        fail with `status_code` before behaving normally."""
        self.failures[key] = _FailurePlan(
            remaining=times, status_code=status_code, headers=headers or {}
        )

    def _maybe_fail(self, key: str) -> HttpResponse | None:
        plan = self.failures.get(key)
        if plan is None or plan.remaining <= 0:
            return None
        plan.remaining -= 1
        return HttpResponse(status_code=plan.status_code, headers=plan.headers, content=b"{}")

    # --- HttpTransport protocol -----------------------------------------

    def request(
        self, method: str, url: str, *, params=None, headers=None, timeout: float = 30.0
    ) -> HttpResponse:
        params = params or {}
        if url.endswith("/files") and "q" in params:
            return self._handle_list(params)
        if url.endswith("/export"):
            file_id = url.rsplit("/", 2)[-2]
            return self._handle_export(file_id, params)
        # .../files/{id} (metadata get or alt=media download)
        file_id = url.rsplit("/", 1)[-1]
        if params.get("alt") == "media":
            return self._handle_download(file_id)
        return self._handle_get_metadata(file_id)

    def _handle_list(self, params: dict[str, Any]) -> HttpResponse:
        match = _PARENT_QUERY_RE.search(params["q"])
        assert match is not None, f"malformed q: {params['q']!r}"
        folder_id = match.group(1)
        self.list_calls.append(
            {"folder_id": folder_id, "page_token": params.get("pageToken"), "params": params}
        )

        failure = self._maybe_fail(f"list:{folder_id}")
        if failure is not None:
            return failure

        if folder_id not in self.folders:
            return HttpResponse(status_code=404, headers={}, content=b'{"error": "not found"}')

        children = self.folders[folder_id]
        page_size = self.page_sizes.get(folder_id, len(children) or 1)
        start = int(params.get("pageToken") or 0)
        end = start + page_size
        page = children[start:end]
        body: dict[str, Any] = {"files": page}
        if end < len(children):
            body["nextPageToken"] = str(end)
        import json

        return HttpResponse(status_code=200, headers={}, content=json.dumps(body).encode("utf-8"))

    def _handle_get_metadata(self, file_id: str) -> HttpResponse:
        import json

        self.get_calls.append(file_id)
        failure = self._maybe_fail(f"get:{file_id}")
        if failure is not None:
            return failure
        metadata = self.files.get(file_id)
        if metadata is None:
            return HttpResponse(status_code=404, headers={}, content=b'{"error": "not found"}')
        return HttpResponse(
            status_code=200, headers={}, content=json.dumps(metadata).encode("utf-8")
        )

    def _handle_download(self, file_id: str) -> HttpResponse:
        failure = self._maybe_fail(f"download:{file_id}")
        if failure is not None:
            return failure
        content = self.file_bytes.get(file_id)
        if content is None:
            return HttpResponse(status_code=404, headers={}, content=b'{"error": "not found"}')
        return HttpResponse(status_code=200, headers={}, content=content)

    def _handle_export(self, file_id: str, params: dict[str, Any]) -> HttpResponse:
        self.export_calls.append(file_id)
        failure = self._maybe_fail(f"export:{file_id}")
        if failure is not None:
            return failure
        content = self.export_bytes.get(file_id)
        if content is None:
            return HttpResponse(status_code=404, headers={}, content=b'{"error": "not found"}')
        return HttpResponse(status_code=200, headers={}, content=content)
