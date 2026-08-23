"""Structured-extraction Pydantic v2 schemas.

Implements the representative schemas from docs/Project_Context_Product_Plan_v1.md
Section 9 ("Representative Pydantic schemas") verbatim in shape and intent:
``ObservationKind``, ``EvidenceSpan``, ``ExtractedObservation``, and
``ExtractionBatch``. These are the *wire* schemas passed to the OpenAI
Responses API as ``text_format`` (Section 12.4, "structured-output
requirements") — the model is constrained to return exactly this shape.

Every model here sets ``extra="forbid"`` so the JSON Schema OpenAI derives
from it carries ``additionalProperties: false`` (Section 12.4: "Set
additionalProperties: false"). Shape correctness is necessary but not
sufficient — Section 12.4 is explicit that "a structurally valid response
is still subjected to evidence validation," which is why this module
contains no chunk-membership, quote-matching, owner-support, or date-
plausibility checks: those require the actual source text and run in
``project_context.services.extraction`` after a batch has already passed
through this schema.

``SCHEMA_VERSION`` is versioned independently of the prompt version
(Section 12.4: "Store schema version separately from prompt version") —
bump it whenever a field, constraint, or enum value here changes in a way
that could affect regression fixtures pinned to it.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Bumped whenever these schemas change in a way that affects a structured
#: model response or the deterministic validators in
#: project_context.services.extraction. Independent of PROMPT_VERSION
#: (project_context.llm.prompts) per Section 12.4.
SCHEMA_VERSION = "extraction_batch_v1"

_MAX_OBSERVATIONS_PER_BATCH = 100
_MAX_EVIDENCE_SPANS_PER_OBSERVATION = 5


class ObservationKind(StrEnum):
    """The allowed atomic-observation taxonomy (Section 4, Section 9).

    Deliberately closed — an unknown kind is a schema-level rejection, not
    something the deterministic validator decides (FR-011: "unknown types
    fail closed").
    """

    UPDATE = "update"
    DECISION = "decision"
    COMMITMENT = "commitment"
    MILESTONE = "milestone"
    RISK = "risk"
    BLOCKER = "blocker"
    OPEN_QUESTION = "open_question"
    STAKEHOLDER = "stakeholder"


class EvidenceSpan(BaseModel):
    """One cited span within one chunk supplied to the model.

    ``chunk_id`` is opaque to the model — Section 12.4: "Keep evidence IDs
    opaque and supplied by the app." Offsets and the quote are validated
    for internal consistency here (end after start, non-empty quote); they
    are validated against the *actual* chunk text only in
    project_context.services.extraction, which is the only place that has
    it.
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=1200)
    location_label: str | None = None  # e.g. "page 3" or "00:14:22"

    @model_validator(mode="after")
    def end_after_start(self) -> EvidenceSpan:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        return self


class ExtractedObservation(BaseModel):
    """One atomic proposition extracted from one chunk (FR-010).

    ``owner_name`` and ``date_value``/``date_text`` are nullable by design
    (Section 12.8: "Owner/date fields must be stated or null; validators
    reject unsupported entities") — the model is instructed never to
    invent them, and the deterministic validator rejects an owner name
    that cannot be found in the cited evidence.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ObservationKind
    subject: str = Field(min_length=1, max_length=300)
    statement: str = Field(min_length=1, max_length=1500)
    owner_name: str | None = Field(default=None, max_length=200)
    date_value: date | None = None
    date_text: str | None = Field(default=None, max_length=100)
    explicitness: str = Field(pattern="^(explicit|strongly_entailed)$")
    proposed_state: str | None = Field(
        default=None,
        pattern="^(open|active|completed|resolved|canceled|superseded)$",
    )
    evidence: list[EvidenceSpan] = Field(
        min_length=1, max_length=_MAX_EVIDENCE_SPANS_PER_OBSERVATION
    )


class ExtractionBatch(BaseModel):
    """The full structured response for one chunk (Stage A, Section 12.3).

    ``source_contains_no_material_updates`` must be internally consistent
    with ``observations`` — the model is explicitly permitted (and
    expected, per Section 12.3: "omission is preferable to invention") to
    return an empty batch, but it must say so explicitly rather than
    leaving the empty case ambiguous.
    """

    model_config = ConfigDict(extra="forbid")

    observations: list[ExtractedObservation] = Field(max_length=_MAX_OBSERVATIONS_PER_BATCH)
    source_contains_no_material_updates: bool

    @model_validator(mode="after")
    def empty_flag_consistent(self) -> ExtractionBatch:
        if self.source_contains_no_material_updates == bool(self.observations):
            raise ValueError("empty flag and observations disagree")
        return self
