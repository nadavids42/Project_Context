"""Corpus/ground-truth schema validation (Prompt 15's required test
category). Covers both `CorpusProject`'s own structural Pydantic
validators and the separate Section-13.2 benchmark-sizing checks
(`project_context.evaluation.schema.benchmark_requirement_violations`),
plus the three real frozen corpora actually satisfying every one.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus
from project_context.evaluation.corpus_data import ALL_PROJECT_BUILDERS, ALL_PROJECT_KEYS
from project_context.evaluation.materialize import load_corpus_project, validate_corpus_project
from project_context.evaluation.schema import (
    BriefTypeLiteral,
    Checkpoint,
    CorpusProject,
    EvidenceMention,
    FactRef,
    GroundTruthItem,
    GroundTruthTransition,
    Materiality,
    TransitionType,
    benchmark_requirement_violations,
)


def _minimal_project(**overrides) -> CorpusProject:  # noqa: ANN003
    from evaluation.conftest import _artifact

    base = dict(
        key="p",
        name="P",
        objective="Obj",
        stage="Stage",
        artifacts=(
            _artifact(
                "a1", "2026-01-01T00:00:00Z", "hello world, this is filler content padded out."
            ),
        ),
        items=(
            GroundTruthItem(
                item_id="i1",
                kind=LedgerItemKind.COMMITMENT,
                canonical_title="Thing",
                transitions=(
                    GroundTruthTransition(
                        transition_id="t1",
                        type=TransitionType.CREATE,
                        mentions=(EvidenceMention(artifact_id="a1", statement="hello world"),),
                        status=LedgerItemStatus.OPEN,
                    ),
                ),
            ),
        ),
        checkpoints=(),
    )
    base.update(overrides)
    return CorpusProject(**base)


def test_tiny_project_is_structurally_valid(tiny_project):
    # No exception means every structural validator (duplicate IDs,
    # referential integrity, transition ordering) passed.
    assert tiny_project.key == "tiny"


def test_duplicate_artifact_id_rejected():
    from evaluation.conftest import _artifact

    dup = _artifact(
        "dup", "2026-01-01T00:00:00Z", "text one padded out enough to be a real block of text."
    )
    with pytest.raises(ValidationError, match="duplicate artifact_id"):
        CorpusProject(
            key="p",
            name="P",
            objective="o",
            stage="s",
            artifacts=(dup, dup),
            items=(),
            checkpoints=(),
        )


def test_transition_citing_unknown_artifact_rejected():
    with pytest.raises(ValidationError, match="unknown artifact_id"):
        _minimal_project(
            items=(
                GroundTruthItem(
                    item_id="i1",
                    kind=LedgerItemKind.COMMITMENT,
                    canonical_title="Thing",
                    transitions=(
                        GroundTruthTransition(
                            transition_id="t1",
                            type=TransitionType.CREATE,
                            mentions=(
                                EvidenceMention(artifact_id="does-not-exist", statement="x"),
                            ),
                            status=LedgerItemStatus.OPEN,
                        ),
                    ),
                ),
            )
        )


def test_supersede_without_predecessor_rejected():
    with pytest.raises(ValidationError, match="SUPERSEDE with no predecessor"):
        GroundTruthTransition(
            transition_id="t1",
            type=TransitionType.SUPERSEDE,
            mentions=(EvidenceMention(artifact_id="a1", statement="x"),),
            status=LedgerItemStatus.OPEN,
        )


def test_non_supersede_with_predecessor_rejected():
    with pytest.raises(ValidationError, match="not SUPERSEDE"):
        GroundTruthTransition(
            transition_id="t1",
            type=TransitionType.CREATE,
            mentions=(EvidenceMention(artifact_id="a1", statement="x"),),
            status=LedgerItemStatus.OPEN,
            supersedes_item_id="other",
        )


def test_first_transition_must_create_or_supersede():
    with pytest.raises(ValidationError, match="must be CREATE or SUPERSEDE"):
        GroundTruthItem(
            item_id="i1",
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Thing",
            transitions=(
                GroundTruthTransition(
                    transition_id="t1",
                    type=TransitionType.UPDATE_OWNER,
                    mentions=(EvidenceMention(artifact_id="a1", statement="x"),),
                    owner="Bob",
                ),
            ),
        )


def test_item_with_no_transitions_rejected():
    with pytest.raises(ValidationError, match="no transitions"):
        GroundTruthItem(
            item_id="i1", kind=LedgerItemKind.COMMITMENT, canonical_title="Thing", transitions=()
        )


def test_create_without_resulting_status_rejected():
    with pytest.raises(ValidationError, match="must set a resulting status"):
        GroundTruthTransition(
            transition_id="t1",
            type=TransitionType.CREATE,
            mentions=(EvidenceMention(artifact_id="a1", statement="x"),),
        )


def test_meeting_prep_checkpoint_requires_meeting_fields():
    with pytest.raises(ValidationError, match="missing meeting_title"):
        Checkpoint(
            checkpoint_id="cp1",
            cutoff_at="2026-01-01T00:00:00Z",
            brief_type=BriefTypeLiteral.MEETING_PREPARATION,
        )


def test_fact_plan_referencing_unknown_item_rejected():
    with pytest.raises(ValidationError, match="unknown item_id"):
        _minimal_project(fact_plan={"a1": (FactRef(item_id="nope", transition_id="t1"),)})


def test_benchmark_requirement_violations_flags_undersized_fixture():
    project = _minimal_project()
    violations = benchmark_requirement_violations(project)
    assert any("material ground-truth" in v for v in violations)
    assert any("artifacts" in v for v in violations)
    assert any("ambiguous-assignment" in v for v in violations)
    assert any("irrelevant" in v for v in violations)


def test_benchmark_requirement_violations_flags_size_but_not_trap_coverage(tiny_project):
    # tiny_project deliberately satisfies the *trap-coverage* rules (one
    # ambiguous artifact, one irrelevant artifact) but is intentionally
    # far smaller than a real benchmark corpus — only the size rules
    # should fire, never the trap-coverage ones.
    violations = benchmark_requirement_violations(tiny_project)
    assert not any("ambiguous-assignment" in v for v in violations)
    assert not any("irrelevant" in v for v in violations)
    assert any("material ground-truth" in v for v in violations)
    assert any("artifacts" in v for v in violations)


@pytest.mark.parametrize("project_key", ALL_PROJECT_KEYS)
def test_real_benchmark_corpus_is_frozen_and_valid(project_key):
    project, chunk_target_chars = load_corpus_project(project_key)
    validate_corpus_project(
        project, chunk_target_chars=chunk_target_chars
    )  # raises on any violation
    assert project.key == project_key
    material_records = sum(
        1
        for item in project.items
        for transition in item.transitions
        if transition.materiality is not Materiality.AMBIGUOUS
    )
    assert material_records >= 25
    assert 12 <= len(project.artifacts) <= 20
    assert any(a.ambiguous_assignment for a in project.artifacts)
    assert any(not a.material for a in project.artifacts)


def test_all_three_project_builders_produce_distinct_keys():
    keys = [builder()[0].key for builder in ALL_PROJECT_BUILDERS]
    assert sorted(keys) == sorted(ALL_PROJECT_KEYS)
    assert len(set(keys)) == 3
