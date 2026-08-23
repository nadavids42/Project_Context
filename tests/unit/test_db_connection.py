"""Tests for the SQLite connection factory and transaction helper."""

from __future__ import annotations

import sqlite3

import pytest

from project_context.db.connection import DEFAULT_BUSY_TIMEOUT_MS, connect, transaction


def test_connect_enables_foreign_keys(tmp_path):
    conn = connect(tmp_path / "app.db")
    try:
        (enabled,) = conn.execute("PRAGMA foreign_keys").fetchone()
        assert enabled == 1
    finally:
        conn.close()


def test_connect_uses_wal_journal_mode_for_file_database(tmp_path):
    conn = connect(tmp_path / "app.db")
    try:
        (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_connect_applies_bounded_busy_timeout(tmp_path):
    conn = connect(tmp_path / "app.db", busy_timeout_ms=2_500)
    try:
        (timeout_ms,) = conn.execute("PRAGMA busy_timeout").fetchone()
        assert timeout_ms == 2_500
    finally:
        conn.close()


def test_connect_default_busy_timeout_is_bounded_not_infinite():
    assert 0 < DEFAULT_BUSY_TIMEOUT_MS < 60_000


def test_connect_returns_named_row_mapping(tmp_path):
    conn = connect(tmp_path / "app.db")
    try:
        conn.execute("CREATE TABLE t (id TEXT, label TEXT)")
        conn.execute("INSERT INTO t (id, label) VALUES ('1', 'a')")
        row = conn.execute("SELECT id, label FROM t").fetchone()
        assert row["id"] == "1"
        assert row["label"] == "a"
        assert row[0] == "1"  # index access still works
        assert dict(row) == {"id": "1", "label": "a"}
    finally:
        conn.close()


def test_connect_creates_parent_directories(tmp_path):
    nested = tmp_path / "nested" / "dirs" / "app.db"
    conn = connect(nested)
    try:
        assert nested.parent.is_dir()
    finally:
        conn.close()


def test_transaction_commits_on_success(tmp_path):
    conn = connect(tmp_path / "app.db")
    try:
        conn.execute("CREATE TABLE t (id TEXT)")
        with transaction(conn):
            conn.execute("INSERT INTO t (id) VALUES ('1')")
        rows = conn.execute("SELECT id FROM t").fetchall()
        assert [row["id"] for row in rows] == ["1"]
    finally:
        conn.close()


def test_transaction_rolls_back_on_exception(tmp_path):
    conn = connect(tmp_path / "app.db")
    try:
        conn.execute("CREATE TABLE t (id TEXT)")
        conn.execute("INSERT INTO t (id) VALUES ('existing')")

        with pytest.raises(RuntimeError), transaction(conn):
            conn.execute("INSERT INTO t (id) VALUES ('should-not-persist')")
            raise RuntimeError("synthetic failure mid-transaction")

        rows = conn.execute("SELECT id FROM t").fetchall()
        assert [row["id"] for row in rows] == ["existing"]
    finally:
        conn.close()


def test_transaction_rolls_back_ddl_too(tmp_path):
    """DDL is transactional here too, since connect() uses autocommit mode
    and transaction() issues an explicit BEGIN/COMMIT/ROLLBACK around it."""
    conn = connect(tmp_path / "app.db")
    try:
        with pytest.raises(RuntimeError), transaction(conn):
            conn.execute("CREATE TABLE should_not_exist (id TEXT)")
            raise RuntimeError("synthetic failure after DDL")

        with pytest.raises(sqlite3.OperationalError):
            conn.execute("SELECT * FROM should_not_exist")
    finally:
        conn.close()
