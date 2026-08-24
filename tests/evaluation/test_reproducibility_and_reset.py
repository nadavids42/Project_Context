"""Database reset/reproducibility and app-commit/version capture
(Prompt 15's required test categories; Section 13.7 step 1: "Freeze
corpus, ground truth, model, prompts, schemas, and app commit"; step 3:
"Reset database and run Project Context incrementally").
"""

from __future__ import annotations

from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.evaluation.ledger_runner import (
    EmptyBriefProvider,
    LedgerRunConfig,
    run_ledger_for_project,
)
from project_context.evaluation.materialize import load_corpus_project
from project_context.evaluation.reproducibility import capture_reproducibility_info


def test_capture_reproducibility_info_has_every_required_field():
    info = capture_reproducibility_info(mode="fake", model="gpt-5.6-terra", reasoning_effort="low")
    d = info.as_dict()
    for key in (
        "generated_at",
        "app_commit",
        "app_commit_dirty",
        "app_version",
        "mode",
        "model",
        "reasoning_effort",
        "extraction_prompt_version",
        "extraction_schema_version",
        "brief_prompt_version",
        "meeting_prep_prompt_version",
        "brief_schema_version",
        "baseline_prompt_version",
        "baseline_schema_version",
        "ground_truth_schema_version",
    ):
        assert key in d, key
    assert d["mode"] == "fake"
    assert d["model"] == "gpt-5.6-terra"


def test_capture_reproducibility_info_reads_this_repos_own_commit():
    info = capture_reproducibility_info(mode="fake", model="m", reasoning_effort="low")
    # This repository is a real git checkout (see environment context) —
    # a commit hash should be found, not silently None.
    assert info.app_commit is not None
    assert len(info.app_commit) == 40


def _claim_fingerprint(cp) -> tuple:  # noqa: ANN001
    """Every claim's *content* — section/text/kind/title/status/owner/
    due_date and its evidence as `(corpus_artifact_id, quote)` pairs
    (already resolved to this harness's own stable string IDs, not raw
    ULID primary keys — see `project_context.evaluation.ledger_runner.
    _evidence_ref_for_fact`) — deliberately excludes anything that is
    expected to differ between runs by design: raw database row IDs
    (ULIDs embed a random component; two fresh-database runs mint
    different ones for identical content) and the rendered Markdown's
    embedded evidence-viewer links, which are built from those same IDs."""
    return tuple(
        (
            c.section,
            c.text,
            c.claim_type,
            c.item_kind,
            c.item_title,
            c.status,
            c.owner,
            c.due_date,
            c.evidence,
        )
        for c in cp.claims
    )


def test_running_the_ledger_twice_against_fresh_databases_is_deterministic(
    tmp_path, migrations_dir
):
    """Section 13.7 step 3's "reset database" requirement: two
    independent fresh-database runs of the same frozen corpus, same
    scripted reviewer, same fake extraction must produce the same
    claims/content — no hidden state carried between runs. (Raw
    Markdown is *not* compared directly: it embeds evidence-viewer links
    built from real database row IDs, which are freshly minted ULIDs —
    intentionally different every run even for identical content.)"""
    project, chunk_target_chars = load_corpus_project("implementation")

    def _run_once(db_name: str) -> tuple[tuple, ...]:
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
        result = run_ledger_for_project(project, chunk_target_chars, config=config)
        conn.close()
        return tuple(_claim_fingerprint(cp) for cp in result.checkpoints)

    first = _run_once("run1.db")
    second = _run_once("run2.db")
    assert first == second
    assert any(fp for fp in first)  # sanity: not accidentally comparing all-empty output


def test_two_projects_in_the_same_reset_database_stay_isolated(tmp_path, migrations_dir):
    """A single connection (Section 13.7's "reset database" happens once
    per run, not once per project — matching real multi-project usage)
    must keep two projects' ledger items completely disjoint."""
    conn = connect(tmp_path / "app.db")
    run_migrations(conn, migrations_dir)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    project_ids = {}
    for key in ("implementation", "advisory"):
        project, chunk_target_chars = load_corpus_project(key)
        config = LedgerRunConfig(
            conn=conn,
            evidence_dir=evidence_dir,
            model="gpt-5.6-terra",
            reasoning_effort="low",
            brief_provider=EmptyBriefProvider(),
        )
        result = run_ledger_for_project(project, chunk_target_chars, config=config)
        project_ids[key] = result.app_project_id

    assert project_ids["implementation"] != project_ids["advisory"]
    rows = conn.execute("SELECT DISTINCT project_id FROM ledger_items").fetchall()
    assert {r["project_id"] for r in rows} == set(project_ids.values())
    conn.close()
