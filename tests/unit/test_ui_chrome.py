"""Tests for shared UI chrome (Section 15: "clear UI" for SQLite
busy/locked errors)."""

from __future__ import annotations

from streamlit.testing.v1 import AppTest


def _at_for_busy_error() -> AppTest:
    def script():
        from project_context.db.connection import DatabaseBusyError
        from project_context.ui.chrome import render_database_busy_error

        render_database_busy_error(DatabaseBusyError("please wait a moment and try again"))

    return AppTest.from_function(script)


def test_render_database_busy_error_shows_the_safe_message_not_a_traceback():
    at = _at_for_busy_error()
    at.run()

    assert not at.exception
    assert any("please wait a moment and try again" in e.value for e in at.error)


def _at_for_privacy_banner() -> AppTest:
    def script():
        from project_context.ui.chrome import render_privacy_banner

        render_privacy_banner()

    return AppTest.from_function(script)


def test_privacy_banner_disclosure_matches_the_actual_extraction_trigger():
    """The privacy/sync correction: the banner's disclosure must match
    what actually triggers an LLM call — an explicit 'Extract
    observations' click or brief generation, never Sync Project."""
    at = _at_for_privacy_banner()
    at.run()

    assert not at.exception
    banner_text = " ".join(w.value for w in at.warning)
    assert "Extract observations" in banner_text
    assert "generate a brief" in banner_text
    assert "never sends anything to an LLM" in banner_text
    assert "nothing is sent automatically or in the background" in banner_text
