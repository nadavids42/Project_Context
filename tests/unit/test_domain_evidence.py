"""Tests for evidence domain validation (FR-005/FR-006): required
fields, length bounds, and enum values."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from project_context.domain.evidence import (
    AUTHOR_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    EvidenceSourceType,
    ManualFileUploadInput,
    ManualTextInput,
)

_OCCURRED_AT = datetime(2026, 8, 20, 14, 30)


def _text_input(**overrides):
    fields = {
        "title": "Kickoff notes",
        "source_type": EvidenceSourceType.MEETING_NOTES,
        "occurred_at": _OCCURRED_AT,
        "text": "Some evidence text.",
        **overrides,
    }
    return ManualTextInput(**fields)


def test_minimal_valid_text_input_is_accepted():
    data = _text_input()

    assert data.title == "Kickoff notes"
    assert data.author is None
    assert data.external_url is None


def test_text_input_requires_non_blank_title():
    with pytest.raises(ValidationError):
        _text_input(title="   ")


def test_text_input_requires_non_blank_text():
    with pytest.raises(ValidationError):
        _text_input(text="   ")


def test_text_input_title_over_max_length_is_rejected():
    with pytest.raises(ValidationError):
        _text_input(title="x" * (TITLE_MAX_LENGTH + 1))


def test_text_input_author_over_max_length_is_rejected():
    with pytest.raises(ValidationError):
        _text_input(author="x" * (AUTHOR_MAX_LENGTH + 1))


def test_text_input_rejects_invalid_source_type():
    with pytest.raises(ValidationError):
        _text_input(source_type="not_a_real_type")


def test_text_input_whitespace_only_optional_fields_normalize_to_none():
    data = _text_input(author="   ", external_url="  ")

    assert data.author is None
    assert data.external_url is None


def test_text_input_strips_title_and_text():
    data = _text_input(title="  Kickoff notes  ", text="  Some evidence text.  ")

    assert data.title == "Kickoff notes"
    assert data.text == "Some evidence text."


def _file_input(**overrides):
    fields = {
        "title": "Contract draft",
        "source_type": EvidenceSourceType.DOCUMENT,
        "occurred_at": _OCCURRED_AT,
        "filename": "contract.txt",
        "data": b"file bytes",
        **overrides,
    }
    return ManualFileUploadInput(**fields)


def test_minimal_valid_file_input_is_accepted():
    data = _file_input()

    assert data.filename == "contract.txt"
    assert data.data == b"file bytes"


def test_file_input_rejects_empty_bytes():
    with pytest.raises(ValidationError):
        _file_input(data=b"")


def test_file_input_requires_non_blank_filename():
    with pytest.raises(ValidationError):
        _file_input(filename="   ")
