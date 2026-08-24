"""Cross-project sentinel detection (Prompt 15's required test category;
Section 15's "Privacy and cross-project leakage tests": "seed two
projects with identical names/terms and distinct secret sentinel
strings... assert no sentinel from project B appears in project A
output").
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
from project_context.evaluation.materialize import load_corpus_project


def test_check_cross_project_leakage_detects_an_injected_leak():
    outputs = {
        "project-a": ("Everything is fine here.",),
        "project-b": ("This mentions SENTINEL_FROM_A by mistake.",),
    }
    sentinels = {"project-a": ("SENTINEL_FROM_A",), "project-b": ("SENTINEL_FROM_B",)}
    result = scoring.check_cross_project_leakage(outputs, sentinels)
    assert len(result.leaked_sentinels) == 1
    assert "SENTINEL_FROM_A" in result.leaked_sentinels[0]
    assert "project-b" in result.leaked_sentinels[0]


def test_check_cross_project_leakage_clean_when_no_sentinel_crosses():
    outputs = {"project-a": ("Nothing suspicious.",), "project-b": ("Also nothing suspicious.",)}
    sentinels = {"project-a": ("SENTINEL_A",), "project-b": ("SENTINEL_B",)}
    result = scoring.check_cross_project_leakage(outputs, sentinels)
    assert result.leaked_sentinels == ()
    assert result.checked_projects == 2


def test_check_cross_project_leakage_never_flags_a_projects_own_sentinel():
    outputs = {"project-a": ("SENTINEL_A appears here, which is fine — it's project-a's own.",)}
    sentinels = {"project-a": ("SENTINEL_A",)}
    result = scoring.check_cross_project_leakage(outputs, sentinels)
    assert result.leaked_sentinels == ()


def test_real_three_project_run_has_zero_cross_project_leakage(migrations_dir, tmp_path):
    """End-to-end: run all three real benchmark corpora through the
    ledger in one shared database (mirroring real project isolation) and
    confirm no project's name/owner names appear in another's output."""
    conn = connect(tmp_path / "app.db")
    run_migrations(conn, migrations_dir)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    outputs_by_project: dict[str, tuple[str, ...]] = {}
    sentinels_by_project: dict[str, tuple[str, ...]] = {}
    for project_key in ("implementation", "advisory", "launch"):
        project, chunk_target_chars = load_corpus_project(project_key)
        config = LedgerRunConfig(
            conn=conn,
            evidence_dir=evidence_dir,
            model="gpt-5.6-terra",
            reasoning_effort="low",
            brief_provider=EmptyBriefProvider(),
        )
        result = run_ledger_for_project(project, chunk_target_chars, config=config)
        outputs_by_project[project_key] = tuple((cp.markdown or "") for cp in result.checkpoints)
        owner_names = {
            transition.owner
            for item in project.items
            for transition in item.transitions
            if transition.owner
        }
        sentinels_by_project[project_key] = (project.name,) + tuple(sorted(owner_names))

    leakage = scoring.check_cross_project_leakage(outputs_by_project, sentinels_by_project)
    assert leakage.leaked_sentinels == (), leakage.leaked_sentinels
    assert leakage.checked_projects == 3
    conn.close()
