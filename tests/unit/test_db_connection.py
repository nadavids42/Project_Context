"""Tests for the SQLite connection factory and transaction helper."""

from __future__ import annotations

import sqlite3
import time

import pytest

from project_context.db.connection import (
    DEFAULT_BUSY_TIMEOUT_MS,
    DatabaseBusyError,
    connect,
    transaction,
)


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


def test_connect_restricts_database_file_to_owner(tmp_path):
    """Section 16: "restrict the data directory to the user" — applied
    to the SQLite file itself, not just its parent directory."""
    import stat

    path = tmp_path / "app.db"
    conn = connect(path)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == stat.S_IRUSR | stat.S_IWUSR
    finally:
        conn.close()


def test_connect_restricts_wal_sidecar_files_to_owner(tmp_path):
    import stat

    path = tmp_path / "app.db"
    conn = connect(path)
    try:
        conn.execute("CREATE TABLE t (id TEXT)")
        conn.execute("INSERT INTO t (id) VALUES ('1')")
        wal_path = path.with_name(path.name + "-wal")
        if wal_path.exists():
            # A second connect() call re-applies the restriction even
            # after WAL/SHM sidecar files exist.
            conn2 = connect(path)
            conn2.close()
            assert stat.S_IMODE(wal_path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    finally:
        conn.close()


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


# --- reentrancy (Prompt 8: composing services.ledger helpers inside a
# larger review transaction) -------------------------------------------


def test_transaction_is_reentrant_and_commits_together(tmp_path):
    conn = connect(tmp_path / "app.db")
    try:
        conn.execute("CREATE TABLE t (id TEXT)")
        with transaction(conn):
            conn.execute("INSERT INTO t (id) VALUES ('outer')")
            with transaction(conn):
                conn.execute("INSERT INTO t (id) VALUES ('inner')")
            # Still inside the outer transaction after the inner
            # context manager exits — nothing committed yet.
            assert conn.in_transaction

        rows = {row["id"] for row in conn.execute("SELECT id FROM t").fetchall()}
        assert rows == {"outer", "inner"}
    finally:
        conn.close()


def test_transaction_nested_failure_rolls_back_the_whole_outer_block(tmp_path):
    conn = connect(tmp_path / "app.db")
    try:
        conn.execute("CREATE TABLE t (id TEXT)")

        with pytest.raises(RuntimeError), transaction(conn):
            conn.execute("INSERT INTO t (id) VALUES ('outer')")
            with transaction(conn):
                conn.execute("INSERT INTO t (id) VALUES ('inner')")
                raise RuntimeError("synthetic failure inside the nested block")

        assert conn.execute("SELECT id FROM t").fetchall() == []
        assert conn.in_transaction is False
    finally:
        conn.close()


# --- busy/locked handling (Section 15: "Simulate SQLite busy/locked
# state with bounded retry and clear UI") -----------------------------


def test_transaction_raises_database_busy_error_on_real_lock_contention(tmp_path):
    """Real two-connection contention, not a mocked exception: a second
    connection with a short busy_timeout, contending for the same
    file-backed database a first connection is already writing to, gets
    a typed, safe-message error — never a raw sqlite3.OperationalError
    — and the wait is bounded, not indefinite."""
    path = tmp_path / "app.db"
    holder = connect(path)
    holder.execute("CREATE TABLE t (id TEXT)")

    contender = connect(path, busy_timeout_ms=200)
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO t (id) VALUES ('1')")

        start = time.monotonic()
        with pytest.raises(DatabaseBusyError) as exc_info, transaction(contender):
            contender.execute("INSERT INTO t (id) VALUES ('2')")
        elapsed_s = time.monotonic() - start

        # Bounded: raised at roughly the 200ms busy_timeout, not left
        # hanging indefinitely (a generous upper bound keeps this
        # robust on a loaded CI box).
        assert elapsed_s < 3.0
        # Safe, useful message — not SQLite's raw driver string.
        assert "database is locked" not in str(exc_info.value).lower()
        assert "try again" in exc_info.value.safe_message.lower()

        holder.commit()
    finally:
        holder.close()
        contender.close()


def test_transaction_does_not_wrap_a_non_lock_operational_error(tmp_path):
    """A genuine schema bug (not lock contention) must propagate as the
    plain sqlite3.OperationalError it is, not get relabeled as busy."""
    conn = connect(tmp_path / "app.db")
    try:
        with pytest.raises(sqlite3.OperationalError) as exc_info, transaction(conn):
            conn.execute("SELECT * FROM this_table_does_not_exist")
        assert "no such table" in str(exc_info.value).lower()
    finally:
        conn.close()


def test_transaction_nested_call_does_not_issue_a_second_begin(tmp_path):
    """A naive nested `BEGIN IMMEDIATE` raises sqlite3.OperationalError
    ("cannot start a transaction within a transaction"); reentrancy must
    prevent that outright, not merely tolerate it."""
    conn = connect(tmp_path / "app.db")
    try:
        conn.execute("CREATE TABLE t (id TEXT)")
        with transaction(conn), transaction(conn):
            conn.execute("INSERT INTO t (id) VALUES ('1')")
    finally:
        conn.close()
