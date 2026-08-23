"""Per-page SQLite connection helper for Streamlit pages.

Streamlit reruns the whole script on every interaction, potentially from
a different thread than the previous run, so this app does not cache one
long-lived `sqlite3.Connection` across reruns (SQLite connections are
not safe to hand between threads). Each page instead opens a short-lived
connection for the duration of one render and closes it — cheap for a
local SQLite file, and it sidesteps that whole class of bug.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from project_context.config import load_config
from project_context.db.connection import connect


@contextmanager
def project_context_connection() -> Iterator[sqlite3.Connection]:
    """Open a connection to the app's configured SQLite database.

    Assumes migrations have already been applied (app.py runs them once
    at startup, before any page renders).
    """
    config = load_config()
    config.ensure_local_directories()
    conn = connect(config.sqlite_path)
    try:
        yield conn
    finally:
        conn.close()
