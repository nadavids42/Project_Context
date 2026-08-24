#!/usr/bin/env python3
"""Local backup/restore for Project Context's SQLite database and
referenced evidence (Section 17: "daily timestamped local backup to an
encrypted location"). All logic lives in `project_context.backup`; see
that module's docstring for exactly what is and is not guaranteed.

Backup — copies the live database (via SQLite's online backup API, safe
even if the app is running) plus every content-addressed evidence
object a `source_contents` row currently references, into a fresh
timestamped directory under `--dest`:

    python scripts/backup.py backup --dest /media/you/encrypted-drive/backups

`--dest` should already be inside a destination you know is encrypted
(an encrypted external drive, or your own already-full-disk-encrypted
home directory) — this script does not encrypt anything itself and
cannot verify that a given path is on encrypted storage.

Restore — copies one backup's database/evidence back out, into a
target data directory:

    python scripts/backup.py restore \\
        --backup-dir /media/you/encrypted-drive/backups/project-context-backup-20260101T000000Z \\
        --target-data-dir /path/to/a/different/or/temporary/data/dir

Refuses to overwrite an existing database at the target unless you also
pass `--force` — this is deliberately not the default, and restoring
into your real, in-use `data/` directory is exactly the mistake this
guards against. Restore into a scratch directory first and inspect it
before ever pointing `--target-data-dir` at a live data directory.

Verify — recomputes checksums for one backup directory without
restoring anything:

    python scripts/backup.py verify --backup-dir /path/to/one/backup
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from project_context.backup import (  # noqa: E402
    BackupError,
    create_backup,
    verify_backup,
)
from project_context.backup import (  # noqa: E402
    restore_backup as _restore_backup,
)
from project_context.config import load_config  # noqa: E402


def _cmd_backup(args: argparse.Namespace) -> int:
    config = load_config()
    try:
        backup_dir = create_backup(
            sqlite_path=config.sqlite_path, evidence_dir=config.evidence_dir, dest=Path(args.dest)
        )
    except BackupError as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    ok = verify_backup(backup_dir)
    print(f"Backup created at {backup_dir}")
    print(f"Verification: {'OK' if ok else 'FAILED — see above path'}")
    return 0 if ok else 1


def _cmd_restore(args: argparse.Namespace) -> int:
    target_data_dir = Path(args.target_data_dir)
    try:
        _restore_backup(
            backup_dir=Path(args.backup_dir),
            target_sqlite_path=target_data_dir / "project_context.db",
            target_evidence_dir=target_data_dir / "evidence",
            force=args.force,
        )
    except BackupError as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 1
    print(f"Restored into {target_data_dir}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    ok = verify_backup(Path(args.backup_dir))
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="Create a timestamped backup.")
    backup_parser.add_argument(
        "--dest", required=True, help="Destination directory (expected to be encrypted storage)."
    )
    backup_parser.set_defaults(func=_cmd_backup)

    restore_parser = subparsers.add_parser("restore", help="Restore one backup.")
    restore_parser.add_argument("--backup-dir", required=True)
    restore_parser.add_argument("--target-data-dir", required=True)
    restore_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing database at --target-data-dir. Off by default.",
    )
    restore_parser.set_defaults(func=_cmd_restore)

    verify_parser = subparsers.add_parser("verify", help="Verify one backup's checksums.")
    verify_parser.add_argument("--backup-dir", required=True)
    verify_parser.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
