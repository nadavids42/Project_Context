"""Local backup and restore (Section 17: "daily timestamped local
backup to an encrypted location"; Section 16: "SQLite secure deletion
is not guaranteed... while explaining backups/copies").

`scripts/backup.py` is the thin CLI wrapper around this module — see
that script's `--help` for the command-line shape. This module is the
part actually covered by tests (`tests/unit/test_backup_restore.py`),
following the same "logic in `src/`, `scripts/*.py` is a thin argparse
wrapper" split every other script in this repository already uses
(`scripts/run_evaluation.py` -> `project_context.evaluation.cli`).

What "encrypted destination" means here
------------------------------------------
This module does not implement encryption itself (Section 17: "a
user-selected encrypted destination *expectation*") — it copies files
to whatever path you give it. The expectation is that path is already
inside an encrypted volume/mount you control (e.g. a LUKS-encrypted
external drive, or a directory under your already-full-disk-encrypted
home directory per Section 16's "require full-disk encryption on the
Ubuntu device"). This module cannot verify that a destination path is
actually encrypted; `create_backup` only refuses an obviously unsafe
destination (nonexistent parent, or the source data directory itself).

Safety properties
--------------------
- The database is copied using SQLite's own online backup API
  (`sqlite3.Connection.backup`), not a raw file copy — safe to run
  while the application itself might still be running, unlike copying
  `project_context.db` directly (which can capture a torn write).
- Only content-addressed evidence objects a `source_contents` row
  *currently* references are copied — never orphaned/leftover files,
  and never a full recursive copy of `evidence_dir` regardless of what
  it contains.
- Every backup lands in a fresh, timestamped subdirectory under `dest`
  (`project-context-backup-<UTC-ISO-compact>/`) — this module never
  writes into an existing backup directory, so "never overwrite an
  active database" is structural for backups.
- `restore_backup` refuses to overwrite an existing `project_context.db`
  at the target unless `force=True` is passed explicitly — restoring
  over a live database by accident is exactly the failure mode this
  guards against.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from project_context import __version__
from project_context.db.connection import connect
from project_context.evidence_store import content_path

_MANIFEST_FILENAME = "manifest.json"
_DB_FILENAME = "project_context.db"
_EVIDENCE_DIRNAME = "evidence"
_BACKUP_DIRNAME_PREFIX = "project-context-backup-"


class BackupError(RuntimeError):
    """Raised for a backup/restore precondition failure — an unsafe
    destination, a missing source, or (for restore) an existing target
    database without `force=True`."""


@dataclass(frozen=True)
class BackupManifest:
    created_at: str
    app_version: str
    source_sqlite_path: str
    sqlite_sha256: str
    content_object_sha256s: tuple[str, ...]
    content_object_total_bytes: int

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> BackupManifest:
        data = json.loads(text)
        data["content_object_sha256s"] = tuple(data["content_object_sha256s"])
        return cls(**data)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _referenced_sha256s(sqlite_path: Path) -> tuple[str, ...]:
    conn = connect(sqlite_path)
    try:
        rows = conn.execute("SELECT DISTINCT sha256 FROM source_contents").fetchall()
    finally:
        conn.close()
    return tuple(sorted(row["sha256"] for row in rows))


def _backup_sqlite_file(source_sqlite_path: Path, dest_path: Path) -> None:
    """Checkpoint the WAL, then copy via SQLite's online backup API —
    safe against a concurrent writer, unlike `shutil.copy`."""
    source_conn = connect(source_sqlite_path)
    try:
        source_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dest_conn = sqlite3.connect(dest_path)
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()


def create_backup(*, sqlite_path: Path, evidence_dir: Path, dest: Path) -> Path:
    """Create one timestamped backup under `dest`. Returns the created
    backup directory's path.

    Raises `BackupError` if `sqlite_path` does not exist yet, or if
    `dest` resolves inside the same data directory `sqlite_path` lives
    in (backing a database up next to itself defeats the point and
    risks the next restore finding it as an active database).
    """
    sqlite_path = sqlite_path.resolve()
    evidence_dir = evidence_dir.resolve()
    dest = dest.resolve()

    if not sqlite_path.exists():
        raise BackupError(f"no database found at {sqlite_path}")
    if dest.is_relative_to(sqlite_path.parent):
        raise BackupError(
            f"backup destination {dest} is inside the source data directory "
            f"{sqlite_path.parent} — choose a separate destination"
        )

    dest.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = dest / f"{_BACKUP_DIRNAME_PREFIX}{timestamp}"
    backup_dir.mkdir()  # fresh directory every call — never overwrites a prior backup

    db_dest = backup_dir / _DB_FILENAME
    _backup_sqlite_file(sqlite_path, db_dest)

    referenced = _referenced_sha256s(sqlite_path)
    evidence_dest_root = backup_dir / _EVIDENCE_DIRNAME
    total_bytes = 0
    for sha256 in referenced:
        source_object = content_path(evidence_dir, sha256)
        if not source_object.exists():
            continue  # already-unavailable evidence (Section 16) — nothing to copy
        dest_object = content_path(evidence_dest_root, sha256)
        dest_object.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_object, dest_object)
        total_bytes += dest_object.stat().st_size

    manifest = BackupManifest(
        created_at=datetime.now(UTC).isoformat(),
        app_version=__version__,
        source_sqlite_path=str(sqlite_path),
        sqlite_sha256=_sha256_file(db_dest),
        content_object_sha256s=referenced,
        content_object_total_bytes=total_bytes,
    )
    (backup_dir / _MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")
    return backup_dir


def read_manifest(backup_dir: Path) -> BackupManifest:
    manifest_path = backup_dir / _MANIFEST_FILENAME
    if not manifest_path.exists():
        raise BackupError(f"{backup_dir} does not look like a backup (no {_MANIFEST_FILENAME})")
    return BackupManifest.from_json(manifest_path.read_text(encoding="utf-8"))


def verify_backup(backup_dir: Path) -> bool:
    """Recompute the database file's SHA-256 and confirm every evidence
    object the manifest lists as copied is present with the digest its
    own filename/path already claims. Returns True iff everything
    matches; never raises for a mismatch (returns False) so a restore
    smoke test can assert on the result rather than catch an exception."""
    manifest = read_manifest(backup_dir)
    db_path = backup_dir / _DB_FILENAME
    if not db_path.exists() or _sha256_file(db_path) != manifest.sqlite_sha256:
        return False
    evidence_root = backup_dir / _EVIDENCE_DIRNAME
    for sha256 in manifest.content_object_sha256s:
        object_path = content_path(evidence_root, sha256)
        if not object_path.exists():
            continue  # was already missing at backup time (Section 16) — not a corruption
        if _sha256_file(object_path) != sha256:
            return False
    return True


def restore_backup(
    *, backup_dir: Path, target_sqlite_path: Path, target_evidence_dir: Path, force: bool = False
) -> None:
    """Restore one backup into `target_sqlite_path`/`target_evidence_dir`.

    Raises `BackupError` — before copying anything — if
    `target_sqlite_path` already exists and `force` is not True
    (Section 16: "Never overwrite an active database without explicit
    confirmation"). Tests exercising this MUST point both target paths
    at a temporary directory, never at the real configured data
    directory.
    """
    manifest = read_manifest(backup_dir)  # raises BackupError if this isn't a backup dir

    if target_sqlite_path.exists() and not force:
        raise BackupError(
            f"{target_sqlite_path} already exists — pass force=True to overwrite it "
            "explicitly (this never happens implicitly)"
        )

    target_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_dir / _DB_FILENAME, target_sqlite_path)

    target_evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_root = backup_dir / _EVIDENCE_DIRNAME
    for sha256 in manifest.content_object_sha256s:
        source_object = content_path(evidence_root, sha256)
        if not source_object.exists():
            continue
        dest_object = content_path(target_evidence_dir, sha256)
        dest_object.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_object, dest_object)
