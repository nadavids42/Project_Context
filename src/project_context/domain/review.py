"""Domain model for proposed mutations, human reviews, and corrections
(Section 9, tables `proposed_mutations`, `reviews`, `corrections`), plus
`ProposalEdit` (Prompt 8) — the validated shape of a human's edit-and-
accept input, applied by `project_context.services.review`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus


class ProposedMutationAction(StrEnum):
    """Matches the `proposed_mutations.action` CHECK constraint exactly
    (Section 9)."""

    CREATE = "create"
    NO_OP = "no_op"
    ADD_EVIDENCE = "add_evidence"
    UPDATE = "update"
    COMPLETE = "complete"
    CANCEL = "cancel"
    SUPERSEDE = "supersede"
    CONFLICT = "conflict"


class ProposedMutationStatus(StrEnum):
    """Matches the `proposed_mutations.status` CHECK constraint exactly
    (Section 9)."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    EDITED_ACCEPTED = "edited_accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ProposedMutation(BaseModel):
    """A proposed-mutation row as stored. See Section 9, table
    `proposed_mutations`."""

    id: str
    project_id: str
    observation_id: str
    action: ProposedMutationAction
    target_ledger_item_id: str | None
    proposed_patch: dict[str, Any] | None
    candidate_features: dict[str, Any] | None
    confidence_score: float | None
    confidence_band: str | None
    status: ProposedMutationStatus
    created_at: str
    reviewed_at: str | None


class ReviewAction(StrEnum):
    """Matches the `reviews.action` CHECK constraint exactly (Section 9)."""

    ACCEPT = "accept"
    EDIT_ACCEPT = "edit_accept"
    REJECT = "reject"
    MARK_COMPLETE = "mark_complete"
    MARK_SUPERSEDED = "mark_superseded"
    TREAT_AS_NEW = "treat_as_new"


class Review(BaseModel):
    """A review row as stored. See Section 9, table `reviews`. At most
    one per proposal (`uq_reviews_proposal`) — a review is a final
    decision act, not a queue."""

    id: str
    project_id: str
    proposal_id: str
    action: ReviewAction
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    reason_code: str | None
    note: str | None
    actor: str
    reviewed_at: str
    duration_ms: int | None


class CorrectionReasonCode(StrEnum):
    """Matches the `corrections.reason_code` CHECK constraint exactly
    (Section 9)."""

    WRONG_TYPE = "wrong_type"
    WRONG_MATCH = "wrong_match"
    UNSUPPORTED = "unsupported"
    WRONG_OWNER = "wrong_owner"
    WRONG_DATE = "wrong_date"
    WRONG_STATUS = "wrong_status"
    WORDING = "wording"
    MISSING = "missing"
    OTHER = "other"


class CorrectionMateriality(StrEnum):
    """Matches the `corrections.materiality` CHECK constraint exactly
    (Section 9)."""

    MINOR = "minor"
    MATERIAL = "material"


class CorrectionTargetType(StrEnum):
    """Matches the `corrections.target_type` CHECK constraint exactly."""

    OBSERVATION = "observation"
    LEDGER_ITEM = "ledger_item"
    LEDGER_VERSION = "ledger_version"
    PROPOSED_MUTATION = "proposed_mutation"


def compute_error_signature(
    *,
    target_type: CorrectionTargetType | str,
    field_name: str,
    reason_code: CorrectionReasonCode | str,
) -> str:
    """A stable, safe (no free text/quotes) signature used for the
    repeated-correction-rate metric (Section 13.5) and the
    `(project_id, error_signature)` index. Deliberately a plain,
    inspectable colon-joined string rather than a hash — every input is
    already a short enum-like value, never source content."""
    target = target_type.value if isinstance(target_type, CorrectionTargetType) else target_type
    reason = reason_code.value if isinstance(reason_code, CorrectionReasonCode) else reason_code
    return f"{target}:{field_name}:{reason}"


class Correction(BaseModel):
    """A correction row as stored. See Section 9, table `corrections`,
    and FR-016. `actor` is an additive field — see
    migrations/0005_ledger_and_review.sql deviation 4."""

    id: str
    project_id: str
    target_type: CorrectionTargetType
    target_id: str
    review_id: str | None
    field_name: str
    original: dict[str, Any] | None
    corrected: dict[str, Any] | None
    reason_code: CorrectionReasonCode
    materiality: CorrectionMateriality
    error_signature: str
    model_id: str | None
    prompt_version: str | None
    actor: str
    created_at: str


class ProposalEdit(BaseModel):
    """Validated user overrides for edit-and-accept (Prompt 8; FR-020;
    Section 6's "Add edit-and-accept fields for wording, kind, owner, due
    date, status, and evidence selection, with server-side validation").

    Every field left `None` means "no override" — `project_context.
    services.review` falls back to the proposal's own `proposed_patch`
    value, then to the current ledger item's existing value. There is
    deliberately no way to *clear* owner/due_date back to null through
    this shape (a known, documented UX limitation — see the Prompt 8
    report) since `None` already means "unchanged" here.

    `action`/`target_ledger_item_id` let a human redirect a proposal
    entirely — accepting a different action than reconciliation proposed
    (e.g. treating an UPDATE as a COMPLETE) or a different candidate item
    (resolving an ambiguous/CONFLICT match) — while `status` is the
    primary lever for *which* transition happens: it is resolved to a
    transition type first (Section 10's status vocabulary), with
    `action` only consulted when `status` is not given.
    """

    model_config = ConfigDict(extra="forbid")

    action: ProposedMutationAction | None = None
    target_ledger_item_id: str | None = None
    kind: LedgerItemKind | None = None
    canonical_title: str | None = None
    canonical_description: str | None = None
    owner_person_id: str | None = None
    due_date: str | None = None
    status: LedgerItemStatus | None = None
    #: Which of the observation's own evidence links to carry forward.
    #: `None` means "all of them." An empty tuple is rejected by
    #: `services.review` (an accepted change must cite at least one
    #: piece of evidence) rather than by this shape, since "at least one"
    #: is a review-transaction rule, not a wire-shape constraint.
    evidence_link_ids: tuple[str, ...] | None = None

    @field_validator("canonical_title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("canonical_title, if given, must not be blank")
        return value
