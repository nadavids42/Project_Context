"""Stored-observation domain model and exact-duplicate fingerprinting.

Distinct from ``project_context.llm.schemas.ExtractedObservation``, which
is the *wire/proposal* shape a provider returns and
``project_context.services.extraction`` evidence-validates before it is
ever persisted. ``Observation`` here is the immutable, stored row (Section
9, table ``observations``) — see migrations/0005_ledger_and_review.sql
for the schema and its documented deviations from Section 9's literal
field list (one ``statement`` column instead of ``predicate``/``object``,
a defaulted ``polarity``, an additive ``schema_version``).

``compute_fingerprint`` implements Section 10.2 #3's exact-duplicate rule
("hash of (content_id, kind, normalized statement, sorted evidence
spans)") — the *fingerprint*, not the fuzzy/candidate matching Section 10
otherwise describes. Building this one normalization primitive is not
reconciliation; it is what Prompt 6 explicitly asks the observation
repository to use for "exact-source fingerprint deduplication."
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel

from project_context.spans import normalize_whitespace

#: A conservative, safe-to-strip punctuation set for fingerprinting only
#: (Section 10.1: "conservative punctuation removal"). Never applied to
#: anything a human sees — this exists purely so two propositions that
#: differ only in trailing punctuation still fingerprint identically.
_FINGERPRINT_PUNCTUATION_TABLE = str.maketrans("", "", ".,;:!?\"'`‘’“”()[]{}")


class ObservationStatus(StrEnum):
    """Matches the `observations.status` CHECK constraint exactly."""

    VALID = "valid"
    INVALID = "invalid"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    RECONCILED = "reconciled"


class Observation(BaseModel):
    """An observation row as stored. See Section 9, table `observations`,
    and migrations/0005_ledger_and_review.sql for documented deviations."""

    id: str
    project_id: str
    content_id: str
    chunk_id: str
    kind: str
    subject: str
    statement: str
    owner_text: str | None
    owner_person_id: str | None
    date_value: str | None
    date_text: str | None
    polarity: str
    explicitness: str
    model_id: str | None
    prompt_version: str | None
    schema_version: str | None
    fingerprint: str
    status: ObservationStatus
    created_at: str


def _normalize_for_fingerprint(text: str) -> str:
    """Unicode NFKC, lowercase, conservative punctuation removal, then
    conservative whitespace collapse (Section 10.1's normalization rule,
    reused here for exact fingerprinting only — no fuzzy/lexical matching
    is implemented)."""
    nfkc = unicodedata.normalize("NFKC", text)
    lowered = nfkc.lower()
    without_punctuation = lowered.translate(_FINGERPRINT_PUNCTUATION_TABLE)
    return normalize_whitespace(without_punctuation)


def compute_fingerprint(
    *,
    content_id: str,
    kind: str,
    statement: str,
    evidence_spans: Sequence[tuple[str, int, int]],
) -> str:
    """The exact-duplicate fingerprint (Section 10.2 #3): a hash of
    content_id, kind, the normalized statement, and the sorted set of
    cited evidence spans (chunk_id, char_start, char_end). Two
    observations with an identical fingerprint are the same proposition
    from the same content, cited the same way — not merely similar."""
    normalized_statement = _normalize_for_fingerprint(statement)
    spans_key = ",".join(
        f"{chunk_id}:{start}:{end}" for chunk_id, start, end in sorted(evidence_spans)
    )
    raw = f"{content_id}|{kind}|{normalized_statement}|{spans_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
