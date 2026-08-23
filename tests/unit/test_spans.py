"""Tests for character-span validation (FR-008)."""

from __future__ import annotations

import pytest

from project_context.spans import InvalidSpanError, validate_span


def test_valid_span_returns_the_slice():
    assert validate_span("Hello world", 0, 5) == "Hello"
    assert validate_span("Hello world", 6, 11) == "world"


def test_full_text_span_is_valid():
    text = "Hello world"
    assert validate_span(text, 0, len(text)) == text


def test_negative_start_is_rejected():
    with pytest.raises(InvalidSpanError, match="char_start"):
        validate_span("Hello", -1, 3)


def test_end_not_greater_than_start_is_rejected():
    with pytest.raises(InvalidSpanError, match="char_end"):
        validate_span("Hello", 3, 3)


def test_end_before_start_is_rejected():
    with pytest.raises(InvalidSpanError):
        validate_span("Hello", 4, 1)


def test_end_beyond_text_length_is_rejected():
    with pytest.raises(InvalidSpanError, match="exceeds text length"):
        validate_span("Hello", 0, 6)


def test_empty_text_has_no_valid_span():
    with pytest.raises(InvalidSpanError):
        validate_span("", 0, 1)
