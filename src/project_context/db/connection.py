"""SQLite connection factory: consistent pragmas and explicit transactions.

The connection is opened in autocommit mode (`isolation_level=None`) so
this module's `transaction()` context manager has full, predictable
control over `BEGIN`/`COMMIT`/`ROLLBACK` — including wrapping DDL
statements, which Python's `sqlite3` module does not reliably include in
its own *implicit* transaction handling.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Owner-only file permissions (0600) — Section 16, "restrict the data
#: directory to the user," applied to the SQLite database file itself
#: (and its WAL/SHM sidecar files) the same way `credentials/store.py`
#: already restricts the encrypted secrets file.
_OWNER_READ_WRITE_FILE = stat.S_IRUSR | stat.S_IWUSR

#: Bounded wait for a locked database before raising, instead of the
#: default (effectively unbounded) blocking behavior.
DEFAULT_BUSY_TIMEOUT_MS = 5_000

#: Sentinel accepted alongside real paths for a private in-memory database
#: (used by tests). WAL mode is not applicable to it and is skipped.
IN_MEMORY = ":memory:"

#: Substrings SQLite uses for its own two lock-contention messages
#: ("database is locked" from another connection holding a write lock;
#: "database is busy" from a conflicting lock within the *same*
#: connection, e.g. a nested writer). Matched case-insensitively so
#: this does not depend on SQLite's exact wording across versions.
_BUSY_MESSAGE_MARKERS = ("locked", "busy")


class DatabaseBusyError(RuntimeError):
    """Raised in place of a raw `sqlite3.OperationalError` when SQLite
    could not acquire a lock within `busy_timeout_ms` (Section 15:
    "Simulate SQLite busy/locked state with bounded retry and clear
    UI"). The *bounded* part is `PRAGMA busy_timeout` itself — SQLite's
    own busy handler already polls/retries internally for up to that
    many milliseconds before the underlying call fails — so this class
    exists to turn that already-bounded failure into a `safe_message`
    fit to show a user directly, per Section 16 ("Exception reporting
    must redact... a useful, safe message"), rather than a raw
    driver/SQL-shaped string."""

    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message


_BUSY_SAFE_MESSAGE = (
    "The local database was busy and this action could not complete. "
    "Another sync, review action, or app instance may be using it right "
    "now — please wait a moment and try again."
)


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _BUSY_MESSAGE_MARKERS)


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
    if target != IN_MEMORY:
        _restrict_database_files_to_owner(Path(target))
    return conn


def _restrict_database_files_to_owner(sqlite_path: Path) -> None:
    """Best-effort `chmod 0600` on the main database file plus any
    WAL/SHM sidecar files WAL mode has created (Section 16). Never
    raises — same rationale as `config._restrict_to_owner`: a
    filesystem without POSIX permission bits must not fail a connect()
    call that otherwise succeeded."""
    for suffix in ("", "-wal", "-shm"):
        candidate = sqlite_path.with_name(sqlite_path.name + suffix)
        try:
            if candidate.exists():
                os.chmod(candidate, _OWNER_READ_WRITE_FILE)
        except OSError:
            pass


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

    Raises `DatabaseBusyError` — never a raw `sqlite3.OperationalError`
    — if SQLite could not acquire a lock within `busy_timeout_ms`
    (`connect()`'s `PRAGMA busy_timeout`), whether that happens on this
    call's own `BEGIN IMMEDIATE` or on any statement inside the block.
    Every other exception (including a non-lock `OperationalError`, e.g.
    a genuine schema bug) propagates completely unchanged.
    """
    already_in_transaction = conn.in_transaction
    if not already_in_transaction:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy_error(exc):
                raise DatabaseBusyError(_BUSY_SAFE_MESSAGE) from exc
            raise
    try:
        yield conn
    except sqlite3.OperationalError as exc:
        if not already_in_transaction:
            conn.rollback()
        if _is_busy_error(exc):
            raise DatabaseBusyError(_BUSY_SAFE_MESSAGE) from exc
        raise
    except BaseException:
        if not already_in_transaction:
            conn.rollback()
        raise
    else:
        if not already_in_transaction:
            conn.commit()
