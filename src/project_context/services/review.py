"""The human review transaction (Section 5.4/5.6/5.7; Section 10.14;
FR-013 through FR-021; Prompt 8). This is the *only* path by which
extracted/reconciled model output may change accepted ledger state —
`project_context.services.ledger`'s write functions are never called
directly from anywhere else that touches a real proposal (FR-012:
"Model output cannot directly update a ledger item").

Six review actions (`project_context.domain.review.ReviewAction`):
accept, edit_and_accept, reject, mark_complete, mark_superseded,
treat_as_new. All but `reject` funnel through one internal engine
(`_apply`) that resolves a single, fully-determined ledger transition
and applies it; `accept` is `edit_and_accept` with no edits, and
`mark_complete`/`mark_superseded`/`treat_as_new` are `edit_and_accept`
with a forced `status`/`action` — see each function's docstring.

Every accepted/reviewed proposal produces, in one transaction (this
module's docstring repeats Prompt 8's own requirement verbatim because
it is the module's entire reason to exist):

- a `reviews` row with before/after snapshots and reason/note;
- creation or update of the current `ledger_items` projection;
- an append-only `ledger_versions` row (except no_op/add_evidence, which
  by definition change no field — Section 10.2 #4/#5 — and so write no
  version);
- `evidence_links` rows to the accepted version (and, for supersession,
  to the predecessor's final version too, with `support_role='supersession'`);
- a final `proposed_mutations.status`;
- a `corrections` row for every model/system-proposed field the human
  changed, with `ledger_items.user_corrected` set so reconciliation
  never silently reverses it again (Section 10.4's `corrected_field_disagreement`
  penalty, Prompt 7).

This composition is possible without duplicating `services.ledger`'s
version-append logic because `db.connection.transaction` is reentrant
(Prompt 8) — `_apply` opens one outer transaction and calls
`services.ledger.create_ledger_item`/`append_ledger_version` (each
already transactional on its own) from inside it; only the outermost
`with transaction(conn):` actually commits or rolls back, so a failure
partway through (an illegal transition, a DB constraint) unwinds
everything written so far in the same call.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from project_context.db import (
    correction_repository,
    evidence_link_repository,
    evidence_repository,
    ledger_repository,
    observation_repository,
    people_repository,
    proposed_mutation_repository,
    review_repository,
)
from project_context.db.connection import transaction
from project_context.domain.evidence import SourceArtifact
from project_context.domain.evidence_links import (
    EvidenceLink,
    EvidenceLinkSupportRole,
    EvidenceLinkTargetType,
)
from project_context.domain.ledger import (
    INITIAL_STATUS_BY_KIND,
    ConfidenceBand,
    LedgerItem,
    LedgerItemKind,
    LedgerItemStatus,
    LedgerTransitionType,
    LedgerVersion,
)
from project_context.domain.observations import Observation
from project_context.domain.reconciliation import NATURAL_LEDGER_KIND
from project_context.domain.review import (
    Correction,
    CorrectionMateriality,
    CorrectionReasonCode,
    CorrectionTargetType,
    ProposalEdit,
    ProposedMutation,
    ProposedMutationAction,
    ProposedMutationStatus,
    Review,
    ReviewAction,
)
from project_context.ids import new_id
from project_context.llm.schemas import ObservationKind
from project_context.services.ledger import append_ledger_version, create_ledger_item

DEFAULT_ACTOR = "local-user"

#: Distinguishes "no override supplied" from "explicitly supplied" for
#: `expected_target_version_id`, since `None` is itself a meaningful
#: value there (it means "I expect there is no target item at all").
_UNSET = object()


class ProposalNotFoundError(LookupError):
    """Raised when a `proposal_id` does not identify an existing proposal
    in the given project."""


class ObservationNotFoundError(LookupError):
    """Raised when a proposal's `observation_id` does not resolve — this
    should be unreachable in practice (observations are immutable and
    never deleted) but is checked explicitly rather than assumed."""


class ReviewActionNotApplicableError(ValueError):
    """Raised when the requested action has no legal ledger transition
    for this proposal as given (e.g. plain `accept` on a `conflict`
    proposal, or `no_op`/`add_evidence` combined with an explicit
    status override)."""


class ReviewValidationError(ValueError):
    """Raised for any server-side edit-and-accept validation failure:
    missing evidence selection, an unknown evidence link id, a missing
    kind for a from-scratch create, etc."""


class StaleReviewError(RuntimeError):
    """Raised when `expected_target_version_id` was supplied and no
    longer matches the target item's live `current_version_id` — the
    review card was rendered against state that has since changed."""


class CrossProjectReferenceError(ValueError):
    """Raised when an edit references a ledger item, evidence link, or
    proposal that does not belong to the project being reviewed in."""


# ---------------------------------------------------------------------------
# Status/action -> ledger transition resolution.
# ---------------------------------------------------------------------------

#: `edits.status`, when given, is the primary lever for which transition
#: happens (Section 10's status vocabulary) — this is what makes
#: `RESOLVE` (risks/blockers/open questions) reachable at all, since
#: `ProposedMutationAction` has no `resolve` value of its own.
_STATUS_TO_TRANSITION: dict[LedgerItemStatus, LedgerTransitionType] = {
    LedgerItemStatus.COMPLETED: LedgerTransitionType.COMPLETE,
    LedgerItemStatus.CANCELED: LedgerTransitionType.CANCEL,
    LedgerItemStatus.SUPERSEDED: LedgerTransitionType.SUPERSEDE,
    LedgerItemStatus.RESOLVED: LedgerTransitionType.RESOLVE,
}

#: `ProposedMutationAction`'s five ledger-mutating values share their
#: exact string with the matching `LedgerTransitionType` — reconciliation
#: (Prompt 7) and this module's transition vocabulary agree by
#: construction, not by coincidence.
_ACTION_TO_TRANSITION: dict[ProposedMutationAction, LedgerTransitionType] = {
    ProposedMutationAction.CREATE: LedgerTransitionType.CREATE,
    ProposedMutationAction.UPDATE: LedgerTransitionType.UPDATE,
    ProposedMutationAction.COMPLETE: LedgerTransitionType.COMPLETE,
    ProposedMutationAction.CANCEL: LedgerTransitionType.CANCEL,
    ProposedMutationAction.SUPERSEDE: LedgerTransitionType.SUPERSEDE,
}

_FIXED_STATUS_FOR_TRANSITION: dict[LedgerTransitionType, LedgerItemStatus] = {
    LedgerTransitionType.COMPLETE: LedgerItemStatus.COMPLETED,
    LedgerTransitionType.CANCEL: LedgerItemStatus.CANCELED,
    LedgerTransitionType.SUPERSEDE: LedgerItemStatus.SUPERSEDED,
    LedgerTransitionType.RESOLVE: LedgerItemStatus.RESOLVED,
}

_NO_LEDGER_MUTATION_ACTIONS = frozenset(
    {ProposedMutationAction.NO_OP, ProposedMutationAction.ADD_EVIDENCE}
)

#: Correction field metadata: (reason_code, materiality) per resolved
#: field name (Section 9's `corrections.reason_code` vocabulary).
_CORRECTION_FIELD_META: dict[str, tuple[CorrectionReasonCode, CorrectionMateriality]] = {
    "kind": (CorrectionReasonCode.WRONG_TYPE, CorrectionMateriality.MATERIAL),
    "canonical_title": (CorrectionReasonCode.WORDING, CorrectionMateriality.MINOR),
    "canonical_description": (CorrectionReasonCode.WORDING, CorrectionMateriality.MINOR),
    "owner_person_id": (CorrectionReasonCode.WRONG_OWNER, CorrectionMateriality.MATERIAL),
    "due_date": (CorrectionReasonCode.WRONG_DATE, CorrectionMateriality.MATERIAL),
    "status": (CorrectionReasonCode.WRONG_STATUS, CorrectionMateriality.MATERIAL),
    "target_ledger_item_id": (CorrectionReasonCode.WRONG_MATCH, CorrectionMateriality.MATERIAL),
    "evidence_link_ids": (CorrectionReasonCode.UNSUPPORTED, CorrectionMateriality.MINOR),
}


def _resolve_transition_type(
    edits_status: LedgerItemStatus | None,
    effective_action: ProposedMutationAction,
    *,
    has_target: bool,
) -> LedgerTransitionType:
    if edits_status is not None:
        transition = _STATUS_TO_TRANSITION.get(edits_status)
        if transition is not None:
            return transition
        return LedgerTransitionType.UPDATE if has_target else LedgerTransitionType.CREATE
    transition = _ACTION_TO_TRANSITION.get(effective_action)
    if transition is None:
        raise ReviewActionNotApplicableError(
            f"{effective_action.value!r} proposals have no direct ledger transition; "
            "use edit_and_accept with an explicit status, treat_as_new, or reject"
        )
    return transition


def _resolve_kind(
    edits: ProposalEdit, patch: dict[str, Any], observation: Observation
) -> LedgerItemKind:
    if edits.kind is not None:
        return edits.kind
    patch_kind = patch.get("kind")
    if patch_kind is not None:
        return LedgerItemKind(patch_kind)
    natural = NATURAL_LEDGER_KIND.get(ObservationKind(observation.kind))
    if natural is None:
        raise ReviewValidationError(
            "a ledger item kind is required: the underlying observation is kind "
            f"{observation.kind!r}, which has no default ledger kind of its own"
        )
    return natural


def _resolve_create_fields(
    edits: ProposalEdit, patch: dict[str, Any], observation: Observation
) -> dict[str, Any]:
    """The full field set a CREATE (or a supersession's successor
    CREATE) needs, merging edits over the proposal's own patch, falling
    back to the observation itself for anything neither supplied."""
    title = edits.canonical_title or patch.get("canonical_title") or observation.subject
    owner_person_id = (
        edits.owner_person_id if edits.owner_person_id is not None else patch.get("owner_person_id")
    )
    return {
        "kind": _resolve_kind(edits, patch, observation),
        "canonical_title": title,
        "canonical_description": (
            edits.canonical_description
            if edits.canonical_description is not None
            else patch.get("canonical_description", observation.statement)
        ),
        "owner_person_id": owner_person_id,
        "due_date": edits.due_date if edits.due_date is not None else patch.get("due_date"),
    }


def _item_snapshot(item: LedgerItem | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "kind": item.kind.value,
        "canonical_title": item.canonical_title,
        "canonical_description": item.canonical_description,
        "status": item.status.value,
        "owner_person_id": item.owner_person_id,
        "due_date": item.due_date,
    }


# ---------------------------------------------------------------------------
# Evidence.
# ---------------------------------------------------------------------------


def _resolve_evidence_links(
    conn: sqlite3.Connection,
    project_id: str,
    observation_id: str,
    evidence_link_ids: tuple[str, ...] | None,
) -> list[EvidenceLink]:
    all_links = evidence_link_repository.list_for_target(
        conn, project_id, EvidenceLinkTargetType.OBSERVATION, observation_id
    )
    if evidence_link_ids is None:
        return all_links
    by_id = {link.id: link for link in all_links}
    selected: list[EvidenceLink] = []
    for link_id in evidence_link_ids:
        link = by_id.get(link_id)
        if link is None:
            raise ReviewValidationError(
                f"evidence link {link_id!r} is not one of this observation's own evidence links"
            )
        selected.append(link)
    return selected


def _link_evidence(
    conn: sqlite3.Connection,
    project_id: str,
    links: list[EvidenceLink],
    *,
    target_type: EvidenceLinkTargetType,
    target_id: str,
    support_role: EvidenceLinkSupportRole,
) -> tuple[EvidenceLink, ...]:
    return tuple(
        evidence_link_repository.insert_link(
            conn,
            project_id,
            target_type=target_type,
            target_id=target_id,
            content_id=link.content_id,
            chunk_id=link.chunk_id,
            char_start=link.char_start,
            char_end=link.char_end,
            quote=link.quote,
            support_role=support_role,
            location=link.location,
        )
        for link in links
    )


# ---------------------------------------------------------------------------
# Corrections.
# ---------------------------------------------------------------------------


def _record_correction_if_changed(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    target_type: CorrectionTargetType,
    target_id: str,
    review_id: str,
    field_name: str,
    original: Any,
    corrected: Any,
    observation: Observation,
    actor: str,
) -> Correction | None:
    """Record one correction row iff `original` (what the model/system
    proposed) and `corrected` (what the human accepted) actually differ.
    `original=None` (the field was never proposed at all — e.g. a bare
    CONFLICT's empty patch) is treated as "nothing to compare," not a
    correction from `None`."""
    if original is None or original == corrected:
        return None
    reason_code, materiality = _CORRECTION_FIELD_META[field_name]
    return correction_repository.insert_correction(
        conn,
        project_id,
        target_type=target_type,
        target_id=target_id,
        field_name=field_name,
        reason_code=reason_code,
        materiality=materiality,
        original={"value": original},
        corrected={"value": corrected},
        review_id=review_id,
        model_id=observation.model_id,
        prompt_version=observation.prompt_version,
        actor=actor,
    )


# ---------------------------------------------------------------------------
# The result every review action returns.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewOutcome:
    review: Review
    proposal: ProposedMutation
    ledger_item: LedgerItem | None
    ledger_version: LedgerVersion | None
    #: Set only for a supersession: the OLD item, refreshed after its
    #: `superseded_by_item_id` was set.
    predecessor_item: LedgerItem | None
    corrections: tuple[Correction, ...]
    #: True when this call found the proposal already decided (by an
    #: earlier call, possibly a duplicate submission of this exact
    #: request) and returned that existing outcome without writing
    #: anything new.
    already_applied: bool


def _idempotent_outcome(
    conn: sqlite3.Connection, project_id: str, proposal: ProposedMutation
) -> ReviewOutcome:
    review = review_repository.get_review_for_proposal(conn, project_id, proposal.id)
    if review is None:
        raise ReviewValidationError(
            f"proposal {proposal.id!r} is {proposal.status.value!r} but has no review record"
        )
    ledger_item = (
        ledger_repository.get_item(conn, project_id, proposal.target_ledger_item_id)
        if proposal.target_ledger_item_id is not None
        else None
    )
    return ReviewOutcome(
        review=review,
        proposal=proposal,
        ledger_item=ledger_item,
        ledger_version=None,
        predecessor_item=None,
        corrections=(),
        already_applied=True,
    )


# ---------------------------------------------------------------------------
# The core engine. Every public accept-family function below is a thin
# wrapper that builds a `ProposalEdit` and calls this.
# ---------------------------------------------------------------------------


def _apply(
    conn: sqlite3.Connection,
    project_id: str,
    proposal_id: str,
    *,
    edits: ProposalEdit,
    review_action: ReviewAction,
    actor: str,
    note: str | None,
    reason_code: str | None,
    expected_target_version_id: Any = _UNSET,
    force_no_target: bool = False,
) -> ReviewOutcome:
    proposal = proposed_mutation_repository.get_proposal(conn, project_id, proposal_id)
    if proposal is None:
        raise ProposalNotFoundError(proposal_id)

    # --- idempotence: a repeated submission (double-click, retried
    # request) for an already-decided proposal returns that decision
    # instead of re-applying it. -----------------------------------------
    if proposal.status is not ProposedMutationStatus.PENDING:
        return _idempotent_outcome(conn, project_id, proposal)

    observation = observation_repository.get_observation(conn, project_id, proposal.observation_id)
    if observation is None:
        raise ObservationNotFoundError(proposal.observation_id)

    effective_target_id = None
    if not force_no_target:
        effective_target_id = (
            edits.target_ledger_item_id
            if edits.target_ledger_item_id is not None
            else proposal.target_ledger_item_id
        )
    target_item: LedgerItem | None = None
    if effective_target_id is not None:
        target_item = ledger_repository.get_item(conn, project_id, effective_target_id)
        if target_item is None:
            raise CrossProjectReferenceError(
                f"ledger item {effective_target_id!r} does not exist in project {project_id!r}"
            )

    # --- optimistic staleness check --------------------------------------
    if expected_target_version_id is not _UNSET:
        live_version_id = target_item.current_version_id if target_item is not None else None
        if live_version_id != expected_target_version_id:
            raise StaleReviewError(
                "this review card is stale: the target ledger item changed since it was "
                f"loaded (expected current version {expected_target_version_id!r}, found "
                f"{live_version_id!r})"
            )

    patch = proposal.proposed_patch or {}
    effective_action = edits.action or proposal.action

    if (
        edits.status is None
        and edits.action is None
        and effective_action is ProposedMutationAction.CONFLICT
    ):
        raise ReviewActionNotApplicableError(
            "a conflict proposal cannot be accepted as-is — use edit_and_accept with an "
            "explicit status/target, treat_as_new, mark_complete, mark_superseded, or reject"
        )

    review_id = new_id()

    with transaction(conn):
        if edits.status is None and effective_action in _NO_LEDGER_MUTATION_ACTIONS:
            before = after = _item_snapshot(target_item)
        else:
            plan = _plan_transition(
                conn, project_id,
                observation=observation, edits=edits, patch=patch,
                effective_action=effective_action, target_item=target_item,
            )
            before, after = plan.before, plan.after

        # The review row must exist before any `ledger_versions`/
        # `corrections` row references it — both carry an *immediate*
        # foreign key to `reviews.id` (SQLite checks it per-statement,
        # not deferred to COMMIT), so it is written first, using
        # before/after snapshots computed purely from already-fetched
        # state (no ledger write has happened yet at this point).
        review = review_repository.insert_review(
            conn, project_id, review_id=review_id, proposal_id=proposal_id,
            action=review_action, before=before, after=after,
            reason_code=reason_code, note=note, actor=actor,
        )

        if edits.status is None and effective_action in _NO_LEDGER_MUTATION_ACTIONS:
            ledger_item, ledger_version, predecessor_item, corrections = _execute_no_mutation(
                conn, project_id,
                observation=observation, effective_action=effective_action,
                target_item=target_item, edits=edits,
            )
        else:
            ledger_item, ledger_version, predecessor_item, corrections = _execute_transition(
                conn, project_id, plan,
                observation=observation, review_id=review_id, actor=actor,
                confidence_band=proposal.confidence_band,
            )

        final_status = (
            ProposedMutationStatus.EDITED_ACCEPTED
            if review_action is ReviewAction.EDIT_ACCEPT
            else ProposedMutationStatus.ACCEPTED
        )
        updated_proposal = proposed_mutation_repository.set_status(
            conn, project_id, proposal_id, final_status, reviewed_at=review.reviewed_at
        )
        assert updated_proposal is not None

    return ReviewOutcome(
        review=review,
        proposal=updated_proposal,
        ledger_item=ledger_item,
        ledger_version=ledger_version,
        predecessor_item=predecessor_item,
        corrections=tuple(corrections),
        already_applied=False,
    )


def _execute_no_mutation(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    observation: Observation,
    effective_action: ProposedMutationAction,
    target_item: LedgerItem | None,
    edits: ProposalEdit,
) -> tuple[LedgerItem | None, None, None, list[Correction]]:
    """NO_OP writes nothing at all (Section 10.2 #4/#5: no changed
    field, nothing new to add). ADD_EVIDENCE links the observation's
    evidence to the item-level target but still writes no new
    `ledger_versions` row — the item's field values are unchanged."""
    if effective_action is ProposedMutationAction.ADD_EVIDENCE:
        if target_item is None:
            raise ReviewValidationError("add_evidence requires a target ledger item")
        links = _resolve_evidence_links(conn, project_id, observation.id, edits.evidence_link_ids)
        if not links:
            raise ReviewValidationError("at least one evidence link is required")
        _link_evidence(
            conn,
            project_id,
            links,
            target_type=EvidenceLinkTargetType.LEDGER_ITEM,
            target_id=target_item.id,
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )
    return target_item, None, None, []


@dataclass(frozen=True)
class _TransitionPlan:
    """The fully-resolved shape of a ledger-mutating transition, computed
    without writing anything — so `_apply` can insert the review row
    (which every write below needs to reference) before any of it runs."""

    transition_type: LedgerTransitionType
    patch: dict[str, Any]
    fields: dict[str, Any]
    status: LedgerItemStatus
    target_item: LedgerItem | None
    links: list[EvidenceLink]
    before: dict[str, Any] | None
    after: dict[str, Any] | None


def _plan_transition(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    observation: Observation,
    edits: ProposalEdit,
    patch: dict[str, Any],
    effective_action: ProposedMutationAction,
    target_item: LedgerItem | None,
) -> _TransitionPlan:
    owner_edit = edits.owner_person_id
    if owner_edit is not None and people_repository.get_person(conn, owner_edit) is None:
        raise ReviewValidationError(f"person {owner_edit!r} does not exist")

    transition_type = _resolve_transition_type(
        edits.status, effective_action, has_target=target_item is not None
    )
    links = _resolve_evidence_links(conn, project_id, observation.id, edits.evidence_link_ids)
    if not links:
        raise ReviewValidationError("at least one evidence link is required to accept this change")

    if transition_type in (LedgerTransitionType.CREATE, LedgerTransitionType.SUPERSEDE):
        if transition_type is LedgerTransitionType.SUPERSEDE and target_item is None:
            raise ReviewValidationError("supersede requires an existing target ledger item")
        fields = _resolve_create_fields(edits, patch, observation)
        status = INITIAL_STATUS_BY_KIND[fields["kind"]]
        before = None
        if transition_type is LedgerTransitionType.SUPERSEDE:
            before = _item_snapshot(target_item)
        after = {
            "kind": fields["kind"].value,
            "canonical_title": fields["canonical_title"],
            "canonical_description": fields["canonical_description"],
            "status": status.value,
            "owner_person_id": fields["owner_person_id"],
            "due_date": fields["due_date"],
        }
        return _TransitionPlan(
            transition_type, patch, fields, status, target_item, links, before, after
        )

    # UPDATE / COMPLETE / CANCEL / RESOLVE — all mutate one existing item.
    if target_item is None:
        raise ReviewValidationError(
            f"{transition_type.value} requires an existing target ledger item"
        )
    owner_person_id = (
        edits.owner_person_id if edits.owner_person_id is not None else patch.get("owner_person_id")
    )
    due_date = edits.due_date if edits.due_date is not None else patch.get("due_date")
    status = (
        edits.status if edits.status is not None
        else _FIXED_STATUS_FOR_TRANSITION.get(transition_type, target_item.status)
    )
    fields = {
        "canonical_title": edits.canonical_title,
        "canonical_description": edits.canonical_description,
        "owner_person_id": owner_person_id,
        "due_date": due_date,
    }
    before = _item_snapshot(target_item)
    resolved_owner = owner_person_id if owner_person_id is not None else target_item.owner_person_id
    resolved_due_date = due_date if due_date is not None else target_item.due_date
    after = {
        "kind": target_item.kind.value,
        "canonical_title": edits.canonical_title or target_item.canonical_title,
        "canonical_description": (
            edits.canonical_description
            if edits.canonical_description is not None
            else target_item.canonical_description
        ),
        "status": status.value,
        "owner_person_id": resolved_owner,
        "due_date": resolved_due_date,
    }
    return _TransitionPlan(
        transition_type, patch, fields, status, target_item, links, before, after
    )


def _execute_transition(
    conn: sqlite3.Connection,
    project_id: str,
    plan: _TransitionPlan,
    *,
    observation: Observation,
    review_id: str,
    actor: str,
    confidence_band: str | None,
) -> tuple[LedgerItem, LedgerVersion, LedgerItem | None, list[Correction]]:
    band = ConfidenceBand(confidence_band) if confidence_band else None

    if plan.transition_type is LedgerTransitionType.CREATE:
        item, version = create_ledger_item(
            conn, project_id,
            kind=plan.fields["kind"],
            canonical_title=plan.fields["canonical_title"],
            canonical_description=plan.fields["canonical_description"],
            owner_person_id=plan.fields["owner_person_id"],
            due_date=plan.fields["due_date"],
            confidence_band=band,
            observation_id=observation.id,
            review_id=review_id,
        )
        corrections = _record_create_corrections(
            conn, project_id, item, plan.patch, plan.fields, review_id, observation, actor
        )
        if corrections:
            item = ledger_repository.update_projection(
                conn, project_id, item.id,
                canonical_title=item.canonical_title,
                canonical_description=item.canonical_description,
                status=item.status,
                owner_person_id=item.owner_person_id,
                due_date=item.due_date,
                effective_at=item.effective_at,
                confidence_band=item.confidence_band,
                current_version_id=item.current_version_id,
                user_corrected=True,
            )
        _link_evidence(
            conn, project_id, plan.links,
            target_type=EvidenceLinkTargetType.LEDGER_ITEM, target_id=item.id,
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )
        _link_evidence(
            conn, project_id, plan.links,
            target_type=EvidenceLinkTargetType.LEDGER_VERSION, target_id=version.id,
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )
        return item, version, None, corrections

    if plan.transition_type is LedgerTransitionType.SUPERSEDE:
        old_item, old_version = append_ledger_version(
            conn, project_id, plan.target_item.id,
            transition_type=LedgerTransitionType.SUPERSEDE,
            status=LedgerItemStatus.SUPERSEDED,
            observation_id=observation.id,
            review_id=review_id,
        )
        new_item, new_version = create_ledger_item(
            conn, project_id,
            kind=plan.fields["kind"],
            canonical_title=plan.fields["canonical_title"],
            canonical_description=plan.fields["canonical_description"],
            owner_person_id=plan.fields["owner_person_id"],
            due_date=plan.fields["due_date"],
            confidence_band=band,
            observation_id=observation.id,
            review_id=review_id,
        )
        ledger_repository.set_supersession_links(
            conn, project_id, old_item.id, superseded_by_item_id=new_item.id
        )
        new_item = ledger_repository.set_supersession_links(
            conn, project_id, new_item.id, supersedes_item_id=old_item.id
        )
        ledger_repository.link_superseding_version(
            conn, project_id, old_version.id, superseded_by_version_id=new_version.id
        )
        corrections = _record_create_corrections(
            conn, project_id, new_item, plan.patch, plan.fields, review_id, observation, actor
        )
        _link_evidence(
            conn, project_id, plan.links,
            target_type=EvidenceLinkTargetType.LEDGER_ITEM, target_id=new_item.id,
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )
        _link_evidence(
            conn, project_id, plan.links,
            target_type=EvidenceLinkTargetType.LEDGER_VERSION, target_id=new_version.id,
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )
        _link_evidence(
            conn, project_id, plan.links,
            target_type=EvidenceLinkTargetType.LEDGER_ITEM, target_id=old_item.id,
            support_role=EvidenceLinkSupportRole.SUPERSESSION,
        )
        _link_evidence(
            conn, project_id, plan.links,
            target_type=EvidenceLinkTargetType.LEDGER_VERSION, target_id=old_version.id,
            support_role=EvidenceLinkSupportRole.SUPERSESSION,
        )
        predecessor_item = ledger_repository.get_item(conn, project_id, old_item.id)
        return new_item, new_version, predecessor_item, corrections

    # UPDATE / COMPLETE / CANCEL / RESOLVE — all mutate one existing item.
    target_item = plan.target_item
    corrections = _record_update_corrections(
        conn, project_id, target_item, plan.patch, plan.after, review_id, observation, actor
    )
    item, version = append_ledger_version(
        conn, project_id, target_item.id,
        transition_type=plan.transition_type,
        status=plan.status,
        canonical_title=plan.fields["canonical_title"],
        canonical_description=plan.fields["canonical_description"],
        owner_person_id=plan.fields["owner_person_id"],
        due_date=plan.fields["due_date"],
        confidence_band=band,
        observation_id=observation.id,
        review_id=review_id,
        user_corrected=True if corrections else None,
    )
    support_role = (
        EvidenceLinkSupportRole.COMPLETION
        if plan.transition_type is LedgerTransitionType.COMPLETE
        else EvidenceLinkSupportRole.SUPPORTS
    )
    _link_evidence(
        conn, project_id, plan.links,
        target_type=EvidenceLinkTargetType.LEDGER_ITEM, target_id=item.id,
        support_role=support_role,
    )
    _link_evidence(
        conn, project_id, plan.links,
        target_type=EvidenceLinkTargetType.LEDGER_VERSION, target_id=version.id,
        support_role=support_role,
    )
    return item, version, None, corrections


def _record_create_corrections(
    conn: sqlite3.Connection,
    project_id: str,
    item: LedgerItem,
    patch: dict[str, Any],
    fields: dict[str, Any],
    review_id: str,
    observation: Observation,
    actor: str,
) -> list[Correction]:
    corrections = []
    create_fields = (
        "kind", "canonical_title", "canonical_description", "owner_person_id", "due_date"
    )
    for field_name in create_fields:
        original = patch.get(field_name)
        corrected = fields[field_name]
        corrected_value = corrected.value if hasattr(corrected, "value") else corrected
        correction = _record_correction_if_changed(
            conn, project_id,
            target_type=CorrectionTargetType.LEDGER_ITEM, target_id=item.id, review_id=review_id,
            field_name=field_name, original=original, corrected=corrected_value,
            observation=observation, actor=actor,
        )
        if correction is not None:
            corrections.append(correction)
    return corrections


def _record_update_corrections(
    conn: sqlite3.Connection,
    project_id: str,
    target_item: LedgerItem,
    patch: dict[str, Any],
    resolved: dict[str, Any],
    review_id: str,
    observation: Observation,
    actor: str,
) -> list[Correction]:
    corrections = []
    for field_name in ("owner_person_id", "due_date", "status"):
        original = patch.get(field_name)
        corrected = resolved[field_name]
        correction = _record_correction_if_changed(
            conn, project_id,
            target_type=CorrectionTargetType.LEDGER_ITEM,
            target_id=target_item.id,
            review_id=review_id,
            field_name=field_name, original=original, corrected=corrected,
            observation=observation, actor=actor,
        )
        if correction is not None:
            corrections.append(correction)
    return corrections


# ---------------------------------------------------------------------------
# Public review actions.
# ---------------------------------------------------------------------------


def accept_proposal(
    conn: sqlite3.Connection,
    project_id: str,
    proposal_id: str,
    *,
    actor: str = DEFAULT_ACTOR,
    note: str | None = None,
    reason_code: str | None = None,
    expected_target_version_id: Any = _UNSET,
) -> ReviewOutcome:
    """Accept a proposal exactly as reconciliation proposed it, with no
    edits. Raises `ReviewActionNotApplicableError` for a `conflict`
    proposal — there is nothing to apply as-is (Section 10.11: "leave
    current ledger state unchanged until review"); use
    `edit_and_accept_proposal`, `treat_as_new`, `mark_complete`,
    `mark_superseded`, or `reject_proposal` instead."""
    return _apply(
        conn, project_id, proposal_id,
        edits=ProposalEdit(),
        review_action=ReviewAction.ACCEPT,
        actor=actor, note=note, reason_code=reason_code,
        expected_target_version_id=expected_target_version_id,
    )


def edit_and_accept_proposal(
    conn: sqlite3.Connection,
    project_id: str,
    proposal_id: str,
    edits: ProposalEdit,
    *,
    actor: str = DEFAULT_ACTOR,
    note: str | None = None,
    reason_code: str | None = None,
    expected_target_version_id: Any = _UNSET,
) -> ReviewOutcome:
    """Accept a proposal with human-supplied overrides for wording,
    kind, owner, due date, status, target item, and/or evidence
    selection (FR-020; Section 6). Any field left `None` on `edits`
    falls back to the proposal's own `proposed_patch`, then to the
    current ledger item's value."""
    return _apply(
        conn, project_id, proposal_id,
        edits=edits,
        review_action=ReviewAction.EDIT_ACCEPT,
        actor=actor, note=note, reason_code=reason_code,
        expected_target_version_id=expected_target_version_id,
    )


def mark_complete(
    conn: sqlite3.Connection,
    project_id: str,
    proposal_id: str,
    *,
    actor: str = DEFAULT_ACTOR,
    note: str | None = None,
    reason_code: str | None = None,
    expected_target_version_id: Any = _UNSET,
) -> ReviewOutcome:
    """Force the resulting transition to COMPLETE, regardless of what
    the proposal itself proposed — the human reviewer decided a
    different candidate/proposal really represents a completion."""
    return _apply(
        conn, project_id, proposal_id,
        edits=ProposalEdit(status=LedgerItemStatus.COMPLETED),
        review_action=ReviewAction.MARK_COMPLETE,
        actor=actor, note=note, reason_code=reason_code,
        expected_target_version_id=expected_target_version_id,
    )


def mark_superseded(
    conn: sqlite3.Connection,
    project_id: str,
    proposal_id: str,
    *,
    actor: str = DEFAULT_ACTOR,
    note: str | None = None,
    reason_code: str | None = None,
    expected_target_version_id: Any = _UNSET,
) -> ReviewOutcome:
    """Force the resulting transition to SUPERSEDE: the target item is
    marked superseded and a new successor item is created from this
    proposal's observation (Section 10.10)."""
    return _apply(
        conn, project_id, proposal_id,
        edits=ProposalEdit(status=LedgerItemStatus.SUPERSEDED),
        review_action=ReviewAction.MARK_SUPERSEDED,
        actor=actor, note=note, reason_code=reason_code,
        expected_target_version_id=expected_target_version_id,
    )


def treat_as_new(
    conn: sqlite3.Connection,
    project_id: str,
    proposal_id: str,
    *,
    actor: str = DEFAULT_ACTOR,
    note: str | None = None,
    reason_code: str | None = None,
    kind: LedgerItemKind | None = None,
    canonical_title: str | None = None,
    canonical_description: str | None = None,
    owner_person_id: str | None = None,
    due_date: str | None = None,
    evidence_link_ids: tuple[str, ...] | None = None,
) -> ReviewOutcome:
    """A proposed match was wrong: ignore whatever candidate
    reconciliation picked (if any) and create a brand-new ledger item
    instead, from this observation (optionally with the same kind of
    field overrides `edit_and_accept_proposal` supports). Never checked
    for staleness against a target — there is deliberately no target."""
    edits = ProposalEdit(
        action=ProposedMutationAction.CREATE,
        kind=kind,
        canonical_title=canonical_title,
        canonical_description=canonical_description,
        owner_person_id=owner_person_id,
        due_date=due_date,
        evidence_link_ids=evidence_link_ids,
    )
    return _apply(
        conn, project_id, proposal_id,
        edits=edits,
        review_action=ReviewAction.TREAT_AS_NEW,
        actor=actor, note=note, reason_code=reason_code,
        expected_target_version_id=_UNSET,
        force_no_target=True,
    )


def reject_proposal(
    conn: sqlite3.Connection,
    project_id: str,
    proposal_id: str,
    *,
    actor: str = DEFAULT_ACTOR,
    reason_code: str | None = None,
    note: str | None = None,
) -> ReviewOutcome:
    """Reject a proposal: records the decision and reason, leaves the
    observation and proposal record intact, and writes no ledger
    version at all (FR-020's "reversible only by a new corrective
    action" — a rejected proposal is simply not acted on, not undone)."""
    proposal = proposed_mutation_repository.get_proposal(conn, project_id, proposal_id)
    if proposal is None:
        raise ProposalNotFoundError(proposal_id)
    if proposal.status is not ProposedMutationStatus.PENDING:
        return _idempotent_outcome(conn, project_id, proposal)

    review_id = new_id()
    with transaction(conn):
        review = review_repository.insert_review(
            conn, project_id, review_id=review_id, proposal_id=proposal_id,
            action=ReviewAction.REJECT, before=None, after=None,
            reason_code=reason_code, note=note, actor=actor,
        )
        updated_proposal = proposed_mutation_repository.set_status(
            conn, project_id, proposal_id,
            ProposedMutationStatus.REJECTED, reviewed_at=review.reviewed_at,
        )
        assert updated_proposal is not None

    return ReviewOutcome(
        review=review, proposal=updated_proposal, ledger_item=None, ledger_version=None,
        predecessor_item=None, corrections=(), already_applied=False,
    )


# ---------------------------------------------------------------------------
# Read model for the review UI (Section 6's review card anatomy). Kept
# here, not in the UI layer, so Streamlit callbacks stay thin.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceCitation:
    link_id: str
    content_id: str
    chunk_id: str | None
    char_start: int
    char_end: int
    quote: str
    source: SourceArtifact | None


@dataclass(frozen=True)
class ReviewCard:
    proposal: ProposedMutation
    observation: Observation
    target_item: LedgerItem | None
    evidence: tuple[EvidenceCitation, ...]


def build_review_card(
    conn: sqlite3.Connection, project_id: str, proposal: ProposedMutation
) -> ReviewCard | None:
    """Everything one review card needs to render (Section 6's "Review
    card anatomy"): the proposal, its observation, the current target
    item (if any), and every cited evidence span with its source
    metadata. Returns `None` if the observation no longer resolves
    (should be unreachable — observations are immutable)."""
    observation = observation_repository.get_observation(conn, project_id, proposal.observation_id)
    if observation is None:
        return None
    target_item = (
        ledger_repository.get_item(conn, project_id, proposal.target_ledger_item_id)
        if proposal.target_ledger_item_id is not None
        else None
    )
    links = evidence_link_repository.list_for_target(
        conn, project_id, EvidenceLinkTargetType.OBSERVATION, proposal.observation_id
    )
    citations = []
    for link in links:
        source = None
        content = evidence_repository.get_content(conn, project_id, link.content_id)
        if content is not None:
            source = evidence_repository.get_artifact(conn, project_id, content.artifact_id)
        citations.append(
            EvidenceCitation(
                link_id=link.id,
                content_id=link.content_id,
                chunk_id=link.chunk_id,
                char_start=link.char_start,
                char_end=link.char_end,
                quote=link.quote,
                source=source,
            )
        )
    return ReviewCard(
        proposal=proposal,
        observation=observation,
        target_item=target_item,
        evidence=tuple(citations),
    )


def list_review_queue(conn: sqlite3.Connection, project_id: str) -> list[ReviewCard]:
    """Every pending proposal for a project, as review cards, sorted
    conflicts/material changes before low-risk creates (Section 6:
    "Group proposals by evidence/source and sort conflicts/material
    changes before low-risk creates"). Grouping by evidence/source is
    the caller's (UI) job — it has the render context to do that
    presentation-wise; this returns one deterministically ordered list.
    """
    pending = proposed_mutation_repository.list_pending_for_project(conn, project_id)
    built = (build_review_card(conn, project_id, p) for p in pending)
    cards = [card for card in built if card is not None]

    def sort_key(card: ReviewCard) -> tuple[int, str]:
        return _action_priority(card.proposal.action), card.proposal.created_at

    return sorted(cards, key=sort_key)


#: Lower sorts first. Conflicts and every action that changes an
#: existing item's material fields (update/cancel/complete/supersede)
#: rank ahead of low-risk creates/add_evidence/no_op.
_ACTION_PRIORITY: dict[ProposedMutationAction, int] = {
    ProposedMutationAction.CONFLICT: 0,
    ProposedMutationAction.SUPERSEDE: 1,
    ProposedMutationAction.CANCEL: 1,
    ProposedMutationAction.UPDATE: 1,
    ProposedMutationAction.COMPLETE: 2,
    ProposedMutationAction.CREATE: 3,
    ProposedMutationAction.ADD_EVIDENCE: 4,
    ProposedMutationAction.NO_OP: 5,
}


def _action_priority(action: ProposedMutationAction) -> int:
    return _ACTION_PRIORITY.get(action, 9)
