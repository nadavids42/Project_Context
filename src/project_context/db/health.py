"""Database connection/migration health check for the Streamlit panel."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from project_context.db.connection import connect
from project_context.db.migrations import applied_versions, discover_migrations


@dataclass(frozen=True)
class DatabaseHealth:
    ok: bool
    sqlite_path: Path
    foreign_keys_enabled: bool
    journal_mode: str
    applied_migration_count: int
    pending_migration_count: int
    error: str | None = None


def check_database_health(sqlite_path: Path, migrations_dir: Path) -> DatabaseHealth:
    """Open a connection and report its configuration and migration state.

    Never raises: any failure is captured in the returned `error` field so
    the Streamlit health panel can render a clear status instead of
    crashing the page.
    """
    try:
        conn = connect(sqlite_path)
    except Exception as exc:
        return DatabaseHealth(
            ok=False,
            sqlite_path=sqlite_path,
            foreign_keys_enabled=False,
            journal_mode="unknown",
            applied_migration_count=0,
            pending_migration_count=0,
            error=str(exc),
        )

    try:
        foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        applied = applied_versions(conn)
        all_migrations = discover_migrations(migrations_dir) if migrations_dir.exists() else []
        pending = [m for m in all_migrations if m.version not in applied]
        return DatabaseHealth(
            ok=foreign_keys_enabled and not pending,
            sqlite_path=sqlite_path,
            foreign_keys_enabled=foreign_keys_enabled,
            journal_mode=journal_mode,
            applied_migration_count=len(applied),
            pending_migration_count=len(pending),
        )
    except Exception as exc:
        return DatabaseHealth(
            ok=False,
            sqlite_path=sqlite_path,
            foreign_keys_enabled=False,
            journal_mode="unknown",
            applied_migration_count=0,
            pending_migration_count=0,
            error=str(exc),
        )
    finally:
        conn.close()
