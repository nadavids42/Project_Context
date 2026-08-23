"""Tests for the shared UTC timestamp helper."""

from __future__ import annotations

from datetime import UTC, datetime

from project_context.timeutil import utc_now_iso


def test_utc_now_iso_round_trips_and_is_utc():
    text = utc_now_iso()
    assert text.endswith("Z")

    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0

    now = datetime.now(UTC)
    assert abs((now - parsed).total_seconds()) < 5


def test_utc_now_iso_is_monotonically_non_decreasing():
    first = utc_now_iso()
    second = utc_now_iso()
    assert first <= second
