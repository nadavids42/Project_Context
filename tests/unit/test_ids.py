"""Tests for centralized ID generation."""

from __future__ import annotations

import time

from project_context.ids import new_id


def test_new_id_is_a_26_character_string():
    identifier = new_id()
    assert isinstance(identifier, str)
    assert len(identifier) == 26


def test_new_id_is_unique_across_many_calls():
    ids = {new_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_new_id_is_lexicographically_time_sortable():
    first = new_id()
    time.sleep(0.01)
    second = new_id()
    assert first < second
