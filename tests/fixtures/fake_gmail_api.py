"""An in-memory fake of the tiny slice of the Gmail v1 REST API
`project_context.connectors.gmail.GmailConnector` calls — `messages.list`
(paginated) and `messages.get` (`format=metadata` and `format=full`).
Implements `project_context.connectors.http.HttpTransport`, so it plugs
directly into `GmailConnector` in place of `RequestsHttpTransport`;
nothing in any test using this fixture ever touches the network
(Section 15: "Tests must not require live credentials").

Query-string correctness (`label:`/`q`/`after:` construction) is
covered directly against `project_context.connectors.gmail`'s pure
query-building helpers, not by filtering here — `messages.list` always
returns every registered message (paginated), regardless of `q`,
matching `FakeDriveApi`'s existing precedent of not re-implementing the
provider's own query semantics.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

from project_context.connectors.http import HttpResponse


def b64url(text: str) -> str:
    """Gmail-shaped base64url body data, no padding — the encoding
    `project_context.domain.email_normalization.decode_base64url` is
    built to handle."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


@dataclass
class _FailurePlan:
    remaining: int
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeGmailApi:
    """Build a small fake mailbox with `add_message`, then pass this
    object as `http_transport=` to `GmailConnector`."""

    #: message_id -> full `messages.get` resource, as the real API
    #: would shape it (`id`, `threadId`, `payload: {headers, ...}`).
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Ids returned, in this order, by `messages.list` (paginated).
    message_order: list[str] = field(default_factory=list)
    page_size: int = 50
    #: key -> failure plan. `"list"` for `messages.list`; `"get:<id>"`
    #: for `messages.get`.
    failures: dict[str, _FailurePlan] = field(default_factory=dict)

    list_calls: list[dict[str, Any]] = field(default_factory=list)
    get_calls: list[dict[str, Any]] = field(default_factory=list)

    # --- fixture-building helpers -------------------------------------

    def add_message(
        self,
        message_id: str,
        *,
        thread_id: str | None = None,
        subject: str = "Subject line",
        from_addr: str = "sender@example.com",
        to_addr: str = "recipient@example.com",
        cc_addr: str | None = None,
        date: str = "Mon, 1 Jun 2026 12:00:00 +0000",
        plain_text: str | None = "Hello there.",
        html_text: str | None = None,
        attachment_filename: str | None = None,
        malformed_plain_data: bool = False,
        no_decodable_part: bool = False,
    ) -> None:
        headers = [
            {"name": "Subject", "value": subject},
            {"name": "From", "value": from_addr},
            {"name": "To", "value": to_addr},
            {"name": "Date", "value": date},
        ]
        if cc_addr:
            headers.append({"name": "Cc", "value": cc_addr})

        parts: list[dict[str, Any]] = []
        if plain_text is not None:
            data = "###not-valid-base64###" if malformed_plain_data else b64url(plain_text)
            parts.append({"mimeType": "text/plain", "body": {"data": data}})
        if html_text is not None:
            parts.append({"mimeType": "text/html", "body": {"data": b64url(html_text)}})
        if attachment_filename:
            parts.append(
                {
                    "mimeType": "application/pdf",
                    "filename": attachment_filename,
                    "body": {"attachmentId": "att-1", "size": 4096},
                }
            )
        if no_decodable_part:
            parts = []

        payload: dict[str, Any] = {"headers": headers, "parts": parts}
        self.messages[message_id] = {
            "id": message_id,
            "threadId": thread_id or message_id,
            "payload": payload,
        }
        if message_id not in self.message_order:
            self.message_order.append(message_id)

    def remove_message(self, message_id: str) -> None:
        """Simulate a message vanishing between `list` and `get` (still
        listed, but 404s on fetch) without registering a 404 failure
        plan — used for "inaccessible message" tests."""
        self.messages.pop(message_id, None)

    def set_page_size(self, size: int) -> None:
        self.page_size = size

    def fail_next(
        self, key: str, *, times: int, status_code: int, headers: dict[str, str] | None = None
    ) -> None:
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
        if url.endswith("/messages"):
            return self._handle_list(params)
        message_id = url.rsplit("/", 1)[-1]
        return self._handle_get(message_id, params)

    def _handle_list(self, params: dict[str, Any]) -> HttpResponse:
        self.list_calls.append(dict(params))
        failure = self._maybe_fail("list")
        if failure is not None:
            return failure

        start = int(params.get("pageToken") or 0)
        max_results = int(params.get("maxResults", self.page_size))
        page_size = min(self.page_size, max_results)
        end = start + page_size
        page_ids = self.message_order[start:end]
        body: dict[str, Any] = {"messages": [{"id": mid, "threadId": mid} for mid in page_ids]}
        if end < len(self.message_order):
            body["nextPageToken"] = str(end)
        return HttpResponse(status_code=200, headers={}, content=json.dumps(body).encode("utf-8"))

    def _handle_get(self, message_id: str, params: dict[str, Any]) -> HttpResponse:
        self.get_calls.append({"id": message_id, "params": dict(params)})
        failure = self._maybe_fail(f"get:{message_id}")
        if failure is not None:
            return failure

        message = self.messages.get(message_id)
        if message is None:
            return HttpResponse(status_code=404, headers={}, content=b'{"error": "not found"}')

        fmt = params.get("format", "full")
        if fmt == "metadata":
            allowed = set(params.get("metadataHeaders", []))
            all_headers = message["payload"]["headers"]
            headers = [h for h in all_headers if h["name"] in allowed] if allowed else all_headers
            resource = {
                "id": message["id"],
                "threadId": message["threadId"],
                "payload": {"headers": headers},
            }
        else:
            resource = message
        return HttpResponse(
            status_code=200, headers={}, content=json.dumps(resource).encode("utf-8")
        )
