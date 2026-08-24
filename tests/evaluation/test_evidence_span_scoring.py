"""Evidence-span scoring (Prompt 15's required test category): a claim's
cited quote must be validated against the *real* artifact text — exactly
like a real extraction span — for both systems.
"""

from __future__ import annotations

from project_context.evaluation.baseline_runner import _validate_claim_evidence
from project_context.evaluation.baseline_schema import BaselineClaim, BaselineEvidenceCitation


def test_real_verbatim_quote_is_kept(tiny_project):
    artifact = tiny_project.artifact_by_id("tiny-a1")
    claim = BaselineClaim(
        text="x",
        claim_type="fact",
        evidence=[
            BaselineEvidenceCitation(
                artifact_id="tiny-a1", quote="Sam will finish the design doc by 2026-01-10."
            )
        ],
    )
    valid = _validate_claim_evidence(claim, {"tiny-a1": artifact})
    assert len(valid) == 1
    assert valid[0].artifact_id == "tiny-a1"


def test_fabricated_quote_is_dropped(tiny_project):
    artifact = tiny_project.artifact_by_id("tiny-a1")
    claim = BaselineClaim(
        text="x",
        claim_type="fact",
        evidence=[
            BaselineEvidenceCitation(
                artifact_id="tiny-a1", quote="This exact sentence was never said."
            )
        ],
    )
    valid = _validate_claim_evidence(claim, {"tiny-a1": artifact})
    assert valid == ()


def test_quote_from_a_different_artifact_than_cited_is_dropped(tiny_project):
    """The quote is real (it appears in tiny-a2's text) but the claim
    cites tiny-a1 for it — must not validate against the wrong source."""
    claim = BaselineClaim(
        text="x",
        claim_type="fact",
        evidence=[
            BaselineEvidenceCitation(
                artifact_id="tiny-a1", quote="The design doc was sent to the client for review."
            )
        ],
    )
    artifacts_by_id = {"tiny-a1": tiny_project.artifact_by_id("tiny-a1")}
    valid = _validate_claim_evidence(claim, artifacts_by_id)
    assert valid == ()


def test_citation_to_an_artifact_outside_the_supplied_context_is_dropped(tiny_project):
    claim = BaselineClaim(
        text="x",
        claim_type="fact",
        evidence=[
            BaselineEvidenceCitation(
                artifact_id="tiny-a2", quote="The design doc was sent to the client for review."
            )
        ],
    )
    # Only tiny-a1 was "supplied" to the model in this scenario.
    valid = _validate_claim_evidence(claim, {"tiny-a1": tiny_project.artifact_by_id("tiny-a1")})
    assert valid == ()


def test_baseline_claim_requires_at_least_one_evidence_entry_for_fact_and_inference():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="must cite at least one evidence quote"):
        BaselineClaim(text="x", claim_type="fact", evidence=[])
    with pytest.raises(ValidationError, match="must cite at least one evidence quote"):
        BaselineClaim(text="x", claim_type="inference", evidence=[])
    # A suggestion legitimately needs none.
    BaselineClaim(text="x", claim_type="suggestion", evidence=[])
