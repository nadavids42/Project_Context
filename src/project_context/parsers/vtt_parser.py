"""VTT parser: cues, timestamps, and speaker labels via the `<v Speaker>`
voice-tag convention where present. Other cue markup (`<b>`, `<i>`,
etc.) is stripped rather than retained — "normalize safe markup"
(Section 8) means the evidence text is plain text, not that any markup
is considered safe to keep.

Adjacent cues from the same speaker are merged into one transcript turn
(Section 8: "merge adjacent cues from the same speaker if speaker
labeling exists"), which is also the chunker's natural block boundary
for VTT.
"""

from __future__ import annotations

import io
import re

import webvtt
from webvtt.errors import MalformedFileError

from project_context.domain.evidence import ParseStatus
from project_context.parsers.models import ParseResult, TextBlock

PARSER_NAME = "vtt"
PARSER_VERSION = "1"

_VOICE_TAG_RE = re.compile(r"<v(?:\.[\w-]+)*\s+([^>]+)>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]*>")
_ENCODINGS_TO_TRY = ("utf-8-sig", "cp1252")


def _decode(data: bytes) -> tuple[str, tuple[str, ...]]:
    for encoding in _ENCODINGS_TO_TRY:
        try:
            return data.decode(encoding), ()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), (
        "some bytes could not be decoded as UTF-8 and were replaced",
    )


def _extract_speaker(raw_text: str) -> str | None:
    match = _VOICE_TAG_RE.search(raw_text)
    return match.group(1).strip() if match else None


def _strip_markup(raw_text: str) -> str:
    return _ANY_TAG_RE.sub("", raw_text).strip()


def parse_vtt(data: bytes) -> ParseResult:
    text, decode_warnings = _decode(data)

    try:
        parsed = webvtt.from_buffer(io.BytesIO(text.encode("utf-8")))
    except MalformedFileError as exc:
        return ParseResult(
            status=ParseStatus.FAILED,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            normalized_text="",
            warnings=(f"could not parse VTT: {exc}",),
        )

    if not parsed.captions:
        # webvtt-py silently drops cues it can't parse (e.g. malformed
        # timestamps) instead of raising — a file that clearly *tried*
        # to define cues (contains a cue arrow) but produced none is a
        # parse failure, not an honestly empty transcript.
        if "-->" in text:
            return ParseResult(
                status=ParseStatus.FAILED,
                parser_name=PARSER_NAME,
                parser_version=PARSER_VERSION,
                normalized_text="",
                warnings=("no cues could be parsed from this VTT file",) + decode_warnings,
            )
        return ParseResult(
            status=ParseStatus.EMPTY,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            normalized_text="",
            warnings=decode_warnings,
        )

    # (speaker, start, end, [cue texts]) — merged in cue order.
    turns: list[list] = []
    for caption in parsed.captions:
        speaker = _extract_speaker(caption.raw_text)
        cue_text = _strip_markup(caption.raw_text)
        if not cue_text:
            continue
        if turns and turns[-1][0] == speaker:
            turns[-1][2] = caption.end
            turns[-1][3].append(cue_text)
        else:
            turns.append([speaker, caption.start, caption.end, [cue_text]])

    blocks: list[TextBlock] = []
    text_parts: list[str] = []
    cursor = 0
    for ordinal, (speaker, start, end, texts) in enumerate(turns, start=1):
        prefix = f"{speaker}: " if speaker else ""
        full_text = f"{prefix}{' '.join(texts)}"
        block_start = cursor
        text_parts.append(full_text)
        cursor += len(full_text)
        blocks.append(
            TextBlock(
                text=full_text,
                char_start=block_start,
                char_end=cursor,
                section_path=f"turn {ordinal}" + (f" ({speaker})" if speaker else ""),
                location_label=f"{start}-{end}",
            )
        )
        text_parts.append("\n\n")
        cursor += 2

    if not blocks:
        return ParseResult(
            status=ParseStatus.EMPTY,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            normalized_text="",
            warnings=decode_warnings,
        )

    normalized_text = "".join(text_parts).strip()
    return ParseResult(
        status=ParseStatus.PARSED,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        normalized_text=normalized_text,
        blocks=tuple(blocks),
        warnings=decode_warnings,
    )
