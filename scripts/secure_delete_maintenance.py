#!/usr/bin/env python3
"""Explicit, optional local secure-delete maintenance (Section 16:
"local prototype deletion should include `PRAGMA secure_delete=ON` and
optional `VACUUM`, while explaining backups/copies"). All logic lives
in `project_context.db.maintenance.run_secure_delete_maintenance`; see
that module's docstring for exactly what this does and does not
guarantee — read it before running this.

This is never run automatically (not after project deletion, not on
any schedule) — you run it yourself, deliberately, when you want to
reduce the chance previously deleted rows' bytes are still readable in
the plain SQLite file on disk:

    python scripts/secure_delete_maintenance.py

Requires exclusive access to the database — **stop the running
application first**. `VACUUM` rewrites the entire database file and
needs roughly its current size again in free disk space. Prints the
database size before/after and how long it took; skips the interactive
confirmation only if `--yes` is passed (for scripted/non-interactive
use, e.g. right before a backup).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from project_context.config import load_config  # noqa: E402
from project_context.db.maintenance import run_secure_delete_maintenance  # noqa: E402

_CONFIRMATION_PHRASE = "RUN SECURE DELETE"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=f"Skip the interactive '{_CONFIRMATION_PHRASE}' confirmation prompt.",
    )
    args = parser.parse_args(argv)

    config = load_config()
    if not config.sqlite_path.exists():
        print(f"No database found at {config.sqlite_path} — nothing to do.", file=sys.stderr)
        return 1

    if not args.yes:
        print(
            f"This rewrites {config.sqlite_path} in place with PRAGMA "
            "secure_delete=ON, then VACUUMs it. Make sure the application is "
            "not currently running against this database, and that you have "
            "a recent backup (see scripts/backup.py) if you want one."
        )
        typed = input(f"Type '{_CONFIRMATION_PHRASE}' to proceed: ")
        if typed != _CONFIRMATION_PHRASE:
            print("Confirmation text did not match — aborted, nothing changed.")
            return 1

    result = run_secure_delete_maintenance(config.sqlite_path)
    print(f"Database: {result.sqlite_path}")
    print(f"Size before: {result.size_before_bytes:,} bytes")
    print(f"Size after:  {result.size_after_bytes:,} bytes")
    print(f"Duration:    {result.duration_ms:,} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
