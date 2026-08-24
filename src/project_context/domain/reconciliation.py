"""Deterministic reconciliation domain logic (Section 10; FR-017 through
FR-020). Pure — no SQL, no I/O, no LLM call. Everything here is a plain
function/dataclass over values a caller already has in memory, so it can
be unit-tested without a database and reused unchanged by whatever
orchestrates it (`project_context.services.reconciliation`).

Scope, deliberately: this module decides *what a proposed_mutations row
should say* (Section 9). It never writes one, and it never touches
`ledger_items`/`ledger_versions` — applying an *accepted* proposal as a
ledger transition is the review transaction's job (still Prompt 8, out of
scope here, same boundary every existing repository docstring in this
codebase already draws).

Everything a human might need to audit a decision — every feature value,
every language match, every escalation reason — is data on the return
values below, not a side effect and not a single opaque score (FR-017:
"candidate features and score are inspectable").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from project_context.domain.ledger import (
    ConfidenceBand,
    LedgerItem,
    LedgerItemKind,
    LedgerItemStatus,
    LedgerStatusError,
    LedgerTransitionError,
    LedgerTransitionType,
    validate_transition,
)
from project_context.domain.observations import Observation
from project_context.domain.people import PersonResolution
from project_context.domain.reconciliation_language import (
    detect_blocking_language,
    detect_cancellation,
    detect_completion,
    detect_date_change,
    detect_delay,
    detect_owner_assignment,
    detect_supersession,
)
from project_context.domain.review import ProposedMutationAction
from project_context.domain.text_normalization import NormalizedDate, normalize_form, token_jaccard
from project_context.llm.schemas import ObservationKind

# ---------------------------------------------------------------------------
# Typed configuration (Section 10.4: "these are hypotheses, not truth ...
# tune against the benchmark"). Centralized here — nothing below reads a
# bare numeric literal for a weight or threshold; every one is a field on
# one of these two models, constructed once as `DEFAULT_RECONCILIATION_CONFIG`.
# ---------------------------------------------------------------------------


class ReconciliationWeights(BaseModel):
    """The auditable match-score feature weights (Section 10.4's starting
    formula, calibrated on golden projects going forward, not here)."""

    model_config = ConfigDict(frozen=True)

    subject_token_similarity: float = 0.35
    owner_match: float = 0.20
    date_proximity: float = 0.15
    item_type_compatibility: float = 0.10
    shared_named_entities: float = 0.10
    source_local_reference: float = 0.10
    mutually_exclusive_owner_penalty: float = 0.25
    completed_long_ago_penalty: float = 0.20
    corrected_field_disagreement_penalty: float = 0.20


class ReconciliationThresholds(BaseModel):
    """Every other tunable constant reconciliation uses, so none of it is
    a scattered magic number in scoring/candidate-generation/
    classification code (Prompt 7: "Centralize weights/thresholds in
    typed configuration")."""

    model_config = ConfigDict(frozen=True)

    #: Section 10.4: ">=0.82 and margin to second candidate >=0.15: strong".
    strong_candidate_score: float = 0.82
    strong_candidate_margin: float = 0.15
    #: Section 10.4: "0.65-0.81: review candidate."
    review_candidate_score: float = 0.65
    #: How similar (Jaccard, post `subject_tokens`) two propositions must
    #: be to be considered "the same proposition" for the ADD_EVIDENCE/
    #: NO_OP branch of Section 10.5.
    same_proposition_token_floor: float = 0.75
    #: The date-proximity feature reaches 0 once two dates are this many
    #: days apart (Section 10.3's "near date" for candidate generation
    #: reuses the same window).
    date_proximity_window_days: int = 14
    #: Section 10.4's `completed_long_ago_penalty`: how many days after a
    #: terminal status counts as "long ago" rather than "just resolved."
    completed_long_ago_days: int = 30
    #: Minimum shared-token floor for Section 10.3 tier 2 (owner/date/
    #: subject-token candidate generation) to consider an item at all.
    owner_date_candidate_token_floor: float = 0.20
    #: Section 10.3 tier 3: "FTS5 top 10."
    fts_candidate_limit: int = 10
    #: Section 10.3 tier 4: how many of the most-recently-changed
    #: compatible items to consider when an explicit local reference
    #: ("that date," "this decision") is present.
    recent_reference_candidate_limit: int = 5


class ReconciliationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    weights: ReconciliationWeights = ReconciliationWeights()
    thresholds: ReconciliationThresholds = ReconciliationThresholds()


DEFAULT_RECONCILIATION_CONFIG = ReconciliationConfig()


# ---------------------------------------------------------------------------
# Item-kind compatibility matrix (Section 10.3's table). Kind is immutable
# per `ledger_items` row once created (see `project_context.domain.ledger`)
# — there is no in-place "this risk item is now a blocker item." A
# cross-kind match (the table's "risk; blocker if..." and "blocker; risk"
# rows) is therefore only ever resolved through SUPERSEDE: a new item of
# the new kind is created and the old one is marked superseded. That is a
# deliberate reading of Section 10.3 by this implementation, not something
# the plan spells out in those words — recorded here once rather than left
# implicit at each call site.
# ---------------------------------------------------------------------------

#: Kinds a candidate may be *without* forcing a kind change (i.e. matched
#: in place; UPDATE/COMPLETE/CANCEL/ADD_EVIDENCE/NO_OP all stay legal).
PRIMARY_COMPATIBLE_KINDS: dict[ObservationKind, frozenset[LedgerItemKind]] = {
    ObservationKind.COMMITMENT: frozenset({LedgerItemKind.COMMITMENT}),
    ObservationKind.DECISION: frozenset({LedgerItemKind.DECISION}),
    ObservationKind.MILESTONE: frozenset({LedgerItemKind.MILESTONE}),
    ObservationKind.RISK: frozenset({LedgerItemKind.RISK}),
    ObservationKind.BLOCKER: frozenset({LedgerItemKind.BLOCKER}),
    ObservationKind.OPEN_QUESTION: frozenset({LedgerItemKind.OPEN_QUESTION}),
    ObservationKind.STAKEHOLDER: frozenset({LedgerItemKind.STAKEHOLDER}),
    #: An `update` observation targets an *existing* item of any kind but
    #: never creates one on its own (Section 10.3: "all, but only if it
    #: explicitly references the item") — candidate generation further
    #: restricts *how* it may be found (see services.reconciliation).
    ObservationKind.UPDATE: frozenset(LedgerItemKind),
}

#: Kinds a candidate may be only through the cross-kind SUPERSEDE path
#: above, always forced to review (Section 10.3: "decision; occasionally
#: milestone only through review"; "risk; blocker if language says work
#: cannot proceed"; "blocker | blocker; risk").
SECONDARY_COMPATIBLE_KINDS: dict[ObservationKind, frozenset[LedgerItemKind]] = {
    ObservationKind.DECISION: frozenset({LedgerItemKind.MILESTONE}),
    ObservationKind.RISK: frozenset({LedgerItemKind.BLOCKER}),
    ObservationKind.BLOCKER: frozenset({LedgerItemKind.RISK}),
}

#: The kind a brand-new ledger item takes when created directly from an
#: observation of this kind. `None` for `update`, which never creates.
NATURAL_LEDGER_KIND: dict[ObservationKind, LedgerItemKind | None] = {
    ObservationKind.COMMITMENT: LedgerItemKind.COMMITMENT,
    ObservationKind.DECISION: LedgerItemKind.DECISION,
    ObservationKind.MILESTONE: LedgerItemKind.MILESTONE,
    ObservationKind.RISK: LedgerItemKind.RISK,
    ObservationKind.BLOCKER: LedgerItemKind.BLOCKER,
    ObservationKind.OPEN_QUESTION: LedgerItemKind.OPEN_QUESTION,
    ObservationKind.STAKEHOLDER: LedgerItemKind.STAKEHOLDER,
    ObservationKind.UPDATE: None,
}

#: Kinds Section 10.10 licenses for supersession ("a newer decision,
#: plan, milestone, or answer" — "answer" is read here as an
#: `open_question` being superseded by a fresher one).
SUPERSEDABLE_KINDS: frozenset[LedgerItemKind] = frozenset(
    {
        LedgerItemKind.DECISION,
        LedgerItemKind.MILESTONE,
        LedgerItemKind.COMMITMENT,
        LedgerItemKind.OPEN_QUESTION,
    }
)

TERMINAL_STATUSES: frozenset[LedgerItemStatus] = frozenset(
    {
        LedgerItemStatus.COMPLETED,
        LedgerItemStatus.RESOLVED,
        LedgerItemStatus.CANCELED,
        LedgerItemStatus.SUPERSEDED,
    }
)


def compatible_kinds(observation_kind: ObservationKind) -> frozenset[LedgerItemKind]:
    """Every ledger kind a candidate for this observation kind may have
    (primary and secondary) — the full set candidate generation is
    allowed to query (Section 10.3, FR-017: "matched only against
    same-project, compatible-type items")."""
    primary = PRIMARY_COMPATIBLE_KINDS.get(observation_kind, frozenset())
    secondary = SECONDARY_COMPATIBLE_KINDS.get(observation_kind, frozenset())
    return primary | secondary


# ---------------------------------------------------------------------------
# Auditable match score (Section 10.4).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchFeatures:
    """Every term of Section 10.4's score, plus the mutually-exclusive
    penalty flags, kept as named fields rather than folded straight into
    a float — this *is* the "candidate features" FR-017 requires be
    inspectable, not merely an intermediate value."""

    subject_token_similarity: float
    owner_match: float
    date_proximity: float
    item_type_compatibility: float
    shared_named_entities: float
    source_local_reference: float
    mutually_exclusive_owner: float
    completed_long_ago: float
    corrected_field_disagreement: float

    def as_dict(self) -> dict[str, float]:
        return {
            "subject_token_similarity": self.subject_token_similarity,
            "owner_match": self.owner_match,
            "date_proximity": self.date_proximity,
            "item_type_compatibility": self.item_type_compatibility,
            "shared_named_entities": self.shared_named_entities,
            "source_local_reference": self.source_local_reference,
            "mutually_exclusive_owner": self.mutually_exclusive_owner,
            "completed_long_ago": self.completed_long_ago,
            "corrected_field_disagreement": self.corrected_field_disagreement,
        }


def score_match(features: MatchFeatures, weights: ReconciliationWeights) -> float:
    """Section 10.4's formula, verbatim, clamped to `[0, 1]` so it always
    satisfies `proposed_mutations.confidence_score`'s CHECK constraint —
    the formula itself can go negative (three penalty terms, no floor) or
    (in principle, with a non-default config) above 1."""
    raw = (
        weights.subject_token_similarity * features.subject_token_similarity
        + weights.owner_match * features.owner_match
        + weights.date_proximity * features.date_proximity
        + weights.item_type_compatibility * features.item_type_compatibility
        + weights.shared_named_entities * features.shared_named_entities
        + weights.source_local_reference * features.source_local_reference
        - weights.mutually_exclusive_owner_penalty * features.mutually_exclusive_owner
        - weights.completed_long_ago_penalty * features.completed_long_ago
        - weights.corrected_field_disagreement_penalty * features.corrected_field_disagreement
    )
    return max(0.0, min(1.0, raw))


def compute_match_features(
    *,
    obs_tokens: frozenset[str],
    obs_entities: frozenset[str],
    resolved_owner: PersonResolution,
    normalized_date: NormalizedDate,
    candidate: LedgerItem,
    candidate_tokens: frozenset[str],
    candidate_entities: frozenset[str],
    is_primary_kind: bool,
    retrieved_via_explicit_reference: bool,
    reference_now: date,
    thresholds: ReconciliationThresholds,
) -> MatchFeatures:
    """Compute every Section 10.4 feature for one (observation, candidate)
    pair. Pure — every input is already a value, not a query."""
    subject_similarity = token_jaccard(obs_tokens, candidate_tokens)
    entity_similarity = token_jaccard(obs_entities, candidate_entities)

    owner_match = 0.5
    mutually_exclusive_owner = 0.0
    if resolved_owner.outcome == "resolved" and resolved_owner.person_id is not None:
        if candidate.owner_person_id is None:
            owner_match = 0.25
        elif candidate.owner_person_id == resolved_owner.person_id:
            owner_match = 1.0
        else:
            owner_match = 0.0
            mutually_exclusive_owner = 1.0
    elif candidate.owner_person_id is not None:
        # The observation names no (or an ambiguous) owner but the
        # candidate has one — weak evidence, not a contradiction.
        owner_match = 0.25

    date_proximity = 0.5
    if normalized_date.value is not None and candidate.due_date is not None:
        try:
            candidate_date = date.fromisoformat(candidate.due_date)
        except ValueError:
            candidate_date = None
        if candidate_date is not None:
            days_apart = abs((normalized_date.value - candidate_date).days)
            date_proximity = max(0.0, 1.0 - days_apart / thresholds.date_proximity_window_days)
    elif (normalized_date.value is not None) != (candidate.due_date is not None):
        date_proximity = 0.25

    item_type_compatibility = 1.0 if is_primary_kind else 0.5

    completed_long_ago = 0.0
    if candidate.status in TERMINAL_STATUSES:
        updated = _parse_utc_date(candidate.updated_at)
        if updated is not None:
            days_since_terminal = (reference_now - updated).days
            if days_since_terminal > thresholds.completed_long_ago_days:
                completed_long_ago = 1.0

    corrected_field_disagreement = 0.0
    if candidate.user_corrected:
        owner_disagrees = (
            resolved_owner.outcome == "resolved"
            and resolved_owner.person_id is not None
            and candidate.owner_person_id is not None
            and resolved_owner.person_id != candidate.owner_person_id
        )
        date_disagrees = (
            normalized_date.value is not None
            and candidate.due_date is not None
            and normalized_date.value.isoformat() != candidate.due_date
        )
        if owner_disagrees or date_disagrees:
            corrected_field_disagreement = 1.0

    return MatchFeatures(
        subject_token_similarity=subject_similarity,
        owner_match=owner_match,
        date_proximity=date_proximity,
        item_type_compatibility=item_type_compatibility,
        shared_named_entities=entity_similarity,
        source_local_reference=1.0 if retrieved_via_explicit_reference else 0.0,
        mutually_exclusive_owner=mutually_exclusive_owner,
        completed_long_ago=completed_long_ago,
        corrected_field_disagreement=corrected_field_disagreement,
    )


def _parse_utc_date(iso_timestamp: str) -> date | None:
    try:
        return datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")).date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Match tier (Section 10.4's thresholds).
# ---------------------------------------------------------------------------


class MatchTier:
    """Not a StrEnum: these four values are an internal classification
    detail of this module, never stored verbatim in the database (the
    stored `confidence_band` is a separate, three-way concept — Section
    10.12)."""

    STRONG = "strong"
    REVIEW = "review"
    UNCERTAIN = "uncertain"
    NONE = "none"


@dataclass(frozen=True)
class ScoredCandidate:
    ledger_item: LedgerItem
    features: MatchFeatures
    score: float
    retrieval_tiers: frozenset[str]


@dataclass(frozen=True)
class MatchOutcome:
    tier: str
    top: ScoredCandidate | None
    margin: float | None
    ranked: tuple[ScoredCandidate, ...]


def determine_match_outcome(
    scored_candidates: list[ScoredCandidate], thresholds: ReconciliationThresholds
) -> MatchOutcome:
    """Rank candidates by score and classify the top one into a tier
    (Section 10.4). A top score `>=0.82` whose margin over the runner-up
    is `<0.15` is *not* promoted to `strong` — Section 10.13's "two
    candidates within 0.15 score" always forces review, so it can only
    ever land in `review` or trigger the close-candidate escalation the
    caller applies on top of this tier."""
    if not scored_candidates:
        return MatchOutcome(tier=MatchTier.NONE, top=None, margin=None, ranked=())

    ranked = tuple(sorted(scored_candidates, key=lambda c: c.score, reverse=True))
    top = ranked[0]
    margin = (top.score - ranked[1].score) if len(ranked) > 1 else None

    if top.score >= thresholds.strong_candidate_score and (
        margin is None or margin >= thresholds.strong_candidate_margin
    ):
        tier = MatchTier.STRONG
    elif top.score >= thresholds.review_candidate_score:
        tier = MatchTier.REVIEW
    else:
        tier = MatchTier.UNCERTAIN
    return MatchOutcome(tier=tier, top=top, margin=margin, ranked=ranked)


# ---------------------------------------------------------------------------
# Action classification (Section 10.5-10.10).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionClassification:
    action: ProposedMutationAction
    target_ledger_item_id: str | None
    proposed_patch: dict[str, Any]
    reasons: tuple[str, ...]
    escalation_reasons: tuple[str, ...]
    explicit_language_used: bool
    transition_domain_valid: bool

    @property
    def escalate(self) -> bool:
        return bool(self.escalation_reasons)


def _create_patch(
    *,
    observation_kind: ObservationKind,
    subject: str,
    statement: str,
    resolved_owner: PersonResolution,
    normalized_date: NormalizedDate,
    supersedes_ledger_item_id: str | None = None,
) -> dict[str, Any]:
    target_kind = NATURAL_LEDGER_KIND[observation_kind]
    assert target_kind is not None, "an update observation never reaches _create_patch"
    owner_person_id = resolved_owner.person_id if resolved_owner.outcome == "resolved" else None
    patch: dict[str, Any] = {
        "kind": target_kind.value,
        "canonical_title": subject,
        "canonical_description": statement,
        "owner_person_id": owner_person_id,
        "due_date": normalized_date.value.isoformat() if normalized_date.value else None,
        "due_date_text": normalized_date.original_text,
    }
    if supersedes_ledger_item_id is not None:
        patch["supersedes_ledger_item_id"] = supersedes_ledger_item_id
    return patch


def _is_domain_valid_transition(
    *,
    kind: LedgerItemKind,
    from_status: LedgerItemStatus,
    to_status: LedgerItemStatus,
    transition_type: LedgerTransitionType,
) -> bool:
    try:
        validate_transition(
            kind=kind, from_status=from_status, to_status=to_status, transition_type=transition_type
        )
    except (LedgerStatusError, LedgerTransitionError):
        return False
    return True


def classify_action(
    *,
    observation_kind: ObservationKind,
    subject: str,
    statement: str,
    explicitness: str,
    resolved_owner: PersonResolution,
    normalized_date: NormalizedDate,
    match_outcome: MatchOutcome,
    already_has_evidence_from_content: bool,
    thresholds: ReconciliationThresholds,
) -> ActionClassification:
    """Section 10.5's decision tree, made concrete. Every branch is a
    named, documented rule; the final fallback is CONFLICT (never a
    silent guess) — CONFLICT "leaves current ledger state unchanged"
    (Section 10.11), so an unrecognized relationship costs a human's
    attention, never a wrong mutation.
    """
    normalized_statement = normalize_form(statement)
    completion = detect_completion(normalized_statement)
    cancellation = detect_cancellation(normalized_statement)
    delay = detect_delay(normalized_statement)
    date_change = detect_date_change(normalized_statement)
    owner_assignment = detect_owner_assignment(normalized_statement)
    supersession = detect_supersession(normalized_statement)
    blocking = detect_blocking_language(normalized_statement)

    escalation_reasons: list[str] = []
    if resolved_owner.outcome == "ambiguous":
        escalation_reasons.append("ambiguous_owner")

    # --- no candidate at all ----------------------------------------------
    if match_outcome.tier is MatchTier.NONE or match_outcome.top is None:
        if observation_kind is ObservationKind.UPDATE:
            # Section 10.3: an `update` observation never creates a
            # ledger item on its own. With no resolvable target, the only
            # safe (non-mutating) action is to hand it to a human.
            escalation_reasons.append("update_observation_without_resolvable_target")
            return ActionClassification(
                action=ProposedMutationAction.CONFLICT,
                target_ledger_item_id=None,
                proposed_patch={},
                reasons=("no candidate found for an update-only observation",),
                escalation_reasons=tuple(escalation_reasons),
                explicit_language_used=False,
                transition_domain_valid=True,
            )
        return ActionClassification(
            action=ProposedMutationAction.CREATE,
            target_ledger_item_id=None,
            proposed_patch=_create_patch(
                observation_kind=observation_kind,
                subject=subject,
                statement=statement,
                resolved_owner=resolved_owner,
                normalized_date=normalized_date,
            ),
            reasons=("no compatible candidate exists in this project",),
            escalation_reasons=tuple(escalation_reasons),
            explicit_language_used=False,
            transition_domain_valid=True,
        )

    top = match_outcome.top
    candidate = top.ledger_item
    features = top.features
    #: Section 10.4: "<0.65: treat as new/uncertain, depending on
    #: language" — a weak score does not, by itself, rule a candidate
    #: out. It only means: without explicit language pointing at this
    #: candidate specifically, don't trust it (see the final fallback
    #: below); *with* explicit language, the ordinary branches below are
    #: still evaluated, always under mandatory escalation.
    is_uncertain_tier = match_outcome.tier is MatchTier.UNCERTAIN

    # --- two (or more) candidates too close to call ----------------------
    if (
        match_outcome.margin is not None
        and match_outcome.margin < thresholds.strong_candidate_margin
        and top.score >= thresholds.review_candidate_score
    ):
        escalation_reasons.append("multiple_candidates_within_score_margin")
        return ActionClassification(
            action=ProposedMutationAction.CONFLICT,
            target_ledger_item_id=None,
            proposed_patch={},
            reasons=(
                "two or more candidates scored within the margin required for a confident match",
            ),
            escalation_reasons=tuple(escalation_reasons),
            explicit_language_used=False,
            transition_domain_valid=True,
        )

    if match_outcome.tier is MatchTier.REVIEW:
        escalation_reasons.append("candidate_below_strong_threshold")
    elif is_uncertain_tier:
        escalation_reasons.append("weak_candidate_match")
    if features.corrected_field_disagreement:
        escalation_reasons.append("corrected_field_disagreement")

    is_terminal_candidate = candidate.status in TERMINAL_STATUSES
    date_changed = _material_date_conflict(normalized_date, candidate.due_date)
    owner_changed = _material_owner_conflict(resolved_owner, candidate.owner_person_id)
    natural_kind = NATURAL_LEDGER_KIND[observation_kind]
    kind_change_requested = natural_kind is not None and candidate.kind != natural_kind

    # --- a completion/cancellation/supersession/field-change directed at
    # an item that is already in a terminal state is a reversal, not an
    # ordinary transition (Section 10.13: "any ... material reversal of a
    # user-corrected field" and the broader "materially inconsistent
    # sources" rule) -----------------------------------------------------
    if is_terminal_candidate and (
        completion.found
        or cancellation.found
        or supersession.found
        or (kind_change_requested and blocking.found)
        or date_changed
        or owner_changed
    ):
        escalation_reasons.append("material_reversal_on_terminal_item")
        return ActionClassification(
            action=ProposedMutationAction.CONFLICT,
            target_ledger_item_id=candidate.id,
            proposed_patch={},
            reasons=(
                "the matched candidate is already in a terminal status; "
                "treating this as a reversal",
            ),
            escalation_reasons=tuple(escalation_reasons),
            explicit_language_used=True,
            transition_domain_valid=False,
        )

    # --- cross-kind match: e.g. a risk that has become a blocker --------
    if kind_change_requested and (blocking.found or supersession.found):
        escalation_reasons.append("item_kind_change_requires_supersession")
        valid = _is_domain_valid_transition(
            kind=candidate.kind,
            from_status=candidate.status,
            to_status=LedgerItemStatus.SUPERSEDED,
            transition_type=LedgerTransitionType.SUPERSEDE,
        )
        return ActionClassification(
            action=ProposedMutationAction.SUPERSEDE,
            target_ledger_item_id=candidate.id,
            proposed_patch=_create_patch(
                observation_kind=observation_kind,
                subject=subject,
                statement=statement,
                resolved_owner=resolved_owner,
                normalized_date=normalized_date,
                supersedes_ledger_item_id=candidate.id,
            ),
            reasons=(
                f"{observation_kind.value} observation matched an existing {candidate.kind.value} "
                "item; kind cannot change in place, so the prior item is superseded",
            ),
            escalation_reasons=tuple(escalation_reasons),
            explicit_language_used=True,
            transition_domain_valid=valid,
        )

    # --- same proposition, no material field change (Section 10.2 #4 /
    # 10.5's first branch) ------------------------------------------------
    same_proposition = (
        features.subject_token_similarity >= thresholds.same_proposition_token_floor
        and not completion.found
        and not cancellation.found
        and not date_change.found
        and not owner_assignment.found
        and not supersession.found
        and not date_changed
        and not owner_changed
    )
    if same_proposition:
        action = (
            ProposedMutationAction.NO_OP
            if already_has_evidence_from_content
            else ProposedMutationAction.ADD_EVIDENCE
        )
        reasons = ["statement matches the candidate with no changed material field"]
        if delay.found:
            reasons.append(
                f"delay language noted ({delay.matched_phrase!r}); not treated as cancellation"
            )
        return ActionClassification(
            action=action,
            target_ledger_item_id=candidate.id,
            proposed_patch={},
            reasons=tuple(reasons),
            escalation_reasons=tuple(escalation_reasons),
            explicit_language_used=False,
            transition_domain_valid=True,
        )

    # --- completion --------------------------------------------------------
    if completion.found:
        escalation_reasons.append("completion")
        if explicitness != "explicit":
            escalation_reasons.append("inferred_not_explicit")
        valid = _is_domain_valid_transition(
            kind=candidate.kind,
            from_status=candidate.status,
            to_status=LedgerItemStatus.COMPLETED,
            transition_type=LedgerTransitionType.COMPLETE,
        )
        return ActionClassification(
            action=ProposedMutationAction.COMPLETE,
            target_ledger_item_id=candidate.id,
            proposed_patch={"status": "completed"},
            reasons=(f"explicit completion language: {completion.matched_phrase!r}",),
            escalation_reasons=tuple(escalation_reasons),
            explicit_language_used=True,
            transition_domain_valid=valid,
        )

    # --- cancellation --------------------------------------------------------
    if cancellation.found:
        escalation_reasons.append("cancellation")
        valid = _is_domain_valid_transition(
            kind=candidate.kind,
            from_status=candidate.status,
            to_status=LedgerItemStatus.CANCELED,
            transition_type=LedgerTransitionType.CANCEL,
        )
        return ActionClassification(
            action=ProposedMutationAction.CANCEL,
            target_ledger_item_id=candidate.id,
            proposed_patch={"status": "canceled"},
            reasons=(f"explicit cancellation language: {cancellation.matched_phrase!r}",),
            escalation_reasons=tuple(escalation_reasons),
            explicit_language_used=True,
            transition_domain_valid=valid,
        )

    # --- supersession (same-kind decision/plan/milestone replacement) ------
    if supersession.found and candidate.kind in SUPERSEDABLE_KINDS:
        escalation_reasons.append("supersession")
        valid = _is_domain_valid_transition(
            kind=candidate.kind,
            from_status=candidate.status,
            to_status=LedgerItemStatus.SUPERSEDED,
            transition_type=LedgerTransitionType.SUPERSEDE,
        )
        return ActionClassification(
            action=ProposedMutationAction.SUPERSEDE,
            target_ledger_item_id=candidate.id,
            proposed_patch=_create_patch(
                observation_kind=observation_kind,
                subject=subject,
                statement=statement,
                resolved_owner=resolved_owner,
                normalized_date=normalized_date,
                supersedes_ledger_item_id=candidate.id,
            ),
            reasons=(f"explicit supersession language: {supersession.matched_phrase!r}",),
            escalation_reasons=tuple(escalation_reasons),
            explicit_language_used=True,
            transition_domain_valid=valid,
        )

    # --- changed due date only ------------------------------------------
    if date_changed and not owner_changed:
        if date_change.found:
            escalation_reasons.append("changed_due_date")
            valid = _is_domain_valid_transition(
                kind=candidate.kind,
                from_status=candidate.status,
                to_status=candidate.status,
                transition_type=LedgerTransitionType.UPDATE,
            )
            return ActionClassification(
                action=ProposedMutationAction.UPDATE,
                target_ledger_item_id=candidate.id,
                proposed_patch={
                    "due_date": (
                        normalized_date.value.isoformat() if normalized_date.value else None
                    ),
                    "due_date_text": normalized_date.original_text,
                },
                reasons=(f"explicit date-change language: {date_change.matched_phrase!r}",),
                escalation_reasons=tuple(escalation_reasons),
                explicit_language_used=True,
                transition_domain_valid=valid,
            )
        escalation_reasons.append("date_changed_without_explicit_language")
        return ActionClassification(
            action=ProposedMutationAction.CONFLICT,
            target_ledger_item_id=candidate.id,
            proposed_patch={},
            reasons=("a different date is stated but no explicit change language was found",),
            escalation_reasons=tuple(escalation_reasons),
            explicit_language_used=False,
            transition_domain_valid=False,
        )

    # --- changed owner only ------------------------------------------------
    if owner_changed and not date_changed:
        if owner_assignment.found:
            escalation_reasons.append("changed_owner")
            valid = _is_domain_valid_transition(
                kind=candidate.kind,
                from_status=candidate.status,
                to_status=candidate.status,
                transition_type=LedgerTransitionType.UPDATE,
            )
            return ActionClassification(
                action=ProposedMutationAction.UPDATE,
                target_ledger_item_id=candidate.id,
                proposed_patch={"owner_person_id": resolved_owner.person_id},
                reasons=(f"explicit reassignment language: {owner_assignment.matched_phrase!r}",),
                escalation_reasons=tuple(escalation_reasons),
                explicit_language_used=True,
                transition_domain_valid=valid,
            )
        escalation_reasons.append("owner_changed_without_explicit_language")
        return ActionClassification(
            action=ProposedMutationAction.CONFLICT,
            target_ledger_item_id=candidate.id,
            proposed_patch={},
            reasons=(
                "a different owner is stated but no explicit reassignment language was found "
                "(assistance language does not count as reassignment)",
            ),
            escalation_reasons=tuple(escalation_reasons),
            explicit_language_used=False,
            transition_domain_valid=False,
        )

    # --- both owner and date changed together, or any other combination
    # of compatible-field changes this module does not have a named rule
    # for: relationship is uncertain, so leave the ledger untouched
    # (Section 10.5's "compatible fields changed but relationship is
    # uncertain: CONFLICT") -------------------------------------------------
    if date_changed and owner_changed:
        escalation_reasons.append("multiple_fields_changed")
    else:
        escalation_reasons.append("unclassified_relationship")

    if is_uncertain_tier and observation_kind is not ObservationKind.UPDATE:
        # Section 10.4: a weak score with no explicit language pointing at
        # this specific candidate is exactly the "treat as new" case —
        # pinning an unexplained relationship to a barely-related
        # candidate is worse than proposing a new item for a human to
        # merge later if it turns out to be a duplicate.
        return ActionClassification(
            action=ProposedMutationAction.CREATE,
            target_ledger_item_id=None,
            proposed_patch=_create_patch(
                observation_kind=observation_kind,
                subject=subject,
                statement=statement,
                resolved_owner=resolved_owner,
                normalized_date=normalized_date,
            ),
            reasons=(
                "the closest candidate scored below the uncertain threshold and no explicit "
                "language ties this statement to it; treated as a new item",
            ),
            escalation_reasons=tuple(escalation_reasons),
            explicit_language_used=False,
            transition_domain_valid=True,
        )

    return ActionClassification(
        action=ProposedMutationAction.CONFLICT,
        target_ledger_item_id=candidate.id,
        proposed_patch={},
        reasons=(
            "no deterministic rule matched a single, unambiguous change; "
            "escalating rather than guessing",
        ),
        escalation_reasons=tuple(escalation_reasons),
        explicit_language_used=False,
        transition_domain_valid=False,
    )


def _material_date_conflict(
    normalized_date: NormalizedDate, candidate_due_date: str | None
) -> bool:
    """A "changed date" only where there is something to change *from* —
    both a stated new date and an existing candidate date, genuinely
    different. Filling in a previously-blank due date is not itself
    flagged as a conflict by this function (a known, documented
    simplification — see the reconciliation report's weak-case list)."""
    return (
        normalized_date.value is not None
        and candidate_due_date is not None
        and normalized_date.value.isoformat() != candidate_due_date
    )


def _material_owner_conflict(
    resolved_owner: PersonResolution, candidate_owner_person_id: str | None
) -> bool:
    if resolved_owner.outcome != "resolved" or resolved_owner.person_id is None:
        return False
    if candidate_owner_person_id is None:
        return False
    return candidate_owner_person_id != resolved_owner.person_id


# ---------------------------------------------------------------------------
# Confidence (Section 10.12): three separate, deterministically-computed
# factors, never a model-reported number. `weakest_band` folds them into
# the single `confidence_band` stored on `proposed_mutations`; the three
# factor bands themselves are preserved in `candidate_features_json` so
# the "separate ... factors/bands" requirement is auditable, not just an
# internal detail lost after this module returns.
# ---------------------------------------------------------------------------

_BAND_RANK: dict[ConfidenceBand, int] = {
    ConfidenceBand.LOW: 0,
    ConfidenceBand.MEDIUM: 1,
    ConfidenceBand.HIGH: 2,
}


def weakest_band(*bands: ConfidenceBand) -> ConfidenceBand:
    """The weakest of several confidence bands — used to fold extraction/
    match/mutation confidence into one overall `confidence_band` (Section
    10.12 keeps the three concepts separate; nothing in the plan mandates
    how to combine them into the one column the schema actually stores,
    so "no factor may inflate another" — the weakest-link rule — is this
    implementation's deliberate, documented choice)."""
    return min(bands, key=lambda band: _BAND_RANK[band])


def extraction_confidence(observation: Observation) -> ConfidenceBand:
    """Section 10.12: "support is explicit; evidence span valid; owner/
    date stated." Evidence-span validity is guaranteed upstream by
    `project_context.services.extraction`/`observations.persist_observation`
    before a row ever reaches this module — every *stored* `Observation`
    already satisfies it, so only explicitness and owner/date presence
    are checked here."""
    has_owner_or_date = (
        bool(observation.owner_text)
        or observation.date_value is not None
        or bool(observation.date_text)
    )
    if observation.explicitness == "explicit" and has_owner_or_date:
        return ConfidenceBand.HIGH
    if observation.explicitness == "explicit" or has_owner_or_date:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW


def match_confidence(
    *,
    match_outcome: MatchOutcome,
    owner_ambiguous: bool,
    corrected_field_conflict: bool,
) -> ConfidenceBand:
    """Section 10.12: "candidate similarity, uniqueness, compatible
    state." "Uniqueness" is the close-margin check; "compatible state" is
    folded into the tier itself (an incompatible-kind candidate is never
    generated at all — see `compatible_kinds`)."""
    if match_outcome.tier is MatchTier.NONE:
        # No candidates at all is not ambiguity — it is a confident "this
        # is new."
        return ConfidenceBand.HIGH
    close_margin = (
        match_outcome.margin is not None
        and match_outcome.margin < 0.15
        and match_outcome.top is not None
        and match_outcome.top.score >= 0.65
    )
    if owner_ambiguous or corrected_field_conflict or close_margin:
        return ConfidenceBand.LOW
    if match_outcome.tier is MatchTier.STRONG:
        return ConfidenceBand.HIGH
    if match_outcome.tier is MatchTier.REVIEW:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW  # MatchTier.UNCERTAIN: a weak, non-close match


def mutation_confidence(classification: ActionClassification) -> ConfidenceBand:
    """Section 10.12: "action language explicit and transition valid."
    CREATE/NO_OP/ADD_EVIDENCE never assert a status transition on an
    existing item, so there is nothing risky to be unsure about; CONFLICT
    is low by definition — it exists precisely because the mutation was
    not clear."""
    if classification.action is ProposedMutationAction.CONFLICT:
        return ConfidenceBand.LOW
    if classification.action in (
        ProposedMutationAction.CREATE,
        ProposedMutationAction.NO_OP,
        ProposedMutationAction.ADD_EVIDENCE,
    ):
        return ConfidenceBand.HIGH
    if not classification.explicit_language_used or not classification.transition_domain_valid:
        return ConfidenceBand.LOW
    return ConfidenceBand.HIGH
