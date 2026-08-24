"""Tests for `project_context.domain.zoom_hints.describe_filename_hint`
— pure filename-pattern recognition, advisory text only (Section 11.6;
Prompt 13)."""

from __future__ import annotations

from project_context.domain.zoom_hints import describe_filename_hint


def test_recognizes_gmt_prefixed_vtt_transcript():
    hint = describe_filename_hint("GMT20260601-140000_Acme-Kickoff.transcript.vtt")
    assert hint is not None
    assert "transcript" in hint.lower()


def test_recognizes_transcript_vtt_suffix_without_gmt_prefix():
    hint = describe_filename_hint("Acme Kickoff Transcript.vtt")
    assert hint is not None
    assert "transcript" in hint.lower()


def test_recognizes_gmt_prefixed_chat_export():
    hint = describe_filename_hint("GMT20260601-140000_Acme-Kickoff.chat.txt")
    assert hint is not None
    assert "chat" in hint.lower()


def test_recognizes_gmt_prefixed_recording_without_a_specific_suffix():
    hint = describe_filename_hint("GMT20260601-140000_Acme-Kickoff_1920x1080.mp4")
    assert hint is not None
    assert "recording" in hint.lower()


def test_recognizes_summary_document():
    hint = describe_filename_hint("Meeting Summary - Acme Kickoff - 2026-06-01.docx")
    assert hint is not None
    assert "summary" in hint.lower()


def test_summary_pattern_requires_a_document_extension():
    assert describe_filename_hint("summary.exe") is None


def test_returns_none_for_an_unrelated_filename():
    assert describe_filename_hint("quarterly-report.pdf") is None
    assert describe_filename_hint("notes.txt") is None


def test_returns_none_for_empty_filename():
    assert describe_filename_hint("") is None


def test_handles_a_full_path_not_just_a_bare_filename():
    hint = describe_filename_hint("some/drive/path/GMT20260601-140000_Recording.transcript.vtt")
    assert hint is not None
