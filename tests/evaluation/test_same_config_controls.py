"""Same-model/config/corpus control checks (Prompt 15's required test
category; Section 13.4: "same model and reasoning configuration; same
source text and cutoff time; same output taxonomy; same citation
validation").
"""

from __future__ import annotations

from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.evaluation.baseline_runner import BaselineRunConfig, run_baseline_for_project
from project_context.evaluation.cli import RunOptions, run_evaluation
from project_context.evaluation.ledger_runner import (
    EmptyBriefProvider,
    LedgerRunConfig,
    run_ledger_for_project,
)
from project_context.evaluation.materialize import load_corpus_project


def test_run_options_carries_one_model_config_to_both_systems():
    """`RunOptions` has exactly one `model`/`reasoning_effort` pair —
    there is no way to configure the two systems differently through the
    public entrypoint, which is the control itself, not just a check on
    it."""
    options = RunOptions(model="gpt-5.6-terra", reasoning_effort="low")
    assert options.model == "gpt-5.6-terra"
    assert options.reasoning_effort == "low"
    # RunOptions has no separate ledger_model/baseline_model fields.
    assert not hasattr(options, "ledger_model")
    assert not hasattr(options, "baseline_model")


def test_ledger_and_baseline_checkpoints_share_the_same_cutoffs(migrations_dir, tmp_path):
    project, chunk_target_chars = load_corpus_project("implementation")
    conn = connect(tmp_path / "app.db")
    run_migrations(conn, migrations_dir)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    ledger_config = LedgerRunConfig(
        conn=conn,
        evidence_dir=evidence_dir,
        model="gpt-5.6-terra",
        reasoning_effort="low",
        brief_provider=EmptyBriefProvider(),
    )
    ledger_result = run_ledger_for_project(project, chunk_target_chars, config=ledger_config)
    baseline_result = run_baseline_for_project(
        project, config=BaselineRunConfig(model="gpt-5.6-terra")
    )

    ledger_ids = {cp.checkpoint_id for cp in ledger_result.checkpoints}
    baseline_ids = {cp.checkpoint_id for cp in baseline_result.checkpoints}
    assert ledger_ids == baseline_ids == {c.checkpoint_id for c in project.checkpoints}
    conn.close()


def test_ledger_and_baseline_share_the_exact_same_source_artifact_text(migrations_dir, tmp_path):
    """Both runners must read the identical normalized text for a given
    artifact — the ledger via real parsing during ingestion, the
    baseline via `project_context.evaluation.corpus_text` directly."""
    from project_context.evaluation import corpus_text

    project, _ = load_corpus_project("implementation")
    artifact = project.artifact_by_id("impl-w1-kickoff-vtt")
    baseline_text = corpus_text.artifact_text(artifact)

    conn = connect(tmp_path / "app.db")
    run_migrations(conn, migrations_dir)
    from datetime import UTC, datetime

    from project_context.domain.evidence import ManualFileUploadInput
    from project_context.domain.projects import ProjectCreateInput
    from project_context.evaluation.schema import ARTIFACT_KIND_MAPPING
    from project_context.services.evidence import submit_file_upload
    from project_context.services.projects import create_project

    created = create_project(conn, ProjectCreateInput(name="x", objective="y", stage="z"))
    source_type, _suffix, _is_markdown = ARTIFACT_KIND_MAPPING[artifact.kind]
    upload = ManualFileUploadInput(
        title=artifact.title,
        source_type=source_type,
        occurred_at=datetime.fromisoformat(artifact.occurred_at.replace("Z", "+00:00")).astimezone(
            UTC
        ),
        author=artifact.author,
        filename=artifact.filename,
        data=artifact.raw_bytes,
    )
    ingest_result = submit_file_upload(
        conn,
        created.id,
        upload,
        evidence_dir=tmp_path / "evidence",
        max_upload_bytes=25_000_000,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    ledger_text = ingest_result.content.normalized_text
    assert ledger_text == baseline_text
    conn.close()


def test_fake_mode_run_evaluation_uses_one_configured_model_throughout():
    report, _raw = run_evaluation(RunOptions(model="gpt-5.6-terra", reasoning_effort="low"))
    assert report.reproducibility.model == "gpt-5.6-terra"
    assert report.reproducibility.reasoning_effort == "low"
    assert report.reproducibility.mode == "fake"
