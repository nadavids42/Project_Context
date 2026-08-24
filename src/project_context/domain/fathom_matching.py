"""Deterministic Fathom meeting -> project assignment rules (Section
11.5; Prompt 13).

Pure functions/dataclasses only — no HTTP, no database, no Fathom API
shapes beyond what `FathomMeetingSummary.from_raw_meeting` reads out of
one already-fetched `GET /meetings` item. `project_context.connectors
.fathom` is the only caller of `evaluate_match`. Modeled directly on
`project_context.domain.calendar_matching` — same "pure tier-evaluation
module, connector applies it" split, same "an explicit rule always beats
an accidental one" philosophy.

Five priority tiers, evaluated in this fixed order (Prompt 13: "exact
manual association, recorded-by/team, client domain, participant/
domain, and bounded time/event association"):

1. `manual` — the recording's own ID appears in the boundary's explicit
   `included_recording_ids` list.
2. `recorded_by` — the meeting's `recorded_by` email appears in the
   boundary's configured `recorded_by_emails` (a team roster tier: "any
   meeting recorded by someone on this client's team").
3. `client_domain` — a calendar invitee or transcript speaker email's
   domain equals the configured `client_domain`.
4. `participant` — a calendar invitee or transcript speaker email
   appears in the explicit `participant_emails` list.
5. `scheduled_event` — the meeting's own conferencing `meeting_url`
   appears in the configured `meeting_urls` list, or its scheduled/
   recorded start time falls inside one of the configured
   `scheduled_windows`.

**Tier 5 is deliberately never auto-assigned.** Unlike tiers 1-4, a
matching meeting URL or a coincidence of timing carries no participant
or domain corroboration — the exact "ambiguity goes to unassigned/
manual review" case Prompt 13 calls out. `evaluate_match` still reports
it as `matched=True, rule_tier=RULE_TIER_SCHEDULED_EVENT` (it *is* a
real, named, auditable rule outcome, not a rejection); it is
`project_context.services.fathom_ingestion.get_or_create_fathom_artifact`
that reads `rule_tier` and stores the artifact as
`ArtifactAvailability.UNASSIGNED` instead of `AVAILABLE` when it fires
alone. See that module's docstring for why the line is drawn there
rather than in this one.

A meeting matching none of the five tiers is not returned by
`FathomConnector.discover()` at all — exactly Calendar's "unmatched
event is simply never yielded" convention, never routed anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

RULE_TIER_MANUAL = "manual"
RULE_TIER_RECORDED_BY = "recorded_by"
RULE_TIER_CLIENT_DOMAIN = "client_domain"
RULE_TIER_PARTICIPANT = "participant"
RULE_TIER_SCHEDULED_EVENT = "scheduled_event"


class InvalidFathomRuleError(ValueError):
    """A configured `scheduled_windows` entry does not parse as a valid
    `start <= end` ISO 8601 range. Raised eagerly, at rule construction
    (mirrors `calendar_matching.InvalidCalendarRuleError` for a bad
    regex) — a malformed window should fail the whole boundary save/
    preview clearly rather than silently matching nothing."""


def _normalize_terms(values: object) -> tuple[str, ...]:
    if not values:
        return ()
    if isinstance(values, str):
        values = [values]
    return tuple(str(v).strip() for v in values if str(v).strip())


def _normalize_emails(values: object) -> tuple[str, ...]:
    if not values:
        return ()
    if isinstance(values, str):
        values = [values]
    return tuple(str(v).strip().lower() for v in values if str(v).strip())


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class ScheduledWindow:
    """One explicit, bounded time range (Prompt 13: "bounded time ...
    association") — e.g. "the recurring Tuesday 10-11am client call
    belongs to this project," entered as one concrete week's window and
    naturally repeated by the user for future weeks, exactly like
    Calendar's boundary is a list of concrete rules rather than a cron
    expression. Both bounds are inclusive UTC ISO 8601 timestamps."""

    start: str
    end: str

    def contains(self, moment: datetime) -> bool:
        start = _parse_iso(self.start)
        end = _parse_iso(self.end)
        if start is None or end is None:
            return False
        return start <= moment <= end


def _validated_window(raw: dict[str, object]) -> ScheduledWindow:
    start_text, end_text = str(raw["start"]), str(raw["end"])
    start, end = _parse_iso(start_text), _parse_iso(end_text)
    if start is None or end is None:
        raise InvalidFathomRuleError(
            f"scheduled_windows entry {{'start': {start_text!r}, 'end': {end_text!r}}} "
            "is not valid ISO 8601"
        )
    if start > end:
        raise InvalidFathomRuleError(
            f"scheduled_windows entry has start {start_text!r} after end {end_text!r}"
        )
    return ScheduledWindow(start=start_text, end=end_text)


@dataclass(frozen=True)
class FathomMatchRules:
    """One project's Fathom boundary — the stored/edited shape of
    `sources.boundary_json` for a `kind='fathom'` source."""

    included_recording_ids: tuple[str, ...] = ()
    recorded_by_emails: tuple[str, ...] = ()
    client_domain: str | None = None
    participant_emails: tuple[str, ...] = ()
    meeting_urls: tuple[str, ...] = ()
    scheduled_windows: tuple[ScheduledWindow, ...] = ()

    @classmethod
    def from_boundary(cls, boundary: dict[str, object] | None) -> FathomMatchRules:
        boundary = boundary or {}
        windows = tuple(
            _validated_window(w)
            for w in (boundary.get("scheduled_windows") or [])
            if w.get("start") and w.get("end")
        )
        return cls(
            included_recording_ids=_normalize_terms(boundary.get("included_recording_ids")),
            recorded_by_emails=_normalize_emails(boundary.get("recorded_by_emails")),
            client_domain=(
                str(boundary["client_domain"]).strip().lower().lstrip("@")
                if boundary.get("client_domain")
                else None
            ),
            participant_emails=_normalize_emails(boundary.get("participant_emails")),
            meeting_urls=_normalize_terms(boundary.get("meeting_urls")),
            scheduled_windows=windows,
        )

    def is_configured(self) -> bool:
        """At least one rule of any tier is set — mirrors Calendar's
        `is_configured` (FR-002: a boundary must be explicit)."""
        return bool(
            self.included_recording_ids
            or self.recorded_by_emails
            or self.client_domain
            or self.participant_emails
            or self.meeting_urls
            or self.scheduled_windows
        )


@dataclass(frozen=True)
class FathomMeetingSummary:
    """The matching-relevant projection of one raw `GET /meetings` item
    — deliberately narrow so `evaluate_match` never depends on the full
    Fathom API response shape."""

    recording_id: str
    title: str
    meeting_url: str | None
    recorded_by_email: str | None
    invitee_emails: tuple[str, ...]
    speaker_emails: tuple[str, ...]
    scheduled_start: str | None
    recording_start: str | None

    @classmethod
    def from_raw_meeting(cls, meeting: dict) -> FathomMeetingSummary:
        recorded_by = meeting.get("recorded_by") or {}
        invitees = tuple(
            i["email"].strip().lower()
            for i in (meeting.get("calendar_invitees") or [])
            if i.get("email")
        )
        speakers = tuple(
            {
                t["speaker"]["matched_calendar_invitee_email"].strip().lower()
                for t in (meeting.get("transcript") or [])
                if (t.get("speaker") or {}).get("matched_calendar_invitee_email")
            }
        )
        return cls(
            recording_id=str(meeting["recording_id"]),
            title=meeting.get("title") or meeting.get("meeting_title") or "",
            meeting_url=meeting.get("meeting_url"),
            recorded_by_email=(recorded_by.get("email") or "").strip().lower() or None,
            invitee_emails=invitees,
            speaker_emails=speakers,
            scheduled_start=meeting.get("scheduled_start_time"),
            recording_start=meeting.get("recording_start_time"),
        )

    @property
    def all_participant_emails(self) -> tuple[str, ...]:
        return self.invitee_emails + self.speaker_emails

    @property
    def best_start_time(self) -> str | None:
        """Prefer the actual recording start over the calendar-scheduled
        one when both exist — the real event, not the invite."""
        return self.recording_start or self.scheduled_start


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    #: One of the `RULE_TIER_*` constants when `matched` is True; `None`
    #: when unmatched.
    rule_tier: str | None
    #: Always populated when informative, including for a preview's
    #: unmatched sample (mirrors `calendar_matching.MatchResult`).
    reason: str | None


def _domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1]


def evaluate_match(meeting: FathomMeetingSummary, rules: FathomMatchRules) -> MatchResult:
    """The single entry point every caller uses. Deterministic and total
    — never raises for a well-formed `FathomMatchRules`."""
    if meeting.recording_id in rules.included_recording_ids:
        return MatchResult(
            matched=True, rule_tier=RULE_TIER_MANUAL,
            reason=f"explicitly included recording ID {meeting.recording_id!r}",
        )

    if meeting.recorded_by_email and meeting.recorded_by_email in rules.recorded_by_emails:
        return MatchResult(
            matched=True, rule_tier=RULE_TIER_RECORDED_BY,
            reason=f"recorded by {meeting.recorded_by_email!r}, a configured team member",
        )

    if rules.client_domain:
        for email in meeting.all_participant_emails:
            if _domain_of(email) == rules.client_domain:
                return MatchResult(
                    matched=True, rule_tier=RULE_TIER_CLIENT_DOMAIN,
                    reason=f"participant {email!r} matches client domain {rules.client_domain!r}",
                )

    if rules.participant_emails:
        for email in meeting.all_participant_emails:
            if email in rules.participant_emails:
                return MatchResult(
                    matched=True, rule_tier=RULE_TIER_PARTICIPANT,
                    reason=f"participant {email!r} is an explicitly configured participant",
                )

    if meeting.meeting_url and meeting.meeting_url in rules.meeting_urls:
        return MatchResult(
            matched=True, rule_tier=RULE_TIER_SCHEDULED_EVENT,
            reason=f"meeting URL {meeting.meeting_url!r} is a configured recurring project link",
        )

    if rules.scheduled_windows:
        moment = _parse_iso(meeting.best_start_time)
        if moment is not None:
            for window in rules.scheduled_windows:
                if window.contains(moment):
                    return MatchResult(
                        matched=True, rule_tier=RULE_TIER_SCHEDULED_EVENT,
                        reason=(
                            f"start time {meeting.best_start_time!r} falls inside configured "
                            f"window {window.start!r}-{window.end!r}"
                        ),
                    )

    return MatchResult(matched=False, rule_tier=None, reason=None)
