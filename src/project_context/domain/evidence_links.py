"""Evidence-link domain model: many-to-many provenance from an
observation, ledger item, ledger version, or (later) brief claim to
exact evidence. See Section 9, table `evidence_links`, and
migrations/0005_ledger_and_review.sql for the polymorphic
`target_type`/`target_id` design and why it carries no SQL foreign key
on `target_id`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class EvidenceLinkTargetType(StrEnum):
    """Matches the `evidence_links.target_type` CHECK constraint exactly.

    `BRIEF_CLAIM` is accepted by the schema for forward compatibility
    (Prompt 6: "can target... later brief claims") but
    `project_context.db.evidence_link_repository.insert_link` refuses to
    actually insert one until a `brief_claims` table exists to validate
    against — see that module.
    """

    OBSERVATION = "observation"
    LEDGER_ITEM = "ledger_item"
    LEDGER_VERSION = "ledger_version"
    BRIEF_CLAIM = "brief_claim"


class EvidenceLinkSupportRole(StrEnum):
    """Matches the `evidence_links.support_role` CHECK constraint exactly."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT = "context"
    COMPLETION = "completion"
    SUPERSESSION = "supersession"


class EvidenceLink(BaseModel):
    """An evidence-link row as stored. See Section 9, table `evidence_links`."""

    id: str
    project_id: str
    target_type: EvidenceLinkTargetType
    target_id: str
    content_id: str
    chunk_id: str | None
    char_start: int
    char_end: int
    quote: str
    location: dict | None
    support_role: EvidenceLinkSupportRole
    created_at: str
