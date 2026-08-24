"""An in-memory fake of the tiny slice of the Google Calendar v3 REST
API `project_context.connectors.calendar.CalendarConnector` calls —
`events.list` (paginated, `timeMin`/`timeMax`/`singleEvents`/
`showDeleted`) and `events.get` (used only by `check_availability`, for
one previously-known event no longer in the current listing).
Implements `project_context.connectors.http.HttpTransport`, so it
plugs directly into `CalendarConnector` in place of
`RequestsHttpTransport`; nothing in any test using this fixture ever
touches the network (Section 15).

Time-window filtering (`timeMin`/`timeMax`) is *not* re-implemented
here — like `FakeDriveApi` not re-implementing Drive's `q=` folder
semantics and `FakeGmailApi` not re-implementing Gmail's search syntax,
this fake returns every registered event on `events.list` regardless
of the requested window; scan-window construction itself is covered
directly against `CalendarConnector`'s own `_time_min`/`_time_max`
computation, not by fake filtering.
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
class FakeCalendarApi:
    """Build a small fake calendar with `add_event`, then pass this
    object as `http_transport=` to `CalendarConnector`."""

    #: event_id -> full Calendar API event resource.
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Ids returned, in this order, by `events.list` (paginated).
    event_order: list[str] = field(default_factory=list)
    page_size: int = 100
    #: key -> failure plan. `"list"` for `events.list`; `"get:<id>"`
    #: for `events.get`.
    failures: dict[str, _FailurePlan] = field(default_factory=dict)

    list_calls: list[dict[str, Any]] = field(default_factory=list)
    get_calls: list[str] = field(default_factory=list)

    # --- fixture-building helpers -------------------------------------

    def add_event(
        self,
        event_id: str,
        *,
        status: str = "confirmed",
        summary: str = "Untitled event",
        description: str | None = None,
        organizer_email: str = "me@example.com",
        attendee_emails: list[str] | None = None,
        start: str = "2026-06-01T09:00:00-04:00",
        end: str = "2026-06-01T10:00:00-04:00",
        time_zone: str | None = "America/New_York",
        all_day: bool = False,
        updated: str = "2026-05-01T12:00:00.000Z",
        html_link: str | None = None,
        hangout_link: str | None = None,
        recurring_event_id: str | None = None,
        visibility: str | None = None,
    ) -> None:
        start_field = {"date": start} if all_day else {"dateTime": start}
        end_field = {"date": end} if all_day else {"dateTime": end}
        if time_zone and not all_day:
            start_field["timeZone"] = time_zone
            end_field["timeZone"] = time_zone

        event: dict[str, Any] = {
            "id": event_id,
            "status": status,
            "summary": summary,
            "organizer": {"email": organizer_email},
            "attendees": [{"email": e} for e in (attendee_emails or [])],
            "start": start_field,
            "end": end_field,
            "updated": updated,
        }
        if description is not None:
            event["description"] = description
        if html_link:
            event["htmlLink"] = html_link
        if hangout_link:
            event["hangoutLink"] = hangout_link
        if recurring_event_id:
            event["recurringEventId"] = recurring_event_id
        if visibility:
            event["visibility"] = visibility

        self.events[event_id] = event
        if event_id not in self.event_order:
            self.event_order.append(event_id)

    def update_event(self, event_id: str, **fields: Any) -> None:
        self.events[event_id].update(fields)

    def remove_event(self, event_id: str) -> None:
        """Simulate an event vanishing (hard-deleted, not merely
        cancelled) between scans — `events.get` now 404s."""
        self.events.pop(event_id, None)

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
        if url.endswith("/events"):
            return self._handle_list(params)
        event_id = url.rsplit("/", 1)[-1]
        return self._handle_get(event_id)

    def _handle_list(self, params: dict[str, Any]) -> HttpResponse:
        self.list_calls.append(dict(params))
        failure = self._maybe_fail("list")
        if failure is not None:
            return failure

        start = int(params.get("pageToken") or 0)
        max_results = int(params.get("maxResults", self.page_size))
        page_size = min(self.page_size, max_results)
        end = start + page_size
        page_ids = self.event_order[start:end]
        body: dict[str, Any] = {"items": [self.events[eid] for eid in page_ids]}
        if end < len(self.event_order):
            body["nextPageToken"] = str(end)
        return HttpResponse(status_code=200, headers={}, content=json.dumps(body).encode("utf-8"))

    def _handle_get(self, event_id: str) -> HttpResponse:
        self.get_calls.append(event_id)
        failure = self._maybe_fail(f"get:{event_id}")
        if failure is not None:
            return failure
        event = self.events.get(event_id)
        if event is None:
            return HttpResponse(status_code=404, headers={}, content=b'{"error": "not found"}')
        return HttpResponse(status_code=200, headers={}, content=json.dumps(event).encode("utf-8"))
