"""Pure unit tests for the reconciliation domain module (Section 10.4-
10.13): the auditable score formula, match-tier thresholds, and a few
action-classification edge cases that do not need a database to exercise
(the full 16-scenario suite lives in `test_reconciliation_service.py`,
end to end against SQLite)."""

from __future__ import annotations

from datetime import date

import pytest

from project_context.domain.ledger import (
    ConfidenceBand,
    LedgerItem,
    LedgerItemKind,
    LedgerItemStatus,
)
from project_context.domain.people import PersonResolution
from project_context.domain.reconciliation import (
    DEFAULT_RECONCILIATION_CONFIG,
    MatchFeatures,
    MatchOutcome,
    MatchTier,
    ScoredCandidate,
    determine_match_outcome,
    match_confidence,
    score_match,
    weakest_band,
)
from project_context.domain.review import ProposedMutationAction


def _features(**overrides: float) -> MatchFeatures:
    base = dict(
        subject_token_similarity=0.0,
        owner_match=0.0,
        date_proximity=0.0,
        item_type_compatibility=0.0,
        shared_named_entities=0.0,
        source_local_reference=0.0,
        mutually_exclusive_owner=0.0,
        completed_long_ago=0.0,
        corrected_field_disagreement=0.0,
    )
    base.update(overrides)
    return MatchFeatures(**base)


def _item(**overrides: object) -> LedgerItem:
    base = dict(
        id="item-1",
        project_id="proj-1",
        kind=LedgerItemKind.COMMITMENT,
        canonical_title="Send the report",
        canonical_description=None,
        status=LedgerItemStatus.OPEN,
        owner_person_id=None,
        due_date=None,
        effective_at=None,
        current_version_id="v1",
        confidence_band=None,
        user_corrected=False,
        created_at="2026-08-01T00:00:00.000000Z",
        updated_at="2026-08-01T00:00:00.000000Z",
    )
    base.update(overrides)
    return LedgerItem(**base)


def _scored(score: float, **feature_overrides: float) -> ScoredCandidate:
    return ScoredCandidate(
        ledger_item=_item(id=f"item-{score}"),
        features=_features(**feature_overrides),
        score=score,
        retrieval_tiers=frozenset({"exact"}),
    )


# --- score_match (Section 10.4's formula) -----------------------------------


def test_score_match_perfect_features_reaches_one():
    features = _features(
        subject_token_similarity=1.0,
        owner_match=1.0,
        date_proximity=1.0,
        item_type_compatibility=1.0,
        shared_named_entities=1.0,
        source_local_reference=1.0,
    )
    assert score_match(features, DEFAULT_RECONCILIATION_CONFIG.weights) == pytest.approx(1.0)


def test_score_match_matches_the_documented_weighted_sum():
    features = _features(subject_token_similarity=0.8, owner_match=1.0, date_proximity=0.5)
    weights = DEFAULT_RECONCILIATION_CONFIG.weights
    expected = (
        weights.subject_token_similarity * 0.8
        + weights.owner_match * 1.0
        + weights.date_proximity * 0.5
    )
    assert score_match(features, weights) == pytest.approx(expected)


def test_score_match_penalties_pull_the_score_down():
    without_penalty = _features(subject_token_similarity=1.0, owner_match=1.0)
    with_penalty = _features(
        subject_token_similarity=1.0, owner_match=1.0, mutually_exclusive_owner=1.0
    )
    weights = DEFAULT_RECONCILIATION_CONFIG.weights
    assert score_match(with_penalty, weights) < score_match(without_penalty, weights)


def test_score_match_clamps_to_zero_when_penalties_exceed_positive_terms():
    features = _features(
        mutually_exclusive_owner=1.0, completed_long_ago=1.0, corrected_field_disagreement=1.0
    )
    assert score_match(features, DEFAULT_RECONCILIATION_CONFIG.weights) == 0.0


def test_score_match_never_exceeds_one_even_with_an_unusual_config():
    from project_context.domain.reconciliation import ReconciliationWeights

    weights = ReconciliationWeights(
        subject_token_similarity=2.0,
        owner_match=2.0,
        date_proximity=0,
        item_type_compatibility=0,
        shared_named_entities=0,
        source_local_reference=0,
    )
    features = _features(subject_token_similarity=1.0, owner_match=1.0)
    assert score_match(features, weights) == 1.0


# --- determine_match_outcome (Section 10.4's thresholds) --------------------


def test_no_candidates_is_the_none_tier():
    outcome = determine_match_outcome([], DEFAULT_RECONCILIATION_CONFIG.thresholds)
    assert outcome.tier is MatchTier.NONE
    assert outcome.top is None
    assert outcome.margin is None


def test_single_strong_candidate_with_no_runner_up():
    outcome = determine_match_outcome([_scored(0.90)], DEFAULT_RECONCILIATION_CONFIG.thresholds)
    assert outcome.tier is MatchTier.STRONG
    assert outcome.margin is None


def test_strong_score_but_close_runner_up_downgrades_to_review():
    outcome = determine_match_outcome(
        [_scored(0.90), _scored(0.85)], DEFAULT_RECONCILIATION_CONFIG.thresholds
    )
    assert outcome.tier is MatchTier.REVIEW
    assert outcome.margin == pytest.approx(0.05)


def test_strong_score_with_sufficient_margin_stays_strong():
    outcome = determine_match_outcome(
        [_scored(0.90), _scored(0.70)], DEFAULT_RECONCILIATION_CONFIG.thresholds
    )
    assert outcome.tier is MatchTier.STRONG


def test_review_band_score():
    outcome = determine_match_outcome([_scored(0.70)], DEFAULT_RECONCILIATION_CONFIG.thresholds)
    assert outcome.tier is MatchTier.REVIEW


def test_below_review_threshold_is_uncertain():
    outcome = determine_match_outcome([_scored(0.40)], DEFAULT_RECONCILIATION_CONFIG.thresholds)
    assert outcome.tier is MatchTier.UNCERTAIN


def test_ranked_candidates_are_sorted_highest_first():
    outcome = determine_match_outcome(
        [_scored(0.40), _scored(0.90), _scored(0.70)], DEFAULT_RECONCILIATION_CONFIG.thresholds
    )
    assert [c.score for c in outcome.ranked] == [0.90, 0.70, 0.40]


# --- confidence bands (Section 10.12) ---------------------------------------


def test_weakest_band_takes_the_lowest_of_several():
    bands = (ConfidenceBand.HIGH, ConfidenceBand.LOW, ConfidenceBand.MEDIUM)
    assert weakest_band(*bands) is ConfidenceBand.LOW
    assert weakest_band(ConfidenceBand.HIGH, ConfidenceBand.HIGH) is ConfidenceBand.HIGH


def test_match_confidence_high_when_no_candidates_exist():
    outcome = MatchOutcome(tier=MatchTier.NONE, top=None, margin=None, ranked=())
    band = match_confidence(
        match_outcome=outcome, owner_ambiguous=False, corrected_field_conflict=False
    )
    assert band is ConfidenceBand.HIGH


def test_match_confidence_low_when_owner_is_ambiguous_even_with_a_strong_score():
    scored = _scored(0.95)
    outcome = MatchOutcome(tier=MatchTier.STRONG, top=scored, margin=None, ranked=(scored,))
    band = match_confidence(
        match_outcome=outcome, owner_ambiguous=True, corrected_field_conflict=False
    )
    assert band is ConfidenceBand.LOW


def test_match_confidence_medium_for_review_tier():
    scored = _scored(0.70)
    outcome = MatchOutcome(tier=MatchTier.REVIEW, top=scored, margin=None, ranked=(scored,))
    band = match_confidence(
        match_outcome=outcome, owner_ambiguous=False, corrected_field_conflict=False
    )
    assert band is ConfidenceBand.MEDIUM


# --- item-kind compatibility (Section 10.3's table) --------------------------


def test_compatible_kinds_commitment_is_commitment_only():
    from project_context.domain.reconciliation import compatible_kinds
    from project_context.llm.schemas import ObservationKind

    assert compatible_kinds(ObservationKind.COMMITMENT) == frozenset({LedgerItemKind.COMMITMENT})


def test_compatible_kinds_risk_includes_blocker_as_secondary():
    from project_context.domain.reconciliation import PRIMARY_COMPATIBLE_KINDS, compatible_kinds
    from project_context.llm.schemas import ObservationKind

    assert compatible_kinds(ObservationKind.RISK) == frozenset(
        {LedgerItemKind.RISK, LedgerItemKind.BLOCKER}
    )
    assert PRIMARY_COMPATIBLE_KINDS[ObservationKind.RISK] == frozenset({LedgerItemKind.RISK})


def test_compatible_kinds_update_spans_every_ledger_kind():
    from project_context.domain.reconciliation import compatible_kinds
    from project_context.llm.schemas import ObservationKind

    assert compatible_kinds(ObservationKind.UPDATE) == frozenset(LedgerItemKind)


# --- classify_action: pure edge cases not covered by the service suite ------


def test_classify_action_with_no_candidates_and_a_non_update_kind_creates():
    from project_context.domain.reconciliation import classify_action
    from project_context.domain.text_normalization import NormalizedDate
    from project_context.llm.schemas import ObservationKind

    outcome = MatchOutcome(tier=MatchTier.NONE, top=None, margin=None, ranked=())
    classification = classify_action(
        observation_kind=ObservationKind.COMMITMENT,
        subject="Send the report",
        statement="Priya will send the report by Friday.",
        explicitness="explicit",
        resolved_owner=PersonResolution(outcome="unknown"),
        normalized_date=NormalizedDate(value=None, original_text=None, ambiguous=False),
        match_outcome=outcome,
        already_has_evidence_from_content=False,
        thresholds=DEFAULT_RECONCILIATION_CONFIG.thresholds,
    )
    assert classification.action is ProposedMutationAction.CREATE
    assert classification.target_ledger_item_id is None
    assert classification.escalate is False


def test_classify_action_update_kind_with_no_candidates_never_creates():
    from project_context.domain.reconciliation import classify_action
    from project_context.domain.text_normalization import NormalizedDate
    from project_context.llm.schemas import ObservationKind

    outcome = MatchOutcome(tier=MatchTier.NONE, top=None, margin=None, ranked=())
    classification = classify_action(
        observation_kind=ObservationKind.UPDATE,
        subject="That item",
        statement="That item is now due Friday.",
        explicitness="explicit",
        resolved_owner=PersonResolution(outcome="unknown"),
        normalized_date=NormalizedDate(
            value=date(2026, 9, 4), original_text="Friday", ambiguous=False
        ),
        match_outcome=outcome,
        already_has_evidence_from_content=False,
        thresholds=DEFAULT_RECONCILIATION_CONFIG.thresholds,
    )
    assert classification.action is ProposedMutationAction.CONFLICT
    assert classification.action is not ProposedMutationAction.CREATE
    assert "update_observation_without_resolvable_target" in classification.escalation_reasons
