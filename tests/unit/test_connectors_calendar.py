"""Tests for `project_context.connectors.calendar.CalendarConnector`:
scan-window construction, pagination, recurring instances, cancelled
events, private fields, timezone handling, preview/preview_detailed,
check_availability, and 401/403/429/5xx (Section 11.4; FR-004, FR-029;
Prompt 12). Every test uses `FakeCalendarApi` — nothing here touches
the network."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fixtures.fake_calendar_api import FakeCalendarApi
from project_context.connectors.calendar import CalendarConnector, validate_scan_window
from project_context.connectors.errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorPermissionError,
)
from project_context.connectors.protocol import ConnectorHealthStatus
from project_context.domain.calendar_matching import CalendarMatchRules
from project_context.domain.evidence import ArtifactAvailability, ArtifactType


def _connector(api: FakeCalendarApi, *, rules: CalendarMatchRules, **kwargs) -> CalendarConnector:
    return CalendarConnector(
        access_token="fake-access-token",
        rules=rules,
        http_transport=api,
        sleep=lambda _s: None,
        rand=lambda: 0.0,
        **kwargs,
    )


_MATCH_ALL = CalendarMatchRules(include_regex=r".*")


# --- scan window construction -----------------------------------------


def test_validate_scan_window_rejects_out_of_bounds_days_back():
    with pytest.raises(ConnectorConfigError):
        validate_scan_window(0, 90)
    with pytest.raises(ConnectorConfigError):
        validate_scan_window(731, 90)


def test_validate_scan_window_rejects_out_of_bounds_days_forward():
    with pytest.raises(ConnectorConfigError):
        validate_scan_window(180, 0)
    with pytest.raises(ConnectorConfigError):
        validate_scan_window(180, 366)


def test_connector_construction_rejects_invalid_scan_window():
    with pytest.raises(ConnectorConfigError):
        CalendarConnector(access_token="t", rules=_MATCH_ALL, days_back=0)


def test_time_window_reflects_days_back_and_forward_from_now_fn():
    fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
    connector = _connector(
        FakeCalendarApi(),
        rules=_MATCH_ALL,
        days_back=10,
        days_forward=5,
        now_fn=lambda: fixed_now,
    )
    assert connector._time_min == "2026-06-05T12:00:00Z"
    assert connector._time_max == "2026-06-20T12:00:00Z"


def test_list_call_uses_computed_time_window_and_bounded_flags():
    api = FakeCalendarApi()
    fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
    connector = _connector(api, rules=_MATCH_ALL, now_fn=lambda: fixed_now)
    connector.discover(None)
    assert api.list_calls
    params = api.list_calls[0]
    assert params["timeMin"] == connector._time_min
    assert params["timeMax"] == connector._time_max
    assert params["singleEvents"] == "true"
    assert params["showDeleted"] == "true"


# --- validate_config ---------------------------------------------------


def test_validate_config_ok():
    api = FakeCalendarApi()
    api.add_event("evt1")
    health = _connector(api, rules=_MATCH_ALL).validate_config()
    assert health.status is ConnectorHealthStatus.OK


def test_validate_config_maps_401_to_auth_error():
    api = FakeCalendarApi()
    api.fail_next("list", times=99, status_code=401)
    health = _connector(api, rules=_MATCH_ALL).validate_config()
    assert health.status is ConnectorHealthStatus.AUTH_ERROR


def test_validate_config_maps_403_to_permission_error():
    api = FakeCalendarApi()
    api.fail_next("list", times=99, status_code=403)
    health = _connector(api, rules=_MATCH_ALL).validate_config()
    assert health.status is ConnectorHealthStatus.PERMISSION_ERROR


def test_validate_config_maps_5xx_after_retries_exhausted_to_error():
    api = FakeCalendarApi()
    api.fail_next("list", times=99, status_code=503)
    health = _connector(api, rules=_MATCH_ALL).validate_config()
    assert health.status is ConnectorHealthStatus.ERROR


# --- discover: matching, pagination -----------------------------------


def test_discover_yields_only_matched_events():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync")
    api.add_event("evt2", summary="Personal dentist appt")
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    page = _connector(api, rules=rules).discover(None)
    assert [a.external_id for a in page.artifacts] == ["evt1"]
    assert page.artifacts[0].artifact_type is ArtifactType.CALENDAR_EVENT


def test_discover_paginates():
    api = FakeCalendarApi()
    for i in range(5):
        api.add_event(f"evt{i}", summary=f"Acme Rollout sync {i}")
    api.set_page_size(2)
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    connector = _connector(api, rules=rules)

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
    assert all_ids == {f"evt{i}" for i in range(5)}


def test_discover_stores_match_rule_and_reason_in_extra():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync")
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    artifact = _connector(api, rules=rules).discover(None).artifacts[0]
    assert artifact.extra["match_rule"] == "project_name_term"
    assert "Acme Rollout" in artifact.extra["match_reason"]


def test_discover_captures_recurring_event_id():
    api = FakeCalendarApi()
    api.add_event("evt1_20260601", summary="Weekly sync", recurring_event_id="evt1")
    rules = CalendarMatchRules(include_regex=".*")
    artifact = _connector(api, rules=rules).discover(None).artifacts[0]
    raw_event = artifact.extra["event"]
    assert raw_event["recurringEventId"] == "evt1"


def test_discover_treats_each_recurring_instance_as_its_own_artifact():
    api = FakeCalendarApi()
    api.add_event("evt1_20260601", summary="Weekly sync", recurring_event_id="evt1")
    api.add_event("evt1_20260608", summary="Weekly sync", recurring_event_id="evt1")
    rules = CalendarMatchRules(include_terms=("Weekly sync",))
    artifacts = _connector(api, rules=rules).discover(None).artifacts
    assert {a.external_id for a in artifacts} == {"evt1_20260601", "evt1_20260608"}


# --- cancelled events ----------------------------------------------------


def test_discover_marks_cancelled_event_as_trashed():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync", status="cancelled")
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    artifact = _connector(api, rules=rules).discover(None).artifacts[0]
    assert artifact.is_trashed is True


def test_discover_does_not_mark_confirmed_event_as_trashed():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync", status="confirmed")
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    artifact = _connector(api, rules=rules).discover(None).artifacts[0]
    assert artifact.is_trashed is False


# --- private fields / timezone ------------------------------------------


def test_fetch_includes_visibility_private_marker():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync", visibility="private")
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    connector = _connector(api, rules=rules)
    artifact = connector.discover(None).artifacts[0]
    raw = connector.fetch(artifact)
    assert "Visibility: private" in raw.data.decode()


def test_fetch_includes_time_zone():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync", time_zone="America/Chicago")
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    connector = _connector(api, rules=rules)
    artifact = connector.discover(None).artifacts[0]
    raw = connector.fetch(artifact)
    assert "Time zone: America/Chicago" in raw.data.decode()


def test_all_day_event_uses_date_not_datetime():
    api = FakeCalendarApi()
    api.add_event(
        "evt1",
        summary="Acme Rollout offsite",
        all_day=True,
        start="2026-06-01",
        end="2026-06-02",
    )
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    connector = _connector(api, rules=rules)
    artifact = connector.discover(None).artifacts[0]
    assert artifact.occurred_at == "2026-06-01T00:00:00Z"
    raw = connector.fetch(artifact)
    assert "Start: 2026-06-01" in raw.data.decode()


# --- fetch: metadata-only vs. description-bearing text ----------------


def test_fetch_zero_http_calls():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync", description="Notes here.")
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    connector = _connector(api, rules=rules)
    artifact = connector.discover(None).artifacts[0]
    calls_before = len(api.get_calls) + len(api.list_calls)
    connector.fetch(artifact)
    assert len(api.get_calls) + len(api.list_calls) == calls_before


def test_fetch_includes_meeting_url_from_hangout_link():
    api = FakeCalendarApi()
    api.add_event(
        "evt1", summary="Acme Rollout sync", hangout_link="https://meet.google.com/abc-defg-hij"
    )
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    connector = _connector(api, rules=rules)
    artifact = connector.discover(None).artifacts[0]
    raw = connector.fetch(artifact)
    assert "https://meet.google.com/abc-defg-hij" in raw.data.decode()


def test_fetch_includes_match_reason():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync")
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    connector = _connector(api, rules=rules)
    artifact = connector.discover(None).artifacts[0]
    raw = connector.fetch(artifact)
    assert "Match reason:" in raw.data.decode()


# --- preview / preview_detailed -----------------------------------------


def test_preview_returns_only_matched_events():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync")
    api.add_event("evt2", summary="Unrelated")
    connector = _connector(api, rules=CalendarMatchRules())  # empty configured rules
    results = connector.preview({"project_name_terms": ["Acme Rollout"]}, limit=20)
    assert [r.external_id for r in results] == ["evt1"]


def test_preview_uses_passed_boundary_not_configured_rules():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Other Project sync")
    connector = _connector(api, rules=CalendarMatchRules(project_name_terms=("Acme",)))
    results = connector.preview({"project_name_terms": ["Other Project"]}, limit=20)
    assert [r.external_id for r in results] == ["evt1"]


def test_preview_detailed_returns_matched_and_unmatched_sample():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync")
    api.add_event("evt2", summary="Unrelated meeting")
    connector = _connector(api, rules=CalendarMatchRules())
    preview = connector.preview_detailed({"project_name_terms": ["Acme Rollout"]}, limit=20)
    assert [m.title for m in preview.matched] == ["Acme Rollout sync"]
    assert [u.title for u in preview.unmatched_sample] == ["Unrelated meeting"]
    assert preview.unmatched_sample[0].reason is None


def test_preview_detailed_shows_reason_for_excluded_event():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout — OOO")
    connector = _connector(api, rules=CalendarMatchRules())
    preview = connector.preview_detailed(
        {"project_name_terms": ["Acme Rollout"], "exclude_terms": ["OOO"]}, limit=20
    )
    assert preview.matched == []
    assert "excluded" in preview.unmatched_sample[0].reason


def test_preview_detailed_raises_config_error_for_invalid_regex():
    api = FakeCalendarApi()
    connector = _connector(api, rules=CalendarMatchRules())
    with pytest.raises(ConnectorConfigError):
        connector.preview_detailed({"include_regex": "(unclosed"}, limit=20)


# --- check_availability ----------------------------------------------------


def test_check_availability_detects_a_cancelled_event():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync", status="cancelled")
    connector = _connector(api, rules=CalendarMatchRules(project_name_terms=("Acme Rollout",)))
    assert connector.check_availability("evt1") is ArtifactAvailability.DELETED_EXTERNAL


def test_check_availability_detects_a_deleted_event():
    api = FakeCalendarApi()  # never registered -> 404
    connector = _connector(api, rules=CalendarMatchRules(project_name_terms=("Acme",)))
    assert connector.check_availability("does-not-exist") is ArtifactAvailability.DELETED_EXTERNAL


def test_check_availability_detects_permission_revoked():
    api = FakeCalendarApi()
    api.fail_next("get:evt1", times=99, status_code=403)
    connector = _connector(api, rules=CalendarMatchRules(project_name_terms=("Acme",)))
    assert connector.check_availability("evt1") is ArtifactAvailability.INACCESSIBLE


def test_check_availability_treats_no_longer_matching_as_unavailable():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Renamed, no longer about Acme")
    connector = _connector(api, rules=CalendarMatchRules(project_name_terms=("Acme Rollout",)))
    assert connector.check_availability("evt1") is ArtifactAvailability.DELETED_EXTERNAL


def test_check_availability_returns_available_when_still_matching():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync")
    connector = _connector(api, rules=CalendarMatchRules(project_name_terms=("Acme Rollout",)))
    assert connector.check_availability("evt1") is ArtifactAvailability.AVAILABLE


# --- retry behavior (429/5xx), exercised end-to-end ------------------------


def test_discover_retries_transparently_on_429_with_retry_after():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync")
    api.fail_next("list", times=1, status_code=429, headers={"Retry-After": "0"})
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    page = _connector(api, rules=rules).discover(None)
    assert [a.external_id for a in page.artifacts] == ["evt1"]


def test_discover_retries_transparently_on_5xx():
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync")
    api.fail_next("list", times=2, status_code=503)
    rules = CalendarMatchRules(project_name_terms=("Acme Rollout",))
    page = _connector(api, rules=rules).discover(None)
    assert [a.external_id for a in page.artifacts] == ["evt1"]


def test_discover_raises_auth_error_for_a_401():
    api = FakeCalendarApi()
    api.fail_next("list", times=99, status_code=401)
    with pytest.raises(ConnectorAuthError):
        _connector(api, rules=_MATCH_ALL).discover(None)


def test_discover_raises_permission_error_for_a_403():
    api = FakeCalendarApi()
    api.fail_next("list", times=99, status_code=403)
    with pytest.raises(ConnectorPermissionError):
        _connector(api, rules=_MATCH_ALL).discover(None)
