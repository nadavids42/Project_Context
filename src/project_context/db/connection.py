"""SQLite connection factory: consistent pragmas and explicit transactions.

The connection is opened in autocommit mode (`isolation_level=None`) so
this module's `transaction()` context manager has full, predictable
control over `BEGIN`/`COMMIT`/`ROLLBACK` — including wrapping DDL
statements, which Python's `sqlite3` module does not reliably include in
its own *implicit* transaction handling.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Bounded wait for a locked database before raising, instead of the
#: default (effectively unbounded) blocking behavior.
DEFAULT_BUSY_TIMEOUT_MS = 5_000

#: Sentinel accepted alongside real paths for a private in-memory database
#: (used by tests). WAL mode is not applicable to it and is skipped.
IN_MEMORY = ":memory:"


def connect(
    database: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    """Open a SQLite connection configured for this application.

    - Foreign key enforcement is turned on (SQLite defaults it to off,
      and it is not persisted — every new connection must set it).
    - WAL journal mode is requested for file-backed databases.
    - A bounded busy timeout replaces indefinite blocking on
      "database is locked".
    - Rows are returned as `sqlite3.Row`, giving both name and index
      access (a "named mapping").
    - The connection is opened in autocommit mode so callers use
      `transaction()` for explicit, atomic units of work.
    """
    target = str(database)
    if target != IN_MEMORY:
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if target != IN_MEMORY:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block as one atomic SQLite transaction.

    Commits on success; rolls back and re-raises on any exception. Because
    `connect()` opens the connection in autocommit mode, this also wraps
    DDL statements (`CREATE TABLE`, etc.), which is what makes the
    migration runner able to roll back a partially-applied migration.

    Reentrant: if a transaction is already active on this connection
    (`conn.in_transaction`, a real-time SQLite property — accurate even
    in autocommit mode, since it reflects whether an explicit `BEGIN` is
    outstanding), this call joins it instead of issuing a nested `BEGIN`
    (which SQLite rejects outright). Only the outermost `transaction()`
    call commits or rolls back; an exception from an inner call still
    propagates all the way out, so the outermost call rolls back
    everything written by every nested call, atomically. This is what
    lets a higher-level service (Prompt 8's review transaction) compose
    several already-transactional helpers — e.g.
    `project_context.services.ledger.create_ledger_item` and
    `append_ledger_version`, each already wrapped in its own
    `transaction()` — inside one larger atomic unit of work, without
    duplicating their internals.
    """
    already_in_transaction = conn.in_transaction
    if not already_in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if not already_in_transaction:
            conn.rollback()
        raise
    else:
        if not already_in_transaction:
            conn.commit()
