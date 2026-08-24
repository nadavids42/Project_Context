"""Read-only Google Calendar connector with deterministic project
matching (Section 11.4; FR-004, FR-029; Prompt 12).

Implements `project_context.connectors.protocol.Connector`:
`events.list` over one bounded time window (default 180 days back, 90
forward — Section 11.4), `singleEvents=true` (Google flattens every
recurring series into individually-IDed instances for us — see the
module docstring's "recurring events" note below) and `showDeleted=true`
(so cancelled instances inside the window are returned rather than
silently vanishing, per Google's own documented behavior — see
`fetch`'s docstring). Every call is `GET` — never a write (FR-004).

**Where rule matching happens.** Google's Calendar API has no way to
filter server-side by "contains this project's name" or "attendee is
from this client domain" — those are this application's own rules
(`project_context.domain.calendar_matching`), not Calendar search
syntax. So, unlike Gmail's `q=` (matched server-side) or Drive's folder
`q=` (also server-side), Calendar matching happens client-side, in
Python, against the full bounded-window event list: `discover()`/
`preview()` fetch every event in the window and yield only the ones
`evaluate_match` matches — everything else (including an event that
matches an include tier but is also excluded) is simply never returned,
exactly like an out-of-boundary Drive file or a non-matching Gmail
message is never returned. This keeps ambiguous/excluded events out of
this project by construction, never routed to an LLM for a tiebreak
(Prompt 12).

**Recurring events, documented simplification.** `singleEvents=true`
asks Google to expand every recurring series into individually-IDed
instances directly in `events.list` — each instance already carries a
stable Google-assigned `id` and a `recurringEventId` pointing back to
its series. This connector treats every instance as its own artifact
(consistent identity via Google's own IDs) and records `recurringEventId`
in `extra` for context; it does not separately track or reconcile the
recurring *master* event as a distinct artifact, since `singleEvents=true`
never returns the bare master by itself.

**No second API call to fetch a changed artifact's body.** Unlike
Gmail (`messages.list` returns IDs only, requiring a follow-up
`messages.get`), Calendar's `events.list` already returns each event's
complete resource — title, description, attendees, everything.
`discover()` therefore embeds the full raw event dict in
`ArtifactMetadata.extra["event"]`, and `fetch()` is a pure local
transform of that embedded dict with **zero** additional HTTP calls.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from project_context.domain.calendar_matching import (
    CalendarEventSummary,
    CalendarMatchRules,
    InvalidCalendarRuleError,
    MatchResult,
    evaluate_match,
)
from project_context.domain.evidence import ArtifactAvailability, ArtifactType, EvidenceSourceType

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
#: The authenticated user's own calendar — Section 11.4 scopes this to
#: one account, same single-account model as Drive/Gmail; a secondary/
#: shared calendar is out of scope for this prompt.
_PRIMARY_CALENDAR_ID = "primary"
CALENDAR_EVENTS_URL = f"{CALENDAR_API_BASE}/calendars/{_PRIMARY_CALENDAR_ID}/events"

_DEFAULT_PAGE_SIZE = 100
_GOOGLE_MAX_PAGE_SIZE = 2500

#: Section 11.4: "Default scan window: 180 days back and 90 days
#: forward, configurable with validated bounds." The bounds themselves
#: are this connector's own choice (the plan specifies the default,
#: not the bounds) — generous enough for any real project's history/
#: lookahead, small enough that a mistyped huge value fails fast rather
#: than scanning years of an unrelated calendar.
DEFAULT_DAYS_BACK = 180
DEFAULT_DAYS_FORWARD = 90
MIN_DAYS_BACK = 1
MAX_DAYS_BACK = 730
MIN_DAYS_FORWARD = 1
MAX_DAYS_FORWARD = 365

_CANCELLED_STATUS = "cancelled"

#: The single-event field list, reused both bare (for `events.get`) and
#: wrapped in `items(...)` (for `events.list`) — kept as one constant
#: so the two calls can never drift out of sync with each other.
_EVENT_FIELDS = (
    "id, status, summary, description, organizer, attendees, "
    "start, end, updated, htmlLink, hangoutLink, conferenceData, recurringEventId, "
    "originalStartTime, visibility, location"
)
_LIST_FIELDS = f"nextPageToken, items({_EVENT_FIELDS})"


def validate_scan_window(days_back: int, days_forward: int) -> None:
    """Raises `ConnectorConfigError` for an out-of-bounds scan window —
    checked once, at connector construction, never per-call."""
    if not (MIN_DAYS_BACK <= days_back <= MAX_DAYS_BACK):
        raise ConnectorConfigError(
            f"scan_days_back must be between {MIN_DAYS_BACK} and {MAX_DAYS_BACK}"
        )
    if not (MIN_DAYS_FORWARD <= days_forward <= MAX_DAYS_FORWARD):
        raise ConnectorConfigError(
            f"scan_days_forward must be between {MIN_DAYS_FORWARD} and {MAX_DAYS_FORWARD}"
        )


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _extract_meeting_url(event: dict[str, Any]) -> str | None:
    hangout = event.get("hangoutLink")
    if hangout:
        return hangout
    for entry_point in (event.get("conferenceData") or {}).get("entryPoints", []):
        if entry_point.get("entryPointType") == "video" and entry_point.get("uri"):
            return entry_point["uri"]
    return None


def _extract_occurred_at(event: dict[str, Any]) -> str | None:
    """`start.dateTime` for a timed event, `start.date` (midnight UTC)
    for an all-day event, `None` only if Google ever omits both
    (unexpected, but never crash the connector over it)."""
    start = event.get("start") or {}
    if start.get("dateTime"):
        return start["dateTime"]
    if start.get("date"):
        return f"{start['date']}T00:00:00Z"
    return None


@dataclass(frozen=True)
class CalendarUnmatchedSample:
    """One unmatched/excluded event, as shown in a rule preview —
    title and reason only, never the description or attendee list
    (Prompt 12: "without displaying private event details beyond the
    selected project UI")."""

    title: str
    reason: str | None


@dataclass(frozen=True)
class CalendarPreview:
    matched: list[ArtifactMetadata]
    unmatched_sample: list[CalendarUnmatchedSample]


class CalendarConnector:
    """One project's Calendar rule boundary (Section 11.4) plus a fixed
    scan window, computed once at construction from `now_fn()` so every
    `discover()` page within one sync uses the same `timeMin`/`timeMax`
    regardless of how long the sync takes or whether it resumes from a
    checkpoint (Prompt 12: "Re-scan the bounded window on manual sync
    ... Do not implement sync tokens in this version")."""

    def __init__(
        self,
        *,
        access_token: str,
        rules: CalendarMatchRules,
        days_back: int = DEFAULT_DAYS_BACK,
        days_forward: int = DEFAULT_DAYS_FORWARD,
        http_transport: HttpTransport | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        validate_scan_window(days_back, days_forward)
        self._access_token = access_token
        self._rules = rules
        now = now_fn()
        self._time_min = _rfc3339(now - timedelta(days=days_back))
        self._time_max = _rfc3339(now + timedelta(days=days_forward))
        self._transport = http_transport or RequestsHttpTransport()
        self._page_size = max(1, min(page_size, _GOOGLE_MAX_PAGE_SIZE))
        self._sleep = sleep
        self._rand = rand

    # --- Connector protocol -------------------------------------------

    def validate_config(self) -> ConnectorHealth:
        try:
            self._list_events_page(
                time_min=self._time_min, time_max=self._time_max,
                page_token=None, max_results=1,
            )
        except ConnectorAuthError as exc:
            return ConnectorHealth(status=ConnectorHealthStatus.AUTH_ERROR, detail=exc.safe_message)
        except ConnectorPermissionError as exc:
            return ConnectorHealth(
                status=ConnectorHealthStatus.PERMISSION_ERROR, detail=exc.safe_message
            )
        except ConnectorError as exc:
            return ConnectorHealth(status=ConnectorHealthStatus.ERROR, detail=exc.safe_message)
        return ConnectorHealth(status=ConnectorHealthStatus.OK)

    def preview(self, boundary: dict[str, Any], limit: int = 20) -> list[ArtifactMetadata]:
        """A dry run over `boundary` (which may not yet be saved) rather
        than this connector's own configured rules (FR-002). Matched
        events only — see `preview_detailed` for the richer matched-
        and-unmatched view the UI's rule preview actually uses."""
        detailed = self.preview_detailed(boundary, limit=limit)
        return detailed.matched

    def discover(self, checkpoint: dict[str, Any] | None) -> DiscoveryPage:
        page_token = checkpoint.get("page_token") if checkpoint else None
        page = self._list_events_page(
            time_min=self._time_min, time_max=self._time_max,
            page_token=page_token, max_results=self._page_size,
        )
        matched: list[ArtifactMetadata] = []
        for event in page.get("items", []):
            metadata = self._to_matched_metadata(event, self._rules)
            if metadata is not None:
                matched.append(metadata)
        next_token = page.get("nextPageToken")
        next_checkpoint = {"page_token": next_token} if next_token else None
        return DiscoveryPage(artifacts=tuple(matched), next_checkpoint=next_checkpoint)

    def fetch(self, artifact: ArtifactMetadata) -> RawArtifact:
        """Zero HTTP calls — see the module docstring. The full raw
        event was already embedded in `discover()`'s returned metadata."""
        event = artifact.extra["event"]
        text = _build_normalized_event_text(event, artifact)
        return RawArtifact(
            metadata=artifact,
            data=text.encode("utf-8"),
            mime_type="text/plain",
            filename=f"calendar-{artifact.external_id}.txt",
        )

    # --- Calendar-specific extra: richer preview (not part of the
    # generic Connector protocol) --------------------------------------

    def preview_detailed(self, boundary: dict[str, Any], *, limit: int = 20) -> CalendarPreview:
        """Matched *and* a bounded sample of unmatched/excluded events
        with their reasons (Prompt 12: "Preview must show recent
        matched/unmatched sample events ... and stored match reasons").
        Never touches this connector's own configured `self._rules` —
        always the passed-in `boundary`, exactly like `preview()`."""
        try:
            rules = CalendarMatchRules.from_boundary(boundary)
        except InvalidCalendarRuleError as exc:
            raise ConnectorConfigError(str(exc)) from exc

        matched: list[ArtifactMetadata] = []
        unmatched: list[CalendarUnmatchedSample] = []
        page_token: str | None = None
        pages = 0
        while len(matched) < limit and len(unmatched) < limit and pages < 50:
            pages += 1
            page = self._list_events_page(
                time_min=self._time_min, time_max=self._time_max,
                page_token=page_token, max_results=self._page_size,
            )
            for event in page.get("items", []):
                if len(matched) >= limit and len(unmatched) >= limit:
                    break
                summary = CalendarEventSummary.from_raw_event(event)
                result = evaluate_match(summary, rules)
                if result.matched:
                    if len(matched) < limit:
                        metadata = self._build_metadata(event, result)
                        matched.append(metadata)
                elif len(unmatched) < limit:
                    unmatched.append(
                        CalendarUnmatchedSample(
                            title=summary.title or "(untitled event)", reason=result.reason
                        )
                    )
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return CalendarPreview(matched=matched, unmatched_sample=unmatched)

    def check_availability(self, external_id: str) -> ArtifactAvailability:
        """Distinguish cancelled/deleted, access-revoked, and rule-
        excluded from still-genuinely-available for one previously-
        known `external_id` no longer seen in the current bounded scan
        (FR-029: "canceled/deleted events update evidence availability
        without erasing imported evidence"). Unlike Drive's equivalent,
        this is the *only* place this connector makes a second,
        single-event API call — targeted, and only for items already
        known to have dropped out of the listing."""
        try:
            event = self._get_single_event(external_id)
        except ConnectorNotFoundError:
            return ArtifactAvailability.DELETED_EXTERNAL
        except ConnectorPermissionError:
            return ArtifactAvailability.INACCESSIBLE
        if event.get("status") == _CANCELLED_STATUS:
            return ArtifactAvailability.DELETED_EXTERNAL
        # Still exists, still confirmed/tentative — most likely no
        # longer matches this project's rules (edited title, removed
        # attendee, a rule change). Rule boundary is authoritative
        # (Section 11.4), so treat "no longer matches" the same as
        # "gone from this source" — the same convention
        # `DriveConnector.check_availability` already establishes for
        # "moved outside the configured folder."
        summary = CalendarEventSummary.from_raw_event(event)
        if evaluate_match(summary, self._rules).matched:
            return ArtifactAvailability.AVAILABLE
        return ArtifactAvailability.DELETED_EXTERNAL

    # --- internals ---------------------------------------------------

    def _to_matched_metadata(
        self, event: dict[str, Any], rules: CalendarMatchRules
    ) -> ArtifactMetadata | None:
        summary = CalendarEventSummary.from_raw_event(event)
        result = evaluate_match(summary, rules)
        if not result.matched:
            return None
        return self._build_metadata(event, result)

    def _build_metadata(self, event: dict[str, Any], result: MatchResult) -> ArtifactMetadata:
        status = event.get("status", "confirmed")
        return ArtifactMetadata(
            external_id=event["id"],
            title=event.get("summary") or "(untitled event)",
            artifact_type=ArtifactType.CALENDAR_EVENT,
            source_type=EvidenceSourceType.OTHER,
            mime_type="text/plain",
            version_marker=event.get("updated") or event["id"],
            author=(event.get("organizer") or {}).get("email"),
            occurred_at=_extract_occurred_at(event),
            external_url=event.get("htmlLink"),
            is_trashed=(status == _CANCELLED_STATUS),
            extra={
                "event": event,
                "match_rule": result.rule_tier,
                "match_reason": result.reason,
            },
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _list_events_page(
        self, *, time_min: str, time_max: str, page_token: str | None, max_results: int
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "showDeleted": "true",
            "orderBy": "startTime",
            "maxResults": max(1, min(max_results, _GOOGLE_MAX_PAGE_SIZE)),
            "fields": _LIST_FIELDS,
        }
        if page_token:
            params["pageToken"] = page_token
        response = request_with_retry(
            self._transport, "GET", CALENDAR_EVENTS_URL, params=params, headers=self._headers(),
            sleep=self._sleep, rand=self._rand,
        )
        return response.json()

    def _get_single_event(self, event_id: str) -> dict[str, Any]:
        response = request_with_retry(
            self._transport, "GET", f"{CALENDAR_EVENTS_URL}/{event_id}",
            params={"fields": _EVENT_FIELDS},
            headers=self._headers(), sleep=self._sleep, rand=self._rand,
        )
        return response.json()


def _build_normalized_event_text(event: dict[str, Any], artifact: ArtifactMetadata) -> str:
    """Header/metadata block (never sent to extraction — see
    `project_context.services.calendar_ingestion`) plus, if present,
    the event's own `description` verbatim. Section 11.4/Prompt 12:
    "Only explicit description text may enter extraction as text
    evidence" — metadata alone (title, attendees, timing) is evidence
    of the meeting's *existence*, never phrased as a sentence a
    downstream reader could mistake for an asserted fact."""
    start = event.get("start") or {}
    end = event.get("end") or {}
    attendees = ", ".join(
        a["email"] for a in event.get("attendees", []) if a.get("email")
    ) or "(none listed)"
    lines = [
        f"Title: {artifact.title}",
        f"Status: {event.get('status', 'confirmed')}",
        f"Organizer: {(event.get('organizer') or {}).get('email', '(unknown)')}",
        f"Attendees: {attendees}",
        f"Start: {start.get('dateTime') or start.get('date') or '(unknown)'}",
        f"End: {end.get('dateTime') or end.get('date') or '(unknown)'}",
    ]
    time_zone = start.get("timeZone")
    if time_zone:
        lines.append(f"Time zone: {time_zone}")
    meeting_url = _extract_meeting_url(event)
    if meeting_url:
        lines.append(f"Meeting URL: {meeting_url}")
    if event.get("recurringEventId"):
        lines.append(f"Recurring series ID: {event['recurringEventId']}")
    if event.get("visibility") == "private":
        lines.append("Visibility: private")
    match_reason = artifact.extra.get("match_reason")
    if match_reason:
        lines.append(f"Match reason: {match_reason}")
    header_block = "\n".join(lines)

    description = (event.get("description") or "").strip()
    if not description:
        return header_block
    return f"{header_block}\n\n{description}"
