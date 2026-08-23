"""Deterministic Gmail message normalization (Section 11.3; Prompt 11).

Pure functions only — no HTTP, no database, no Gmail-specific error
types (those live in `project_context.connectors.gmail`, which is the
only caller). Given one already-fetched Gmail API `messages.get`
`payload` dict, this module:

- decodes the base64url body Gmail actually returns;
- walks `payload.parts` to find a usable `text/plain` part, excluding
  anything with a `filename` (an attachment — Prompt 11: "Exclude
  attachments in this version");
- falls back to `text/html`, converted to plain text through a small
  stdlib-only, script/style-stripping parser, only when no `text/plain`
  part exists (Prompt 11: "Convert safe HTML body to normalized text
  only when no usable plain-text part exists");
- builds one normalized text document — a header block followed by the
  full body — that becomes the artifact's complete, immutable stored
  evidence (Prompt 11: "retaining the complete imported normalized body
  as evidence/versioned content");
- separately locates a conservative quoted-history/signature boundary
  *within* that same body, as a character offset connector/ingestion
  code can use to limit what gets chunked for extraction, without ever
  discarding anything from the stored text itself (Prompt 11: "Trim
  obvious quoted history/signatures conservatively for the extraction
  view while retaining the complete imported normalized body").
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

#: Gmail's search operators (`after:`, `before:`) only accept day
#: granularity — see `project_context.connectors.gmail` for how this
#: shapes the 48-hour overlap watermark.
GMAIL_SEARCH_DATE_FORMAT = "%Y/%m/%d"


class EmailDecodeError(ValueError):
    """One message's body could not be decoded as declared (malformed
    base64url, or a multipart structure with no decodable text part).
    Never carries the undecodable bytes/text itself — only a safe,
    fixed description (Section 16: exception messages must not leak
    source content)."""


@dataclass(frozen=True)
class ExtractedBody:
    text: str
    used_html_fallback: bool


def decode_base64url(data: str) -> bytes:
    """Gmail's `body.data` is base64url, commonly without the trailing
    `=` padding a strict decoder requires — pad it out before decoding
    rather than relying on a decoder default. Uses `validate=True` so a
    genuinely malformed payload (stray non-alphabet characters) raises
    `EmailDecodeError` instead of `base64`'s default behavior of
    silently discarding anything that doesn't fit — a decode failure
    should be surfaced (Section 11.3: "multipart decode failures
    isolated"), not masked as a shorter, silently-wrong body."""
    translated = data.translate(str.maketrans("-_", "+/"))
    padded = translated + "=" * (-len(translated) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EmailDecodeError("message body could not be base64url-decoded") from exc


class _HtmlTextExtractor(HTMLParser):
    """Extracts visible text from HTML, dropping `<script>`/`<style>`
    content entirely and inserting a newline at common block
    boundaries so paragraphs don't run together. This is text
    extraction only — it never executes or renders anything, so it is
    safe to run on untrusted message HTML."""

    _BLOCK_TAGS = frozenset({"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"})
    _SKIP_TAGS = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "br" or tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = re.sub(r"\n[ \t]*", "\n", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()


def html_to_text(html: str) -> str:
    """Safe, dependency-free HTML-to-text: strips tags/script/style,
    keeps visible text, and unescapes entities (via
    `HTMLParser(convert_charrefs=True)`). Only reachable when no usable
    `text/plain` part was found (Prompt 11: HTML fallback path)."""
    extractor = _HtmlTextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.text()


def _walk_leaf_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Depth-first leaves of `payload`'s MIME tree — handles nested
    `multipart/mixed` > `multipart/alternative` structures, which real
    Gmail messages commonly use."""
    parts = payload.get("parts")
    if not parts:
        return [payload]
    leaves: list[dict[str, Any]] = []
    for part in parts:
        leaves.extend(_walk_leaf_parts(part))
    return leaves


def extract_plain_text_body(payload: dict[str, Any]) -> ExtractedBody:
    """Prefer `text/plain`; fall back to `text/html` converted to text
    only if no plain part exists. Parts carrying a `filename` are
    attachments and are always skipped (Prompt 11: no attachments in
    this version). Raises `EmailDecodeError` if a candidate part's
    `body.data` fails to decode, or if the message has no usable text
    part at all."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    saw_any_text_part = False

    for part in _walk_leaf_parts(payload):
        if part.get("filename"):
            continue  # attachment — excluded (Prompt 11)
        mime_type = part.get("mimeType", "")
        if mime_type not in ("text/plain", "text/html"):
            continue
        body = part.get("body") or {}
        data = body.get("data")
        if not data:
            continue
        saw_any_text_part = True
        decoded = decode_base64url(data).decode("utf-8", errors="replace")
        if mime_type == "text/plain":
            plain_parts.append(decoded)
        else:
            html_parts.append(decoded)

    if plain_parts:
        return ExtractedBody(text="\n\n".join(plain_parts), used_html_fallback=False)
    if html_parts:
        return ExtractedBody(text=html_to_text("\n\n".join(html_parts)), used_html_fallback=True)
    if not saw_any_text_part:
        raise EmailDecodeError("message has no decodable text/plain or text/html part")
    return ExtractedBody(text="", used_html_fallback=False)


def get_header(headers: list[dict[str, str]], name: str) -> str | None:
    """Case-insensitive lookup in Gmail's `payload.headers` list shape
    (`[{"name": ..., "value": ...}, ...]`)."""
    lowered = name.lower()
    for header in headers:
        if header.get("name", "").lower() == lowered:
            return header.get("value")
    return None


def parse_rfc2822_date(value: str | None) -> str | None:
    """Best-effort RFC 2822 `Date:` header -> UTC ISO 8601. Returns
    `None` (never raises) for a missing/unparseable value — an
    unparseable date is a display/sort inconvenience, not a reason to
    fail the whole message."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.isoformat() + "Z"
    from datetime import UTC

    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_normalized_email_text(
    *,
    subject: str | None,
    from_header: str | None,
    to_header: str | None,
    cc_header: str | None,
    date_header: str | None,
    message_id: str,
    thread_id: str,
    body_text: str,
) -> tuple[str, int]:
    """One normalized text document: a plain header block, a blank
    line, then the full body. Returns `(full_text, body_start_offset)`
    — `body_start_offset` is the character offset where `body_text`
    begins within `full_text`, so a caller can translate a
    body-relative quote boundary (see `find_quote_boundary`) into a
    valid offset in the stored/parsed text without re-searching it."""
    lines = [
        f"Subject: {subject or '(no subject)'}",
        f"From: {from_header or '(unknown sender)'}",
    ]
    if to_header:
        lines.append(f"To: {to_header}")
    if cc_header:
        lines.append(f"Cc: {cc_header}")
    lines.append(f"Date: {date_header or '(unknown date)'}")
    lines.append(f"Message-ID: {message_id}")
    lines.append(f"Thread-ID: {thread_id}")
    header_block = "\n".join(lines)
    full_text = f"{header_block}\n\n{body_text}"
    return full_text, len(header_block) + 2


#: Common Gmail/Outlook/Apple Mail quoted-reply header, e.g. "On Wed,
#: Aug 20, 2026 at 3:04 PM Jane Doe <jane@example.com> wrote:".
_QUOTE_HEADER_RE = re.compile(r"^\s*On .{0,300}\bwrote:\s*$", re.IGNORECASE)
#: Outlook/plain-text forward and reply markers.
_ORIGINAL_MESSAGE_RE = re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE)
_FORWARDED_RE = re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE)
#: RFC 3676 plain-text signature delimiter ("-- " on its own line,
#: trailing space optional in the wild).
_SIGNATURE_DELIM_RE = re.compile(r"^--\s?$")
#: A `>`-prefixed quoted line. Two in a row is treated as the start of
#: a quoted block rather than one stray '>' character in prose.
_QUOTE_MARKER_RE = re.compile(r"^\s*>")


def find_quote_boundary(body_text: str) -> int:
    """The character offset in `body_text` where quoted history or a
    signature conservatively appears to begin, or `len(body_text)` if
    no such boundary is recognized — never trims when unsure (Prompt
    11: "conservatively"). Only a fixed set of well-known, unambiguous
    markers are recognized; anything else leaves the full body
    visible."""
    offset = 0
    consecutive_quote_lines = 0
    quote_run_start = 0
    for line in body_text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if (
            _QUOTE_HEADER_RE.match(stripped)
            or _ORIGINAL_MESSAGE_RE.match(stripped)
            or _FORWARDED_RE.match(stripped)
            or _SIGNATURE_DELIM_RE.match(stripped)
        ):
            return offset
        if _QUOTE_MARKER_RE.match(stripped):
            if consecutive_quote_lines == 0:
                quote_run_start = offset
            consecutive_quote_lines += 1
            if consecutive_quote_lines >= 2:
                return quote_run_start
        else:
            consecutive_quote_lines = 0
        offset += len(line)
    return len(body_text)
