"""Shared result types both runners (`project_context.evaluation.
ledger_runner`, `project_context.evaluation.baseline_runner`) produce, so
`project_context.evaluation.scoring` has exactly one shape to score
regardless of which system produced it (Section 13.4: "same output
taxonomy").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class EvidenceRef:
    artifact_id: str
    quote: str


@dataclass(frozen=True)
class ScoredClaim:
    """One brief claim, normalized to a common shape for scoring —
    whether it came from the ledger's `BriefClaimRecord`/`BriefFact` or
    the baseline's `BaselineClaim` (`project_context.evaluation.
    baseline_schema`)."""

    section: str
    text: str
    claim_type: str
    #: `None` when this claim does not represent one atomic item (e.g. a
    #: pure prose "no accepted items" placeholder).
    item_kind: str | None
    item_title: str | None
    status: str | None
    owner: str | None
    due_date: str | None
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class CheckpointRunResult:
    checkpoint_id: str
    brief_type: str
    system: Literal["ledger", "baseline"]
    claims: tuple[ScoredClaim, ...]
    markdown: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    latency_ms: int
    #: Ledger only: counts of review actions applied to reach this
    #: checkpoint's cutoff, *since the previous checkpoint* (Section
    #: 13.5's "review burden... minutes/sync"; see
    #: `project_context.evaluation.ledger_runner`'s module docstring for
    #: what counts as one "sync" in this harness). Always zero tuple
    #: values for the baseline system, which has no review step.
    accepted_without_edit: int = 0
    edited_accepted: int = 0
    rejected: int = 0


@dataclass(frozen=True)
class ProjectRunResult:
    project_key: str
    system: Literal["ledger", "baseline"]
    checkpoints: tuple[CheckpointRunResult, ...] = field(default_factory=tuple)
