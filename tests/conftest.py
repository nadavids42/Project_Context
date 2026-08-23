"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"


@pytest.fixture
def migrations_dir() -> Path:
    """The repository's real, numbered migrations directory."""
    return MIGRATIONS_DIR
