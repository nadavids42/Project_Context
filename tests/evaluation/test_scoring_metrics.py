"""Metric formulas, including zero-denominator/ambiguous exclusions, and
a fully deterministic fixture producing known metric values (Prompt 15's
required test categories).
"""

from __future__ import annotations

from project_context.evaluation import scoring
from project_context.evaluation.runner_types import CheckpointRunResult, EvidenceRef, ScoredClaim


def _claim(**overrides) -> ScoredClaim:  # noqa: ANN003
    base = dict(
        section="open_commitments",
        text="x",
        claim_type="fact",
        item_kind="commitment",
        item_title="Design doc",
        status="open",
        owner="Sam",
        due_date="2026-01-10",
        evidence=(),
    )
    base.update(overrides)
    return ScoredClaim(**base)


# ---------------------------------------------------------------------------
# Metric/zero_metric/add_metrics primitives.
# ---------------------------------------------------------------------------


def test_metric_value_is_none_for_zero_denominator():
    assert scoring.Metric(0, 0).value is None


def test_metric_value_is_correct_ratio():
    assert scoring.Metric(3, 4).value == 0.75


def test_zero_metric_has_none_value():
    assert scoring.zero_metric().value is None


def test_add_metrics_sums_numerator_and_denominator():
    total = scoring.add_metrics(scoring.Metric(1, 2), scoring.Metric(2, 3))
    assert (total.numerator, total.denominator) == (3, 5)


def test_add_metrics_of_two_zero_metrics_stays_none():
    total = scoring.add_metrics(scoring.zero_metric(), scoring.zero_metric())
    assert total.value is None


# ---------------------------------------------------------------------------
# match_claims_to_items.
# ---------------------------------------------------------------------------


def test_match_claims_to_items_matches_by_normalized_title(tiny_project):
    claims = (_claim(item_title="  Design Doc  "),)  # case/whitespace-insensitive
    matched, unmatched = scoring.match_claims_to_items(tiny_project, claims)
    assert "commitment-design-doc" in matched
    assert unmatched == 0


def test_match_claims_to_items_counts_true_hallucination_separately(tiny_project):
    claims = (_claim(item_title="Something ground truth never heard of"),)
    matched, unmatched = scoring.match_claims_to_items(tiny_project, claims)
    assert matched == {}
    assert unmatched == 1


def test_match_claims_to_items_merges_fields_across_sections(tiny_project):
    """A claim in one section stating only status, and another in a
    different section stating only owner, must both credit the same
    item — neither should be discarded in favor of the other."""
    claims = (
        _claim(section="recent_changes", status="open", owner=None, due_date=None, evidence=()),
        _claim(
            section="open_commitments", status=None, owner="Sam", due_date="2026-01-10", evidence=()
        ),
    )
    matched, _ = scoring.match_claims_to_items(tiny_project, claims)
    merged = matched["commitment-design-doc"]
    assert merged.status == "open"
    assert merged.owner == "Sam"
    assert merged.due_date == "2026-01-10"


def test_match_claims_to_items_ignores_claims_without_item_fields(tiny_project):
    claims = (_claim(item_kind=None, item_title=None),)
    matched, unmatched = scoring.match_claims_to_items(tiny_project, claims)
    assert matched == {}
    assert unmatched == 0


# ---------------------------------------------------------------------------
# score_checkpoint: hand-computable known values.
# ---------------------------------------------------------------------------


def _checkpoint(project, checkpoint_id: str):
    return next(c for c in project.checkpoints if c.checkpoint_id == checkpoint_id)


def test_score_checkpoint_perfect_ledger_claim_scores_full_marks(tiny_project):
    checkpoint = _checkpoint(tiny_project, "tiny-cp-mid")
    quote = "Sam will finish the design doc by 2026-01-10."
    claim = _claim(
        claim_type="fact",
        status="open",
        owner="Sam",
        due_date="2026-01-10",
        evidence=(EvidenceRef(artifact_id="tiny-a1", quote=quote),),
    )
    run = CheckpointRunResult(
        checkpoint_id="tiny-cp-mid",
        brief_type="current_project",
        system="ledger",
        claims=(claim,),
        markdown=None,
        input_tokens=0,
        output_tokens=0,
        estimated_cost_usd=0.0,
        latency_ms=0,
    )
    score = scoring.score_checkpoint(tiny_project, checkpoint, run)
    # tiny_project's ground truth has exactly one item — matched and
    # correct, so both precision and recall are perfect 1/1.
    assert score.item_precision.value == 1.0
    assert score.item_recall == scoring.Metric(1, 1)
    assert score.field_accuracy_status == scoring.Metric(1, 1)
    assert score.field_accuracy_owner == scoring.Metric(1, 1)
    assert score.field_accuracy_due_date == scoring.Metric(1, 1)
    assert score.evidence_correctness == scoring.Metric(1, 1)
    assert score.unsupported_claim_rate == scoring.Metric(0, 1)
    assert score.misleading == ()


def test_score_checkpoint_wrong_owner_is_field_miss_and_misleading(tiny_project):
    checkpoint = _checkpoint(tiny_project, "tiny-cp-mid")
    quote = "Sam will finish the design doc by 2026-01-10."
    claim = _claim(
        owner="WrongPerson",
        evidence=(EvidenceRef(artifact_id="tiny-a1", quote=quote),),
    )
    run = CheckpointRunResult(
        checkpoint_id="tiny-cp-mid",
        brief_type="current_project",
        system="baseline",
        claims=(claim,),
        markdown=None,
        input_tokens=0,
        output_tokens=0,
        estimated_cost_usd=0.0,
        latency_ms=0,
    )
    score = scoring.score_checkpoint(tiny_project, checkpoint, run)
    assert score.field_accuracy_owner == scoring.Metric(0, 1)
    assert len(score.misleading) == 1
    assert score.misleading[0].kind == "materially_misleading"
    assert "owner=Sam" in score.misleading[0].expected
    assert "owner=WrongPerson" in score.misleading[0].actual


def test_score_checkpoint_claim_with_no_evidence_is_unsupported(tiny_project):
    checkpoint = _checkpoint(tiny_project, "tiny-cp-mid")
    claim = _claim(evidence=())
    run = CheckpointRunResult(
        checkpoint_id="tiny-cp-mid",
        brief_type="current_project",
        system="baseline",
        claims=(claim,),
        markdown=None,
        input_tokens=0,
        output_tokens=0,
        estimated_cost_usd=0.0,
        latency_ms=0,
    )
    score = scoring.score_checkpoint(tiny_project, checkpoint, run)
    assert score.unsupported_claim_rate == scoring.Metric(1, 1)
    assert score.evidence_correctness == scoring.Metric(0, 1)
    assert len(score.misleading) == 1
    assert score.misleading[0].kind == "unsupported"


def test_score_checkpoint_premature_claim_flagged_only_if_item_does_not_exist_at_all(tiny_project):
    checkpoint = _checkpoint(tiny_project, "tiny-cp-mid")
    early = "2020-01-01T00:00:00Z"
    # Build a project-local checkpoint even earlier than tiny-a1's own
    # occurred_at — the design-doc item does not exist there at all.
    from project_context.evaluation.schema import BriefTypeLiteral, Checkpoint

    too_early = Checkpoint(
        checkpoint_id="too-early", cutoff_at=early, brief_type=BriefTypeLiteral.CURRENT_PROJECT
    )
    claim = _claim(
        evidence=(
            EvidenceRef(
                artifact_id="tiny-a1", quote="Sam will finish the design doc by 2026-01-10."
            ),
        )
    )
    run = CheckpointRunResult(
        checkpoint_id="too-early",
        brief_type="current_project",
        system="baseline",
        claims=(claim,),
        markdown=None,
        input_tokens=0,
        output_tokens=0,
        estimated_cost_usd=0.0,
        latency_ms=0,
    )
    score = scoring.score_checkpoint(tiny_project, too_early, run)
    assert any(
        m.kind == "materially_misleading" and "does not exist yet" in m.expected
        for m in score.misleading
    )
    del checkpoint  # unused; kept for symmetry with the other tests in this module


def test_ambiguous_transition_excluded_from_recall_denominator():
    """Section 13.3: "Ambiguous items are labeled and excluded from
    strict precision/recall." A `Materiality.AMBIGUOUS` transition's
    item must not count toward `material_expected` — an empty run
    against it is neither a recall miss nor scored at all."""
    from evaluation.conftest import _artifact
    from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus
    from project_context.evaluation.schema import (
        BriefTypeLiteral,
        Checkpoint,
        CorpusProject,
        EvidenceMention,
        GroundTruthItem,
        GroundTruthTransition,
        Materiality,
        TransitionType,
    )

    artifact = _artifact(
        "a1", "2026-01-01T00:00:00Z", "Jordan will do something ambiguous here today."
    )
    ambiguous_item = GroundTruthItem(
        item_id="ambiguous-item",
        kind=LedgerItemKind.COMMITMENT,
        canonical_title="Ambiguous commitment",
        transitions=(
            GroundTruthTransition(
                transition_id="t1",
                type=TransitionType.CREATE,
                mentions=(
                    EvidenceMention(
                        artifact_id="a1", statement="Jordan will do something ambiguous here today."
                    ),
                ),
                status=LedgerItemStatus.OPEN,
                materiality=Materiality.AMBIGUOUS,
            ),
        ),
    )
    checkpoint = Checkpoint(
        checkpoint_id="cp1",
        cutoff_at="2026-01-02T00:00:00Z",
        brief_type=BriefTypeLiteral.CURRENT_PROJECT,
    )
    project = CorpusProject(
        key="p",
        name="P",
        objective="o",
        stage="s",
        artifacts=(artifact,),
        items=(ambiguous_item,),
        checkpoints=(checkpoint,),
    )
    empty_run = CheckpointRunResult(
        checkpoint_id="cp1",
        brief_type="current_project",
        system="ledger",
        claims=(),
        markdown=None,
        input_tokens=0,
        output_tokens=0,
        estimated_cost_usd=0.0,
        latency_ms=0,
    )
    score = scoring.score_checkpoint(project, checkpoint, empty_run)
    assert score.item_recall.denominator == 0  # excluded entirely, not scored as a miss
    assert score.item_recall.value is None


def test_meeting_prep_checkpoint_never_expects_decision_or_stakeholder_kinds():
    """A Meeting Preparation Brief has no decisions/stakeholders section
    at all (Section 5.9) — such an item must never count against recall
    for a meeting_preparation checkpoint, even though it always would
    for a current_project one."""
    from evaluation.conftest import _artifact
    from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus
    from project_context.evaluation.schema import (
        BriefTypeLiteral,
        Checkpoint,
        CorpusProject,
        EvidenceMention,
        GroundTruthItem,
        GroundTruthTransition,
        TransitionType,
    )

    artifact = _artifact(
        "d1", "2026-01-01T00:00:00Z", "We decided X. This was noted for the project record."
    )
    decision = GroundTruthItem(
        item_id="decision-x",
        kind=LedgerItemKind.DECISION,
        canonical_title="Decision X",
        transitions=(
            GroundTruthTransition(
                transition_id="d1-create",
                type=TransitionType.CREATE,
                mentions=(EvidenceMention(artifact_id="d1", statement="We decided X."),),
                status=LedgerItemStatus.ACTIVE,
            ),
        ),
    )
    meeting_cp = Checkpoint(
        checkpoint_id="mp1",
        cutoff_at="2026-01-02T00:00:00Z",
        brief_type=BriefTypeLiteral.MEETING_PREPARATION,
        meeting_title="Sync",
        meeting_scheduled_at="2026-01-02T00:00:00Z",
    )
    current_cp = Checkpoint(
        checkpoint_id="cp1",
        cutoff_at="2026-01-02T00:00:00Z",
        brief_type=BriefTypeLiteral.CURRENT_PROJECT,
    )
    project = CorpusProject(
        key="p",
        name="P",
        objective="o",
        stage="s",
        artifacts=(artifact,),
        items=(decision,),
        checkpoints=(meeting_cp, current_cp),
    )
    empty_run_meeting = CheckpointRunResult(
        checkpoint_id="mp1",
        brief_type="meeting_preparation",
        system="ledger",
        claims=(),
        markdown=None,
        input_tokens=0,
        output_tokens=0,
        estimated_cost_usd=0.0,
        latency_ms=0,
    )
    empty_run_current = CheckpointRunResult(
        checkpoint_id="cp1",
        brief_type="current_project",
        system="ledger",
        claims=(),
        markdown=None,
        input_tokens=0,
        output_tokens=0,
        estimated_cost_usd=0.0,
        latency_ms=0,
    )
    meeting_score = scoring.score_checkpoint(project, meeting_cp, empty_run_meeting)
    current_score = scoring.score_checkpoint(project, current_cp, empty_run_current)
    assert meeting_score.item_recall.denominator == 0  # nothing expected -> not scored at all
    assert current_score.item_recall == scoring.Metric(0, 1)  # expected, and missed


# ---------------------------------------------------------------------------
# PooledMetrics derived ratios.
# ---------------------------------------------------------------------------


def test_pooled_acceptance_rate_formula():
    from project_context.evaluation.scoring import PooledMetrics, zero_metric

    pooled = PooledMetrics(
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
        accepted_without_edit=6,
        edited_accepted=2,
        rejected=2,
        misleading=(),
    )
    assert pooled.acceptance_rate == scoring.Metric(8, 10)
    assert pooled.edit_free_acceptance_rate == scoring.Metric(6, 10)


def test_pooled_acceptance_rate_zero_denominator_when_no_reviews():
    from project_context.evaluation.scoring import PooledMetrics, zero_metric

    pooled = PooledMetrics(
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
    assert pooled.acceptance_rate.value is None
    assert pooled.edit_free_acceptance_rate.value is None
