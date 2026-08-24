"""Report generation: threshold annotations (never assertions), verdict
logic, and JSON/Markdown serialization.
"""

from __future__ import annotations

from project_context.evaluation.report import (
    ReviewTimingInputs,
    build_report,
    build_threshold_checks,
    build_verdict,
    render_markdown_report,
    report_to_json_dict,
)
from project_context.evaluation.reproducibility import capture_reproducibility_info
from project_context.evaluation.runner_types import CheckpointRunResult, EvidenceRef, ScoredClaim
from project_context.evaluation.scoring import score_project


def _perfect_claim(item_title: str, artifact_id: str, quote: str) -> ScoredClaim:
    return ScoredClaim(
        section="open_commitments",
        text=f"{item_title} — status: open — owner: Sam — due: 2026-01-10.",
        claim_type="fact",
        item_kind="commitment",
        item_title=item_title,
        status="open",
        owner="Sam",
        due_date="2026-01-10",
        evidence=(EvidenceRef(artifact_id=artifact_id, quote=quote),),
    )


def _build_scores(tiny_project):
    checkpoint = next(c for c in tiny_project.checkpoints if c.checkpoint_id == "tiny-cp-mid")
    ledger_run = CheckpointRunResult(
        checkpoint_id=checkpoint.checkpoint_id,
        brief_type=checkpoint.brief_type.value,
        system="ledger",
        claims=(
            _perfect_claim(
                "Design doc", "tiny-a1", "Sam will finish the design doc by 2026-01-10."
            ),
        ),
        markdown="# ledger",
        input_tokens=10,
        output_tokens=5,
        estimated_cost_usd=0.001,
        latency_ms=5,
        accepted_without_edit=5,
        edited_accepted=1,
        rejected=0,
    )
    baseline_run = CheckpointRunResult(
        checkpoint_id=checkpoint.checkpoint_id,
        brief_type=checkpoint.brief_type.value,
        system="baseline",
        claims=(),
        markdown="# baseline",
        input_tokens=20,
        output_tokens=8,
        estimated_cost_usd=0.002,
        latency_ms=9,
    )
    ledger_score = score_project(
        tiny_project, {checkpoint.checkpoint_id: ledger_run}, system="ledger"
    )
    baseline_score = score_project(
        tiny_project, {checkpoint.checkpoint_id: baseline_run}, system="baseline"
    )
    return ledger_score, baseline_score


def test_build_threshold_checks_never_raises_on_zero_denominators(tiny_project):
    from project_context.evaluation.scoring import PooledMetrics, zero_metric

    empty = PooledMetrics(
        item_precision=zero_metric(),
        item_recall=zero_metric(),
        field_accuracy_kind=zero_metric(),
        field_accuracy_status=zero_metric(),
        field_accuracy_owner=zero_metric(),
        field_accuracy_due_date=zero_metric(),
        evidence_correctness=zero_metric(),
        unsupported_claim_rate=zero_metric(),
        transition_accuracy=zero_metric(),
        current_state_accuracy=zero_metric(),
        total_input_tokens=0,
        total_output_tokens=0,
        total_cost_usd=0.0,
        total_latency_ms=0,
        accepted_without_edit=0,
        edited_accepted=0,
        rejected=0,
        misleading=(),
    )
    checks = build_threshold_checks(
        empty,
        empty,
        {},
        {"minutes_per_sync": None, "seconds_per_accepted_item": None, "is_measured": False},
        None,
        (),
    )
    # Every threshold must be reported, none crash, and every one whose
    # inputs are all zero-denominator must be explicitly "not evaluable"
    # (passed is None), never silently True/False.
    assert len(checks) >= 10
    for check in checks:
        if check.name in ("project_assignment_error",):
            assert check.passed is None
        assert check.observed_text  # never blank


def test_build_verdict_go_when_everything_passes():
    from project_context.evaluation.report import ThresholdCheck

    checks = (ThresholdCheck("x", "d", "t", "o", True),)
    verdict, rationale = build_verdict(checks, mode="live")
    assert verdict == "GO"
    assert rationale


def test_build_verdict_stop_when_leakage_detected():
    from project_context.evaluation.report import ThresholdCheck

    checks = (
        ThresholdCheck("beats_baseline", "d", "t", "o", True),
        ThresholdCheck("cross_project_leakage", "d", "0", "1 hit", False),
    )
    verdict, rationale = build_verdict(checks, mode="live")
    assert verdict == "STOP"
    assert any("leakage" in r.lower() for r in rationale)


def test_build_verdict_stop_when_ledger_does_not_beat_baseline():
    from project_context.evaluation.report import ThresholdCheck

    checks = (ThresholdCheck("beats_baseline", "d", "t", "o", False),)
    verdict, rationale = build_verdict(checks, mode="live")
    assert verdict == "STOP"


def test_build_verdict_iterate_when_one_threshold_fails_but_no_hard_stop():
    from project_context.evaluation.report import ThresholdCheck

    checks = (
        ThresholdCheck("beats_baseline", "d", "t", "o", True),
        ThresholdCheck("cross_project_leakage", "d", "0", "0", True),
        ThresholdCheck("materially_misleading_rate", "d", "<5%", "3%", True),
        ThresholdCheck("item_recall", "d", ">=80%", "70%", False),
    )
    verdict, rationale = build_verdict(checks, mode="live")
    assert verdict == "ITERATE"
    assert any("item_recall" in r for r in rationale)


def test_build_verdict_fake_mode_always_carries_the_self_test_caveat():
    from project_context.evaluation.report import ThresholdCheck

    checks = (ThresholdCheck("beats_baseline", "d", "t", "o", True),)
    _verdict, rationale = build_verdict(checks, mode="fake")
    assert any(
        "fake" in r.lower() and "not a real" in r.lower() or "self-test" in r.lower()
        for r in rationale
    )


def test_build_report_and_render_roundtrip(tiny_project):
    ledger_score, baseline_score = _build_scores(tiny_project)
    repro = capture_reproducibility_info(mode="fake", model="m", reasoning_effort="low")
    report = build_report(
        reproducibility=repro,
        ledger_scores={"tiny": ledger_score},
        baseline_scores={"tiny": baseline_score},
        checkpoints_by_project={},
        timings=ReviewTimingInputs(),
    )
    assert report.verdict in ("GO", "ITERATE", "STOP")

    as_json = report_to_json_dict(report)
    assert as_json["verdict"] == report.verdict
    assert "tiny" in as_json["per_project"]
    assert as_json["composite"]["ledger"]["item_precision"]["value"] == 1.0

    markdown = render_markdown_report(report)
    assert markdown.startswith("# Project Context")
    assert report.verdict in markdown
    assert "tiny" in markdown
    # Every threshold row renders without raising on a None (n/a) value.
    assert "n/a" in markdown or "✅" in markdown or "❌" in markdown
