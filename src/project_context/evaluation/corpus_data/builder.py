"""Shared assembly infrastructure for the three corpus builders.

Each project module (``implementation``/``advisory``/``launch``) authors
its content as an ordered list of ``ArtifactSpec``s, each holding an
ordered list of ``FactBlock``s — one block per VTT turn / TXT-Markdown
paragraph, in the exact order it will appear in the artifact. A block
either carries a ``FactSpec`` (one mention of one ground-truth
transition) or is ``None`` (deliberately irrelevant/no-material-content).

This is the **single** authoring point: `assemble()` derives, from these
block lists alone —

- every artifact's raw bytes (via
  ``project_context.evaluation.corpus_data.text_helpers``);
- every ``GroundTruthItem``/``GroundTruthTransition`` (grouping blocks by
  ``transition_id``, then by ``item_id``, in first-appearance order —
  which is why a project module must list an item's CREATE block before
  any of its later transitions' blocks, matching real chronology);
- the ``fact_plan`` (artifact_id -> ordered `(item_id, transition_id) |
  None` per chunk) the ledger runner's scripted extraction walks in
  lock-step with real chunking; and
- a shared, self-verifying ``chunk_target_chars`` (see
  ``text_helpers.choose_chunk_target_chars``) — so no project module
  hand-tunes a chunking constant.

so a project's transition metadata (owner/due_date/status/supersession)
can never drift from its evidence text: both come from the same
``FactSpec``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus
from project_context.evaluation.corpus_data.text_helpers import (
    choose_chunk_target_chars,
    pad,
    paragraphs_bytes,
    vtt_bytes,
)
from project_context.evaluation.schema import (
    ARTIFACT_KIND_MAPPING,
    AmbiguousAliasSeed,
    ArtifactKind,
    BaselineScriptedClaim,
    Checkpoint,
    CorpusArtifact,
    CorpusProject,
    EvidenceMention,
    FactRef,
    GroundTruthItem,
    GroundTruthTransition,
    Materiality,
    TransitionType,
)


@dataclass(frozen=True)
class FactSpec:
    """The full metadata one ``FactBlock`` contributes to its ground-truth
    transition. Every block naming the same ``(item_id, transition_id)``
    pair must agree on every other field — `assemble()` asserts this."""

    item_id: str
    kind: LedgerItemKind
    canonical_title: str
    transition_id: str
    type: TransitionType
    aliases: tuple[str, ...] = ()
    #: The ground-truth **resolved** owner after this transition — what
    #: a correct system's output must show. `None` means "no owner" or
    #: (Section 13.2's participant-ambiguity trap) "correctly left
    #: unresolved," which are the same observable outcome.
    owner: str | None = None
    due_date: str | None = None
    status: LedgerItemStatus | None = None
    supersedes_item_id: str | None = None
    materiality: Materiality = Materiality.MATERIAL
    notes: str | None = None
    #: The raw owner name text the *evidence itself* states, only when it
    #: differs from `owner` — the participant-ambiguity trap's whole
    #: point: the text says "Jordan" (ambiguous between two real people)
    #: while the ground-truth resolved `owner` stays `None`. Defaults to
    #: `owner` when unset (the ordinary, unambiguous case).
    observed_owner_text: str | None = None

    @property
    def extraction_owner_text(self) -> str | None:
        return self.observed_owner_text if self.observed_owner_text is not None else self.owner


@dataclass(frozen=True)
class FactBlock:
    """One authored block. `speaker` is used for VTT artifacts only (and
    must alternate turn-to-turn — see `text_helpers.vtt_bytes`); ignored
    for TXT/Markdown artifacts. `raw_statement` is padded (`text_helpers.
    pad`) before rendering, so ground truth always cites the exact,
    unpadded sentence as its `EvidenceMention.statement`."""

    raw_statement: str
    fact: FactSpec | None = None
    speaker: str | None = None


@dataclass(frozen=True)
class ArtifactSpec:
    artifact_id: str
    kind: ArtifactKind
    title: str
    occurred_at: str
    project_key: str
    blocks: list[FactBlock]
    author: str | None = None
    ambiguous_assignment: bool = False
    #: An artifact whose blocks are all `fact=None` is material=False by
    #: construction; set this True only to force it even if some
    #: FactSpec-carrying block slipped in (defensive, should not happen).
    force_non_material: bool = False


@dataclass(frozen=True)
class ProjectSpec:
    key: str
    name: str
    objective: str
    stage: str
    artifacts: list[ArtifactSpec]
    checkpoints: tuple[Checkpoint, ...]
    baseline_fake_claims: dict[str, tuple[BaselineScriptedClaim, ...]] = field(default_factory=dict)
    ambiguous_aliases: tuple[AmbiguousAliasSeed, ...] = ()


def _block_rendered_text(block: FactBlock) -> str:
    # Always render the author's own text, padded — a `fact=None` block
    # (Section 13.2's irrelevant evidence, or a deliberately non-
    # extractable mention like the tentative-vs-final trap) still needs
    # its *exact* authored wording to survive into the artifact, since
    # ground truth/report checks and baseline evidence citations may
    # need to quote it verbatim (see `project_context.evaluation.
    # corpus_data.advisory`'s tentative fee-structure line).
    return pad(block.raw_statement)


def _artifact_raw_bytes(spec: ArtifactSpec) -> bytes:
    _source_type, _suffix, _is_markdown = ARTIFACT_KIND_MAPPING[spec.kind]
    if spec.kind is ArtifactKind.MEETING_TRANSCRIPT:
        turns = [(block.speaker or "Speaker", _block_rendered_text(block)) for block in spec.blocks]
        return vtt_bytes(turns)
    paragraphs = [_block_rendered_text(block) for block in spec.blocks]
    return paragraphs_bytes(paragraphs)


def _block_group_text(spec: ArtifactSpec) -> list[str]:
    """The rendered text of each block exactly as it will appear as one
    parser block — VTT blocks are prefixed `"Speaker: "` by the real
    parser (`project_context.parsers.vtt_parser`), so that prefix is
    reproduced here for the chunk-target-chars computation."""
    if spec.kind is ArtifactKind.MEETING_TRANSCRIPT:
        return [
            f"{block.speaker or 'Speaker'}: {_block_rendered_text(block)}" for block in spec.blocks
        ]
    return [_block_rendered_text(block) for block in spec.blocks]


def assemble(project_spec: ProjectSpec) -> tuple[CorpusProject, int]:
    """Build a full `CorpusProject` plus the shared `chunk_target_chars`
    both runners must ingest with. Returns `(project, chunk_target_chars)`
    rather than folding the constant into `CorpusProject` itself — it is
    an ingestion parameter, not ground truth."""
    artifacts: list[CorpusArtifact] = []
    fact_plan: dict[str, tuple[FactRef | None, ...]] = {}
    transitions_by_key: dict[tuple[str, str], list[EvidenceMention]] = {}
    fact_specs_by_key: dict[tuple[str, str], FactSpec] = {}
    item_order: list[str] = []
    transition_order_by_item: dict[str, list[str]] = {}

    block_groups: list[list[str]] = []

    for artifact_spec in project_spec.artifacts:
        _source_type, suffix, _is_markdown = ARTIFACT_KIND_MAPPING[artifact_spec.kind]
        raw_bytes = _artifact_raw_bytes(artifact_spec)
        block_groups.append(_block_group_text(artifact_spec))

        plan_entries: list[FactRef | None] = []
        has_material = False
        for block in artifact_spec.blocks:
            if block.fact is None:
                plan_entries.append(None)
                continue
            has_material = True
            key = (block.fact.item_id, block.fact.transition_id)
            plan_entries.append(
                FactRef(item_id=block.fact.item_id, transition_id=block.fact.transition_id)
            )
            existing_spec = fact_specs_by_key.get(key)
            if existing_spec is not None:
                _assert_same_fact(existing_spec, block.fact)
            else:
                fact_specs_by_key[key] = block.fact
                if block.fact.item_id not in item_order:
                    item_order.append(block.fact.item_id)
                transition_order_by_item.setdefault(block.fact.item_id, [])
                if block.fact.transition_id not in transition_order_by_item[block.fact.item_id]:
                    transition_order_by_item[block.fact.item_id].append(block.fact.transition_id)
            transitions_by_key.setdefault(key, []).append(
                EvidenceMention(
                    artifact_id=artifact_spec.artifact_id,
                    statement=block.raw_statement,
                    observed_owner=block.fact.extraction_owner_text,
                )
            )

        fact_plan[artifact_spec.artifact_id] = tuple(plan_entries)
        artifacts.append(
            CorpusArtifact(
                artifact_id=artifact_spec.artifact_id,
                kind=artifact_spec.kind,
                title=artifact_spec.title,
                occurred_at=artifact_spec.occurred_at,
                author=artifact_spec.author,
                filename=f"{artifact_spec.artifact_id}.{suffix}",
                raw_bytes=raw_bytes,
                project_key=artifact_spec.project_key,
                ambiguous_assignment=artifact_spec.ambiguous_assignment,
                material=has_material and not artifact_spec.force_non_material,
            )
        )

    items: list[GroundTruthItem] = []
    for item_id in item_order:
        transitions: list[GroundTruthTransition] = []
        for transition_id in transition_order_by_item[item_id]:
            key = (item_id, transition_id)
            spec = fact_specs_by_key[key]
            transitions.append(
                GroundTruthTransition(
                    transition_id=transition_id,
                    type=spec.type,
                    mentions=tuple(transitions_by_key[key]),
                    owner=spec.owner,
                    due_date=spec.due_date,
                    status=spec.status,
                    supersedes_item_id=spec.supersedes_item_id,
                    materiality=spec.materiality,
                    notes=spec.notes,
                )
            )
        first_spec = fact_specs_by_key[(item_id, transition_order_by_item[item_id][0])]
        items.append(
            GroundTruthItem(
                item_id=item_id,
                kind=first_spec.kind,
                canonical_title=first_spec.canonical_title,
                aliases=first_spec.aliases,
                transitions=tuple(transitions),
            )
        )

    chunk_target_chars = choose_chunk_target_chars(block_groups)

    project = CorpusProject(
        key=project_spec.key,
        name=project_spec.name,
        objective=project_spec.objective,
        stage=project_spec.stage,
        artifacts=tuple(artifacts),
        items=tuple(items),
        checkpoints=project_spec.checkpoints,
        fact_plan=fact_plan,
        baseline_fake_claims=project_spec.baseline_fake_claims,
        ambiguous_aliases=project_spec.ambiguous_aliases,
    )
    return project, chunk_target_chars


def _assert_same_fact(existing: FactSpec, new: FactSpec) -> None:
    for field_name in (
        "kind",
        "canonical_title",
        "type",
        "owner",
        "due_date",
        "status",
        "supersedes_item_id",
    ):
        if getattr(existing, field_name) != getattr(new, field_name):
            raise ValueError(
                f"conflicting FactSpec for transition {existing.transition_id!r}: "
                f"{field_name} disagrees ({getattr(existing, field_name)!r} vs "
                f"{getattr(new, field_name)!r})"
            )
