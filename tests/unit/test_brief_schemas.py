"""Tests for the Stage C wire schema (`project_context.llm.schemas.
BriefComposition`/`BriefSectionOutput`/`BriefClaimOutput`; Section 12.4;
Prompt 9)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from project_context.llm.schemas import (
    BRIEF_SCHEMA_VERSION,
    BriefClaimOutput,
    BriefComposition,
    BriefSectionOutput,
)


def test_brief_schema_version_is_set():
    assert BRIEF_SCHEMA_VERSION == "brief_composition_v1"


def test_valid_fact_claim_round_trips():
    claim = BriefClaimOutput(
        text="The report is open, due Sep 4.", claim_type="fact", fact_ids=["f1"]
    )
    assert claim.claim_type == "fact"
    assert claim.fact_ids == ["f1"]


def test_fact_claim_without_fact_ids_is_rejected():
    with pytest.raises(ValidationError):
        BriefClaimOutput(text="The report is open.", claim_type="fact", fact_ids=[])


def test_inference_claim_without_fact_ids_is_rejected():
    with pytest.raises(ValidationError):
        BriefClaimOutput(text="This will likely slip.", claim_type="inference", fact_ids=[])


def test_inference_claim_with_fact_ids_is_valid():
    claim = BriefClaimOutput(
        text="Two related dates now conflict.", claim_type="inference", fact_ids=["f1", "f2"]
    )
    assert claim.claim_type == "inference"


def test_suggestion_claim_needs_no_fact_ids():
    claim = BriefClaimOutput(
        text="Consider revisiting the timeline.", claim_type="suggestion", fact_ids=[]
    )
    assert claim.fact_ids == []


def test_unknown_claim_type_is_rejected():
    with pytest.raises(ValidationError):
        BriefClaimOutput(text="Something.", claim_type="opinion", fact_ids=["f1"])


def test_extra_property_is_rejected():
    with pytest.raises(ValidationError):
        BriefClaimOutput.model_validate(
            {"text": "Something.", "claim_type": "fact", "fact_ids": ["f1"], "confidence": 0.9}
        )


def test_section_round_trips_with_multiple_claims():
    section = BriefSectionOutput(
        section="open_commitments",
        claims=[
            BriefClaimOutput(text="A.", claim_type="fact", fact_ids=["f1"]),
            BriefClaimOutput(text="B.", claim_type="fact", fact_ids=["f2"]),
        ],
    )
    assert len(section.claims) == 2


def test_composition_with_no_sections_is_valid():
    composition = BriefComposition(sections=[])
    assert composition.sections == []


def test_composition_round_trips_with_multiple_sections():
    composition = BriefComposition(
        sections=[
            BriefSectionOutput(
                section="open_commitments",
                claims=[BriefClaimOutput(text="A.", claim_type="fact", fact_ids=["f1"])],
            ),
            BriefSectionOutput(section="decisions", claims=[]),
        ]
    )
    assert [s.section for s in composition.sections] == ["open_commitments", "decisions"]


def test_claim_text_length_is_bounded():
    with pytest.raises(ValidationError):
        BriefClaimOutput(text="x" * 1201, claim_type="fact", fact_ids=["f1"])


def test_claim_text_must_be_non_empty():
    with pytest.raises(ValidationError):
        BriefClaimOutput(text="", claim_type="fact", fact_ids=["f1"])
