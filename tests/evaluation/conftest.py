"""Shared fixtures for the evaluation-harness test suite."""

from __future__ import annotations

import pytest

from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus
from project_context.evaluation.schema import (
    ArtifactKind,
    BriefTypeLiteral,
    Checkpoint,
    CorpusArtifact,
    CorpusProject,
    EvidenceMention,
    FactRef,
    GroundTruthItem,
    GroundTruthTransition,
    TransitionType,
)

_K = LedgerItemKind
_S = LedgerItemStatus
_T = TransitionType


def _artifact(
    artifact_id: str, occurred_at: str, text: str, *, ambiguous: bool = False, material: bool = True
) -> CorpusArtifact:
    return CorpusArtifact(
        artifact_id=artifact_id,
        kind=ArtifactKind.DOCUMENT,
        title=artifact_id,
        occurred_at=occurred_at,
        filename=f"{artifact_id}.md",
        raw_bytes=text.encode("utf-8"),
        project_key="tiny",
        ambiguous_assignment=ambiguous,
        material=material,
    )


@pytest.fixture
def tiny_project() -> CorpusProject:
    """A minimal, fully deterministic two-item, two-artifact project —
    small enough to hand-compute every expected metric value, unlike the
    three full benchmark corpora (used for scale/integration coverage
    elsewhere, not formula verification)."""
    a1_text = "Sam will finish the design doc by 2026-01-10. This was noted for the project record."
    a2_text = (
        "The design doc was sent to the client for review. This was noted for the project record."
    )
    irrelevant_text = (
        "By the way, nothing project-related happened today, just routine chatter here."
    )

    artifacts = (
        _artifact("tiny-a1", "2026-01-01T09:00:00Z", a1_text),
        _artifact("tiny-a2", "2026-01-08T09:00:00Z", a2_text),
        _artifact("tiny-a3-irrelevant", "2026-01-09T09:00:00Z", irrelevant_text, material=False),
        _artifact(
            "tiny-a4-ambiguous",
            "2026-01-10T09:00:00Z",
            "Ambiguous mention of another project.",
            ambiguous=True,
            material=False,
        ),
    )

    commitment_statement = "Sam will finish the design doc by 2026-01-10."
    complete_statement = "The design doc was sent to the client for review."

    item = GroundTruthItem(
        item_id="commitment-design-doc",
        kind=_K.COMMITMENT,
        canonical_title="Design doc",
        transitions=(
            GroundTruthTransition(
                transition_id="design-doc-create",
                type=_T.CREATE,
                mentions=(
                    EvidenceMention(
                        artifact_id="tiny-a1", statement=commitment_statement, observed_owner="Sam"
                    ),
                ),
                owner="Sam",
                due_date="2026-01-10",
                status=_S.OPEN,
            ),
            GroundTruthTransition(
                transition_id="design-doc-complete",
                type=_T.UPDATE_STATUS,
                mentions=(EvidenceMention(artifact_id="tiny-a2", statement=complete_statement),),
                status=_S.COMPLETED,
            ),
        ),
    )
    # Deliberately just this one real item — `CorpusProject` itself does
    # not enforce Section 13.2's >=25-material-records/12-20-artifacts
    # benchmark-sizing rules (see `project_context.evaluation.schema.
    # require_benchmark_corpus`, called only for the three real frozen
    # corpora); a small, fully hand-computable fixture is the point here.

    checkpoints = (
        Checkpoint(
            checkpoint_id="tiny-cp-mid",
            cutoff_at="2026-01-08T09:00:00Z",
            brief_type=BriefTypeLiteral.CURRENT_PROJECT,
        ),
        Checkpoint(
            checkpoint_id="tiny-cp-final",
            cutoff_at="2026-01-11T00:00:00Z",
            brief_type=BriefTypeLiteral.CURRENT_PROJECT,
        ),
    )

    fact_plan = {
        "tiny-a1": (FactRef(item_id="commitment-design-doc", transition_id="design-doc-create"),),
        "tiny-a2": (FactRef(item_id="commitment-design-doc", transition_id="design-doc-complete"),),
        "tiny-a3-irrelevant": (None,),
        "tiny-a4-ambiguous": (None,),
    }

    return CorpusProject(
        key="tiny",
        name="Tiny Test Project",
        objective="A minimal fixture for exact metric verification.",
        stage="Test",
        artifacts=artifacts,
        items=(item,),
        checkpoints=checkpoints,
        fact_plan=fact_plan,
    )
