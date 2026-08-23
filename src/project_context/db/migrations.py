"""A small numbered SQL migration runner.

Each migration is a `NNNN_description.sql` file under a migrations
directory. Migrations are applied in filename order, each inside one
atomic transaction (`db.connection.transaction`), and recorded in
`schema_migrations` only as part of that same transaction — so a failing
migration cannot be half-applied or mistakenly recorded as successful,
and re-running the runner against an up-to-date database is a no-op.

`schema_migrations` itself is infrastructure for the runner, not an
application migration, so it is created directly by this module rather
than via a numbered `.sql` file.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from project_context.db.connection import transaction
from project_context.timeutil import utc_now_iso

_MIGRATION_FILENAME_RE = re.compile(r"^(?P<version>\d{4})_(?P<slug>[a-z0-9_]+)\.sql$")

_CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """Raised when a migration file is malformed or fails to apply."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover_migrations(migrations_dir: Path) -> list[Migration]:
    """Find and order every `NNNN_description.sql` file in a directory."""
    migrations: list[Migration] = []
    seen_versions: dict[int, Path] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"Migration filename {path.name!r} does not match the required "
                "NNNN_description.sql pattern"
            )
        version = int(match.group("version"))
        if version in seen_versions:
            raise MigrationError(
                f"Duplicate migration version {version}: "
                f"{seen_versions[version].name!r} and {path.name!r}"
            )
        seen_versions[version] = path
        migrations.append(Migration(version=version, name=match.group("slug"), path=path))
    return sorted(migrations, key=lambda m: m.version)


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Return the set of migration version numbers already recorded."""
    conn.execute(_CREATE_MIGRATIONS_TABLE)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def _is_comment_only(sql_fragment: str) -> bool:
    lines = [line.strip() for line in sql_fragment.splitlines() if line.strip()]
    return all(line.startswith("--") for line in lines)


def _split_statements(sql: str) -> list[str]:
    """Split a migration file into individual complete SQL statements.

    Uses the standard library's own notion of "a complete statement"
    (`sqlite3.complete_statement`), which correctly accounts for
    semicolons inside string literals and comments, rather than a naive
    split on `;`.
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            candidate = buffer.strip()
            buffer = ""
            if candidate and not _is_comment_only(candidate):
                statements.append(candidate)

    leftover = buffer.strip()
    if leftover and not _is_comment_only(leftover):
        raise MigrationError(f"Incomplete trailing SQL statement: {leftover!r}")
    return statements


def apply_migration(conn: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration inside a single transaction and record it."""
    statements = _split_statements(migration.sql)
    if not statements:
        raise MigrationError(f"Migration {migration.path.name} contains no SQL statements")

    with transaction(conn):
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, utc_now_iso()),
        )


def run_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> list[Migration]:
    """Apply every pending migration in `migrations_dir`, in order.

    Returns the migrations actually applied during this call — an empty
    list on a repeat run against an up-to-date database. Stops at the
    first failure; that migration is left unrecorded and its own changes
    rolled back, while migrations applied earlier in this same call (or
    in a previous call) remain applied.
    """
    already_applied = applied_versions(conn)
    pending = [m for m in discover_migrations(migrations_dir) if m.version not in already_applied]

    newly_applied: list[Migration] = []
    for migration in pending:
        try:
            apply_migration(conn, migration)
        except Exception as exc:
            raise MigrationError(
                f"Migration {migration.path.name} failed and was not recorded as applied: {exc}"
            ) from exc
        newly_applied.append(migration)
    return newly_applied
