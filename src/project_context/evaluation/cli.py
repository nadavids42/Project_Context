"""Orchestrates one full evaluation run: load the frozen corpora, run
both systems on all three projects, score, and write JSON + Markdown
reports (Section 13.7).

`scripts/run_evaluation.py` is the thin command-line entrypoint; this
module is the part that's actually tested (`tests/evaluation/`) and
reusable from a notebook/REPL without going through `argparse`.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from project_context.config import DEFAULT_OPENAI_MODEL
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.evaluation import scoring
from project_context.evaluation.baseline_runner import BaselineRunConfig, run_baseline_for_project
from project_context.evaluation.corpus_data import ALL_PROJECT_KEYS
from project_context.evaluation.ledger_runner import (
    EmptyBriefProvider,
    LedgerRunConfig,
    run_ledger_for_project,
)
from project_context.evaluation.materialize import DEFAULT_CORPUS_ROOT, load_corpus_project
from project_context.evaluation.report import (
    EvaluationReport,
    MeetingPrepTimingObservation,
    ReviewTimingInputs,
    build_report,
    render_markdown_report,
    write_json_report,
)
from project_context.evaluation.reproducibility import capture_reproducibility_info
from project_context.evaluation.runner_types import CheckpointRunResult
from project_context.llm.provider import DEFAULT_REASONING_EFFORT, LLMProvider
from project_context.timeutil import utc_now_iso

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "evaluation_runs"


@dataclass(frozen=True)
class RunOptions:
    corpus_root: Path = DEFAULT_CORPUS_ROOT
    output_dir: Path = DEFAULT_OUTPUT_DIR
    model: str = DEFAULT_OPENAI_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    #: `None` -> fake/deterministic mode. Given -> live-model mode, used
    #: for BOTH the ledger's extraction/brief calls and the baseline's
    #: one-shot calls (Section 13.4: "same model and reasoning
    #: configuration").
    live_provider: LLMProvider | None = None
    timings: ReviewTimingInputs = ReviewTimingInputs()
    prep_time_observations: tuple[MeetingPrepTimingObservation, ...] = ()
    project_keys: tuple[str, ...] = ALL_PROJECT_KEYS
    db_path: Path | None = None
    evidence_dir: Path | None = None


@dataclass(frozen=True)
class RawRunArtifacts:
    """Every raw output the evaluation procedure asks be preserved
    (Section 13.7 step 2/7: "raw predictions and scores saved")."""

    ledger_checkpoints: dict[str, tuple[CheckpointRunResult, ...]]
    baseline_checkpoints: dict[str, tuple[CheckpointRunResult, ...]]


def run_evaluation(options: RunOptions | None = None) -> tuple[EvaluationReport, RawRunArtifacts]:
    options = options or RunOptions()
    mode = "live" if options.live_provider is not None else "fake"
    reproducibility = capture_reproducibility_info(
        mode=mode, model=options.model, reasoning_effort=options.reasoning_effort
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = options.db_path or (Path(tmp_dir) / "evaluation.db")
        evidence_dir = options.evidence_dir or (Path(tmp_dir) / "evidence")
        evidence_dir.mkdir(parents=True, exist_ok=True)

        conn = connect(db_path)
        run_migrations(conn, REPO_ROOT / "migrations")

        ledger_scores: dict[str, scoring.ProjectScore] = {}
        baseline_scores: dict[str, scoring.ProjectScore] = {}
        ledger_checkpoints: dict[str, tuple[CheckpointRunResult, ...]] = {}
        baseline_checkpoints: dict[str, tuple[CheckpointRunResult, ...]] = {}
        leakage_texts: dict[str, tuple[str, ...]] = {}
        sentinels_by_project: dict[str, tuple[str, ...]] = {}

        try:
            for project_key in options.project_keys:
                project, chunk_target_chars = load_corpus_project(
                    project_key, root=options.corpus_root
                )

                ledger_config = LedgerRunConfig(
                    conn=conn,
                    evidence_dir=evidence_dir,
                    model=options.model,
                    reasoning_effort=options.reasoning_effort,
                    brief_provider=(
                        options.live_provider
                        if options.live_provider is not None
                        else EmptyBriefProvider()
                    ),
                    live_extraction_provider=options.live_provider,
                )
                ledger_result = run_ledger_for_project(
                    project, chunk_target_chars, config=ledger_config
                )

                baseline_config = BaselineRunConfig(
                    model=options.model,
                    reasoning_effort=options.reasoning_effort,
                    live_provider=options.live_provider,
                )
                baseline_result = run_baseline_for_project(project, config=baseline_config)

                ledger_checkpoints[project_key] = ledger_result.checkpoints
                baseline_checkpoints[project_key] = baseline_result.checkpoints

                ledger_scores[project_key] = scoring.score_project(
                    project,
                    {cp.checkpoint_id: cp for cp in ledger_result.checkpoints},
                    system="ledger",
                )
                baseline_scores[project_key] = scoring.score_project(
                    project,
                    {cp.checkpoint_id: cp for cp in baseline_result.checkpoints},
                    system="baseline",
                )

                all_texts = tuple(
                    (cp.markdown or "") + "\n" + "\n".join(c.text for c in cp.claims)
                    for cp in list(ledger_result.checkpoints) + list(baseline_result.checkpoints)
                )
                leakage_texts[project_key] = all_texts
                owner_names = {
                    transition.owner
                    for item in project.items
                    for transition in item.transitions
                    if transition.owner
                }
                sentinels_by_project[project_key] = (project.name,) + tuple(sorted(owner_names))
        finally:
            conn.close()

    leakage = scoring.check_cross_project_leakage(leakage_texts, sentinels_by_project)

    report = build_report(
        reproducibility=reproducibility,
        ledger_scores=ledger_scores,
        baseline_scores=baseline_scores,
        checkpoints_by_project=ledger_checkpoints,
        timings=options.timings,
        prep_time_observations=options.prep_time_observations,
        leakage_hits=leakage.leaked_sentinels,
    )
    raw = RawRunArtifacts(
        ledger_checkpoints=ledger_checkpoints, baseline_checkpoints=baseline_checkpoints
    )
    return report, raw


def _raw_checkpoints_dict(
    checkpoints_by_project: dict[str, tuple[CheckpointRunResult, ...]],
) -> dict:
    return {
        project_key: [
            {
                "checkpoint_id": cp.checkpoint_id,
                "brief_type": cp.brief_type,
                "system": cp.system,
                "markdown": cp.markdown,
                "input_tokens": cp.input_tokens,
                "output_tokens": cp.output_tokens,
                "estimated_cost_usd": cp.estimated_cost_usd,
                "latency_ms": cp.latency_ms,
                "accepted_without_edit": cp.accepted_without_edit,
                "edited_accepted": cp.edited_accepted,
                "rejected": cp.rejected,
                "claims": [
                    {
                        "section": c.section,
                        "text": c.text,
                        "claim_type": c.claim_type,
                        "item_kind": c.item_kind,
                        "item_title": c.item_title,
                        "status": c.status,
                        "owner": c.owner,
                        "due_date": c.due_date,
                        "evidence": [
                            {"artifact_id": e.artifact_id, "quote": e.quote} for e in c.evidence
                        ],
                    }
                    for c in cp.claims
                ],
            }
            for cp in checkpoints
        ]
        for project_key, checkpoints in checkpoints_by_project.items()
    }


def write_run_outputs(
    report: EvaluationReport, raw: RawRunArtifacts, *, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> Path:
    """Writes the JSON report, Markdown report, and raw ledger/baseline
    predictions into one timestamped run directory (Section 13.7 step 2:
    "raw predictions and scores saved"). Returns the run directory."""
    run_dir = (
        output_dir
        / f"{report.reproducibility.mode}-{utc_now_iso().replace(':', '').replace('.', '')}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    write_json_report(report, run_dir / "report.json")
    (run_dir / "report.md").write_text(render_markdown_report(report), encoding="utf-8")
    (run_dir / "raw_ledger_predictions.json").write_text(
        json.dumps(_raw_checkpoints_dict(raw.ledger_checkpoints), indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "raw_baseline_predictions.json").write_text(
        json.dumps(_raw_checkpoints_dict(raw.baseline_checkpoints), indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir


__all__ = ["RawRunArtifacts", "RunOptions", "run_evaluation", "write_run_outputs"]
