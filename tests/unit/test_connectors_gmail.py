"""Tests for `project_context.connectors.gmail.GmailConnector`:
validate_config, preview, list-then-get discovery, pagination, fetch
(multipart/HTML-fallback/malformed), 400/401/403/429/5xx, and the
label/query builder (Section 11.3; FR-004, FR-028; Prompt 11). Every
test uses `FakeGmailApi` (tests/fixtures/fake_gmail_api.py) — nothing
here ever touches the network."""

from __future__ import annotations

import pytest

from fixtures.fake_gmail_api import FakeGmailApi
from project_context.connectors.errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorParseError,
    ConnectorPermissionError,
)
from project_context.connectors.gmail import GmailConnector, _build_query, _quote_label
from project_context.connectors.protocol import ConnectorHealthStatus
from project_context.domain.evidence import ArtifactType, EvidenceSourceType


def _connector(api: FakeGmailApi, *, label=None, query=None, since_date=None) -> GmailConnector:
    return GmailConnector(
        access_token="fake-access-token",
        label=label,
        query=query,
        since_date=since_date,
        http_transport=api,
        sleep=lambda _s: None,
        rand=lambda: 0.0,
    )


# --- construction / query building ----------------------------------------


def test_construction_requires_a_label_or_a_query():
    with pytest.raises(ConnectorConfigError):
        GmailConnector(access_token="t")


def test_build_query_combines_label_query_and_since_date():
    query = _build_query(
        label="Project Alpha", query="from:client@example.com", since_date="2026/06/01"
    )
    assert query == 'label:"Project Alpha" (from:client@example.com) after:2026/06/01'


def test_build_query_handles_label_only():
    assert _build_query(label="Alpha", query=None, since_date=None) == 'label:"Alpha"'


def test_quote_label_escapes_embedded_quotes():
    assert _quote_label('Say "hi"') == 'label:"Say \\"hi\\""'


# --- validate_config ---------------------------------------------------


def test_validate_config_ok_when_list_succeeds():
    api = FakeGmailApi()
    api.add_message("m1")
    health = _connector(api, label="Alpha").validate_config()
    assert health.status is ConnectorHealthStatus.OK


def test_validate_config_maps_401_to_auth_error():
    api = FakeGmailApi()
    api.fail_next("list", times=99, status_code=401)
    health = _connector(api, label="Alpha").validate_config()
    assert health.status is ConnectorHealthStatus.AUTH_ERROR


def test_validate_config_maps_403_to_permission_error():
    api = FakeGmailApi()
    api.fail_next("list", times=99, status_code=403)
    health = _connector(api, label="Alpha").validate_config()
    assert health.status is ConnectorHealthStatus.PERMISSION_ERROR


def test_validate_config_maps_400_to_config_error_for_malformed_query():
    api = FakeGmailApi()
    api.fail_next("list", times=99, status_code=400)
    health = _connector(api, query="((( bad syntax").validate_config()
    assert health.status is ConnectorHealthStatus.CONFIG_ERROR


def test_validate_config_maps_5xx_after_retries_exhausted_to_error():
    api = FakeGmailApi()
    api.fail_next("list", times=99, status_code=503)
    health = _connector(api, label="Alpha").validate_config()
    assert health.status is ConnectorHealthStatus.ERROR


# --- discover: list-then-get, pagination ------------------------------------


def test_discover_lists_then_gets_each_message():
    api = FakeGmailApi()
    api.add_message("m1", subject="Kickoff", from_addr="alice@example.com")
    api.add_message("m2", subject="Follow-up", from_addr="bob@example.com")

    page = _connector(api, label="Alpha").discover(None)

    assert {a.external_id for a in page.artifacts} == {"m1", "m2"}
    assert page.next_checkpoint is None
    # list-then-get: exactly one list call, then one get per message.
    assert len(api.list_calls) == 1
    assert {c["id"] for c in api.get_calls} == {"m1", "m2"}
    assert all(c["params"]["format"] == "metadata" for c in api.get_calls)


def test_discover_paginates():
    api = FakeGmailApi()
    for i in range(5):
        api.add_message(f"m{i}")
    api.set_page_size(2)

    connector = _connector(api, label="Alpha")
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
    assert all_ids == {f"m{i}" for i in range(5)}


def test_discover_populates_artifact_metadata_from_headers():
    api = FakeGmailApi()
    api.add_message(
        "m1",
        subject="Kickoff call",
        from_addr="alice@example.com",
        date="Mon, 1 Jun 2026 12:00:00 +0000",
    )
    artifact = _connector(api, label="Alpha").discover(None).artifacts[0]
    assert artifact.title == "Kickoff call"
    assert artifact.author == "alice@example.com"
    assert artifact.artifact_type is ArtifactType.EMAIL
    assert artifact.source_type is EvidenceSourceType.EMAIL
    assert artifact.occurred_at is not None
    assert artifact.occurred_at.startswith("2026-06-01")
    assert artifact.version_marker == "gmail:m1"


def test_discover_skips_a_message_that_vanishes_between_list_and_get():
    api = FakeGmailApi()
    api.add_message("m1")
    api.add_message("m2")
    api.remove_message("m2")  # still listed, but get() now 404s

    page = _connector(api, label="Alpha").discover(None)
    assert [a.external_id for a in page.artifacts] == ["m1"]


def test_discover_skips_a_message_whose_get_is_permission_denied():
    api = FakeGmailApi()
    api.add_message("m1")
    api.add_message("m2")
    api.fail_next("get:m2", times=99, status_code=403)

    page = _connector(api, label="Alpha").discover(None)
    assert [a.external_id for a in page.artifacts] == ["m1"]


def test_discover_raises_auth_error_for_a_401_list():
    api = FakeGmailApi()
    api.fail_next("list", times=99, status_code=401)
    with pytest.raises(ConnectorAuthError):
        _connector(api, label="Alpha").discover(None)


def test_discover_raises_permission_error_for_a_403_list():
    api = FakeGmailApi()
    api.fail_next("list", times=99, status_code=403)
    with pytest.raises(ConnectorPermissionError):
        _connector(api, label="Alpha").discover(None)


def test_discover_retries_transparently_on_429_with_retry_after():
    api = FakeGmailApi()
    api.add_message("m1")
    api.fail_next("list", times=1, status_code=429, headers={"Retry-After": "0"})
    page = _connector(api, label="Alpha").discover(None)
    assert [a.external_id for a in page.artifacts] == ["m1"]


def test_discover_retries_transparently_on_5xx():
    api = FakeGmailApi()
    api.add_message("m1")
    api.fail_next("list", times=2, status_code=503)
    page = _connector(api, label="Alpha").discover(None)
    assert [a.external_id for a in page.artifacts] == ["m1"]


def test_discover_raises_config_error_for_a_malformed_query():
    api = FakeGmailApi()
    api.fail_next("list", times=99, status_code=400)
    with pytest.raises(ConnectorConfigError):
        _connector(api, query="((( bad").discover(None)


# --- preview -----------------------------------------------------------


def test_preview_uses_the_passed_boundary_not_the_configured_one():
    api = FakeGmailApi()
    api.add_message("m1", subject="Matches other boundary")
    connector = _connector(api, label="Configured")  # different from boundary passed below
    results = connector.preview({"label": "Other"}, limit=20)
    assert [r.external_id for r in results] == ["m1"]


def test_preview_is_bounded_by_limit():
    api = FakeGmailApi()
    for i in range(10):
        api.add_message(f"m{i}")
    results = _connector(api, label="Alpha").preview({"label": "Alpha"}, limit=3)
    assert len(results) == 3


def test_preview_requires_a_label_or_query_in_the_boundary():
    api = FakeGmailApi()
    with pytest.raises(ConnectorConfigError):
        _connector(api, label="Alpha").preview({}, limit=20)


# --- fetch -----------------------------------------------------------------


def test_fetch_normalizes_headers_and_plain_text_body():
    api = FakeGmailApi()
    api.add_message(
        "m1",
        subject="Kickoff",
        from_addr="alice@example.com",
        to_addr="bob@example.com",
        plain_text="Let's meet Friday.",
    )
    connector = _connector(api, label="Alpha")
    artifact = connector.discover(None).artifacts[0]
    raw = connector.fetch(artifact)

    text = raw.data.decode("utf-8")
    assert "Subject: Kickoff" in text
    assert "From: alice@example.com" in text
    assert "To: bob@example.com" in text
    assert "Message-ID: m1" in text
    assert "Let's meet Friday." in text
    assert raw.mime_type == "text/plain"
    assert raw.filename == "gmail-m1.txt"


def test_fetch_falls_back_to_html_when_no_plain_text_part_exists():
    api = FakeGmailApi()
    api.add_message("m1", plain_text=None, html_text="<p>Only <b>HTML</b> body.</p>")
    connector = _connector(api, label="Alpha")
    artifact = connector.discover(None).artifacts[0]
    raw = connector.fetch(artifact)
    text = raw.data.decode("utf-8")
    assert "Only" in text and "HTML" in text and "body." in text
    assert "<p>" not in text


def test_fetch_excludes_attachments():
    api = FakeGmailApi()
    api.add_message("m1", plain_text="Body text.", attachment_filename="report.pdf")
    connector = _connector(api, label="Alpha")
    artifact = connector.discover(None).artifacts[0]
    raw = connector.fetch(artifact)
    assert "report.pdf" not in raw.data.decode("utf-8")


def test_fetch_raises_connector_parse_error_on_malformed_body():
    api = FakeGmailApi()
    api.add_message("m1", malformed_plain_data=True)
    connector = _connector(api, label="Alpha")
    artifact = connector.discover(None).artifacts[0]
    with pytest.raises(ConnectorParseError):
        connector.fetch(artifact)


def test_fetch_uses_full_format_not_metadata():
    api = FakeGmailApi()
    api.add_message("m1")
    connector = _connector(api, label="Alpha")
    artifact = connector.discover(None).artifacts[0]
    connector.fetch(artifact)
    full_calls = [c for c in api.get_calls if c["params"].get("format") == "full"]
    assert len(full_calls) == 1
