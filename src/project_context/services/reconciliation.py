"""Reconciliation orchestration (Section 10; FR-017 through FR-020;
Prompt 7). Combines candidate generation (I/O, this module) with the pure
domain logic in `project_context.domain.reconciliation` to turn one
validated, unreconciled `Observation` into one idempotent
`proposed_mutations` row.

Scope boundary, same as every other service in this codebase: this module
writes `proposed_mutations` and updates `observations.status` only. It
never inserts/updates `ledger_items` or `ledger_versions` — turning an
*accepted* proposal into a ledger transition is the review transaction's
job (Prompt 8, still out of scope). Every reconciliation test in
`tests/unit/test_reconciliation_service.py` asserts this directly (zero
ledger-table row-count change across a reconciliation run).

No LLM call happens anywhere in this module or the domain module it
calls into — every decision is deterministic Python over rows already in
SQLite.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from project_context.db import (
    evidence_link_repository,
    ledger_repository,
    observation_repository,
    people_repository,
    proposed_mutation_repository,
)
from project_context.db.connection import transaction
from project_context.domain.evidence_links import EvidenceLinkTargetType
from project_context.domain.ledger import LedgerItem
from project_context.domain.observations import Observation, ObservationStatus
from project_context.domain.people import PersonResolution
from project_context.domain.reconciliation import (
    DEFAULT_RECONCILIATION_CONFIG,
    PRIMARY_COMPATIBLE_KINDS,
    ActionClassification,
    MatchOutcome,
    MatchTier,
    ReconciliationConfig,
    ScoredCandidate,
    classify_action,
    compatible_kinds,
    compute_match_features,
    determine_match_outcome,
    extraction_confidence,
    match_confidence,
    mutation_confidence,
    score_match,
    weakest_band,
)
from project_context.domain.reconciliation_language import detect_local_reference
from project_context.domain.review import ProposedMutation
from project_context.domain.text_normalization import (
    NormalizedDate,
    extract_named_entities,
    normalize_date,
    normalize_form,
    subject_tokens,
    token_jaccard,
)
from project_context.llm.schemas import ObservationKind


class ObservationNotFoundError(LookupError):
    """Raised when `reconcile_observation` is given an observation id that
    does not exist in the given project."""


class ObservationNotReconcilableError(ValueError):
    """Raised when the target observation exists but is not in a state
    reconciliation is willing to process (only `valid` observations are)."""


# ---------------------------------------------------------------------------
# Candidate generation (Section 10.3's four-tier query order). SQL/lookup
# I/O lives here; scoring and everything after it is pure domain logic.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateRecord:
    ledger_item: LedgerItem
    retrieval_tiers: frozenset[str]


def generate_candidates(
    conn: sqlite3.Connection,
    project_id: str,
    observation: Observation,
    *,
    observation_kind: ObservationKind,
    obs_tokens: frozenset[str],
    local_reference_found: bool,
    config: ReconciliationConfig,
) -> list[CandidateRecord]:
    """Section 10.3's candidate query order, unioned (not short-
    circuited — "if no candidate, propose create" only applies to the
    *union* being empty). Always `project_id`-scoped and restricted to
    `compatible_kinds(observation_kind)` — never a cross-project or
    incompatible-type query (FR-017)."""
    all_kinds = compatible_kinds(observation_kind)
    if not all_kinds:
        return []

    is_update = observation_kind is ObservationKind.UPDATE
    items_by_id: dict[str, LedgerItem] = {}
    tiers_by_id: dict[str, set[str]] = defaultdict(set)

    def remember(item: LedgerItem, tier: str) -> None:
        items_by_id[item.id] = item
        tiers_by_id[item.id].add(tier)

    # Tier 1: exact stable identity — the candidate's own title, once
    # normalized, is exactly the observation's subject.
    normalized_subject = normalize_form(observation.subject)
    for kind in all_kinds:
        for item in ledger_repository.list_items_for_project(conn, project_id, kind=kind):
            if normalize_form(item.canonical_title) == normalized_subject:
                remember(item, "exact")

    if not is_update:
        # Tier 2: shared subject tokens above a floor (owner/date
        # proximity are scored, not filtered, here — filtering on tokens
        # alone keeps this tier cheap and avoids missing a candidate
        # whose owner/date differ, e.g. this observation is *changing*
        # them).
        for kind in all_kinds:
            for item in ledger_repository.list_items_for_project(conn, project_id, kind=kind):
                item_tokens = subject_tokens(item.canonical_title, item.canonical_description or "")
                similarity = token_jaccard(obs_tokens, item_tokens)
                if similarity >= config.thresholds.owner_date_candidate_token_floor:
                    remember(item, "owner_date_subject")

        # Tier 3: FTS5 top N within the project, filtered back to a
        # compatible kind (search_ledger_items already applies
        # project_id; FTS result ids never carry it themselves).
        query_text = f"{observation.subject} {observation.statement}".strip()
        if query_text:
            for result in ledger_repository.search_ledger_items(
                conn, project_id, query_text, limit=config.thresholds.fts_candidate_limit
            ):
                item = ledger_repository.get_item(conn, project_id, result.ledger_item_id)
                if item is not None and item.kind in all_kinds:
                    remember(item, "fts")

    # Tier 4: recently-changed compatible items, only when the
    # observation's own text carries an explicit local/anaphoric
    # reference ("that date," "this decision") — never a blanket "recent
    # items" query.
    if local_reference_found:
        for kind in all_kinds:
            recent = ledger_repository.list_items_for_project(conn, project_id, kind=kind)
            for item in recent[: config.thresholds.recent_reference_candidate_limit]:
                remember(item, "recent_reference")

    return [
        CandidateRecord(ledger_item=items_by_id[item_id], retrieval_tiers=frozenset(tiers))
        for item_id, tiers in tiers_by_id.items()
    ]


def score_candidates(
    *,
    observation_kind: ObservationKind,
    obs_tokens: frozenset[str],
    obs_entities: frozenset[str],
    resolved_owner: PersonResolution,
    normalized_date: NormalizedDate,
    candidates: list[CandidateRecord],
    reference_now: date,
    config: ReconciliationConfig,
) -> list[ScoredCandidate]:
    primary_kinds = PRIMARY_COMPATIBLE_KINDS.get(observation_kind, frozenset())
    scored: list[ScoredCandidate] = []
    for record in candidates:
        item = record.ledger_item
        item_tokens = subject_tokens(item.canonical_title, item.canonical_description or "")
        item_entities = extract_named_entities(
            f"{item.canonical_title} {item.canonical_description or ''}"
        )
        retrieved_via_reference = bool({"exact", "recent_reference"} & record.retrieval_tiers)
        features = compute_match_features(
            obs_tokens=obs_tokens,
            obs_entities=obs_entities,
            resolved_owner=resolved_owner,
            normalized_date=normalized_date,
            candidate=item,
            candidate_tokens=item_tokens,
            candidate_entities=item_entities,
            is_primary_kind=item.kind in primary_kinds,
            retrieved_via_explicit_reference=retrieved_via_reference,
            reference_now=reference_now,
            thresholds=config.thresholds,
        )
        score = score_match(features, config.weights)
        scored.append(
            ScoredCandidate(
                ledger_item=item,
                features=features,
                score=score,
                retrieval_tiers=record.retrieval_tiers,
            )
        )
    return scored


# ---------------------------------------------------------------------------
# Owner resolution (Section 10.1: "exact email, then known alias, then
# unresolved name"). Thin wrapper over the already-tested
# `people_repository.resolve_person` — reconciliation does not
# re-implement resolution, only decides how to call it.
# ---------------------------------------------------------------------------


def _resolve_observation_owner(
    conn: sqlite3.Connection, observation: Observation
) -> PersonResolution:
    if observation.owner_person_id is not None:
        return PersonResolution(outcome="resolved", person_id=observation.owner_person_id)
    if not observation.owner_text:
        return PersonResolution(outcome="unknown")
    if "@" in observation.owner_text:
        return people_repository.resolve_person(
            conn, email=observation.owner_text, name=observation.owner_text
        )
    return people_repository.resolve_person(conn, name=observation.owner_text)


def _reference_date(created_at: str) -> date:
    return datetime.fromisoformat(created_at.replace("Z", "+00:00")).date()


def _already_has_evidence_from_content(
    conn: sqlite3.Connection, project_id: str, ledger_item_id: str, content_id: str
) -> bool:
    """Section 10.2 #4/#5's distinction between ADD_EVIDENCE (a genuinely
    new source corroborating an existing item) and NO_OP (this exact
    source has already contributed evidence to it — nothing new)."""
    links = evidence_link_repository.list_for_target(
        conn, project_id, EvidenceLinkTargetType.LEDGER_ITEM, ledger_item_id
    )
    return any(link.content_id == content_id for link in links)


@dataclass(frozen=True)
class ReconciliationResult:
    """What `reconcile_observation` produces, beyond the stored
    `ProposedMutation` row itself — the full audit trail, for callers
    (tests, a future review UI) that want more than what fits in
    `candidate_features_json`."""

    proposal: ProposedMutation
    match_outcome: MatchOutcome
    classification: ActionClassification
    resolved_owner: PersonResolution
    normalized_date: NormalizedDate
    created: bool


def reconcile_observation(
    conn: sqlite3.Connection,
    project_id: str,
    observation_id: str,
    *,
    config: ReconciliationConfig = DEFAULT_RECONCILIATION_CONFIG,
) -> ReconciliationResult:
    """Reconcile one observation into one `proposed_mutations` row.

    Idempotent: if a proposal already exists for this observation (from
    an earlier run of reconciliation, pending or already reviewed), that
    existing row is returned unchanged and nothing new is written — "a
    repeated reconciliation run must not duplicate proposals."
    """
    existing = proposed_mutation_repository.list_for_observation(conn, project_id, observation_id)
    if existing:
        # `list_for_observation` orders by created_at ASC; the most
        # recent decision (or the only pending one — at most one pending
        # proposal can exist per observation) is the last element.
        latest = existing[-1]
        observation = observation_repository.get_observation(conn, project_id, observation_id)
        if observation is None:
            raise ObservationNotFoundError(observation_id)
        return _result_for_existing_proposal(conn, project_id, observation, latest, config)

    observation = observation_repository.get_observation(conn, project_id, observation_id)
    if observation is None:
        raise ObservationNotFoundError(observation_id)
    if observation.status is not ObservationStatus.VALID:
        raise ObservationNotReconcilableError(
            f"observation {observation_id!r} is not reconcilable in status "
            f"{observation.status.value!r}"
        )

    observation_kind = ObservationKind(observation.kind)
    resolved_owner = _resolve_observation_owner(conn, observation)
    normalized_date = normalize_date(observation.date_value, observation.date_text)
    obs_tokens = subject_tokens(observation.subject, observation.statement)
    obs_entities = extract_named_entities(f"{observation.subject} {observation.statement}")
    normalized_statement = normalize_form(observation.statement)
    local_reference_found = detect_local_reference(normalized_statement).found

    candidates = generate_candidates(
        conn,
        project_id,
        observation,
        observation_kind=observation_kind,
        obs_tokens=obs_tokens,
        local_reference_found=local_reference_found,
        config=config,
    )
    reference_now = _reference_date(observation.created_at)
    scored = score_candidates(
        observation_kind=observation_kind,
        obs_tokens=obs_tokens,
        obs_entities=obs_entities,
        resolved_owner=resolved_owner,
        normalized_date=normalized_date,
        candidates=candidates,
        reference_now=reference_now,
        config=config,
    )
    match_outcome = determine_match_outcome(scored, config.thresholds)

    already_has_evidence = False
    if match_outcome.top is not None:
        already_has_evidence = _already_has_evidence_from_content(
            conn, project_id, match_outcome.top.ledger_item.id, observation.content_id
        )

    classification = classify_action(
        observation_kind=observation_kind,
        subject=observation.subject,
        statement=observation.statement,
        explicitness=observation.explicitness,
        resolved_owner=resolved_owner,
        normalized_date=normalized_date,
        match_outcome=match_outcome,
        already_has_evidence_from_content=already_has_evidence,
        thresholds=config.thresholds,
    )

    extraction_band = extraction_confidence(observation)
    top_has_corrected_conflict = (
        match_outcome.top is not None
        and match_outcome.top.features.corrected_field_disagreement > 0
    )
    match_band = match_confidence(
        match_outcome=match_outcome,
        owner_ambiguous=resolved_owner.outcome == "ambiguous",
        corrected_field_conflict=top_has_corrected_conflict,
    )
    mutation_band = mutation_confidence(classification)
    overall_band = weakest_band(extraction_band, match_band, mutation_band)

    candidate_features_payload = {
        "match_tier": match_outcome.tier,
        "margin": match_outcome.margin,
        "candidates": [
            {
                "ledger_item_id": sc.ledger_item.id,
                "kind": sc.ledger_item.kind.value,
                "status": sc.ledger_item.status.value,
                "score": round(sc.score, 4),
                "retrieval_tiers": sorted(sc.retrieval_tiers),
                "features": sc.features.as_dict(),
            }
            for sc in match_outcome.ranked
        ],
        "action_reasons": list(classification.reasons),
        "escalate": classification.escalate,
        "escalation_reasons": list(classification.escalation_reasons),
        "owner_resolution": {
            "outcome": resolved_owner.outcome,
            "person_id": resolved_owner.person_id,
            "candidate_person_ids": list(resolved_owner.candidate_person_ids),
        },
        "normalized_date": {
            "value": normalized_date.value.isoformat() if normalized_date.value else None,
            "original_text": normalized_date.original_text,
            "ambiguous": normalized_date.ambiguous,
        },
        "confidence_factors": {
            "extraction": extraction_band.value,
            "match": match_band.value,
            "mutation": mutation_band.value,
        },
    }

    with transaction(conn):
        proposal = proposed_mutation_repository.insert_proposal(
            conn,
            project_id,
            observation_id=observation_id,
            action=classification.action,
            target_ledger_item_id=classification.target_ledger_item_id,
            proposed_patch=classification.proposed_patch or None,
            candidate_features=candidate_features_payload,
            confidence_score=(match_outcome.top.score if match_outcome.top is not None else None),
            confidence_band=overall_band.value,
        )
        observation_repository.update_status(
            conn, project_id, observation_id, ObservationStatus.RECONCILED
        )

    return ReconciliationResult(
        proposal=proposal,
        match_outcome=match_outcome,
        classification=classification,
        resolved_owner=resolved_owner,
        normalized_date=normalized_date,
        created=True,
    )


def _result_for_existing_proposal(
    conn: sqlite3.Connection,
    project_id: str,
    observation: Observation,
    proposal: ProposedMutation,
    config: ReconciliationConfig,
) -> ReconciliationResult:
    """Rebuild the same audit-trail shape `reconcile_observation` returns
    for a fresh run, but for an already-existing proposal — so callers
    (including the idempotency tests) can inspect a repeated call's
    result the same way as a first call's, without a second write."""
    observation_kind = ObservationKind(observation.kind)
    resolved_owner = _resolve_observation_owner(conn, observation)
    normalized_date = normalize_date(observation.date_value, observation.date_text)
    empty_outcome = MatchOutcome(tier=MatchTier.NONE, top=None, margin=None, ranked=())
    classification = ActionClassification(
        action=proposal.action,
        target_ledger_item_id=proposal.target_ledger_item_id,
        proposed_patch=proposal.proposed_patch or {},
        reasons=(),
        escalation_reasons=(),
        explicit_language_used=False,
        transition_domain_valid=True,
    )
    del observation_kind, config  # not needed to describe an already-decided proposal
    return ReconciliationResult(
        proposal=proposal,
        match_outcome=empty_outcome,
        classification=classification,
        resolved_owner=resolved_owner,
        normalized_date=normalized_date,
        created=False,
    )


def reconcile_pending_observations(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    config: ReconciliationConfig = DEFAULT_RECONCILIATION_CONFIG,
) -> list[ReconciliationResult]:
    """Reconcile every `valid`, not-yet-reconciled observation in a
    project. Each observation is reconciled in its own transaction (via
    `reconcile_observation`) so one bad observation cannot roll back
    proposals already written for others in the same run."""
    pending = observation_repository.list_observations_for_project(
        conn, project_id, status=ObservationStatus.VALID
    )
    return [reconcile_observation(conn, project_id, obs.id, config=config) for obs in pending]
