"""Tests for `project_context.domain.email_normalization`: base64url
decoding, multipart/HTML-fallback body extraction, header lookup, date
parsing, and conservative quote-boundary detection (Section 11.3;
Prompt 11)."""

from __future__ import annotations

import pytest

from fixtures.fake_gmail_api import b64url
from project_context.domain.email_normalization import (
    EmailDecodeError,
    build_normalized_email_text,
    decode_base64url,
    extract_plain_text_body,
    find_quote_boundary,
    get_header,
    html_to_text,
    parse_rfc2822_date,
)

# --- decode_base64url ----------------------------------------------------


def test_decode_base64url_handles_missing_padding():
    assert decode_base64url(b64url("hello world")) == b"hello world"


def test_decode_base64url_raises_email_decode_error_for_garbage():
    with pytest.raises(EmailDecodeError):
        decode_base64url("###not valid###")


# --- extract_plain_text_body ---------------------------------------------


def _plain_payload(text: str) -> dict:
    return {"headers": [], "parts": [{"mimeType": "text/plain", "body": {"data": b64url(text)}}]}


def test_extract_plain_text_body_prefers_plain_text():
    payload = {
        "headers": [],
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64url("Plain body.")}},
            {"mimeType": "text/html", "body": {"data": b64url("<p>HTML body.</p>")}},
        ],
    }
    result = extract_plain_text_body(payload)
    assert result.text == "Plain body."
    assert result.used_html_fallback is False


def test_extract_plain_text_body_falls_back_to_html_when_no_plain_part():
    payload = {
        "headers": [],
        "parts": [{"mimeType": "text/html", "body": {"data": b64url("<p>Only HTML.</p>")}}],
    }
    result = extract_plain_text_body(payload)
    assert "Only HTML." in result.text
    assert result.used_html_fallback is True


def test_extract_plain_text_body_excludes_attachments():
    payload = {
        "headers": [],
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64url("Body text.")}},
            {
                "mimeType": "application/pdf",
                "filename": "report.pdf",
                "body": {"attachmentId": "a1", "size": 999},
            },
        ],
    }
    result = extract_plain_text_body(payload)
    assert result.text == "Body text."


def test_extract_plain_text_body_walks_nested_multipart_alternative():
    payload = {
        "headers": [],
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64url("Nested plain.")}},
                    {"mimeType": "text/html", "body": {"data": b64url("<p>Nested html</p>")}},
                ],
            }
        ],
    }
    result = extract_plain_text_body(payload)
    assert result.text == "Nested plain."


def test_extract_plain_text_body_raises_for_malformed_base64():
    payload = _plain_payload("placeholder")
    payload["parts"][0]["body"]["data"] = "###garbage###"
    with pytest.raises(EmailDecodeError):
        extract_plain_text_body(payload)


def test_extract_plain_text_body_raises_when_no_usable_text_part_exists():
    payload = {"headers": [], "parts": []}
    with pytest.raises(EmailDecodeError):
        extract_plain_text_body(payload)


def test_extract_plain_text_body_ignores_a_pure_attachment_message():
    payload = {
        "headers": [],
        "parts": [
            {
                "mimeType": "application/pdf",
                "filename": "report.pdf",
                "body": {"attachmentId": "a1", "size": 999},
            }
        ],
    }
    with pytest.raises(EmailDecodeError):
        extract_plain_text_body(payload)


# --- html_to_text ----------------------------------------------------------


def test_html_to_text_strips_tags_and_keeps_text():
    text = html_to_text("<p>Hello <b>world</b></p><p>Second paragraph.</p>")
    assert "Hello" in text
    assert "world" in text
    assert "Second paragraph." in text
    assert "<p>" not in text and "<b>" not in text


def test_html_to_text_drops_script_and_style_content():
    text = html_to_text("<style>.a{color:red}</style><script>evil()</script><p>Visible</p>")
    assert "Visible" in text
    assert "evil()" not in text
    assert "color:red" not in text


def test_html_to_text_unescapes_entities():
    assert "&" in html_to_text("<p>Fish &amp; chips</p>")


# --- get_header / parse_rfc2822_date --------------------------------------


def test_get_header_is_case_insensitive():
    headers = [{"name": "Subject", "value": "Hello"}]
    assert get_header(headers, "subject") == "Hello"
    assert get_header(headers, "SUBJECT") == "Hello"


def test_get_header_returns_none_when_absent():
    assert get_header([], "Subject") is None


def test_parse_rfc2822_date_converts_to_utc_iso():
    result = parse_rfc2822_date("Mon, 1 Jun 2026 12:00:00 +0000")
    assert result is not None
    assert result.startswith("2026-06-01T12:00:00")


def test_parse_rfc2822_date_returns_none_for_unparseable_value():
    assert parse_rfc2822_date("not a date") is None


def test_parse_rfc2822_date_returns_none_for_missing_value():
    assert parse_rfc2822_date(None) is None


# --- build_normalized_email_text -------------------------------------------


def test_build_normalized_email_text_includes_all_fields_and_body():
    full_text, body_start = build_normalized_email_text(
        subject="Kickoff",
        from_header="a@example.com",
        to_header="b@example.com",
        cc_header="c@example.com",
        date_header="Mon, 1 Jun 2026 12:00:00 +0000",
        message_id="msg-1",
        thread_id="thread-1",
        body_text="Let's meet Friday.",
    )
    assert "Subject: Kickoff" in full_text
    assert "From: a@example.com" in full_text
    assert "To: b@example.com" in full_text
    assert "Cc: c@example.com" in full_text
    assert "Message-ID: msg-1" in full_text
    assert "Thread-ID: thread-1" in full_text
    assert full_text[body_start:] == "Let's meet Friday."


def test_build_normalized_email_text_handles_missing_optional_fields():
    full_text, body_start = build_normalized_email_text(
        subject=None,
        from_header=None,
        to_header=None,
        cc_header=None,
        date_header=None,
        message_id="msg-1",
        thread_id="thread-1",
        body_text="Body.",
    )
    assert "(no subject)" in full_text
    assert "(unknown sender)" in full_text
    assert "Cc:" not in full_text
    assert full_text[body_start:] == "Body."


# --- find_quote_boundary ----------------------------------------------------


def test_find_quote_boundary_returns_full_length_when_no_marker_present():
    body = "This is a normal reply with no quoted history."
    assert find_quote_boundary(body) == len(body)


def test_find_quote_boundary_detects_gmail_style_on_wrote_header():
    body = (
        "Sounds good.\n\nOn Mon, Aug 20, 2026 at 3:04 PM Jane Doe <jane@example.com> "
        "wrote:\n> Old content"
    )
    boundary = find_quote_boundary(body)
    assert body[boundary:].startswith("On Mon,")
    assert "Sounds good." in body[:boundary]


def test_find_quote_boundary_detects_original_message_marker():
    body = "New content here.\n\n-----Original Message-----\nFrom: bob@example.com\nOld stuff"
    boundary = find_quote_boundary(body)
    assert body[boundary:].startswith("-----Original Message-----")


def test_find_quote_boundary_detects_forwarded_message_marker():
    body = "FYI.\n\n---------- Forwarded message ----------\nFrom: bob@example.com"
    boundary = find_quote_boundary(body)
    assert body[boundary:].startswith("---------- Forwarded message")


def test_find_quote_boundary_detects_two_consecutive_angle_bracket_lines():
    body = "My reply.\n> quoted line one\n> quoted line two\n> quoted line three"
    boundary = find_quote_boundary(body)
    assert body[boundary:].startswith("> quoted line one")


def test_find_quote_boundary_ignores_a_single_stray_angle_bracket_line():
    body = "See attached: 2 > 1 is true.\nMore normal text follows."
    assert find_quote_boundary(body) == len(body)


def test_find_quote_boundary_detects_signature_delimiter():
    body = "Talk soon.\n--\nJane Doe\nSenior Consultant"
    boundary = find_quote_boundary(body)
    assert body[boundary:].startswith("--")


def test_find_quote_boundary_does_not_trim_a_double_dash_inside_prose():
    body = "The budget--which we discussed--is approved."
    assert find_quote_boundary(body) == len(body)
