"""Capture everything Section 13.7 step 1 requires frozen before a run:
"Freeze corpus, ground truth, model, prompts, schemas, and app commit."
Also Section 15's golden-dataset requirement: "Store the exact
application commit, model/configuration, prompt/schema versions, and raw
metric report."
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from project_context.evaluation.baseline_schema import (
    BASELINE_PROMPT_VERSION,
    BASELINE_SCHEMA_VERSION,
)
from project_context.evaluation.schema import GROUND_TRUTH_SCHEMA_VERSION
from project_context.llm.prompts import (
    BRIEF_PROMPT_VERSION,
    MEETING_PREP_PROMPT_VERSION,
    PROMPT_VERSION,
)
from project_context.llm.schemas import BRIEF_SCHEMA_VERSION, SCHEMA_VERSION
from project_context.timeutil import utc_now_iso

REPO_ROOT = Path(__file__).resolve().parents[3]


def capture_git_commit(repo_root: Path = REPO_ROOT) -> str | None:
    """The current commit hash, or `None` if this is not a git checkout
    or `git` is unavailable — never raises (a missing commit hash is a
    report caveat, not a fatal error)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def capture_git_dirty(repo_root: Path = REPO_ROOT) -> bool | None:
    """True if the working tree has uncommitted changes relative to
    `HEAD` — a run against a dirty tree is still valid, but the report
    must say so, since "frozen" implies a specific, inspectable commit."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


@dataclass(frozen=True)
class ReproducibilityInfo:
    generated_at: str
    app_commit: str | None
    app_commit_dirty: bool | None
    app_version: str
    mode: str  # "fake" | "live"
    model: str
    reasoning_effort: str
    extraction_prompt_version: str = PROMPT_VERSION
    extraction_schema_version: str = SCHEMA_VERSION
    brief_prompt_version: str = BRIEF_PROMPT_VERSION
    meeting_prep_prompt_version: str = MEETING_PREP_PROMPT_VERSION
    brief_schema_version: str = BRIEF_SCHEMA_VERSION
    baseline_prompt_version: str = BASELINE_PROMPT_VERSION
    baseline_schema_version: str = BASELINE_SCHEMA_VERSION
    ground_truth_schema_version: str = GROUND_TRUTH_SCHEMA_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "app_commit": self.app_commit,
            "app_commit_dirty": self.app_commit_dirty,
            "app_version": self.app_version,
            "mode": self.mode,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "extraction_prompt_version": self.extraction_prompt_version,
            "extraction_schema_version": self.extraction_schema_version,
            "brief_prompt_version": self.brief_prompt_version,
            "meeting_prep_prompt_version": self.meeting_prep_prompt_version,
            "brief_schema_version": self.brief_schema_version,
            "baseline_prompt_version": self.baseline_prompt_version,
            "baseline_schema_version": self.baseline_schema_version,
            "ground_truth_schema_version": self.ground_truth_schema_version,
        }


def _app_version() -> str:
    try:
        import project_context

        return getattr(project_context, "__version__", "0.1.0")
    except ImportError:
        return "unknown"


def capture_reproducibility_info(
    *, mode: str, model: str, reasoning_effort: str, repo_root: Path = REPO_ROOT
) -> ReproducibilityInfo:
    return ReproducibilityInfo(
        generated_at=utc_now_iso(),
        app_commit=capture_git_commit(repo_root),
        app_commit_dirty=capture_git_dirty(repo_root),
        app_version=_app_version(),
        mode=mode,
        model=model,
        reasoning_effort=reasoning_effort,
    )
