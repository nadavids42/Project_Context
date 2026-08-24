"""The baseline system's own wire schema (Section 13.4's control: "same
output taxonomy... same citation validation").

The baseline has no ledger, no accepted facts, and no opaque fact IDs to
cite — it is handed raw evidence text directly (Section 13.4: "provide
the same model either all evidence that fits a fixed token budget or the
most recent artifacts") and must extract *and* compose in one call. To
keep the comparison meaningful rather than "structured system vs.
freeform prose," this schema asks the baseline for the same taxonomy the
ledger's `project_context.llm.schemas.BriefComposition` claims resolve
to — one or more sections, each a list of claims that (when the claim
concerns one atomic item) carry the same structured fields
(`item_kind`/`item_title`/`status`/`owner`/`due_date`) `project_context.
evaluation.runner_types.ScoredClaim` needs — plus its own citations:
`(artifact_id, quote)` pairs the baseline must quote *verbatim* from the
evidence text it was given. `project_context.evaluation.baseline_runner`
validates each quote against the real artifact text exactly like
`project_context.services.extraction.validate_observation` validates a
real extraction span (Section 13.4: "same citation validation") —
independent evidence validation, not the model self-reporting confidence.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

BASELINE_SCHEMA_VERSION = "baseline_brief_v1"
BASELINE_PROMPT_VERSION = "baseline_brief_v1"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = _REPO_ROOT / "prompts"
_PROMPT_FILENAME = f"{BASELINE_PROMPT_VERSION}.md"

_ITEM_KIND_PATTERN = "^(decision|commitment|milestone|risk|blocker|open_question|stakeholder)$"
_STATUS_PATTERN = "^(open|active|completed|resolved|canceled|superseded)$"

_MAX_CLAIMS_PER_SECTION = 30
_MAX_SECTIONS = 10
_MAX_EVIDENCE_PER_CLAIM = 5


class BaselineEvidenceCitation(BaseModel):
    """One verbatim quote from one artifact, identified by the same
    opaque `artifact_id` the baseline was given in its input (Section
    12.4: "Keep evidence IDs opaque and supplied by the app" — here the
    artifact ID itself, since the baseline has no chunk/evidence-link ID
    system of its own to cite instead)."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    quote: str = Field(min_length=1, max_length=1200)


class BaselineClaim(BaseModel):
    """One claim. `item_kind`/`item_title`/`status`/`owner`/`due_date`
    are populated only when this claim concerns one atomic project-state
    item — a pure narrative claim (e.g. "No accepted open risks") leaves
    them unset. `evidence` may be empty only for a `suggestion`, mirroring
    `project_context.llm.schemas.BriefClaimOutput`'s own
    fact/inference-must-cite-something rule."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1200)
    claim_type: str = Field(pattern="^(fact|inference|suggestion)$")
    item_kind: str | None = Field(default=None, pattern=_ITEM_KIND_PATTERN)
    item_title: str | None = Field(default=None, max_length=300)
    status: str | None = Field(default=None, pattern=_STATUS_PATTERN)
    owner: str | None = Field(default=None, max_length=200)
    due_date: str | None = Field(default=None, max_length=20)
    evidence: list[BaselineEvidenceCitation] = Field(
        default_factory=list, max_length=_MAX_EVIDENCE_PER_CLAIM
    )

    @model_validator(mode="after")
    def _fact_and_inference_cite_something(self) -> BaselineClaim:
        if self.claim_type in ("fact", "inference") and not self.evidence:
            raise ValueError(f"a {self.claim_type!r} claim must cite at least one evidence quote")
        return self


class BaselineSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: str = Field(min_length=1, max_length=100)
    claims: list[BaselineClaim] = Field(default_factory=list, max_length=_MAX_CLAIMS_PER_SECTION)


class BaselineBriefComposition(BaseModel):
    """The full structured response for one baseline brief-generation
    call — the baseline's direct analog of
    `project_context.llm.schemas.BriefComposition`."""

    model_config = ConfigDict(extra="forbid")

    sections: list[BaselineSection] = Field(default_factory=list, max_length=_MAX_SECTIONS)


def load_baseline_system_prompt(*, prompts_dir: Path = _PROMPTS_DIR) -> str:
    path = prompts_dir / _PROMPT_FILENAME
    if not path.is_file():
        raise FileNotFoundError(
            f"baseline prompt {_PROMPT_FILENAME!r} not found under {prompts_dir}"
        )
    return path.read_text(encoding="utf-8")
