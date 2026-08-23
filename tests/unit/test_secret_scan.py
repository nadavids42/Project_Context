"""Repository secret-pattern scan (Section 16; FR-032; Prompt 10:
"Full suite, lint, and a secret-pattern scan").

Scans every tracked, human-authored text file (via `git ls-files` —
never `.gitignore`d local data/credentials) for the shape of a real
OpenAI/Google/AWS/GitHub secret, and separately proves the `.gitignore`
patterns that keep local credential material out of the repository in
the first place still cover this prompt's actual file names.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Real-shaped secret patterns — deliberately specific (a fixed vendor
#: prefix plus a long token body) so this never flags this file's own
#: pattern *strings* below, sample IDs like ULIDs, or SHA-256 hex
#: digests, all of which are legitimate non-secret content throughout
#: this repository.
_SECRET_PATTERNS = {
    "openai_api_key": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "google_oauth_client_secret": re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b"),
    "google_oauth_access_token": re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}\b"),
    "google_oauth_refresh_token": re.compile(r"\b1//[A-Za-z0-9_-]{20,}\b"),
    "aws_access_key_id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
}

#: Extensions worth scanning as text; binary/lockfile-ish content is
#: skipped rather than decoded.
_TEXT_SUFFIXES = {
    ".py", ".sql", ".md", ".toml", ".txt", ".cfg", ".ini", ".yaml", ".yml", ".env",
}


def _tracked_files() -> list[Path]:
    """Every file that either already is tracked, or *would* be the
    next time someone runs `git add .` — i.e. everything not excluded
    by `.gitignore`. Scanning only already-committed files would miss
    an uncommitted new file entirely (this repository frequently has a
    full prompt's worth of new, not-yet-committed source and test files
    at once), which would make this scan a no-op for exactly the
    content most likely to be new and unreviewed."""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
        check=True, capture_output=True, text=True,
    )
    return [REPO_ROOT / line for line in result.stdout.splitlines() if line]


def test_no_tracked_file_contains_a_real_shaped_secret():
    findings = []
    for path in _tracked_files():
        if path.suffix not in _TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pattern in _SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append((path.relative_to(REPO_ROOT), name))
    assert findings == [], f"possible secret(s) found in tracked files: {findings}"


def test_env_and_credential_files_are_not_tracked_by_git():
    tracked_names = {path.name for path in _tracked_files()}
    assert ".env" not in tracked_names
    assert "secrets.enc" not in tracked_names
    assert "master.key" not in tracked_names


def test_gitignore_covers_every_local_credential_artifact():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    # Section 16 / Prompt 10's actual on-disk artifacts:
    #   - the encrypted-fallback secrets file (credentials/store.py)
    #   - the local SQLite database + evidence directory (both under data/)
    #   - any legacy patterns the project already anticipated
    assert "secrets.enc" in gitignore
    assert "data/" in gitignore
    assert ".env" in gitignore


def test_credentials_directory_lives_under_the_gitignored_data_directory():
    """`AppConfig.credentials_dir` defaults under `data_dir` (already
    covered by the blanket `data/` gitignore pattern) rather than
    needing its own dedicated ignore rule."""
    from project_context.config import load_config

    config = load_config(_env_file=None, data_dir=str(REPO_ROOT / "data"))
    assert config.credentials_dir.is_relative_to(config.data_dir)
