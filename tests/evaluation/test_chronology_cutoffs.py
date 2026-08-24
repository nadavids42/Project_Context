"""Chronological cutoff enforcement (Prompt 15's required test category).

Covers `project_context.evaluation.ground_truth_state` (the pure
function every runner/scorer relies on for "what should be true right
before this cutoff") and the ledger runner's own artifact-visibility
loop.
"""

from __future__ import annotations

from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.evaluation import ground_truth_state as gts
from project_context.evaluation.ledger_runner import (
    EmptyBriefProvider,
    LedgerRunConfig,
    run_ledger_for_project,
)


def test_transition_invisible_strictly_at_or_after_cutoff(tiny_project):
    item = tiny_project.item_by_id("commitment-design-doc")
    # tiny-a1 (create) occurs 2026-01-01T09:00:00Z.
    before = gts.state_at_cutoff(tiny_project, item, cutoff_at="2026-01-01T09:00:00Z")
    assert before.exists is False  # cutoff exactly at occurred_at is NOT visible (strictly before)

    # A later cutoff in the same lexicographic-comparable ISO shape
    # (matching every other timestamp in this fixture — all zero-padded,
    # no fractional seconds, so plain string comparison is safe).
    after = gts.state_at_cutoff(tiny_project, item, cutoff_at="2026-01-01T09:00:01Z")
    assert after.exists is True
    assert after.status.value == "open"


def test_state_at_cutoff_folds_only_visible_transitions_in_order(tiny_project):
    item = tiny_project.item_by_id("commitment-design-doc")
    mid = gts.state_at_cutoff(tiny_project, item, cutoff_at="2026-01-05T00:00:00Z")
    assert mid.status.value == "open"  # completion (tiny-a2, Jan 8) not yet visible

    final = gts.state_at_cutoff(tiny_project, item, cutoff_at="2026-01-11T00:00:00Z")
    assert final.status.value == "completed"


def test_state_at_cutoff_before_project_start_does_not_exist(tiny_project):
    item = tiny_project.item_by_id("commitment-design-doc")
    state = gts.state_at_cutoff(tiny_project, item, cutoff_at="2020-01-01T00:00:00Z")
    assert state.exists is False
    assert state.status is None
    assert state.owner is None


def test_all_states_at_cutoff_covers_every_item(tiny_project):
    states = gts.all_states_at_cutoff(tiny_project, cutoff_at="2026-01-11T00:00:00Z")
    assert set(states) == {item.item_id for item in tiny_project.items}


def test_ledger_runner_never_ingests_an_artifact_at_or_after_the_checkpoint_cutoff(
    tiny_project, migrations_dir, tmp_path
):
    """Integration-level check: the ledger runner's own chronological
    ingestion loop respects the same strictly-before contract — an
    artifact at/after a checkpoint's cutoff must not have been ingested
    (and so must not affect that checkpoint's evidence count) yet."""
    conn = connect(tmp_path / "app.db")
    run_migrations(conn, migrations_dir)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    # tiny_project's checkpoints: mid (cutoff = tiny-a2's own occurred_at)
    # and final (after everything). At "mid," tiny-a2 (the completion)
    # must NOT yet be visible — assert via the resulting ledger item's
    # status: still "open," never "completed," at that checkpoint.
    config = LedgerRunConfig(
        conn=conn,
        evidence_dir=evidence_dir,
        model="gpt-5.6-terra",
        reasoning_effort="low",
        brief_provider=EmptyBriefProvider(),
    )
    result = run_ledger_for_project(tiny_project, chunk_target_chars=200, config=config)
    mid_checkpoint = next(cp for cp in result.checkpoints if cp.checkpoint_id == "tiny-cp-mid")
    design_doc_claims = [c for c in mid_checkpoint.claims if c.item_title == "Design doc"]
    assert design_doc_claims, (
        "expected at least one claim about the design doc by the mid checkpoint"
    )
    assert all(c.status != "completed" for c in design_doc_claims)

    final_checkpoint = next(cp for cp in result.checkpoints if cp.checkpoint_id == "tiny-cp-final")
    final_claims = [c for c in final_checkpoint.claims if c.item_title == "Design doc"]
    assert any(c.status == "completed" for c in final_claims)
    conn.close()
