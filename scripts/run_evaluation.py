#!/usr/bin/env python3
"""Run the three-project evaluation harness (Section 13/14 W4.3).

Fake/deterministic mode (default, safe, no network, no cost):

    python scripts/run_evaluation.py

Live-model mode (Section 13.4's "explicitly invoked live-model mode") —
calls the real configured OpenAI model for every extraction and brief
composition call across all three projects and both systems. This
**costs real money** and requires `OPENAI_API_KEY` to be set. It is
never run implicitly — you must pass `--live` *and* type the
confirmation phrase this script prints, or pass `--yes-i-know-this-costs-money`
to skip the interactive prompt in a non-interactive context:

    OPENAI_API_KEY=... python scripts/run_evaluation.py --live

Supplying real review-timing / meeting-prep-timing observations (Section
13.5's "supplied/manual timing inputs") — JSON files, see
`--timings-help`:

    python scripts/run_evaluation.py --timings review_timings.json \\
        --prep-timings prep_timings.json

Output: writes `report.json`, `report.md`, and raw ledger/baseline
predictions into a new timestamped directory under
`data/evaluation_runs/` (gitignored) and prints the verdict + path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from project_context.config import DEFAULT_OPENAI_MODEL  # noqa: E402
from project_context.evaluation.cli import (  # noqa: E402
    RunOptions,
    run_evaluation,
    write_run_outputs,
)
from project_context.evaluation.corpus_data import ALL_PROJECT_KEYS  # noqa: E402
from project_context.evaluation.report import (  # noqa: E402
    MeetingPrepTimingObservation,
    ReviewTimingInputs,
)
from project_context.llm.provider import DEFAULT_REASONING_EFFORT  # noqa: E402

_TIMINGS_HELP = """
--timings <file.json>: a JSON object of real observed review-action
seconds, e.g.:
    {"seconds_per_accept": 6.0, "seconds_per_edit": 22.0, "seconds_per_reject": 12.0}

--prep-timings <file.json>: a JSON list of real observed meeting-prep
timing pairs, e.g.:
    [{"checkpoint_id": "impl-cp-before-w2", "baseline_minutes": 25.0, "system_minutes": 8.0}, ...]

Without these, review-burden figures use a documented ASSUMED timing
model (see project_context.evaluation.report.DEFAULT_SECONDS_PER_*) and
meeting-prep-time-saved is reported as "not measured."
""".strip()


def _load_review_timings(path: Path | None) -> ReviewTimingInputs:
    if path is None:
        return ReviewTimingInputs()
    data = json.loads(path.read_text(encoding="utf-8"))
    return ReviewTimingInputs(
        seconds_per_accept=data.get("seconds_per_accept", ReviewTimingInputs().seconds_per_accept),
        seconds_per_edit=data.get("seconds_per_edit", ReviewTimingInputs().seconds_per_edit),
        seconds_per_reject=data.get("seconds_per_reject", ReviewTimingInputs().seconds_per_reject),
        is_measured=True,
    )


def _load_prep_timings(path: Path | None) -> tuple[MeetingPrepTimingObservation, ...]:
    if path is None:
        return ()
    data = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        MeetingPrepTimingObservation(
            checkpoint_id=row["checkpoint_id"],
            baseline_minutes=float(row["baseline_minutes"]),
            system_minutes=float(row["system_minutes"]),
        )
        for row in data
    )


def _build_live_provider():
    # The provider itself is model-agnostic (project_context.llm.provider.
    # LLMProvider) — the model is selected per call via ModelConfig, which
    # RunOptions.model already threads through every runner call.
    from project_context.services.extraction import build_default_provider

    provider = build_default_provider()
    if provider is None:
        print(
            "ERROR: --live requires OPENAI_API_KEY to be set in the environment.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return provider


def _confirm_live_run(model: str, project_keys: tuple[str, ...]) -> None:
    print("=" * 72)
    print("LIVE-MODEL EVALUATION — this calls the real OpenAI API and costs money.")
    print(f"Model: {model}")
    print(f"Projects: {', '.join(project_keys)}")
    print(
        "Every extraction call (one per chunk) and every brief-composition call "
        "(one per checkpoint per system) across all selected projects will hit "
        "the real API. See Section 12.7 for the per-call cost-abort default ($1)."
    )
    print("=" * 72)
    phrase = "RUN LIVE EVALUATION"
    typed = input(f"Type '{phrase}' to proceed, or anything else to abort: ").strip()
    if typed != phrase:
        print("Aborted.")
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--live", action="store_true", help="Use the real configured OpenAI model (costs money)."
    )
    parser.add_argument(
        "--yes-i-know-this-costs-money",
        action="store_true",
        help="Skip the interactive --live confirmation prompt (for non-interactive/CI use only).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_OPENAI_MODEL,
        help=f"Model to use (default: {DEFAULT_OPENAI_MODEL}).",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=DEFAULT_REASONING_EFFORT,
        help=f"Reasoning effort (default: {DEFAULT_REASONING_EFFORT}).",
    )
    parser.add_argument(
        "--projects",
        nargs="+",
        choices=ALL_PROJECT_KEYS,
        default=list(ALL_PROJECT_KEYS),
        help="Subset of projects to run (default: all three).",
    )
    parser.add_argument(
        "--timings", type=Path, default=None, help="JSON file of real review-timing observations."
    )
    parser.add_argument(
        "--prep-timings",
        type=Path,
        default=None,
        help="JSON file of real meeting-prep timing observations.",
    )
    parser.add_argument(
        "--timings-help",
        action="store_true",
        help="Print the --timings/--prep-timings file format and exit.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Override the output directory (default: data/evaluation_runs/).",
    )
    args = parser.parse_args(argv)

    if args.timings_help:
        print(_TIMINGS_HELP)
        return 0

    project_keys = tuple(args.projects)
    live_provider = None
    if args.live:
        if not args.yes_i_know_this_costs_money:
            _confirm_live_run(args.model, project_keys)
        live_provider = _build_live_provider()

    options = RunOptions(
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        live_provider=live_provider,
        timings=_load_review_timings(args.timings),
        prep_time_observations=_load_prep_timings(args.prep_timings),
        project_keys=project_keys,
    )
    report, raw = run_evaluation(options)

    kwargs = {} if args.output_dir is None else {"output_dir": args.output_dir}
    run_dir = write_run_outputs(report, raw, **kwargs)

    print(f"\nMode: {report.reproducibility.mode}")
    print(f"Verdict: {report.verdict}")
    for line in report.verdict_rationale:
        print(f"  - {line}")
    print(f"\nFull report written to: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
