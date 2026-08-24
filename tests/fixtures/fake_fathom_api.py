"""An in-memory fake of the tiny slice of the Fathom REST API
`project_context.connectors.fathom.FathomConnector` calls — `GET
/meetings`, cursor-paginated. Implements
`project_context.connectors.http.HttpTransport`, so it plugs directly
into `FathomConnector` in place of `RequestsHttpTransport`; nothing in
any test using this fixture ever touches the network (Section 15).

Response item shape matches `developers.fathom.ai/api-reference/
meetings/list-meetings` (fetched 2026-08-23) — see
`project_context.connectors.fathom`'s module docstring for the full
field list and the explicit "this is a documented assumption, not a
verified live response" caveat.

`created_after` filtering is *not* re-implemented here — like
`FakeCalendarApi` not re-implementing Calendar's `timeMin`/`timeMax`
window, this fake returns every registered meeting on every page
request regardless of the requested `created_after`; the connector's
own watermark computation is covered directly against
`project_context.services.sync._fathom_created_after`, not by fake
filtering.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from project_context.connectors.http import HttpResponse


@dataclass
class _FailurePlan:
    remaining: int
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeFathomApi:
    """Build a small fake meeting list with `add_meeting`, then pass
    this object as `http_transport=` to `FathomConnector`."""

    #: recording_id (str) -> full `GET /meetings` item.
    meetings: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Ids returned, in this order, by `/meetings` (paginated).
    meeting_order: list[str] = field(default_factory=list)
    page_size: int = 20
    #: `"list"` for every `/meetings` call.
    failures: dict[str, _FailurePlan] = field(default_factory=dict)

    list_calls: list[dict[str, Any]] = field(default_factory=list)

    # --- fixture-building helpers -------------------------------------

    def add_meeting(
        self,
        recording_id: str,
        *,
        title: str = "Untitled meeting",
        meeting_url: str | None = "https://zoom.us/j/000000000",
        share_url: str | None = None,
        playback_url: str | None = None,
        created_at: str = "2026-06-01T14:00:00Z",
        scheduled_start_time: str | None = "2026-06-01T14:00:00Z",
        scheduled_end_time: str | None = "2026-06-01T14:30:00Z",
        recording_start_time: str | None = "2026-06-01T14:01:00Z",
        recording_end_time: str | None = "2026-06-01T14:29:00Z",
        recorded_by_email: str | None = "me@example.com",
        recorded_by_name: str | None = "Me",
        recorded_by_team: str | None = None,
        transcript: list[dict[str, Any]] | None = None,
        default_summary_markdown: str | None = None,
        action_items: list[dict[str, Any]] | None = None,
        calendar_invitees: list[dict[str, Any]] | None = None,
        transcript_language: str | None = "en",
        meeting_type: str | None = None,
    ) -> None:
        meeting: dict[str, Any] = {
            "recording_id": recording_id,
            "title": title,
            "meeting_title": title,
            "url": playback_url or f"https://fathom.video/calls/{recording_id}",
            "meeting_url": meeting_url,
            "share_url": share_url or f"https://fathom.video/share/{recording_id}",
            "created_at": created_at,
            "scheduled_start_time": scheduled_start_time,
            "scheduled_end_time": scheduled_end_time,
            "recording_start_time": recording_start_time,
            "recording_end_time": recording_end_time,
            "transcript_language": transcript_language,
            "transcript": transcript if transcript is not None else [],
            "calendar_invitees": calendar_invitees or [],
            "recorded_by": (
                {"name": recorded_by_name, "email": recorded_by_email, "team": recorded_by_team}
                if recorded_by_email
                else {}
            ),
        }
        if meeting_type:
            meeting["meeting_type"] = meeting_type
        if default_summary_markdown:
            meeting["default_summary"] = {
                "template_name": "default",
                "markdown_formatted": default_summary_markdown,
            }
        if action_items is not None:
            meeting["action_items"] = action_items

        self.meetings[recording_id] = meeting
        if recording_id not in self.meeting_order:
            self.meeting_order.append(recording_id)

    def update_meeting(self, recording_id: str, **fields: Any) -> None:
        self.meetings[recording_id].update(fields)

    def remove_meeting(self, recording_id: str) -> None:
        self.meetings.pop(recording_id, None)
        if recording_id in self.meeting_order:
            self.meeting_order.remove(recording_id)

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
        return self._handle_list(params, headers or {})

    def _handle_list(self, params: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        self.list_calls.append({"params": dict(params), "headers": dict(headers)})
        failure = self._maybe_fail("list")
        if failure is not None:
            return failure

        cursor = params.get("cursor")
        start = int(cursor) if cursor else 0
        end = start + self.page_size
        page_ids = self.meeting_order[start:end]
        body: dict[str, Any] = {
            "limit": self.page_size,
            "items": [self.meetings[rid] for rid in page_ids],
        }
        if end < len(self.meeting_order):
            body["next_cursor"] = str(end)
        return HttpResponse(status_code=200, headers={}, content=json.dumps(body).encode("utf-8"))
