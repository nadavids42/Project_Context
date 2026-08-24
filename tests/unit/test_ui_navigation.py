"""Tests for `project_context.ui.navigation.run_navigation_with_busy_guard`
(Section 15: "Simulate SQLite busy/locked state with bounded retry and
clear UI") — every page's `DatabaseBusyError` funnels through this one
shared catch rather than each page needing its own."""

from __future__ import annotations

from streamlit.testing.v1 import AppTest

from project_context.db.connection import DatabaseBusyError
from project_context.ui.navigation import run_navigation_with_busy_guard


class _RaisingNavigation:
    def run(self) -> None:
        raise DatabaseBusyError("please wait a moment and try again")


class _OkNavigation:
    def __init__(self) -> None:
        self.ran = False

    def run(self) -> None:
        self.ran = True


def test_run_navigation_with_busy_guard_passes_through_on_success():
    navigation = _OkNavigation()
    run_navigation_with_busy_guard(navigation)
    assert navigation.ran is True


def test_run_navigation_with_busy_guard_renders_friendly_error_via_streamlit():
    def script():
        from project_context.db.connection import DatabaseBusyError
        from project_context.ui.navigation import run_navigation_with_busy_guard

        class RaisingNavigation:
            def run(self) -> None:
                raise DatabaseBusyError("please wait a moment and try again")

        run_navigation_with_busy_guard(RaisingNavigation())

    at = AppTest.from_function(script)
    at.run()

    assert not at.exception
    assert any("please wait a moment and try again" in e.value for e in at.error)
