"""Schema-level validation for the structured-extraction Pydantic models
(Section 9, Section 15 "Schema validation"): invalid enums, reversed
spans, empty observation lists vs. the empty flag, and oversized/extra
fields. These are pure Pydantic-layer tests — no provider, no database,
no chunk text — the deterministic *evidence* validator (chunk
membership, quote matching, owner support, date plausibility) is tested
separately in tests/unit/test_extraction_service.py, since it needs real
chunk text to compare against."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from project_context.llm.schemas import (
    EvidenceSpan,
    ExtractedObservation,
    ExtractionBatch,
    ObservationKind,
)

VALID_SPAN = {
    "chunk_id": "chunk-1",
    "char_start": 0,
    "char_end": 5,
    "quote": "hello",
}


def _observation(**overrides):
    base = {
        "kind": "commitment",
        "subject": "Priya",
        "statement": "Priya will send the report by Friday.",
        "owner_name": "Priya",
        "explicitness": "explicit",
        "evidence": [VALID_SPAN],
    }
    base.update(overrides)
    return base


def test_observation_kind_enum_has_exactly_the_eight_allowed_values():
    assert {k.value for k in ObservationKind} == {
        "update",
        "decision",
        "commitment",
        "milestone",
        "risk",
        "blocker",
        "open_question",
        "stakeholder",
    }


def test_valid_observation_round_trips():
    obs = ExtractedObservation.model_validate(_observation())
    assert obs.kind == ObservationKind.COMMITMENT
    assert obs.owner_name == "Priya"


def test_unknown_kind_is_rejected():
    with pytest.raises(ValidationError):
        ExtractedObservation.model_validate(_observation(kind="not_a_real_kind"))


def test_extra_property_is_rejected():
    with pytest.raises(ValidationError):
        ExtractedObservation.model_validate(_observation(unexpected_field="surprise"))


def test_reversed_span_is_rejected():
    with pytest.raises(ValidationError):
        EvidenceSpan.model_validate(
            {"chunk_id": "chunk-1", "char_start": 10, "char_end": 5, "quote": "x"}
        )


def test_zero_width_span_is_rejected():
    with pytest.raises(ValidationError):
        EvidenceSpan.model_validate(
            {"chunk_id": "chunk-1", "char_start": 5, "char_end": 5, "quote": "x"}
        )


def test_empty_quote_is_rejected_at_schema_level():
    with pytest.raises(ValidationError):
        EvidenceSpan.model_validate(
            {"chunk_id": "chunk-1", "char_start": 0, "char_end": 5, "quote": ""}
        )


def test_negative_char_start_is_rejected():
    with pytest.raises(ValidationError):
        EvidenceSpan.model_validate(
            {"chunk_id": "chunk-1", "char_start": -1, "char_end": 5, "quote": "hello"}
        )


def test_oversized_subject_is_rejected():
    with pytest.raises(ValidationError):
        ExtractedObservation.model_validate(_observation(subject="x" * 301))


def test_invalid_explicitness_is_rejected():
    with pytest.raises(ValidationError):
        ExtractedObservation.model_validate(_observation(explicitness="pretty_sure"))


def test_invalid_proposed_state_is_rejected():
    with pytest.raises(ValidationError):
        ExtractedObservation.model_validate(_observation(proposed_state="deleted"))


def test_observation_requires_at_least_one_evidence_span():
    with pytest.raises(ValidationError):
        ExtractedObservation.model_validate(_observation(evidence=[]))


def test_owner_name_and_date_default_to_null():
    obs = ExtractedObservation.model_validate(
        {
            "kind": "risk",
            "subject": "Launch date",
            "statement": "The launch date may slip.",
            "explicitness": "strongly_entailed",
            "evidence": [VALID_SPAN],
        }
    )
    assert obs.owner_name is None
    assert obs.date_value is None
    assert obs.date_text is None


def test_extraction_batch_with_observations_and_empty_flag_true_is_rejected():
    with pytest.raises(ValidationError):
        ExtractionBatch.model_validate(
            {
                "observations": [_observation()],
                "source_contains_no_material_updates": True,
            }
        )


def test_extraction_batch_empty_observations_and_empty_flag_false_is_rejected():
    with pytest.raises(ValidationError):
        ExtractionBatch.model_validate(
            {"observations": [], "source_contains_no_material_updates": False}
        )


def test_extraction_batch_empty_result_is_valid():
    batch = ExtractionBatch.model_validate(
        {"observations": [], "source_contains_no_material_updates": True}
    )
    assert batch.observations == []


def test_extraction_batch_with_observations_is_valid():
    batch = ExtractionBatch.model_validate(
        {
            "observations": [_observation(), _observation(subject="Second item")],
            "source_contains_no_material_updates": False,
        }
    )
    assert len(batch.observations) == 2
