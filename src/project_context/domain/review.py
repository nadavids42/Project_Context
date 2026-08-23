"""Domain model for proposed mutations, human reviews, and corrections
(Section 9, tables `proposed_mutations`, `reviews`, `corrections`).

No reconciliation engine or review transaction exists yet — these models
and their repositories (`project_context.db.proposed_mutation_repository`,
`.review_repository`, `.correction_repository`) support pending/reviewed
queue *state* only (Prompt 6). The transactional "apply an accepted
review as a ledger transition" flow is Prompt 8's job.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel


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
