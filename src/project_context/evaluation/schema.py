"""Corpus and ground-truth schema for the evaluation harness (Section
13.2/13.3).

Two Pydantic model families live here:

- **Corpus**: ``CorpusArtifact``/``CorpusProject`` — the synthetic
  evidence itself (meeting transcripts, emails, documents, calendar
  metadata) plus its chronology. Nothing here is a ground-truth claim —
  an artifact is just evidence text with metadata, exactly like a real
  ``source_artifacts``/``source_contents`` row pair would be.
- **Ground truth**: ``GroundTruthItem``/``GroundTruthTransition`` — the
  authored-correct atomic items and their field/status transitions over
  time, each carrying the exact evidence span it must be traceable to
  (Section 13.3: "exact supporting/contradicting evidence spans").
  ``Checkpoint`` is the third piece: a pre-meeting (or end-of-corpus)
  cutoff at which both systems must produce a brief (Section 13.3:
  "expected brief facts before each simulated meeting").

Per Section 13.3, ground truth is authored **separately** from any
application output — nothing in this module imports from
``project_context.services``/``project_context.db``. The one place a
harness-owned enum deliberately reuses an application enum is
``GroundTruthItem.kind: LedgerItemKind`` — the item taxonomy itself is
not harness-specific, and duplicating it here would only invite drift.

Every model is frozen (Pydantic ``ConfigDict(frozen=True)``) — ground
truth, once authored, is read-only for the rest of the harness; nothing
scoring or a runner does may mutate it in place.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from project_context.domain.evidence import EvidenceSourceType
from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus
from project_context.llm.schemas import ObservationKind

#: Every timestamp in this module (`CorpusArtifact.occurred_at`,
#: `Checkpoint.cutoff_at`/`meeting_scheduled_at`) is compared with plain
#: Python string `<`/`>=` throughout `project_context.evaluation.
#: ground_truth_state` and the runners — safe *only* if every timestamp
#: shares this exact fixed-width shape (no fractional seconds, always
#: `Z`). Mixing in a fractional-second timestamp would silently break
#: every comparison (`"."` sorts before `"Z"` and every digit in ASCII),
#: so every timestamp field in this module is validated against it.
_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _require_comparable_timestamp(value: str, field_name: str) -> str:
    if not _ISO_TIMESTAMP_RE.match(value):
        raise ValueError(
            f"{field_name}={value!r} must match YYYY-MM-DDTHH:MM:SSZ exactly (no fractional "
            "seconds) — every timestamp in this module is compared as a plain string, and a "
            "differently-shaped one would silently compare incorrectly"
        )
    return value


#: Schema version for the corpus/ground-truth wire format itself
#: (independent of the extraction/brief schema versions the app already
#: tracks) — bumped whenever a field here changes shape in a way that
#: would invalidate a previously materialized corpus file.
GROUND_TRUTH_SCHEMA_VERSION = "benchmark_ground_truth_v1"


class ArtifactKind(StrEnum):
    """What kind of synthetic evidence this is (Section 13.2's required
    artifact mix). Distinct from ``project_context.domain.evidence.
    EvidenceSourceType`` — that is the *application's* vocabulary for how
    an artifact is filed; this is the *corpus's* vocabulary for how it
    was authored, mapped onto ``EvidenceSourceType``/a filename suffix by
    ``artifact_mapping`` below so both runners ingest through the exact
    same real parsers (VTT/TXT/MD)."""

    MEETING_TRANSCRIPT = "meeting_transcript"
    EMAIL = "email"
    DOCUMENT = "document"
    CALENDAR_EVENT = "calendar_event"


#: (EvidenceSourceType, filename suffix, is_markdown) per ArtifactKind —
#: the one place both the ledger runner's real ingestion and the
#: corpus materializer decide which real parser an artifact goes
#: through. Calendar metadata is deliberately plain TXT, not a real
#: `.ics` file: Section 11.4's Calendar connector is out of this
#: harness's scope (Section 13.4 controls compare the ledger/baseline
#: on identical source *text*, not on connector-specific artifact
#: typing) — see `project_context.evaluation.ledger_runner`'s module
#: docstring for the documented consequence (manual `cutoff_override`
#: instead of automatic previous-meeting detection).
ARTIFACT_KIND_MAPPING: dict[ArtifactKind, tuple[EvidenceSourceType, str, bool]] = {
    ArtifactKind.MEETING_TRANSCRIPT: (EvidenceSourceType.CALL_RECORDING, "vtt", False),
    ArtifactKind.EMAIL: (EvidenceSourceType.EMAIL, "txt", False),
    ArtifactKind.DOCUMENT: (EvidenceSourceType.DOCUMENT, "md", True),
    ArtifactKind.CALENDAR_EVENT: (EvidenceSourceType.OTHER, "txt", False),
}


class CorpusArtifact(BaseModel):
    """One synthetic piece of evidence (Section 13.2).

    ``raw_bytes`` is the **only** authored content — the exact bytes a
    real upload would carry, run through the same real parser
    (``project_context.parsers``) real ingestion uses via
    ``project_context.evaluation.corpus_text.parse_artifact``. Neither
    this model nor the corpus builders store a separately-authored
    "normalized text" copy: doing so would risk the baseline runner (which
    reads text directly) and the ledger runner (which ingests bytes
    through the real parser) silently seeing different text, which would
    invalidate Section 13.4's "same source text" control at its root.

    ``project_key`` is this artifact's **correct** assignment — always
    the project it is authored under; ``ambiguous_assignment=True``
    marks the one deliberately-confusable item per project (Section
    13.2: "one ambiguous assignment case") without changing where it
    actually lives. ``material=False`` marks deliberately irrelevant
    evidence (Section 13.2) that must yield no ground-truth item at all.
    """

    model_config = ConfigDict(frozen=True)

    artifact_id: str
    kind: ArtifactKind
    title: str
    occurred_at: str
    author: str | None = None
    filename: str
    raw_bytes: bytes = Field(repr=False)
    project_key: str
    ambiguous_assignment: bool = False
    material: bool = True

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_comparable(cls, value: str) -> str:
        return _require_comparable_timestamp(value, "occurred_at")

    @model_validator(mode="after")
    def _raw_bytes_not_blank(self) -> CorpusArtifact:
        if not self.raw_bytes.strip():
            raise ValueError(f"artifact {self.artifact_id!r} has blank raw_bytes")
        return self


class TransitionType(StrEnum):
    """The atomic change a ``GroundTruthTransition`` represents. Mirrors
    ``project_context.domain.ledger.LedgerTransitionType`` in spirit but
    is intentionally its own vocabulary: ground truth describes *what
    happened in the world*, not which internal ledger transition should
    fire (that mapping is `project_context.evaluation.
    reviewer_protocol`'s job, and re-deriving it independently there is
    the point — a bug in that mapping should make scoring disagree with
    the ledger, not agree by construction)."""

    CREATE = "create"
    UPDATE_OWNER = "update_owner"
    UPDATE_DATE = "update_date"
    UPDATE_STATUS = "update_status"
    SUPERSEDE = "supersede"


class Materiality(StrEnum):
    """Section 13.3: "Ambiguous items are labeled and excluded from
    strict precision/recall or scored separately." ``TRAP`` marks a
    transition that exists specifically to test a failure mode (Section
    13.2's "required traps") — always scored as ``MATERIAL``, labeled
    separately purely for report readability."""

    MATERIAL = "material"
    TRAP = "trap"
    AMBIGUOUS = "ambiguous"


class EvidenceMention(BaseModel):
    """One artifact's evidence for a ``GroundTruthTransition``.
    ``statement`` must be an exact substring of the cited
    ``artifact_id``'s parsed text (``project_context.evaluation.
    corpus_text.artifact_text``) — validated by
    ``project_context.evaluation.materialize.validate_corpus_project``,
    not by this model alone (which has no access to the artifact text).

    ``observed_owner`` is the raw owner-name text *this evidence itself
    states*, used only to build the scripted extraction's
    ``ExtractedObservation.owner_name`` — almost always equal to the
    transition's own resolved ``owner``. The one deliberate exception is
    Section 13.2's participant-ambiguity trap: the text says "Jordan"
    (``observed_owner``) while the ground-truth resolved owner stays
    ``None`` (``GroundTruthTransition.owner``)."""

    model_config = ConfigDict(frozen=True)

    artifact_id: str
    statement: str
    observed_owner: str | None = None


class GroundTruthTransition(BaseModel):
    """One atomic, evidenced change to one ``GroundTruthItem`` (Section
    13.3's per-item requirement list).

    ``mentions`` is almost always exactly one ``EvidenceMention`` — a
    second entry deliberately models Section 13.2's "repeated action
    wording" trap: the *same* transition (no field change) restated in a
    second artifact, which a correct reviewer must treat as additional
    evidence for the existing item, never a duplicate. Only the first
    mention's artifact is used to compute the resulting item's
    ledger creation citation.

    ``owner``/``due_date``/``status`` are each "the resulting value after
    this transition," carried forward unchanged by
    ``project_context.evaluation.ground_truth_state.state_at_cutoff``
    when a later transition leaves them unset — e.g. a pure due-date
    change transition sets ``due_date`` and leaves ``owner``/``status``
    ``None`` (unchanged).
    """

    model_config = ConfigDict(frozen=True)

    transition_id: str
    type: TransitionType
    mentions: tuple[EvidenceMention, ...] = Field(min_length=1)
    owner: str | None = None
    due_date: str | None = None
    status: LedgerItemStatus | None = None
    #: SUPERSEDE only: the item_id of the predecessor item this
    #: transition's item supersedes. Always the *first* (creating)
    #: transition of the successor item.
    supersedes_item_id: str | None = None
    materiality: Materiality = Materiality.MATERIAL
    notes: str | None = None

    @model_validator(mode="after")
    def _supersede_shape(self) -> GroundTruthTransition:
        if self.type is TransitionType.SUPERSEDE and self.supersedes_item_id is None:
            raise ValueError(f"transition {self.transition_id!r} is SUPERSEDE with no predecessor")
        if self.type is not TransitionType.SUPERSEDE and self.supersedes_item_id is not None:
            raise ValueError(
                f"transition {self.transition_id!r} sets supersedes_item_id but is not SUPERSEDE"
            )
        if self.type in (TransitionType.CREATE, TransitionType.SUPERSEDE) and self.status is None:
            raise ValueError(f"transition {self.transition_id!r} must set a resulting status")
        return self


class GroundTruthItem(BaseModel):
    """One atomic ground-truth item and its full transition history, in
    chronological order (Section 13.3). ``observation_kind`` is the
    ``ObservationKind`` every transition's synthetic observation is
    authored under — always equal to this item's own ``kind`` for every
    transition (including the SUPERSEDE case: a risk-turned-blocker is
    two ``GroundTruthItem``s, and the successor's own ``kind`` is
    ``BLOCKER`` — see ``project_context.domain.reconciliation.
    NATURAL_LEDGER_KIND``, which this harness deliberately never
    special-cases beyond that one built-in mapping).
    """

    model_config = ConfigDict(frozen=True)

    item_id: str
    kind: LedgerItemKind
    canonical_title: str
    aliases: tuple[str, ...] = ()
    transitions: tuple[GroundTruthTransition, ...]

    @property
    def observation_kind(self) -> ObservationKind:
        return ObservationKind(self.kind.value)

    @model_validator(mode="after")
    def _first_transition_creates(self) -> GroundTruthItem:
        if not self.transitions:
            raise ValueError(f"item {self.item_id!r} has no transitions")
        first = self.transitions[0]
        if first.type not in (TransitionType.CREATE, TransitionType.SUPERSEDE):
            raise ValueError(
                f"item {self.item_id!r}'s first transition must be CREATE or SUPERSEDE, "
                f"got {first.type.value!r}"
            )
        ids = [t.transition_id for t in self.transitions]
        if len(ids) != len(set(ids)):
            raise ValueError(f"item {self.item_id!r} has duplicate transition_id values")
        return self


class BriefTypeLiteral(StrEnum):
    """Matches ``project_context.domain.briefs.BriefType`` values exactly
    — kept as the harness's own enum rather than importing the app one so
    this module's only application-layer dependency stays
    ``LedgerItemKind``/``LedgerItemStatus``/``EvidenceSourceType`` (the
    taxonomy itself), never a service/db import."""

    CURRENT_PROJECT = "current_project"
    MEETING_PREPARATION = "meeting_preparation"


class Checkpoint(BaseModel):
    """One evaluation point: a cutoff both systems generate a brief at
    (Section 13.3: "expected brief facts before each simulated meeting";
    Section 13.4: "same source text and cutoff time").

    ``cutoff_at`` follows ``project_context.retrieval.meeting_prep.
    compute_cutoff``'s own semantics: an artifact with
    ``occurred_at < cutoff_at`` is visible; one at or after it is not
    (Section 13.4: "at each cutoff... no persistent ledger or prior
    human corrections" applies equally to "no evidence from the
    future").

    ``required_phrases``/``forbidden_phrases`` are a small, hand-picked
    set of literal substrings a *rendered brief's Markdown* is expected
    to (or must not) contain — used only as a lightweight, human-
    readable sanity signal in the report; the real scoring is always the
    structured item/field comparison in ``project_context.evaluation.
    scoring``, never string matching alone.
    """

    model_config = ConfigDict(frozen=True)

    checkpoint_id: str
    cutoff_at: str
    brief_type: BriefTypeLiteral
    meeting_title: str | None = None
    meeting_purpose: str | None = None
    meeting_scheduled_at: str | None = None
    participant_lines: tuple[str, ...] = ()
    required_phrases: tuple[str, ...] = ()
    forbidden_phrases: tuple[str, ...] = ()

    @field_validator("cutoff_at")
    @classmethod
    def _cutoff_at_comparable(cls, value: str) -> str:
        return _require_comparable_timestamp(value, "cutoff_at")

    @field_validator("meeting_scheduled_at")
    @classmethod
    def _meeting_scheduled_at_comparable(cls, value: str | None) -> str | None:
        return (
            _require_comparable_timestamp(value, "meeting_scheduled_at")
            if value is not None
            else value
        )

    @model_validator(mode="after")
    def _meeting_fields_required(self) -> Checkpoint:
        if self.brief_type is BriefTypeLiteral.MEETING_PREPARATION and (
            not self.meeting_title or not self.meeting_scheduled_at
        ):
            raise ValueError(
                f"checkpoint {self.checkpoint_id!r} is meeting_preparation but is "
                "missing meeting_title/meeting_scheduled_at"
            )
        return self


class BaselineScriptedClaim(BaseModel):
    """One hand-authored claim the **fake-mode baseline provider**
    returns for one checkpoint (deliberately independent of ground
    truth — see ``project_context.evaluation.baseline_runner``'s module
    docstring for why fake-mode baseline output is scripted-imperfect
    rather than scripted-perfect). Shape mirrors
    ``project_context.evaluation.baseline_schema.BaselineClaim`` exactly
    so the fake provider can be turned directly into one."""

    model_config = ConfigDict(frozen=True)

    section: str
    text: str
    claim_type: str = "fact"
    item_kind: LedgerItemKind | None = None
    item_title: str | None = None
    status: LedgerItemStatus | None = None
    owner: str | None = None
    due_date: str | None = None
    #: (artifact_id, exact quote) pairs — validated at score time against
    #: that artifact's real text, exactly like a live model's citations
    #: would be (Section 13.4: "same citation validation").
    evidence: tuple[tuple[str, str], ...] = ()


class FactRef(BaseModel):
    """One chunk's `(item_id, transition_id)` attribution — what
    ``project_context.evaluation.ledger_runner``'s scripted extraction
    walks in lock-step with real chunking to build each chunk's
    deterministic ``ExtractionBatch`` response. Produced by
    ``project_context.evaluation.corpus_data.builder.assemble`` from the
    same authored blocks ``GroundTruthTransition.mentions`` come from —
    never authored or edited independently of them."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    transition_id: str


class AmbiguousAliasSeed(BaseModel):
    """Two or more real, distinct people who deliberately share one short
    alias in evidence text (Section 13.2's "participant ambiguity" trap
    — e.g. "Jordan" spoken alone when both "Jordan Lee" and "Jordan
    Ruiz" are real project people). `project_context.evaluation.
    ledger_runner` pre-registers each of `full_names` as its own person
    with both its own full name and `shared_alias` as aliases, so
    `project_context.db.people_repository.resolve_person` genuinely
    returns an `ambiguous` outcome for `shared_alias` — the same
    mechanism a live project's contact list would hit, not a harness
    special case."""

    model_config = ConfigDict(frozen=True)

    shared_alias: str
    full_names: tuple[str, ...] = Field(min_length=2)


class CorpusProject(BaseModel):
    """One full synthetic benchmark project (Section 13.2): its
    artifacts (chronology lives in their own ``occurred_at`` ordering,
    stored once — not duplicated into a separate chronology structure),
    its ground-truth items, and its evaluation checkpoints.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    objective: str
    stage: str
    artifacts: tuple[CorpusArtifact, ...]
    items: tuple[GroundTruthItem, ...]
    checkpoints: tuple[Checkpoint, ...]
    #: artifact_id -> one entry per parsed block/chunk, in order — `None`
    #: for a deliberately irrelevant block. See `FactRef`.
    fact_plan: dict[str, tuple[FactRef | None, ...]] = Field(default_factory=dict)
    #: See `AmbiguousAliasSeed`. Empty for a project with no
    #: participant-ambiguity trap.
    ambiguous_aliases: tuple[AmbiguousAliasSeed, ...] = ()
    #: checkpoint_id -> the fake-mode baseline's scripted (deliberately
    #: imperfect) claims for that checkpoint. Every checkpoint must have
    #: an entry (possibly empty) so fake-mode runs never silently fall
    #: back to "no baseline output" for one.
    baseline_fake_claims: dict[str, tuple[BaselineScriptedClaim, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _referential_integrity(self) -> CorpusProject:
        artifact_ids = {a.artifact_id for a in self.artifacts}
        if len(artifact_ids) != len(self.artifacts):
            raise ValueError(f"project {self.key!r} has duplicate artifact_id values")
        item_ids = {i.item_id for i in self.items}
        if len(item_ids) != len(self.items):
            raise ValueError(f"project {self.key!r} has duplicate item_id values")

        for item in self.items:
            for transition in item.transitions:
                for mention in transition.mentions:
                    if mention.artifact_id not in artifact_ids:
                        raise ValueError(
                            f"item {item.item_id!r} transition {transition.transition_id!r} "
                            f"cites unknown artifact_id {mention.artifact_id!r}"
                        )
                if transition.supersedes_item_id is not None:
                    if transition.supersedes_item_id not in item_ids:
                        raise ValueError(
                            f"item {item.item_id!r} supersedes unknown item "
                            f"{transition.supersedes_item_id!r}"
                        )
                    if transition.supersedes_item_id == item.item_id:
                        raise ValueError(f"item {item.item_id!r} cannot supersede itself")

        checkpoint_ids = {c.checkpoint_id for c in self.checkpoints}
        if len(checkpoint_ids) != len(self.checkpoints):
            raise ValueError(f"project {self.key!r} has duplicate checkpoint_id values")
        for checkpoint_id in self.baseline_fake_claims:
            if checkpoint_id not in checkpoint_ids:
                raise ValueError(
                    f"baseline_fake_claims references unknown checkpoint_id {checkpoint_id!r}"
                )

        transition_ids_by_item = {
            item.item_id: {t.transition_id for t in item.transitions} for item in self.items
        }
        for artifact_id, refs in self.fact_plan.items():
            if artifact_id not in artifact_ids:
                raise ValueError(f"fact_plan references unknown artifact_id {artifact_id!r}")
            for ref in refs:
                if ref is None:
                    continue
                if ref.item_id not in transition_ids_by_item:
                    raise ValueError(f"fact_plan references unknown item_id {ref.item_id!r}")
                if ref.transition_id not in transition_ids_by_item[ref.item_id]:
                    raise ValueError(
                        f"fact_plan references unknown transition_id {ref.transition_id!r} "
                        f"on item {ref.item_id!r}"
                    )

        return self

    def artifacts_sorted(self) -> tuple[CorpusArtifact, ...]:
        return tuple(sorted(self.artifacts, key=lambda a: (a.occurred_at, a.artifact_id)))

    def artifact_by_id(self, artifact_id: str) -> CorpusArtifact:
        for artifact in self.artifacts:
            if artifact.artifact_id == artifact_id:
                return artifact
        raise KeyError(f"unknown artifact_id {artifact_id!r} in project {self.key!r}")

    def item_by_id(self, item_id: str) -> GroundTruthItem:
        for item in self.items:
            if item.item_id == item_id:
                return item
        raise KeyError(f"unknown item_id {item_id!r} in project {self.key!r}")


#: Section 13.2's size/composition requirements for one of the three
#: *frozen benchmark* corpora specifically — deliberately **not** a
#: `CorpusProject` model validator, so a small hand-built `CorpusProject`
#: (a unit-test fixture verifying one metric formula, say) remains
#: constructible without also satisfying full-benchmark-scale
#: requirements that have nothing to do with what it is testing.
#: `project_context.evaluation.materialize.validate_corpus_project` calls
#: this in addition to its own checks for the three real corpora.
def benchmark_requirement_violations(project: CorpusProject) -> tuple[str, ...]:
    violations: list[str] = []
    material_records = sum(
        1
        for item in project.items
        for transition in item.transitions
        if transition.materiality is not Materiality.AMBIGUOUS
    )
    if material_records < 25:
        violations.append(
            f"only {material_records} material ground-truth items/transitions; "
            "Section 13.2 requires at least 25"
        )
    if not (12 <= len(project.artifacts) <= 20):
        violations.append(f"{len(project.artifacts)} artifacts; Section 13.2 requires 12-20")
    if not any(a.ambiguous_assignment for a in project.artifacts):
        violations.append("no ambiguous-assignment artifact (Section 13.2)")
    if not any(not a.material for a in project.artifacts):
        violations.append("no deliberately irrelevant artifact (Section 13.2)")
    return tuple(violations)


def require_benchmark_corpus(project: CorpusProject) -> None:
    """Raise `ValueError` (naming every violation) unless `project`
    satisfies `benchmark_requirement_violations`."""
    violations = benchmark_requirement_violations(project)
    if violations:
        raise ValueError(
            f"project {project.key!r} is not a valid benchmark corpus: " + "; ".join(violations)
        )
