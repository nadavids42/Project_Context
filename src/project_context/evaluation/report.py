"""JSON + Markdown report generation (Section 13.7 step 7: "Publish
per-type confusion matrix, raw errors, correction recurrence, cost,
latency, and review time"; step 8: "Make a go/iterate/stop decision").

Section 13.6's thresholds are encoded here **only as report
annotations** — a `ThresholdCheck` records what was measured against
what number and whether it passed, but nothing in this module or
anywhere else in the evaluation harness raises, fails a test, or blocks
a build based on them (Prompt 15: "not test assertions that tempt
implementation gaming"). The verdict this module computes
(`build_verdict`) is a mechanical summary of those same annotations,
offered as a starting point for a human decision — not a substitute for
reading the per-project detail, the individually-listed misleading
claims, and (Section 13.6's hold/iterate note) the raw counts a
three-project sample cannot make statistically definitive.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass

from project_context.evaluation.reproducibility import ReproducibilityInfo
from project_context.evaluation.runner_types import CheckpointRunResult
from project_context.evaluation.scoring import (
    Metric,
    PooledMetrics,
    ProjectScore,
    add_metrics,
    zero_metric,
)

#: Documented, assumed review-time-per-action model (Section 13.5:
#: "Median active review minutes/sync and seconds/accepted item").
#: These are NOT observed human timings — this harness runs a scripted
#: reviewer, not a timed human — so review-burden figures computed from
#: them are explicitly labeled "assumed" in the report unless real
#: timings are supplied (see `ReviewTimingInputs`).
DEFAULT_SECONDS_PER_ACCEPT = 8.0
DEFAULT_SECONDS_PER_EDIT = 25.0
DEFAULT_SECONDS_PER_REJECT = 15.0


@dataclass(frozen=True)
class ReviewTimingInputs:
    """Either the documented assumed per-action seconds (`is_measured=
    False`, the harness default) or real human-observed seconds per
    action, supplied by the caller (`is_measured=True`) — Section 13.5:
    "review burden... include rejections/edits."""

    seconds_per_accept: float = DEFAULT_SECONDS_PER_ACCEPT
    seconds_per_edit: float = DEFAULT_SECONDS_PER_EDIT
    seconds_per_reject: float = DEFAULT_SECONDS_PER_REJECT
    is_measured: bool = False


@dataclass(frozen=True)
class MeetingPrepTimingObservation:
    checkpoint_id: str
    baseline_minutes: float
    system_minutes: float


@dataclass(frozen=True)
class ThresholdCheck:
    name: str
    description: str
    threshold_text: str
    observed_text: str
    #: `None` = not evaluable this run (e.g. no supplied manual timing
    #: input) — reported as "not measured," never silently passed.
    passed: bool | None


def _pct(metric: Metric) -> str:
    if metric.value is None:
        return "n/a (0/0)"
    return f"{metric.value * 100:.1f}% ({metric.numerator}/{metric.denominator})"


def _check(
    name: str,
    description: str,
    threshold_text: str,
    observed: float | None,
    *,
    ge: float | None = None,
    le: float | None = None,
) -> ThresholdCheck:
    if observed is None:
        return ThresholdCheck(name, description, threshold_text, "n/a (0/0)", None)
    passed = True
    if ge is not None:
        passed = passed and observed >= ge
    if le is not None:
        passed = passed and observed <= le
    return ThresholdCheck(name, description, threshold_text, f"{observed * 100:.1f}%", passed)


def review_burden_for_project(
    pooled: PooledMetrics, num_checkpoints: int, timings: ReviewTimingInputs
) -> dict:
    total_actions = pooled.accepted_without_edit + pooled.edited_accepted + pooled.rejected
    total_seconds = (
        pooled.accepted_without_edit * timings.seconds_per_accept
        + pooled.edited_accepted * timings.seconds_per_edit
        + pooled.rejected * timings.seconds_per_reject
    )
    minutes_per_sync = (total_seconds / 60.0 / num_checkpoints) if num_checkpoints else None
    seconds_per_accepted_item = (
        total_seconds / (pooled.accepted_without_edit + pooled.edited_accepted)
        if (pooled.accepted_without_edit + pooled.edited_accepted)
        else None
    )
    return {
        "accepted_without_edit": pooled.accepted_without_edit,
        "edited_accepted": pooled.edited_accepted,
        "rejected": pooled.rejected,
        "total_actions": total_actions,
        "minutes_per_sync": minutes_per_sync,
        "seconds_per_accepted_item": seconds_per_accepted_item,
        "is_measured": timings.is_measured,
    }


def meeting_prep_time_saved(observations: tuple[MeetingPrepTimingObservation, ...]) -> dict | None:
    if not observations:
        return None
    deltas = [o.baseline_minutes - o.system_minutes for o in observations]
    pct = [
        (d / o.baseline_minutes * 100.0) if o.baseline_minutes else None
        for d, o in zip(deltas, observations, strict=True)
    ]
    pct_clean = [p for p in pct if p is not None]
    return {
        "n": len(observations),
        "median_minutes_saved": statistics.median(deltas),
        "median_percent_saved": statistics.median(pct_clean) if pct_clean else None,
        "observations": [
            {
                "checkpoint_id": o.checkpoint_id,
                "baseline_minutes": o.baseline_minutes,
                "system_minutes": o.system_minutes,
                "minutes_saved": o.baseline_minutes - o.system_minutes,
            }
            for o in observations
        ],
    }


@dataclass(frozen=True)
class EvaluationReport:
    reproducibility: ReproducibilityInfo
    project_keys: tuple[str, ...]
    ledger_scores: dict[str, ProjectScore]
    baseline_scores: dict[str, ProjectScore]
    ledger_composite: PooledMetrics
    baseline_composite: PooledMetrics
    review_burden_by_project: dict[str, dict]
    review_burden_composite: dict
    meeting_prep_time_saved: dict | None
    leakage_sentinel_hits: tuple[str, ...]
    threshold_checks: tuple[ThresholdCheck, ...]
    verdict: str
    verdict_rationale: tuple[str, ...]


def _pool_across_projects(scores: dict[str, ProjectScore]) -> PooledMetrics:
    item_precision = zero_metric()
    item_recall = zero_metric()
    field_kind = zero_metric()
    field_status = zero_metric()
    field_owner = zero_metric()
    field_due_date = zero_metric()
    evidence_correctness = zero_metric()
    unsupported = zero_metric()
    transition = zero_metric()
    current_state = zero_metric()
    total_input_tokens = total_output_tokens = total_latency_ms = 0
    total_cost = 0.0
    accepted = edited = rejected = 0
    misleading: list = []
    for score in scores.values():
        pooled = score.pooled()
        item_precision = add_metrics(item_precision, pooled.item_precision)
        item_recall = add_metrics(item_recall, pooled.item_recall)
        field_kind = add_metrics(field_kind, pooled.field_accuracy_kind)
        field_status = add_metrics(field_status, pooled.field_accuracy_status)
        field_owner = add_metrics(field_owner, pooled.field_accuracy_owner)
        field_due_date = add_metrics(field_due_date, pooled.field_accuracy_due_date)
        evidence_correctness = add_metrics(evidence_correctness, pooled.evidence_correctness)
        unsupported = add_metrics(unsupported, pooled.unsupported_claim_rate)
        transition = add_metrics(transition, pooled.transition_accuracy)
        current_state = add_metrics(current_state, pooled.current_state_accuracy)
        total_input_tokens += pooled.total_input_tokens
        total_output_tokens += pooled.total_output_tokens
        total_latency_ms += pooled.total_latency_ms
        total_cost += pooled.total_cost_usd
        accepted += pooled.accepted_without_edit
        edited += pooled.edited_accepted
        rejected += pooled.rejected
        misleading.extend(pooled.misleading)
    return PooledMetrics(
        item_precision=item_precision,
        item_recall=item_recall,
        field_accuracy_kind=field_kind,
        field_accuracy_status=field_status,
        field_accuracy_owner=field_owner,
        field_accuracy_due_date=field_due_date,
        evidence_correctness=evidence_correctness,
        unsupported_claim_rate=unsupported,
        transition_accuracy=transition,
        current_state_accuracy=current_state,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_cost_usd=total_cost,
        total_latency_ms=total_latency_ms,
        accepted_without_edit=accepted,
        edited_accepted=edited,
        rejected=rejected,
        misleading=tuple(misleading),
    )


def build_threshold_checks(
    ledger_composite: PooledMetrics,
    baseline_composite: PooledMetrics,
    ledger_scores: dict[str, ProjectScore],
    review_burden_composite: dict,
    prep_time_saved: dict | None,
    leakage_hits: tuple[str, ...],
) -> tuple[ThresholdCheck, ...]:
    checks: list[ThresholdCheck] = []

    checks.append(
        _check(
            "item_precision",
            "Material item precision, overall (ledger)",
            ">= 90%",
            ledger_composite.item_precision.value,
            ge=0.90,
        )
    )
    per_project_recall = {
        key: score.pooled().item_recall.value for key, score in ledger_scores.items()
    }
    overall_recall_ok = (
        ledger_composite.item_recall.value is not None
        and ledger_composite.item_recall.value >= 0.80
    )
    no_project_below_75 = all(v is None or v >= 0.75 for v in per_project_recall.values())
    checks.append(
        ThresholdCheck(
            "item_recall",
            "Material item recall, overall >=80%, no project <75% (ledger)",
            ">= 80% overall, >= 75% every project",
            f"overall={_pct(ledger_composite.item_recall)}; per-project="
            + ", ".join(
                f"{k}={'n/a' if v is None else f'{v * 100:.1f}%'}"
                for k, v in per_project_recall.items()
            ),
            (overall_recall_ok and no_project_below_75)
            if ledger_composite.item_recall.value is not None
            else None,
        )
    )
    checks.append(
        _check(
            "evidence_correctness",
            "Evidence correctness (ledger)",
            ">= 95%",
            ledger_composite.evidence_correctness.value,
            ge=0.95,
        )
    )
    checks.append(
        _check(
            "unsupported_claim_rate",
            "Unsupported material claim rate (ledger)",
            "<= 2%",
            ledger_composite.unsupported_claim_rate.value,
            le=0.02,
        )
    )
    per_project_misleading_rate = {}
    for key, score in ledger_scores.items():
        pooled = score.pooled()
        total_factual = pooled.evidence_correctness.denominator
        rate = (len(pooled.misleading) / total_factual) if total_factual else None
        per_project_misleading_rate[key] = rate
    misleading_ok = all(v is None or v < 0.05 for v in per_project_misleading_rate.values())
    checks.append(
        ThresholdCheck(
            "materially_misleading_rate",
            "Materially misleading item rate, every project (ledger)",
            "< 5% every project",
            (
                ", ".join(
                    f"{k}={'n/a' if v is None else f'{v * 100:.1f}%'}"
                    for k, v in per_project_misleading_rate.items()
                )
                or "n/a (0 projects)"
            ),
            misleading_ok,
        )
    )
    checks.append(
        _check(
            "current_state_accuracy",
            "Current-state/supersession accuracy (ledger)",
            ">= 85%",
            ledger_composite.current_state_accuracy.value,
            ge=0.85,
        )
    )
    checks.append(
        ThresholdCheck(
            "project_assignment_error",
            "Project assignment error rate",
            "<= 1%",
            "n/a — this harness assigns each artifact to its project deterministically "
            "at ingestion (Section 12.1: no LLM-based auto-assignment in MVP); not exercised "
            "by this evaluation",
            None,
        )
    )
    checks.append(
        ThresholdCheck(
            "cross_project_leakage",
            "Cross-project leakage",
            "= 0",
            f"{len(leakage_hits)} sentinel hit(s)" if leakage_hits else "0",
            len(leakage_hits) == 0,
        )
    )
    checks.append(
        _check(
            "acceptance_rate",
            "Overall proposal acceptance rate (ledger)",
            ">= 80%",
            ledger_composite.acceptance_rate.value,
            ge=0.80,
        )
    )
    checks.append(
        _check(
            "edit_free_acceptance_rate",
            "Accepted without edits (ledger)",
            ">= 65%",
            ledger_composite.edit_free_acceptance_rate.value,
            ge=0.65,
        )
    )
    minutes_per_sync = review_burden_composite.get("minutes_per_sync")
    seconds_per_item = review_burden_composite.get("seconds_per_accepted_item")
    is_measured = review_burden_composite.get("is_measured")
    measured_note = "" if is_measured else " [ASSUMED timing model, not observed]"
    checks.append(
        ThresholdCheck(
            "review_burden_minutes_per_sync",
            "Median review burden, minutes/sync (ledger)",
            "<= 5 min/sync",
            (
                f"{minutes_per_sync:.2f} min/sync{measured_note}"
                if minutes_per_sync is not None
                else "n/a"
            ),
            (minutes_per_sync <= 5.0) if minutes_per_sync is not None else None,
        )
    )
    checks.append(
        ThresholdCheck(
            "review_burden_seconds_per_item",
            "Median review burden, seconds/accepted item (ledger)",
            "<= 20 sec/item",
            (
                f"{seconds_per_item:.1f} sec/item{measured_note}"
                if seconds_per_item is not None
                else "n/a"
            ),
            (seconds_per_item <= 20.0) if seconds_per_item is not None else None,
        )
    )
    if prep_time_saved is None:
        checks.append(
            ThresholdCheck(
                "meeting_prep_time_saved",
                "Median meeting-prep time reduced",
                ">= 50% and >= 10 minutes",
                "n/a — no manual timing inputs supplied (see scripts/run_evaluation.py --timings)",
                None,
            )
        )
    else:
        pct_val = prep_time_saved.get("median_percent_saved")
        min_val = prep_time_saved.get("median_minutes_saved")
        ok = (pct_val is not None and pct_val >= 50.0) and (min_val is not None and min_val >= 10.0)
        checks.append(
            ThresholdCheck(
                "meeting_prep_time_saved",
                "Median meeting-prep time reduced",
                ">= 50% and >= 10 minutes",
                f"{min_val:.1f} min ({pct_val:.1f}%)"
                if min_val is not None and pct_val is not None
                else "n/a",
                ok,
            )
        )
    current_state_delta = None
    if (
        ledger_composite.current_state_accuracy.value is not None
        and baseline_composite.current_state_accuracy.value is not None
    ):
        current_state_delta = (
            ledger_composite.current_state_accuracy.value
            - baseline_composite.current_state_accuracy.value
        )
    unsupported_cut = None
    if (
        ledger_composite.unsupported_claim_rate.value is not None
        and baseline_composite.unsupported_claim_rate.value is not None
        and baseline_composite.unsupported_claim_rate.value > 0
    ):
        unsupported_cut = 1 - (
            ledger_composite.unsupported_claim_rate.value
            / baseline_composite.unsupported_claim_rate.value
        )
    beats_baseline = (current_state_delta is not None and current_state_delta >= 0.10) or (
        unsupported_cut is not None and unsupported_cut >= 0.50
    )
    delta_text = "n/a" if current_state_delta is None else f"{current_state_delta * 100:+.1f}pp"
    cut_text = "n/a" if unsupported_cut is None else f"{unsupported_cut * 100:.1f}%"
    checks.append(
        ThresholdCheck(
            "beats_baseline",
            "Ledger beats baseline: current-state +10pp OR unsupported-claims -50%",
            ">= +10pp current-state accuracy OR >= 50% fewer unsupported claims",
            f"current_state_delta={delta_text}, unsupported_cut={cut_text}",
            beats_baseline
            if (current_state_delta is not None or unsupported_cut is not None)
            else None,
        )
    )
    return tuple(checks)


def build_verdict(checks: tuple[ThresholdCheck, ...], *, mode: str) -> tuple[str, tuple[str, ...]]:
    rationale: list[str] = []

    hard_stop = False
    beats_baseline_check = next((c for c in checks if c.name == "beats_baseline"), None)
    if beats_baseline_check is not None and beats_baseline_check.passed is False:
        hard_stop = True
        rationale.append(
            "No-go (Section 13.6): the ledger does not beat the recent-context baseline on "
            "current-state accuracy or unsupported-claim reduction."
        )
    leakage_check = next((c for c in checks if c.name == "cross_project_leakage"), None)
    if leakage_check is not None and leakage_check.passed is False:
        hard_stop = True
        rationale.append(
            f"No-go (Section 13.6): cross-project leakage detected — {leakage_check.observed_text}."
        )
    misleading_check = next((c for c in checks if c.name == "materially_misleading_rate"), None)
    if misleading_check is not None and misleading_check.passed is False:
        hard_stop = True
        rationale.append(
            f"No-go (Section 13.6): materially misleading item rate reached 5% in at least one "
            f"project — {misleading_check.observed_text}."
        )

    if hard_stop:
        if mode != "live":
            rationale.append(
                "NOTE: this run used fake/deterministic mode. Fake-mode baseline output is a "
                "hand-authored illustrative fixture, not a model prediction (see "
                "project_context.evaluation.baseline_runner's module docstring) — this verdict is "
                "NOT a real product decision. Run with --live before treating any STOP/GO here as "
                "final."
            )
        return "STOP", tuple(rationale)

    evaluable = [c for c in checks if c.passed is not None]
    failed = [c for c in evaluable if not c.passed]
    not_evaluable = [c for c in checks if c.passed is None]

    if not failed and not not_evaluable:
        verdict = "GO"
        rationale.append("All Section 13.6 go-criteria thresholds were met.")
    elif not failed:
        verdict = "ITERATE"
        rationale.append(
            "No go-criteria threshold failed outright, but "
            f"{len(not_evaluable)} threshold(s) could not be evaluated this run "
            f"({', '.join(c.name for c in not_evaluable)}) — supply manual timing inputs and/or "
            "run live-model mode before calling this GO."
        )
    else:
        verdict = "ITERATE"
        rationale.append(
            f"{len(failed)} of {len(evaluable)} evaluable go-criteria threshold(s) failed: "
            + "; ".join(
                f"{c.name} (observed {c.observed_text}, needs {c.threshold_text})" for c in failed
            )
        )
        rationale.append(
            "Section 13.6: proceed with another internal iteration only if evidence correctness "
            "is high but recall, match rules, or review UX miss one threshold with a diagnosed "
            "fix — see per-project detail and the individually-listed misleading claims below."
        )

    rationale.append(
        "Section 13.6: a three-project sample is directional product evidence, not statistical "
        "proof — treat this verdict as a starting point for a human decision, not a final answer."
    )
    if mode != "live":
        rationale.append(
            "NOTE: this run used fake/deterministic mode. Fake-mode baseline output is a "
            "hand-authored illustrative fixture, not a model prediction — this verdict is a "
            "mechanism self-test, NOT a product decision. Run with --live before acting on it."
        )
    return verdict, tuple(rationale)


def build_report(
    *,
    reproducibility: ReproducibilityInfo,
    ledger_scores: dict[str, ProjectScore],
    baseline_scores: dict[str, ProjectScore],
    checkpoints_by_project: dict[str, tuple[CheckpointRunResult, ...]],
    timings: ReviewTimingInputs,
    prep_time_observations: tuple[MeetingPrepTimingObservation, ...] = (),
    leakage_hits: tuple[str, ...] = (),
) -> EvaluationReport:
    project_keys = tuple(sorted(ledger_scores))
    ledger_composite = _pool_across_projects(ledger_scores)
    baseline_composite = _pool_across_projects(baseline_scores)

    review_burden_by_project = {
        key: review_burden_for_project(
            ledger_scores[key].pooled(), len(ledger_scores[key].checkpoints), timings
        )
        for key in project_keys
    }
    total_checkpoints = sum(len(s.checkpoints) for s in ledger_scores.values())
    review_burden_composite = review_burden_for_project(
        ledger_composite, total_checkpoints, timings
    )

    prep_time_saved = meeting_prep_time_saved(prep_time_observations)

    checks = build_threshold_checks(
        ledger_composite,
        baseline_composite,
        ledger_scores,
        review_burden_composite,
        prep_time_saved,
        leakage_hits,
    )
    verdict, rationale = build_verdict(checks, mode=reproducibility.mode)

    return EvaluationReport(
        reproducibility=reproducibility,
        project_keys=project_keys,
        ledger_scores=ledger_scores,
        baseline_scores=baseline_scores,
        ledger_composite=ledger_composite,
        baseline_composite=baseline_composite,
        review_burden_by_project=review_burden_by_project,
        review_burden_composite=review_burden_composite,
        meeting_prep_time_saved=prep_time_saved,
        leakage_sentinel_hits=leakage_hits,
        threshold_checks=checks,
        verdict=verdict,
        verdict_rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Serialization.
# ---------------------------------------------------------------------------


def _metric_dict(metric: Metric) -> dict:
    return {"numerator": metric.numerator, "denominator": metric.denominator, "value": metric.value}


def _pooled_dict(pooled: PooledMetrics) -> dict:
    return {
        "item_precision": _metric_dict(pooled.item_precision),
        "item_recall": _metric_dict(pooled.item_recall),
        "field_accuracy_kind": _metric_dict(pooled.field_accuracy_kind),
        "field_accuracy_status": _metric_dict(pooled.field_accuracy_status),
        "field_accuracy_owner": _metric_dict(pooled.field_accuracy_owner),
        "field_accuracy_due_date": _metric_dict(pooled.field_accuracy_due_date),
        "field_accuracy_combined": _metric_dict(pooled.field_accuracy_combined),
        "evidence_correctness": _metric_dict(pooled.evidence_correctness),
        "unsupported_claim_rate": _metric_dict(pooled.unsupported_claim_rate),
        "transition_accuracy": _metric_dict(pooled.transition_accuracy),
        "current_state_accuracy": _metric_dict(pooled.current_state_accuracy),
        "acceptance_rate": _metric_dict(pooled.acceptance_rate),
        "edit_free_acceptance_rate": _metric_dict(pooled.edit_free_acceptance_rate),
        "total_input_tokens": pooled.total_input_tokens,
        "total_output_tokens": pooled.total_output_tokens,
        "total_cost_usd": pooled.total_cost_usd,
        "total_latency_ms": pooled.total_latency_ms,
        "accepted_without_edit": pooled.accepted_without_edit,
        "edited_accepted": pooled.edited_accepted,
        "rejected": pooled.rejected,
        "misleading_count": len(pooled.misleading),
    }


def _checkpoint_dict(cp) -> dict:  # noqa: ANN001
    return {
        "checkpoint_id": cp.checkpoint_id,
        "brief_type": cp.brief_type,
        "system": cp.system,
        "item_precision": _metric_dict(cp.item_precision),
        "item_recall": _metric_dict(cp.item_recall),
        "field_accuracy_kind": _metric_dict(cp.field_accuracy_kind),
        "field_accuracy_status": _metric_dict(cp.field_accuracy_status),
        "field_accuracy_owner": _metric_dict(cp.field_accuracy_owner),
        "field_accuracy_due_date": _metric_dict(cp.field_accuracy_due_date),
        "evidence_correctness": _metric_dict(cp.evidence_correctness),
        "unsupported_claim_rate": _metric_dict(cp.unsupported_claim_rate),
        "transition_accuracy": _metric_dict(cp.transition_accuracy),
        "current_state_accuracy": _metric_dict(cp.current_state_accuracy),
        "input_tokens": cp.input_tokens,
        "output_tokens": cp.output_tokens,
        "estimated_cost_usd": cp.estimated_cost_usd,
        "latency_ms": cp.latency_ms,
        "accepted_without_edit": cp.accepted_without_edit,
        "edited_accepted": cp.edited_accepted,
        "rejected": cp.rejected,
        "misleading": [
            {
                "claim_text": m.claim_text,
                "section": m.section,
                "kind": m.kind,
                "item_title": m.item_title,
                "expected": m.expected,
                "actual": m.actual,
            }
            for m in cp.misleading
        ],
    }


def report_to_json_dict(report: EvaluationReport) -> dict:
    return {
        "reproducibility": report.reproducibility.as_dict(),
        "project_keys": list(report.project_keys),
        "per_project": {
            key: {
                "ledger": {
                    "composite": _pooled_dict(report.ledger_scores[key].pooled()),
                    "checkpoints": [
                        _checkpoint_dict(cp) for cp in report.ledger_scores[key].checkpoints
                    ],
                },
                "baseline": {
                    "composite": _pooled_dict(report.baseline_scores[key].pooled()),
                    "checkpoints": [
                        _checkpoint_dict(cp) for cp in report.baseline_scores[key].checkpoints
                    ],
                },
                "review_burden": report.review_burden_by_project[key],
            }
            for key in report.project_keys
        },
        "composite": {
            "ledger": _pooled_dict(report.ledger_composite),
            "baseline": _pooled_dict(report.baseline_composite),
        },
        "review_burden_composite": report.review_burden_composite,
        "meeting_prep_time_saved": report.meeting_prep_time_saved,
        "leakage_sentinel_hits": list(report.leakage_sentinel_hits),
        "threshold_checks": [
            {
                "name": c.name,
                "description": c.description,
                "threshold": c.threshold_text,
                "observed": c.observed_text,
                "passed": c.passed,
            }
            for c in report.threshold_checks
        ],
        "verdict": report.verdict,
        "verdict_rationale": list(report.verdict_rationale),
    }


def write_json_report(report: EvaluationReport, path) -> None:  # noqa: ANN001
    path.write_text(
        json.dumps(report_to_json_dict(report), indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


def _fmt_metric(metric: Metric) -> str:
    if metric.value is None:
        return "n/a"
    return f"{metric.value * 100:.1f}% ({metric.numerator}/{metric.denominator})"


def render_markdown_report(report: EvaluationReport) -> str:
    lines: list[str] = []
    lines.append("# Project Context — Three-Project Benchmark Report")
    lines.append("")
    lines.append(
        f"**Verdict: `{report.verdict}`** — generated {report.reproducibility.generated_at} "
        f"(mode: {report.reproducibility.mode})"
    )
    lines.append("")
    if report.reproducibility.mode != "live":
        lines.append(
            "> **This run used fake/deterministic mode.** The baseline system's output is a "
            "hand-authored, deliberately imperfect fixture illustrating known failure modes — "
            "**not** a real model's prediction. This report is a mechanism self-test proving the "
            "harness computes every required metric correctly and reproducibly. It is **not** the "
            "product decision described in Section 13. Run `scripts/run_evaluation.py --live` "
            "(after explicit confirmation; costs real API usage) for the evaluation that actually "
            "answers Section 13.1's question."
        )
        lines.append("")
    lines.append(
        "Per Section 13.6: a three-project synthetic sample is **directional product evidence, "
        "not statistical proof**. Read the per-project breakdown and the individually-listed "
        "misleading/unsupported claims below before treating any single number as decisive."
    )
    lines.append("")

    lines.append("## Reproducibility")
    lines.append("")
    repro = report.reproducibility
    lines.append(
        f"- App commit: `{repro.app_commit or 'unknown'}`"
        + (" (dirty working tree)" if repro.app_commit_dirty else "")
    )
    lines.append(f"- App version: `{repro.app_version}`")
    lines.append(f"- Model: `{repro.model}` (reasoning effort: `{repro.reasoning_effort}`)")
    lines.append(
        f"- Extraction prompt/schema: `{repro.extraction_prompt_version}` / "
        f"`{repro.extraction_schema_version}`"
    )
    lines.append(
        f"- Brief/meeting-prep prompt/schema: `{repro.brief_prompt_version}` / "
        f"`{repro.meeting_prep_prompt_version}` / `{repro.brief_schema_version}`"
    )
    lines.append(
        f"- Baseline prompt/schema: `{repro.baseline_prompt_version}` / "
        f"`{repro.baseline_schema_version}`"
    )
    lines.append(f"- Ground-truth schema: `{repro.ground_truth_schema_version}`")
    lines.append("")

    lines.append("## Go / iterate / stop thresholds (Section 13.6) — annotations only")
    lines.append("")
    lines.append("| Threshold | Required | Observed | Passed |")
    lines.append("|---|---|---|---|")
    for check in report.threshold_checks:
        mark = "✅" if check.passed else ("❌" if check.passed is False else "—")
        lines.append(
            f"| {check.description} | {check.threshold_text} | {check.observed_text} | {mark} |"
        )
    lines.append("")
    lines.append("**Rationale:**")
    for line in report.verdict_rationale:
        lines.append(f"- {line}")
    lines.append("")

    lines.append("## Composite (all three projects pooled) — ledger vs. baseline")
    lines.append("")
    lines.append("| Metric | Ledger | Baseline |")
    lines.append("|---|---|---|")
    lc, bc = report.ledger_composite, report.baseline_composite
    for label, attr in (
        ("Item precision", "item_precision"),
        ("Item recall", "item_recall"),
        ("Field accuracy — kind/type", "field_accuracy_kind"),
        ("Field accuracy — status", "field_accuracy_status"),
        ("Field accuracy — owner", "field_accuracy_owner"),
        ("Field accuracy — due date", "field_accuracy_due_date"),
        ("Field accuracy — combined", "field_accuracy_combined"),
        ("Evidence correctness", "evidence_correctness"),
        ("Unsupported-claim rate", "unsupported_claim_rate"),
        ("Completion/cancel/update/supersession accuracy", "transition_accuracy"),
        ("Current-state accuracy", "current_state_accuracy"),
    ):
        lines.append(
            f"| {label} | {_fmt_metric(getattr(lc, attr))} | {_fmt_metric(getattr(bc, attr))} |"
        )
    lines.append(
        f"| Total tokens (in/out) | {lc.total_input_tokens}/{lc.total_output_tokens} | "
        f"{bc.total_input_tokens}/{bc.total_output_tokens} |"
    )
    lines.append(
        f"| Total estimated cost (USD) | ${lc.total_cost_usd:.4f} | ${bc.total_cost_usd:.4f} |"
    )
    lines.append(f"| Total latency (ms) | {lc.total_latency_ms} | {bc.total_latency_ms} |")
    lines.append(
        f"| Materially misleading/unsupported claims (count) | {len(lc.misleading)} | "
        f"{len(bc.misleading)} |"
    )
    lines.append("")
    lines.append(
        f"Ledger-only: acceptance rate {_fmt_metric(lc.acceptance_rate)}, edit-free acceptance "
        f"rate {_fmt_metric(lc.edit_free_acceptance_rate)}."
    )
    lines.append("")

    lines.append("## Review burden (ledger only)")
    lines.append("")
    rb = report.review_burden_composite
    measured_note = (
        "" if rb.get("is_measured") else " — **assumed** timing model, not observed human time"
    )
    lines.append(
        f"- {rb['accepted_without_edit']} accepted without edit, {rb['edited_accepted']} "
        f"edited-and-accepted, {rb['rejected']} rejected ({rb['total_actions']} total review "
        f"actions across {len(report.project_keys)} projects)"
    )
    if rb.get("minutes_per_sync") is not None:
        lines.append(
            f"- Minutes per sync: {rb['minutes_per_sync']:.2f}{measured_note}; "
            f"seconds per accepted item: {rb['seconds_per_accepted_item']:.1f}{measured_note}"
        )
    lines.append("")

    if report.meeting_prep_time_saved is not None:
        mp = report.meeting_prep_time_saved
        pct_saved = mp["median_percent_saved"]
        pct_saved_text = "n/a" if pct_saved is None else f"{pct_saved:.1f}%"
        lines.append("## Meeting-preparation time saved (supplied manual timing inputs)")
        lines.append("")
        lines.append(
            f"- n={mp['n']}; median minutes saved: {mp['median_minutes_saved']:.1f}; "
            f"median percent saved: {pct_saved_text}"
        )
        lines.append("")
    else:
        lines.append("## Meeting-preparation time saved")
        lines.append("")
        lines.append(
            "Not measured this run — requires supplied manual timing observations "
            "(`scripts/run_evaluation.py --timings <file>`); see Section 13.5."
        )
        lines.append("")

    lines.append("## Cross-project leakage")
    lines.append("")
    if report.leakage_sentinel_hits:
        lines.append("**LEAKAGE DETECTED:**")
        for hit in report.leakage_sentinel_hits:
            lines.append(f"- {hit}")
    else:
        lines.append(
            "No sentinel from any project was found in another project's output. 0 leakage."
        )
    lines.append("")

    lines.append("## Per-project breakdown")
    lines.append("")
    for key in report.project_keys:
        lines.append(f"### {key}")
        lines.append("")
        lscore = report.ledger_scores[key].pooled()
        bscore = report.baseline_scores[key].pooled()
        lines.append("| Metric | Ledger | Baseline |")
        lines.append("|---|---|---|")
        for label, attr in (
            ("Item precision", "item_precision"),
            ("Item recall", "item_recall"),
            ("Evidence correctness", "evidence_correctness"),
            ("Unsupported-claim rate", "unsupported_claim_rate"),
            ("Current-state accuracy", "current_state_accuracy"),
            ("Transition accuracy", "transition_accuracy"),
        ):
            lines.append(
                f"| {label} | {_fmt_metric(getattr(lscore, attr))} | "
                f"{_fmt_metric(getattr(bscore, attr))} |"
            )
        lines.append("")
        rb_p = report.review_burden_by_project[key]
        lines.append(
            f"Review actions: {rb_p['accepted_without_edit']} accepted, "
            f"{rb_p['edited_accepted']} edited, {rb_p['rejected']} rejected."
        )
        lines.append("")

    lines.append("## Individually-flagged unsupported / materially misleading claims")
    lines.append("")
    lines.append(
        "Per Prompt 15: every claim below is listed individually with its expected vs. actual "
        "state, exactly as found — never folded into a single opaque rate. `unsupported` = no "
        "verbatim supporting quote found; `materially_misleading` = a real, verbatim quote cited "
        "in support of a claim that contradicts ground truth's expected state at that checkpoint."
    )
    lines.append("")
    all_misleading = list(report.ledger_composite.misleading) + list(
        report.baseline_composite.misleading
    )
    if not all_misleading:
        lines.append("None.")
    else:
        lines.append("| System | Checkpoint | Kind | Claim | Expected | Actual |")
        lines.append("|---|---|---|---|---|---|")
        for m in all_misleading:
            claim_text = m.claim_text.replace("|", "\\|")
            lines.append(
                f"| {m.system} | {m.checkpoint_id} | {m.kind} | {claim_text} | {m.expected} | "
                f"{m.actual} |"
            )
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"
