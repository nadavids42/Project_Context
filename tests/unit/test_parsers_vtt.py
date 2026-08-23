"""Tests for the VTT parser: cues, timestamps, speaker labels via `<v>`
tags, adjacent-same-speaker merging, safe markup stripping, and visible
failure on malformed cues (Section 15)."""

from __future__ import annotations

from project_context.domain.evidence import ParseStatus
from project_context.parsers.vtt_parser import parse_vtt

_VALID_VTT_WITH_SPEAKERS = b"""WEBVTT

00:00:00.000 --> 00:00:02.000
<v Alice>Hello there, how are you?</v>

00:00:02.500 --> 00:00:04.000
<v Alice>Following up on that thought.</v>

00:00:04.500 --> 00:00:06.000
<v Bob>I am doing well, thanks.</v>
"""

_VTT_WITHOUT_SPEAKERS = b"""WEBVTT

00:00:00.000 --> 00:00:02.000
Some unattributed narration.

00:00:02.500 --> 00:00:04.000
More narration continues here.
"""

_MALFORMED_VTT = b"""WEBVTT

not-a-timestamp --> also-not-a-timestamp
This cue can never be parsed.
"""


def test_valid_vtt_extracts_cues_with_speakers():
    result = parse_vtt(_VALID_VTT_WITH_SPEAKERS)

    assert result.status is ParseStatus.PARSED
    assert len(result.blocks) == 2  # Alice's two adjacent cues merge into one turn
    assert result.blocks[0].text == "Alice: Hello there, how are you? Following up on that thought."
    assert result.blocks[1].text == "Bob: I am doing well, thanks."


def test_vtt_adjacent_same_speaker_cues_are_merged_with_extended_end_time():
    result = parse_vtt(_VALID_VTT_WITH_SPEAKERS)

    assert result.blocks[0].section_path == "turn 1 (Alice)"
    assert result.blocks[0].location_label == "00:00:00.000-00:00:04.000"


def test_vtt_block_char_offsets_resolve_back_into_normalized_text():
    result = parse_vtt(_VALID_VTT_WITH_SPEAKERS)

    for block in result.blocks:
        assert result.normalized_text[block.char_start : block.char_end] == block.text


def test_vtt_without_speaker_tags_has_no_speaker_prefix():
    result = parse_vtt(_VTT_WITHOUT_SPEAKERS)

    assert result.status is ParseStatus.PARSED
    # Both cues have no speaker, so — same rule as same-named speakers —
    # they merge into one turn.
    assert result.blocks[0].text == "Some unattributed narration. More narration continues here."
    assert result.blocks[0].section_path == "turn 1"
    assert "(" not in result.blocks[0].section_path


def test_vtt_strips_markup_tags_other_than_voice():
    data = b"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n<b>Bold</b> and <i>italic</i> text.\n"

    result = parse_vtt(data)

    assert result.blocks[0].text == "Bold and italic text."


def test_malformed_vtt_cues_fail_visibly_not_silently_empty():
    result = parse_vtt(_MALFORMED_VTT)

    assert result.status is ParseStatus.FAILED
    assert result.warnings != ()


def test_genuinely_empty_vtt_is_status_empty_not_failed():
    result = parse_vtt(b"WEBVTT\n")

    assert result.status is ParseStatus.EMPTY


def test_vtt_without_webvtt_header_fails_visibly():
    result = parse_vtt(b"this is not a webvtt file")

    assert result.status is ParseStatus.FAILED
    assert result.warnings != ()
