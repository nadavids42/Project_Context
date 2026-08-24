"""Read-only Fathom API-key polling connector with deterministic project
matching (Section 11.5; FR-004, FR-030; Prompt 13).

Implements `project_context.connectors.protocol.Connector` against the
private, user-generated-API-key shape of Fathom's REST API: `GET
/meetings` (`https://api.fathom.ai/external/v1/meetings`), authenticated
with `X-Api-Key` (never OAuth — Section 11.5: "Prototype: user-generated
API key in `X-Api-Key`"), cursor-paginated, filtered by a `created_after`
watermark computed once per sync by the caller
(`project_context.services.sync.sync_fathom_project`) exactly like
Gmail's `since_date`. Every call is `GET` — never a write (FR-004) —
and this connector never calls `/recordings`, downloads recording
media, registers a webhook, or performs OAuth (Prompt 13: explicitly
excluded).

**Response shape**, as an assumption encoded directly in this module
and in `tests/fixtures/fake_fathom_api.py`: fetched from Fathom's own
published API reference (`developers.fathom.ai/api-reference/meetings/
list-meetings`) on 2026-08-23, not guessed. Key fields used here:
`recording_id` (an integer on the wire — always treated as `str()` for
`ArtifactMetadata.external_id`, matching every other connector's string
external ID), `title`/`meeting_title`, `url` (playback), `share_url`,
`meeting_url` (the conferencing join link), `created_at`,
`scheduled_start_time`/`scheduled_end_time`,
`recording_start_time`/`recording_end_time`, `recorded_by` (`{name,
email, email_domain, team}`), `calendar_invitees` (`[{name, email,
email_domain, is_external, ...}]`), `transcript` (`[{speaker:
{display_name, matched_calendar_invitee_email}, text, timestamp}]`),
`default_summary` (`{template_name, markdown_formatted}`), and
`action_items` (`[{description, completed, recording_timestamp,
assignee: {name, email, team}, ...}]`). The list response wraps items
in `{"limit": ..., "next_cursor": ..., "items": [...]}`; a `None`/
absent/empty `next_cursor` means no further page — the same "opaque
checkpoint token" convention as Gmail's `nextPageToken` and Calendar's
page token. If Fathom's actual production response ever diverges from
this (undocumented fields, a different pagination sentinel), this
module and its fake need updating together — nothing here has been
exercised against a live account (Section 15: "connector tests must not
require live credentials").

**Two calls per meeting, never more.** `discover()` requests
`include_transcript=true&include_summary=true&include_action_items=true`
directly on `GET /meetings` — Fathom's documented "heavy request"
shape — so `fetch()` makes **zero** further HTTP calls, exactly like
`CalendarConnector.fetch()`: the full meeting object is already
embedded in `ArtifactMetadata.extra["meeting"]` by `discover()`.
`validate_config()`/`preview()` deliberately omit every `include_*`
flag — a light, non-heavy call — so a boundary-rule preview does not
compete with `discover()`'s heavy-request budget (Section 11.5: "Keep
concurrency low"; Prompt 13: "conservative sequential/bounded
requests").

**No project-side API filtering, by design.** Fathom's `/meetings`
supports server-side `recorded_by[]`/`calendar_invitees_domains[]`
filters, but this connector never sends them — matching is always our
own deterministic `project_context.domain.fathom_matching.evaluate_match`
applied client-side to the same global, unscoped meeting feed, exactly
Calendar's "global feed, local filter" precedent
(`project_context.connectors.calendar`'s module docstring). This keeps
one project's match tiers fully auditable and independent of trusting
a second, less precise server-side filter semantics to agree with them.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from project_context.connectors.errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
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
from project_context.domain.evidence import ArtifactType, EvidenceSourceType
from project_context.domain.fathom_matching import (
    FathomMatchRules,
    FathomMeetingSummary,
    InvalidFathomRuleError,
    MatchResult,
    evaluate_match,
)

FATHOM_API_BASE = "https://api.fathom.ai/external/v1"
FATHOM_MEETINGS_URL = f"{FATHOM_API_BASE}/meetings"

_DEFAULT_PAGE_LIMIT = 20


def _version_marker(recording_id: str, meeting: dict[str, Any]) -> str:
    """`recording_id` plus a short hash of every field whose change must
    trigger reprocessing (Prompt 13: "Keep meeting/transcript data
    immutable by version/hash and reprocess only changed versions").
    Hashing `transcript`/`default_summary`/`action_items` — not the
    whole meeting object — means metadata-only fields Fathom might churn
    (e.g. `crm_matches`) never spuriously invalidate an already-stored,
    already-reviewed version; only a genuine transcript/summary/action-
    item change (post-processing catching up, a later edit) does, which
    is exactly what a later overlap rescan must be able to detect
    without any webhook (Section 11.5: "Periodically rescan recent
    meetings because webhooks do not fire on later transcript/summary
    edits")."""
    fingerprint = {
        "transcript": meeting.get("transcript"),
        "default_summary": meeting.get("default_summary"),
        "action_items": meeting.get("action_items"),
    }
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"fathom:{recording_id}:{digest}"


@dataclass(frozen=True)
class FathomUnmatchedSample:
    """One unmatched meeting, as shown in a rule preview — title only,
    never transcript/summary content (mirrors
    `calendar.CalendarUnmatchedSample`)."""

    title: str
    reason: str | None


@dataclass(frozen=True)
class FathomPreview:
    matched: list[ArtifactMetadata]
    unmatched_sample: list[FathomUnmatchedSample]


class FathomConnector:
    """One project's Fathom rule boundary (Section 11.5) plus a
    `created_after` watermark, computed once by the caller from this
    source's own last successful sync minus a 48-hour overlap (Prompt
    13: "Poll using a last-created watermark plus a 48-hour overlap") —
    this connector itself does not compute the overlap, exactly like
    `GmailConnector` receives an already-computed `since_date` rather
    than a raw last-success timestamp."""

    def __init__(
        self,
        *,
        api_key: str,
        rules: FathomMatchRules,
        created_after: str | None = None,
        http_transport: HttpTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if not api_key:
            raise ConnectorConfigError("a Fathom API key is required")
        self._api_key = api_key
        self._rules = rules
        self._created_after = created_after
        self._transport = http_transport or RequestsHttpTransport()
        self._sleep = sleep
        self._rand = rand

    # --- Connector protocol -------------------------------------------

    def validate_config(self) -> ConnectorHealth:
        try:
            self._list_meetings_page(cursor=None, heavy=False)
        except ConnectorAuthError as exc:
            return ConnectorHealth(status=ConnectorHealthStatus.AUTH_ERROR, detail=exc.safe_message)
        except ConnectorPermissionError as exc:
            return ConnectorHealth(
                status=ConnectorHealthStatus.PERMISSION_ERROR, detail=exc.safe_message
            )
        except ConnectorConfigError as exc:
            return ConnectorHealth(
                status=ConnectorHealthStatus.CONFIG_ERROR, detail=exc.safe_message
            )
        except ConnectorError as exc:
            return ConnectorHealth(status=ConnectorHealthStatus.ERROR, detail=exc.safe_message)
        return ConnectorHealth(status=ConnectorHealthStatus.OK)

    def preview(self, boundary: dict[str, Any], limit: int = 20) -> list[ArtifactMetadata]:
        """A dry run over `boundary` (which may not yet be saved) rather
        than this connector's own configured rules (FR-002). Matched
        meetings only — see `preview_detailed` for the richer matched-
        and-unmatched view the UI's rule preview actually uses."""
        return self.preview_detailed(boundary, limit=limit).matched

    def preview_detailed(self, boundary: dict[str, Any], *, limit: int = 20) -> FathomPreview:
        try:
            rules = FathomMatchRules.from_boundary(boundary)
        except InvalidFathomRuleError as exc:
            raise ConnectorConfigError(str(exc)) from exc

        matched: list[ArtifactMetadata] = []
        unmatched: list[FathomUnmatchedSample] = []
        cursor: str | None = None
        pages = 0
        while len(matched) < limit and len(unmatched) < limit and pages < 50:
            pages += 1
            page = self._list_meetings_page(cursor=cursor, heavy=False)
            for meeting in page.get("items", []):
                if len(matched) >= limit and len(unmatched) >= limit:
                    break
                summary = FathomMeetingSummary.from_raw_meeting(meeting)
                result = evaluate_match(summary, rules)
                if result.matched:
                    if len(matched) < limit:
                        matched.append(self._build_metadata(meeting, result))
                elif len(unmatched) < limit:
                    unmatched.append(
                        FathomUnmatchedSample(
                            title=summary.title or "(untitled meeting)", reason=result.reason
                        )
                    )
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return FathomPreview(matched=matched, unmatched_sample=unmatched)

    def discover(self, checkpoint: dict[str, Any] | None) -> DiscoveryPage:
        cursor = checkpoint.get("cursor") if checkpoint else None
        page = self._list_meetings_page(cursor=cursor, heavy=True)
        matched: list[ArtifactMetadata] = []
        for meeting in page.get("items", []):
            summary = FathomMeetingSummary.from_raw_meeting(meeting)
            result = evaluate_match(summary, self._rules)
            if result.matched:
                matched.append(self._build_metadata(meeting, result))
        next_cursor = page.get("next_cursor")
        next_checkpoint = {"cursor": next_cursor} if next_cursor else None
        return DiscoveryPage(artifacts=tuple(matched), next_checkpoint=next_checkpoint)

    def fetch(self, artifact: ArtifactMetadata) -> RawArtifact:
        """Zero HTTP calls — see the module docstring. The full raw
        meeting was already embedded in `discover()`'s returned
        metadata. Text synthesis (metadata header, transcript turns,
        summary/action items) lives in
        `project_context.services.fathom_ingestion` — same division as
        Calendar's `_build_normalized_event_text`, kept there instead of
        here only because the ingestion module also needs to know the
        transcript's exact character range to chunk just that section
        (mirrors `calendar_ingestion.store_calendar_artifact`)."""
        from project_context.services.fathom_ingestion import build_normalized_meeting_text

        meeting = artifact.extra["meeting"]
        text, _transcript_start, _transcript_end = build_normalized_meeting_text(meeting, artifact)
        return RawArtifact(
            metadata=artifact,
            data=text.encode("utf-8"),
            mime_type="text/plain",
            filename=f"fathom-{artifact.external_id}.txt",
        )

    # --- internals ---------------------------------------------------

    def _build_metadata(self, meeting: dict[str, Any], result: MatchResult) -> ArtifactMetadata:
        recording_id = str(meeting["recording_id"])
        recorded_by = meeting.get("recorded_by") or {}
        occurred_at = meeting.get("recording_start_time") or meeting.get("scheduled_start_time")
        external_url = meeting.get("share_url") or meeting.get("url")
        return ArtifactMetadata(
            external_id=recording_id,
            title=meeting.get("title") or meeting.get("meeting_title") or "(untitled meeting)",
            artifact_type=ArtifactType.MEETING,
            source_type=EvidenceSourceType.CALL_RECORDING,
            mime_type="text/plain",
            version_marker=_version_marker(recording_id, meeting),
            author=recorded_by.get("email"),
            occurred_at=occurred_at,
            external_url=external_url,
            extra={
                "meeting": meeting,
                "match_rule": result.rule_tier,
                "match_reason": result.reason,
            },
        )

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key}

    def _list_meetings_page(self, *, cursor: str | None, heavy: bool) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if self._created_after:
            params["created_after"] = self._created_after
        if heavy:
            params["include_transcript"] = "true"
            params["include_summary"] = "true"
            params["include_action_items"] = "true"
        response = request_with_retry(
            self._transport, "GET", FATHOM_MEETINGS_URL, params=params, headers=self._headers(),
            sleep=self._sleep, rand=self._rand,
        )
        return response.json()
