"""Tests for `project_context.connectors.fathom.FathomConnector`:
validate_config, preview/preview_detailed, cursor pagination, missing/
changed transcript, transcript-turn merging, 401/403/429/5xx, and
`build_normalized_meeting_text`'s primary/secondary section split
(Section 11.5; FR-004, FR-030; Prompt 13). Every test uses
`FakeFathomApi` (tests/fixtures/fake_fathom_api.py) — nothing here ever
touches the network."""

from __future__ import annotations

import pytest

from fixtures.fake_fathom_api import FakeFathomApi
from project_context.connectors.errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorPermissionError,
)
from project_context.connectors.fathom import FathomConnector
from project_context.connectors.protocol import ConnectorHealthStatus
from project_context.domain.evidence import ArtifactType, EvidenceSourceType
from project_context.domain.fathom_matching import RULE_TIER_CLIENT_DOMAIN, FathomMatchRules
from project_context.services.fathom_ingestion import build_normalized_meeting_text


def _connector(api: FakeFathomApi, *, rules=None, created_after=None) -> FathomConnector:
    rules = rules if rules is not None else FathomMatchRules(client_domain="acme.com")
    return FathomConnector(
        api_key="fake-api-key",
        rules=rules,
        created_after=created_after,
        http_transport=api,
        sleep=lambda _s: None,
        rand=lambda: 0.0,
    )


# --- construction --------------------------------------------------------


def test_construction_requires_an_api_key():
    with pytest.raises(ConnectorConfigError):
        FathomConnector(api_key="", rules=FathomMatchRules())


# --- validate_config -----------------------------------------------------


def test_validate_config_ok_when_list_succeeds():
    api = FakeFathomApi()
    api.add_meeting("rec1")
    assert _connector(api).validate_config().status is ConnectorHealthStatus.OK


def test_validate_config_never_requests_transcript_or_summary():
    """A light, non-heavy call (Section 11.5: "Keep concurrency low")."""
    api = FakeFathomApi()
    api.add_meeting("rec1")
    _connector(api).validate_config()
    assert len(api.list_calls) == 1
    assert "include_transcript" not in api.list_calls[0]["params"]
    assert "include_summary" not in api.list_calls[0]["params"]


def test_validate_config_sends_the_api_key_header():
    api = FakeFathomApi()
    api.add_meeting("rec1")
    _connector(api).validate_config()
    assert api.list_calls[0]["headers"]["X-Api-Key"] == "fake-api-key"


def test_validate_config_maps_401_to_auth_error():
    api = FakeFathomApi()
    api.fail_next("list", times=99, status_code=401)
    assert _connector(api).validate_config().status is ConnectorHealthStatus.AUTH_ERROR


def test_validate_config_maps_403_to_permission_error():
    api = FakeFathomApi()
    api.fail_next("list", times=99, status_code=403)
    assert _connector(api).validate_config().status is ConnectorHealthStatus.PERMISSION_ERROR


def test_validate_config_maps_5xx_after_retries_exhausted_to_error():
    api = FakeFathomApi()
    api.fail_next("list", times=99, status_code=503)
    assert _connector(api).validate_config().status is ConnectorHealthStatus.ERROR


# --- discover: pagination, matching, heavy request shape -------------------


def test_discover_requests_transcript_summary_and_action_items():
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}])
    _connector(api).discover(None)
    params = api.list_calls[0]["params"]
    assert params["include_transcript"] == "true"
    assert params["include_summary"] == "true"
    assert params["include_action_items"] == "true"


def test_discover_only_returns_matched_meetings():
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}])
    api.add_meeting("rec2", calendar_invitees=[{"email": "x@other.com"}])
    page = _connector(api).discover(None)
    assert [a.external_id for a in page.artifacts] == ["rec1"]


def test_discover_populates_artifact_metadata():
    api = FakeFathomApi()
    api.add_meeting(
        "rec1",
        title="Acme Kickoff",
        calendar_invitees=[{"email": "x@acme.com"}],
        recorded_by_email="me@example.com",
        recording_start_time="2026-06-01T14:01:00Z",
        share_url="https://fathom.video/share/rec1",
    )
    artifact = _connector(api).discover(None).artifacts[0]
    assert artifact.title == "Acme Kickoff"
    assert artifact.artifact_type is ArtifactType.MEETING
    assert artifact.source_type is EvidenceSourceType.CALL_RECORDING
    assert artifact.author == "me@example.com"
    assert artifact.occurred_at == "2026-06-01T14:01:00Z"
    assert artifact.external_url == "https://fathom.video/share/rec1"
    assert artifact.extra["match_rule"] == RULE_TIER_CLIENT_DOMAIN
    assert "acme.com" in artifact.extra["match_reason"]


def test_discover_paginates():
    api = FakeFathomApi()
    for i in range(5):
        api.add_meeting(f"rec{i}", calendar_invitees=[{"email": "x@acme.com"}])
    api.set_page_size(2)

    connector = _connector(api)
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
    assert all_ids == {f"rec{i}" for i in range(5)}


def test_discover_sends_created_after_watermark():
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}])
    _connector(api, created_after="2026-05-30T00:00:00Z").discover(None)
    assert api.list_calls[0]["params"]["created_after"] == "2026-05-30T00:00:00Z"


def test_discover_raises_auth_error_for_a_401():
    api = FakeFathomApi()
    api.fail_next("list", times=99, status_code=401)
    with pytest.raises(ConnectorAuthError):
        _connector(api).discover(None)


def test_discover_raises_permission_error_for_a_403():
    api = FakeFathomApi()
    api.fail_next("list", times=99, status_code=403)
    with pytest.raises(ConnectorPermissionError):
        _connector(api).discover(None)


def test_discover_retries_transparently_on_429_with_retry_after():
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}])
    api.fail_next("list", times=1, status_code=429, headers={"Retry-After": "0"})
    page = _connector(api).discover(None)
    assert [a.external_id for a in page.artifacts] == ["rec1"]


def test_discover_retries_transparently_on_5xx():
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}])
    api.fail_next("list", times=2, status_code=503)
    page = _connector(api).discover(None)
    assert [a.external_id for a in page.artifacts] == ["rec1"]


# --- preview / preview_detailed --------------------------------------------


def test_preview_uses_the_passed_boundary_not_the_configured_one():
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@other.com"}])
    connector = _connector(api, rules=FathomMatchRules(client_domain="acme.com"))
    results = connector.preview({"client_domain": "other.com"}, limit=20)
    assert [r.external_id for r in results] == ["rec1"]


def test_preview_never_requests_transcript_or_summary():
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}])
    _connector(api).preview({"client_domain": "acme.com"}, limit=20)
    assert "include_transcript" not in api.list_calls[0]["params"]


def test_preview_detailed_reports_unmatched_sample_with_reason():
    api = FakeFathomApi()
    api.add_meeting("rec1", title="Matches", calendar_invitees=[{"email": "x@acme.com"}])
    api.add_meeting("rec2", title="Does not match", calendar_invitees=[{"email": "x@other.com"}])
    preview = _connector(api).preview_detailed({"client_domain": "acme.com"}, limit=20)
    assert [m.title for m in preview.matched] == ["Matches"]
    assert [u.title for u in preview.unmatched_sample] == ["Does not match"]


def test_preview_raises_config_error_for_an_invalid_window_boundary():
    api = FakeFathomApi()
    with pytest.raises(ConnectorConfigError):
        _connector(api).preview(
            {"scheduled_windows": [{"start": "bad", "end": "2026-01-01T00:00:00Z"}]}
        )


# --- fetch: zero extra HTTP calls, missing/present transcript --------------


def test_fetch_makes_no_additional_http_calls():
    api = FakeFathomApi()
    api.add_meeting(
        "rec1",
        calendar_invitees=[{"email": "x@acme.com"}],
        transcript=[{"speaker": {"display_name": "Alice"}, "text": "Hi.", "timestamp": "00:00:01"}],
    )
    connector = _connector(api)
    artifact = connector.discover(None).artifacts[0]
    calls_before = len(api.list_calls)
    connector.fetch(artifact)
    assert len(api.list_calls) == calls_before


def test_fetch_includes_metadata_and_transcript_text():
    api = FakeFathomApi()
    api.add_meeting(
        "rec1",
        title="Acme Kickoff",
        calendar_invitees=[{"email": "x@acme.com"}],
        transcript=[
            {
                "speaker": {"display_name": "Alice"},
                "text": "Let's ship Friday.",
                "timestamp": "00:01:15",
            },
        ],
    )
    connector = _connector(api)
    artifact = connector.discover(None).artifacts[0]
    raw = connector.fetch(artifact)
    text = raw.data.decode("utf-8")
    assert "Title: Acme Kickoff" in text
    assert "[00:01:15] Alice: Let's ship Friday." in text
    assert raw.mime_type == "text/plain"
    assert raw.filename == "fathom-rec1.txt"


def test_fetch_with_missing_transcript_still_includes_metadata():
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}], transcript=[])
    connector = _connector(api)
    artifact = connector.discover(None).artifacts[0]
    raw = connector.fetch(artifact)
    text = raw.data.decode("utf-8")
    assert "Recording ID: rec1" in text


# --- version_marker: changed transcript --------------------------------


def test_version_marker_changes_when_transcript_changes():
    api = FakeFathomApi()
    api.add_meeting(
        "rec1",
        calendar_invitees=[{"email": "x@acme.com"}],
        transcript=[{"speaker": {"display_name": "A"}, "text": "v1", "timestamp": "00:00:01"}],
    )
    connector = _connector(api)
    first = connector.discover(None).artifacts[0]

    api.update_meeting(
        "rec1",
        transcript=[{"speaker": {"display_name": "A"}, "text": "v2", "timestamp": "00:00:01"}],
    )
    second = connector.discover(None).artifacts[0]
    assert first.version_marker != second.version_marker


def test_version_marker_stable_when_nothing_material_changes():
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}])
    connector = _connector(api)
    first = connector.discover(None).artifacts[0]
    second = connector.discover(None).artifacts[0]
    assert first.version_marker == second.version_marker


# --- transcript turn merging -------------------------------------------


def test_adjacent_same_speaker_cues_are_merged_into_one_turn():
    meeting = {
        "recording_id": "rec1",
        "title": "X",
        "transcript": [
            {"speaker": {"display_name": "Alice"}, "text": "Hello,", "timestamp": "00:00:01"},
            {"speaker": {"display_name": "Alice"}, "text": "how are you?", "timestamp": "00:00:03"},
            {"speaker": {"display_name": "Bob"}, "text": "Good.", "timestamp": "00:00:05"},
        ],
    }
    from project_context.connectors.protocol import ArtifactMetadata

    artifact = ArtifactMetadata(
        external_id="rec1",
        title="X",
        artifact_type=ArtifactType.MEETING,
        version_marker="v1",
        extra={},
    )
    text, start, end = build_normalized_meeting_text(meeting, artifact)
    transcript_text = text[start:end]
    assert "[00:00:01] Alice: Hello, how are you?" in transcript_text
    assert "[00:00:05] Bob: Good." in transcript_text
    # Exactly two merged turns, not three raw cues.
    assert transcript_text.count("Alice:") == 1
    assert transcript_text.count("Bob:") == 1


# --- secondary evidence: summary/action items never overlap the transcript
# range --------------------------------------------------------------------


def test_summary_and_action_items_are_outside_the_transcript_range():
    meeting = {
        "recording_id": "rec1",
        "title": "X",
        "transcript": [
            {
                "speaker": {"display_name": "Alice"},
                "text": "We decided X.",
                "timestamp": "00:00:01",
            },
        ],
        "default_summary": {"markdown_formatted": "Fathom's own generated summary."},
        "action_items": [{"description": "Send the report", "completed": False}],
    }
    from project_context.connectors.protocol import ArtifactMetadata

    artifact = ArtifactMetadata(
        external_id="rec1",
        title="X",
        artifact_type=ArtifactType.MEETING,
        version_marker="v1",
        extra={},
    )
    text, start, end = build_normalized_meeting_text(meeting, artifact)
    transcript_text = text[start:end]
    assert "We decided X." in transcript_text
    assert "Fathom's own generated summary." not in transcript_text
    assert "Send the report" not in transcript_text
    # But both are still present in the full stored text (visible evidence).
    assert "Fathom's own generated summary." in text
    assert "Send the report" in text
