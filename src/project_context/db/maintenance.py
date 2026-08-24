"""Explicit, optional local secure-delete maintenance (Section 16,
"Deletion and retention": "SQLite secure deletion is not guaranteed by
row deletes; local prototype deletion should include `PRAGMA
secure_delete=ON` and optional `VACUUM`, while explaining backups/
copies").

Deliberately **not** run automatically by
`project_context.services.project_deletion` or anything else — this is
a separate, user-initiated maintenance operation (this module's own
docstring and `scripts/secure_delete_maintenance.py`'s confirmation
prompt are the only ways it runs), because `VACUUM` briefly needs
exclusive access to the whole database and roughly the database's own
size again in free disk space.

What this actually does, and does not, guarantee
--------------------------------------------------
`PRAGMA secure_delete` is a *per-connection* setting (verified directly
against `sqlite3` — it is not persisted in the database file itself and
defaults back to off on the next connection), so merely turning it on
does nothing to bytes SQLite already freed on earlier connections. The
useful operation is turning it on *for this connection* and then
running `VACUUM` *on that same connection*: `VACUUM` rewrites the
entire database file from scratch, and with `secure_delete` enabled for
the connection doing the rewrite, pages that become free in the
process are zeroed rather than left with stale bytes — which is the
retroactive half this module exists for.

Known limitations (Section 16: "while explaining backups/copies" —
this list is that explanation):

- Only the **current** on-disk SQLite file is affected. Any existing
  backup, snapshot, or copy taken before running this retains whatever
  it already had — this is not a way to purge those.
- A WAL/SHM sidecar can retain recently-written frames; this function
  checkpoints the WAL (`PRAGMA wal_checkpoint(TRUNCATE)`) before
  `VACUUM` specifically to fold those back into the main file first,
  but a *concurrent* writer during the checkpoint could still leave
  something behind — this is why the maintenance script asks for
  exclusive access.
- SSD wear-leveling, copy-on-write filesystems (e.g. btrfs/ZFS
  snapshots), and OS-level write caching can retain physical copies of
  "overwritten" data at a layer SQLite has no visibility into or
  control over. This module cannot address that; full-disk encryption
  (Section 16's other recommendation) is the mitigation for data at
  rest on the physical medium.
- This is not the same guarantee as cryptographic erasure. It reduces
  the chance old bytes are recoverable by reading the plain SQLite file
  directly; it is not a certified data-destruction procedure.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from project_context.db.connection import connect


@dataclass(frozen=True)
class SecureDeleteResult:
    sqlite_path: Path
    size_before_bytes: int
    size_after_bytes: int
    duration_ms: int


def run_secure_delete_maintenance(sqlite_path: Path) -> SecureDeleteResult:
    """Checkpoint the WAL, enable `secure_delete` on a fresh connection,
    and `VACUUM` — see the module docstring for exactly what this does
    and does not guarantee. Opens and closes its own connection (never
    reuses a caller's) so `secure_delete` is unambiguously active for
    the entire `VACUUM`. Raises `sqlite3.OperationalError` (unchanged,
    not wrapped) if another connection is holding a conflicting lock —
    callers should ensure the application is not running against this
    database first."""
    size_before = sqlite_path.stat().st_size
    start = time.monotonic()

    conn = connect(sqlite_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("VACUUM")
    finally:
        conn.close()

    duration_ms = int((time.monotonic() - start) * 1000)
    size_after = sqlite_path.stat().st_size
    return SecureDeleteResult(
        sqlite_path=sqlite_path,
        size_before_bytes=size_before,
        size_after_bytes=size_after,
        duration_ms=duration_ms,
    )


def secure_delete_is_active(conn: sqlite3.Connection) -> bool:
    """True iff `PRAGMA secure_delete` is currently on for this specific
    connection — exposed for tests and diagnostics, since the setting
    is not visible any other way (it is not a `sources`/config row)."""
    (value,) = conn.execute("PRAGMA secure_delete").fetchone()
    return bool(value)
