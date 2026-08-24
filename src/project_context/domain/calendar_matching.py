"""Deterministic Calendar event → project assignment rules (Section
11.4; Prompt 12).

Pure functions/dataclasses only — no HTTP, no database, no Google API
shapes beyond what `CalendarEventSummary.from_raw_event` reads out of
one already-fetched event dict. `project_context.connectors.calendar`
is the only caller of `evaluate_match`.

Four priority tiers, evaluated in this fixed order (Section 11.4:
"Weighted deterministic rule: explicit event include ID > project-name
term > client domain + participant > configured regex"):

1. `event_id` — the event's own ID appears in the boundary's explicit
   `included_event_ids` list.
2. `project_name_term` — the title or description contains one of the
   boundary's configured `project_name_terms`.
3. `domain_participant` — an attendee/organizer email's domain equals
   the configured `client_domain`, or an attendee/organizer email
   appears in the explicit `participant_emails` list.
4. `include_rule` — the title or description matches a configured
   `include_terms` entry or the `include_regex` pattern.

An explicit exclude (`exclude_terms`/`exclude_regex`) always overrides
every tier above, deterministically and without exception — the same
"a corrected-field disagreement forces review regardless of score"
safety principle Section 10.4 already applies to reconciliation. This
is a deliberate, documented interpretation of Section 11.4's single
combined "include/exclude" tier: an event that would otherwise match
is never silently included just because a higher tier also fired.

Ambiguity (an event that matches an include tier *and* an exclude
rule) is resolved by exclusion, never by escalating to an LLM
(Prompt 12: "Ambiguous matches must remain unassigned/manual; they do
not go to an LLM for project selection") — the exact conflict is still
named in `MatchResult.reason` so it is visible in a rule preview.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Section 11.4 / Prompt 12 tier names, in priority order — also the
#: exact values migrations/0010's `match_rule` CHECK constraint allows.
RULE_TIER_EVENT_ID = "event_id"
RULE_TIER_PROJECT_NAME_TERM = "project_name_term"
RULE_TIER_DOMAIN_PARTICIPANT = "domain_participant"
RULE_TIER_INCLUDE_RULE = "include_rule"


class InvalidCalendarRuleError(ValueError):
    """An `include_regex`/`exclude_regex` pattern does not compile.
    Raised eagerly, at rule construction, never per-event — a bad
    pattern should fail the whole sync/preview clearly rather than
    silently matching zero (or every) event."""


def _normalize_terms(terms: object) -> tuple[str, ...]:
    if not terms:
        return ()
    if isinstance(terms, str):
        terms = [terms]
    return tuple(str(term).strip() for term in terms if str(term).strip())


def _normalize_emails(emails: object) -> tuple[str, ...]:
    if not emails:
        return ()
    if isinstance(emails, str):
        emails = [emails]
    return tuple(str(email).strip().lower() for email in emails if str(email).strip())


@dataclass(frozen=True)
class CalendarMatchRules:
    """One project's Calendar boundary — the stored/edited shape of
    `sources.boundary_json` for a `kind='calendar'` source."""

    included_event_ids: tuple[str, ...] = ()
    project_name_terms: tuple[str, ...] = ()
    client_domain: str | None = None
    participant_emails: tuple[str, ...] = ()
    include_terms: tuple[str, ...] = ()
    include_regex: str | None = None
    exclude_terms: tuple[str, ...] = ()
    exclude_regex: str | None = None
    #: Section 11.4: "Default scan window: 180 days back and 90 days
    #: forward, configurable with validated bounds" — bounds enforced
    #: by `project_context.connectors.calendar.validate_scan_window`,
    #: not here (this dataclass only carries the configured values).
    scan_days_back: int = 180
    scan_days_forward: int = 90

    _include_pattern: re.Pattern[str] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _exclude_pattern: re.Pattern[str] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.include_regex:
            try:
                object.__setattr__(
                    self, "_include_pattern", re.compile(self.include_regex, re.IGNORECASE)
                )
            except re.error as exc:
                raise InvalidCalendarRuleError(
                    f"include_regex is not a valid regular expression: {exc}"
                ) from exc
        if self.exclude_regex:
            try:
                object.__setattr__(
                    self, "_exclude_pattern", re.compile(self.exclude_regex, re.IGNORECASE)
                )
            except re.error as exc:
                raise InvalidCalendarRuleError(
                    f"exclude_regex is not a valid regular expression: {exc}"
                ) from exc

    @classmethod
    def from_boundary(cls, boundary: dict[str, object] | None) -> CalendarMatchRules:
        boundary = boundary or {}
        kwargs: dict[str, object] = {
            "included_event_ids": _normalize_terms(boundary.get("included_event_ids")),
            "project_name_terms": _normalize_terms(boundary.get("project_name_terms")),
            "client_domain": (
                str(boundary["client_domain"]).strip().lower().lstrip("@")
                if boundary.get("client_domain")
                else None
            ),
            "participant_emails": _normalize_emails(boundary.get("participant_emails")),
            "include_terms": _normalize_terms(boundary.get("include_terms")),
            "include_regex": (str(boundary["include_regex"]).strip() or None)
            if boundary.get("include_regex")
            else None,
            "exclude_terms": _normalize_terms(boundary.get("exclude_terms")),
            "exclude_regex": (str(boundary["exclude_regex"]).strip() or None)
            if boundary.get("exclude_regex")
            else None,
        }
        if boundary.get("scan_days_back"):
            kwargs["scan_days_back"] = int(boundary["scan_days_back"])
        if boundary.get("scan_days_forward"):
            kwargs["scan_days_forward"] = int(boundary["scan_days_forward"])
        return cls(**kwargs)

    def is_configured(self) -> bool:
        """At least one rule of any tier is set — mirrors Drive's
        non-empty-folder-id / Gmail's label-or-query "boundary actually
        configured" check (FR-002)."""
        return bool(
            self.included_event_ids
            or self.project_name_terms
            or self.client_domain
            or self.participant_emails
            or self.include_terms
            or self.include_regex
        )


@dataclass(frozen=True)
class CalendarEventSummary:
    """The matching-relevant projection of one raw Calendar API event
    resource — deliberately narrow so `evaluate_match` never depends on
    the full Google API shape."""

    event_id: str
    title: str
    description: str
    organizer_email: str | None
    attendee_emails: tuple[str, ...]

    @classmethod
    def from_raw_event(cls, event: dict) -> CalendarEventSummary:
        organizer_email = (event.get("organizer") or {}).get("email")
        attendee_emails = tuple(
            a["email"].strip().lower()
            for a in event.get("attendees", [])
            if a.get("email")
        )
        return cls(
            event_id=event["id"],
            title=event.get("summary") or "",
            description=event.get("description") or "",
            organizer_email=organizer_email.strip().lower() if organizer_email else None,
            attendee_emails=attendee_emails,
        )

    @property
    def searchable_text(self) -> str:
        return f"{self.title}\n{self.description}"

    @property
    def all_emails(self) -> tuple[str, ...]:
        return ((self.organizer_email,) if self.organizer_email else ()) + self.attendee_emails


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    #: One of the `RULE_TIER_*` constants when `matched` is True;
    #: `None` when unmatched (including "excluded").
    rule_tier: str | None
    #: Always populated when informative — even for an unmatched/
    #: excluded event, so a rule preview can explain a near-miss
    #: (Prompt 12: "Preview must show recent matched/unmatched sample
    #: events ... and stored match reasons").
    reason: str | None


def _term_hit(text: str, terms: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for term in terms:
        if term.lower() in lowered:
            return term
    return None


def _exclude_hit(event: CalendarEventSummary, rules: CalendarMatchRules) -> str | None:
    term = _term_hit(event.searchable_text, rules.exclude_terms)
    if term:
        return f'exclude term {term!r}'
    if rules._exclude_pattern and rules._exclude_pattern.search(event.searchable_text):
        return f"exclude pattern {rules.exclude_regex!r}"
    return None


def _domain_participant_hit(event: CalendarEventSummary, rules: CalendarMatchRules) -> str | None:
    if rules.client_domain:
        for email in event.all_emails:
            if email.rsplit("@", 1)[-1] == rules.client_domain:
                return f"participant {email!r} matches client domain {rules.client_domain!r}"
    if rules.participant_emails:
        for email in event.all_emails:
            if email in rules.participant_emails:
                return f"participant {email!r} is an explicitly configured participant"
    return None


def _include_rule_hit(event: CalendarEventSummary, rules: CalendarMatchRules) -> str | None:
    term = _term_hit(event.searchable_text, rules.include_terms)
    if term:
        return f"title/description contains include term {term!r}"
    if rules._include_pattern and rules._include_pattern.search(event.searchable_text):
        return f"title/description matches include pattern {rules.include_regex!r}"
    return None


def evaluate_match(event: CalendarEventSummary, rules: CalendarMatchRules) -> MatchResult:
    """The single entry point every caller uses. Deterministic, total
    (never raises for a well-formed `CalendarMatchRules` — regex
    validity is checked once at construction, not per event), and pure."""
    include_hit: tuple[str, str] | None = None
    if event.event_id in rules.included_event_ids:
        include_hit = (RULE_TIER_EVENT_ID, f"explicitly included event ID {event.event_id!r}")
    else:
        term = _term_hit(event.searchable_text, rules.project_name_terms)
        if term:
            include_hit = (
                RULE_TIER_PROJECT_NAME_TERM, f"title/description contains project term {term!r}"
            )
        else:
            domain_reason = _domain_participant_hit(event, rules)
            if domain_reason:
                include_hit = (RULE_TIER_DOMAIN_PARTICIPANT, domain_reason)
            else:
                include_reason = _include_rule_hit(event, rules)
                if include_reason:
                    include_hit = (RULE_TIER_INCLUDE_RULE, include_reason)

    exclude_reason = _exclude_hit(event, rules)
    if exclude_reason and include_hit:
        tier, reason = include_hit
        return MatchResult(
            matched=False, rule_tier=None,
            reason=(
                f"excluded despite matching {tier} ({reason}) — "
                f"{exclude_reason} takes precedence"
            ),
        )
    if exclude_reason:
        return MatchResult(matched=False, rule_tier=None, reason=f"excluded by {exclude_reason}")
    if include_hit:
        tier, reason = include_hit
        return MatchResult(matched=True, rule_tier=tier, reason=reason)
    return MatchResult(matched=False, rule_tier=None, reason=None)
