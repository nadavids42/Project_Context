"""The one function that answers "what should this ground-truth item
look like right before this cutoff?" — used by both
``project_context.evaluation.scoring`` (to know the expected state) and
indirectly by ``project_context.evaluation.reviewer_protocol`` (to know
what a correct reviewer should have accepted at the point each artifact
was ingested). Keeping this in one module means scoring can never
silently define "expected state" differently from what the ledger runner
was actually driven to accept.
"""

from __future__ import annotations

from dataclasses import dataclass

from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus
from project_context.evaluation.schema import (
    CorpusProject,
    GroundTruthItem,
    GroundTruthTransition,
    TransitionType,
)


@dataclass(frozen=True)
class ItemState:
    """The cumulative, resolved state of one ``GroundTruthItem`` as of
    some cutoff: every field carried forward from the most recent
    transition that set it, `None` if the item does not exist yet at all
    (no transition has occurred before the cutoff)."""

    item_id: str
    kind: LedgerItemKind
    canonical_title: str
    aliases: tuple[str, ...]
    exists: bool
    status: LedgerItemStatus | None
    owner: str | None
    due_date: str | None
    #: The most recent transition applied (None if `exists` is False).
    last_transition: GroundTruthTransition | None
    #: True once some other item's transition recorded
    #: `supersedes_item_id == this item's id` before the cutoff.
    superseded: bool


def _earliest_mention_occurred_at(transition: GroundTruthTransition, project: CorpusProject) -> str:
    """The transition becomes visible as soon as its *first* (earliest-
    occurring) evidence mention lands — a later, repeated-wording mention
    (Section 13.2's trap) never delays when the fact first became known."""
    return min(project.artifact_by_id(m.artifact_id).occurred_at for m in transition.mentions)


def _visible_transitions(
    item: GroundTruthItem, project: CorpusProject, *, cutoff_at: str
) -> list[GroundTruthTransition]:
    """This item's own transitions whose earliest evidence mention
    occurred strictly before `cutoff_at`, in chronological (occurred_at,
    then authored order) sequence — mirrors `project_context.retrieval.
    meeting_prep.compute_cutoff`'s "strictly before" semantics exactly."""
    dated = [
        (_earliest_mention_occurred_at(t, project), index, t)
        for index, t in enumerate(item.transitions)
    ]
    visible = [
        (occurred_at, index, t) for occurred_at, index, t in dated if occurred_at < cutoff_at
    ]
    visible.sort(key=lambda row: (row[0], row[1]))
    return [t for _occurred_at, _index, t in visible]


def state_at_cutoff(project: CorpusProject, item: GroundTruthItem, *, cutoff_at: str) -> ItemState:
    """Fold `item`'s transitions up to (not including) `cutoff_at` into
    one resolved `ItemState`. A transition that leaves
    `owner`/`due_date`/`status` unset carries the prior value forward
    unchanged (`project_context.evaluation.schema.GroundTruthTransition`'s
    documented "resulting value" contract)."""
    visible = _visible_transitions(item, project, cutoff_at=cutoff_at)
    if not visible:
        return ItemState(
            item_id=item.item_id,
            kind=item.kind,
            canonical_title=item.canonical_title,
            aliases=item.aliases,
            exists=False,
            status=None,
            owner=None,
            due_date=None,
            last_transition=None,
            superseded=is_superseded_by_cutoff(project, item.item_id, cutoff_at=cutoff_at),
        )

    status: LedgerItemStatus | None = None
    owner: str | None = None
    due_date: str | None = None
    for transition in visible:
        if transition.status is not None:
            status = transition.status
        if transition.owner is not None:
            owner = transition.owner
        if transition.due_date is not None:
            due_date = transition.due_date

    superseded = is_superseded_by_cutoff(project, item.item_id, cutoff_at=cutoff_at)
    if superseded:
        # A predecessor item's own transitions never record its own
        # supersession (Section 10.10: the successor's SUPERSEDE
        # transition is what closes the predecessor out) — real ledger
        # state nonetheless flips the predecessor's *status* to
        # `superseded` at that moment (`project_context.services.review`'s
        # SUPERSEDE transition applies to the target item directly), so
        # the expected status here must match that, not the predecessor's
        # last own-transition status.
        status = LedgerItemStatus.SUPERSEDED

    return ItemState(
        item_id=item.item_id,
        kind=item.kind,
        canonical_title=item.canonical_title,
        aliases=item.aliases,
        exists=True,
        status=status,
        owner=owner,
        due_date=due_date,
        last_transition=visible[-1],
        superseded=superseded,
    )


def is_superseded_by_cutoff(project: CorpusProject, item_id: str, *, cutoff_at: str) -> bool:
    """True once some other item's SUPERSEDE transition (targeting
    `item_id`) is visible before `cutoff_at` — the successor item's own
    `state_at_cutoff` already reports `exists=True` once that happens, so
    this only needs to answer the predecessor's side of the relationship."""
    for other in project.items:
        if other.item_id == item_id:
            continue
        for transition in _visible_transitions(other, project, cutoff_at=cutoff_at):
            if (
                transition.type is TransitionType.SUPERSEDE
                and transition.supersedes_item_id == item_id
            ):
                return True
    return False


def transition_occurred_at(project: CorpusProject, transition: GroundTruthTransition) -> str:
    """The timestamp a transition became visible — its earliest evidence
    mention's artifact `occurred_at` (mirrors `_earliest_mention_occurred_at`,
    exposed publicly for scoring's "is this change recent enough to
    plausibly appear in a Meeting Preparation Brief" check)."""
    return _earliest_mention_occurred_at(transition, project)


def all_states_at_cutoff(project: CorpusProject, *, cutoff_at: str) -> dict[str, ItemState]:
    """`state_at_cutoff` for every item in `project` — the ledger
    runner's per-checkpoint "what must be ingested/accepted so far" view
    and scoring's "what must the brief reflect" view, in one call."""
    return {
        item.item_id: state_at_cutoff(project, item, cutoff_at=cutoff_at) for item in project.items
    }
