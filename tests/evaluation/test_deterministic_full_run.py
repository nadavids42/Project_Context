"""Deterministic fixture produces known metric values (Prompt 15's
required test category) — a fully scripted end-to-end run (real
migrations, real ingestion/extraction/reconciliation/review, real brief
generation) against the small `tiny_project` fixture, where every metric
can be hand-verified against exactly two authored facts.
"""

from __future__ import annotations

from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.evaluation import scoring
from project_context.evaluation.ledger_runner import (
    EmptyBriefProvider,
    LedgerRunConfig,
    run_ledger_for_project,
)


def test_tiny_project_end_to_end_produces_exact_known_scores(
    tiny_project, migrations_dir, tmp_path
):
    conn = connect(tmp_path / "app.db")
    run_migrations(conn, migrations_dir)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    config = LedgerRunConfig(
        conn=conn,
        evidence_dir=evidence_dir,
        model="gpt-5.6-terra",
        reasoning_effort="low",
        brief_provider=EmptyBriefProvider(),
    )
    result = run_ledger_for_project(tiny_project, chunk_target_chars=200, config=config)
    assert [cp.checkpoint_id for cp in result.checkpoints] == ["tiny-cp-mid", "tiny-cp-final"]

    project_score = scoring.score_project(
        tiny_project, {cp.checkpoint_id: cp for cp in result.checkpoints}, system="ledger"
    )
    pooled = project_score.pooled()

    # Exactly known by construction: tiny_project's ground truth has
    # exactly one item (the design doc commitment), correctly extracted,
    # reconciled, reviewed, and rendered by the real pipeline at both
    # `current_project` checkpoints (which expect every existing item).
    assert pooled.item_precision.value == 1.0
    assert pooled.evidence_correctness.value == 1.0
    assert pooled.unsupported_claim_rate.value == 0.0
    assert pooled.misleading == ()
    assert pooled.item_recall == scoring.Metric(2, 2)
    assert pooled.current_state_accuracy == scoring.Metric(2, 2)
    # The commitment's one real status transition (create -> completed)
    # is a scorable "transition" only at the checkpoint where it has
    # already happened relative to its own creation — see
    # transition_accuracy's docstring in scoring.py for what counts.
    assert pooled.transition_accuracy.numerator == pooled.transition_accuracy.denominator
    assert pooled.transition_accuracy.denominator >= 1

    conn.close()


def test_tiny_project_run_is_stable_across_repeated_invocations(
    tiny_project, migrations_dir, tmp_path
):
    def _run(db_name: str) -> scoring.PooledMetrics:
        conn = connect(tmp_path / db_name)
        run_migrations(conn, migrations_dir)
        evidence_dir = tmp_path / f"{db_name}-evidence"
        evidence_dir.mkdir()
        config = LedgerRunConfig(
            conn=conn,
            evidence_dir=evidence_dir,
            model="gpt-5.6-terra",
            reasoning_effort="low",
            brief_provider=EmptyBriefProvider(),
        )
        result = run_ledger_for_project(tiny_project, chunk_target_chars=200, config=config)
        score = scoring.score_project(
            tiny_project, {cp.checkpoint_id: cp for cp in result.checkpoints}, system="ledger"
        )
        conn.close()
        return score.pooled()

    first = _run("a.db")
    second = _run("b.db")
    assert (first.item_precision, first.item_recall, first.evidence_correctness) == (
        second.item_precision,
        second.item_recall,
        second.evidence_correctness,
    )
