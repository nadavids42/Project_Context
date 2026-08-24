"""The ledger (Project Context) system under evaluation (Section 13.4:
"Incrementally ingest the same artifacts in timestamp order, review
proposals using a predefined reviewer protocol, and generate the brief
from accepted ledger state").

Runs entirely against real application code — real parsers, real
chunking, real `project_context.services.extraction`/`reconciliation`/
`review`/`briefs`/`meeting_prep` — nothing here re-implements any of
that. What this module owns is purely evaluation-harness plumbing:

- seeding people/aliases so scripted owner resolution and the
  participant-ambiguity trap work exactly like a live project's contact
  list would;
- building each chunk's scripted `ExtractionBatch` response in
  fake/deterministic mode (Section 13.4's "fake/deterministic mode for
  CI"), from `CorpusProject.fact_plan` — in live mode, chunks are simply
  handed to the real provider unscripted;
- matching each resulting observation back to the one ground-truth item
  it concerns (`_match_item`: exact via `fact_plan` in fake mode;
  best-effort kind+title matching in live mode, since a real model's
  wording will not exactly echo the corpus author's) and computing the
  one correct review action from `project_context.evaluation.
  ground_truth_state` (`_expected_outcome_for`) — never from what
  reconciliation itself guessed;
- applying `project_context.evaluation.reviewer_protocol`'s scripted
  review decision to every resulting proposal;
- generating both brief types at each `Checkpoint`'s cutoff, ingesting
  only the artifacts that have become visible since the previous one;
- normalizing every rendered claim into `project_context.evaluation.
  runner_types.ScoredClaim` for scoring.

**What counts as one "sync"** (Section 13.5's "review burden... minutes/
sync"): this harness runs one project's entire corpus as a sequence of
checkpoint-bounded batches — every artifact that becomes visible between
two consecutive checkpoints is ingested/reviewed together, immediately
before generating that checkpoint's brief. That batch **is** the "sync"
this harness measures per-checkpoint review counts against; a real
deployment's actual sync cadence (manual, per Section 8's ADR-007) may
batch differently, so review-burden minutes/sync should be read as
"minutes per this benchmark's checkpoint-bounded batch," stated plainly
in the generated report rather than left implicit.

**Live-model-mode item matching is a documented, best-effort
simplification.** In fake mode, every observation is traced back to its
exact ground-truth transition via `CorpusProject.fact_plan` — exact by
construction. A real model's extraction cannot be traced that precisely
(it will not echo the corpus author's exact wording), so live mode
matches an observation to a ground-truth item by `(observation kind,
normalized title/alias equality)` only; an observation that cannot be
matched this way is rejected outright rather than guessed at (Section
12.8: "Model output is a proposal, never a ledger write" — an unverified
live proposal this harness cannot confirm against ground truth is not
something a scripted reviewer can respect). This makes live-mode ledger
scores a **conservative floor**, not a ceiling: a real reviewer with
domain knowledge (not just an exact-title-match heuristic) would recover
some of what this harness's matcher rejects.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from project_context.db import (
    evidence_link_repository,
    evidence_repository,
    people_repository,
    proposed_mutation_repository,
)
from project_context.domain.briefs import BriefFact, ClaimValidationStatus
from project_context.domain.evidence import ManualFileUploadInput
from project_context.domain.people import AliasType
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.review import ProposedMutationStatus, ReviewAction
from project_context.evaluation import ground_truth_state as gts
from project_context.evaluation.reviewer_protocol import ExpectedOutcome, review_scripted_proposal
from project_context.evaluation.runner_types import CheckpointRunResult, EvidenceRef, ScoredClaim
from project_context.evaluation.schema import (
    ARTIFACT_KIND_MAPPING,
    BriefTypeLiteral,
    Checkpoint,
    CorpusProject,
    GroundTruthItem,
    TransitionType,
)
from project_context.llm.provider import (
    LLMProvider,
    ModelConfig,
    StructuredResult,
    estimate_cost_usd,
)
from project_context.llm.schemas import EvidenceSpan, ExtractedObservation, ExtractionBatch
from project_context.services import observations as observations_service
from project_context.services import reconciliation as reconciliation_service
from project_context.services.briefs import generate_current_project_brief
from project_context.services.evidence import submit_file_upload
from project_context.services.extraction import ExtractionStatus, extract_content
from project_context.services.meeting_prep import generate_meeting_prep_brief
from project_context.services.projects import create_project
from project_context.services.review import reject_proposal

DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class ScriptedExtractionMismatchError(RuntimeError):
    """Raised when fake-mode scripted extraction can't line up chunks
    with `CorpusProject.fact_plan` — always a corpus-authoring bug,
    caught earlier in normal use by
    `project_context.evaluation.materialize.validate_corpus_project`."""


class FakeScriptedProvider:
    """A minimal `LLMProvider` for fake/deterministic mode: returns
    exactly the queued `ExtractionBatch` for each chunk, in call order.
    Distinct from `tests.fixtures.fake_llm_provider.FakeLLMProvider`
    (this harness deliberately does not import test fixtures into
    application-adjacent code) but the same idea — never touches the
    network."""

    def __init__(self, responses: list[ExtractionBatch]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def generate_structured(
        self, *, task: str, system: str, input_text: str, response_model, config: ModelConfig
    ) -> StructuredResult:
        if not self._responses:
            raise ScriptedExtractionMismatchError(
                f"scripted provider called with no queued response (call #{self.calls + 1}, "
                f"task={task!r})"
            )
        self.calls += 1
        parsed = self._responses.pop(0)
        input_tokens = len(input_text) // 4
        return StructuredResult(
            parsed=parsed,
            provider="scripted",
            model=config.model,
            request_id=None,
            input_tokens=input_tokens,
            output_tokens=40,
            latency_ms=1,
            estimated_cost_usd=estimate_cost_usd(config.model, input_tokens, 40),
        )


class EmptyBriefProvider:
    """Fake-mode Stage C (brief composition) provider: always returns an
    empty `BriefComposition`, regardless of what it is asked to compose.
    `project_context.services.brief_shared`'s per-fact deterministic
    fallback claim text already renders a fully correct, fully
    deterministic brief from accepted ledger facts alone whenever the
    model returns nothing usable for a section — so fake mode never
    needs scripted prose, only scripted extraction."""

    def generate_structured(
        self, *, task: str, system: str, input_text: str, response_model, config: ModelConfig
    ) -> StructuredResult:
        from project_context.llm.schemas import BriefComposition

        input_tokens = len(input_text) // 4
        return StructuredResult(
            parsed=BriefComposition(sections=[]),
            provider="scripted",
            model=config.model,
            request_id=None,
            input_tokens=input_tokens,
            output_tokens=1,
            latency_ms=1,
            estimated_cost_usd=estimate_cost_usd(config.model, input_tokens, 1),
        )


@dataclass(frozen=True)
class LedgerRunConfig:
    conn: sqlite3.Connection
    evidence_dir: Path
    model: str
    reasoning_effort: str
    brief_provider: LLMProvider
    #: `None` -> fake/deterministic mode (scripted extraction from
    #: `CorpusProject.fact_plan`). Given -> live-model mode: every chunk
    #: is sent to this real provider unscripted (see module docstring's
    #: live-mode matching caveat).
    live_extraction_provider: LLMProvider | None = None
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES


def _parse_occurred_at(occurred_at: str) -> datetime:
    return datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).astimezone(UTC)


def _collect_owner_names(project: CorpusProject) -> set[str]:
    """Every distinct owner-name string ground truth mentions, excluding
    any name that is actually a deliberately-shared ambiguous alias
    (`project.ambiguous_aliases`) — that name is seeded separately, once
    per real person who shares it, never as a person of its own (module
    docstring's participant-ambiguity trap: "Jordan" alone must resolve
    ambiguously between two real people, not to a literal person named
    "Jordan")."""
    shared_aliases = {seed.shared_alias for seed in project.ambiguous_aliases}
    names: set[str] = set()
    for item in project.items:
        for transition in item.transitions:
            if transition.owner:
                names.add(transition.owner)
            for mention in transition.mentions:
                if mention.observed_owner:
                    names.add(mention.observed_owner)
    return names - shared_aliases


def seed_people(conn: sqlite3.Connection, project: CorpusProject) -> dict[str, str]:
    """Pre-create one `people` row (with a matching NAME alias) per
    distinct owner name this project's ground truth mentions — real
    reconciliation only recognizes a reassignment when the new name
    resolves to a known person (same rationale as `tests/golden_projects/
    golden_fixtures.py`'s own `_create_people_for`). Also seeds
    `project.ambiguous_aliases` (Section 13.2's participant-ambiguity
    trap): each full name gets its own person, sharing one alias value
    with every other name in the same seed so `people_repository.
    resolve_person` genuinely returns `ambiguous`, not a guess."""
    name_to_person_id: dict[str, str] = {}
    for name in sorted(_collect_owner_names(project)):
        person = people_repository.create_person(conn, display_name=name)
        people_repository.add_alias(conn, person.id, alias_type=AliasType.NAME, alias_value=name)
        name_to_person_id[name] = person.id
    for seed in project.ambiguous_aliases:
        for full_name in seed.full_names:
            if full_name in name_to_person_id:
                continue
            person = people_repository.create_person(conn, display_name=full_name)
            people_repository.add_alias(
                conn, person.id, alias_type=AliasType.NAME, alias_value=full_name
            )
            people_repository.add_alias(
                conn, person.id, alias_type=AliasType.NAME, alias_value=seed.shared_alias
            )
            name_to_person_id[full_name] = person.id
    return name_to_person_id


# ---------------------------------------------------------------------------
# Matching an observation back to its ground-truth item and the one correct
# review outcome (module docstring).
# ---------------------------------------------------------------------------


def _normalize_title(title: str) -> str:
    return " ".join(title.strip().lower().split())


def _match_item(
    project: CorpusProject, observation: ExtractedObservation
) -> GroundTruthItem | None:
    """Match an observation back to the one ground-truth item it
    concerns, by exact `(kind, normalized title/alias)`. In fake mode
    this is exact by construction (`_build_extracted_observation` always
    sets `subject=item.canonical_title`); in live mode it is a
    best-effort heuristic (module docstring's documented limitation) —
    the same function serves both, so fake mode also exercises the exact
    matching path live mode depends on."""
    normalized_subject = _normalize_title(observation.subject)
    candidates = [
        item
        for item in project.items
        if item.observation_kind == observation.kind
        and (
            _normalize_title(item.canonical_title) == normalized_subject
            or any(_normalize_title(alias) == normalized_subject for alias in item.aliases)
        )
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _expected_outcome_for(
    conn: sqlite3.Connection, project: CorpusProject, item: GroundTruthItem, *, at: str
) -> ExpectedOutcome | None:
    """The one correct review outcome for an observation about `item`,
    evidenced at timestamp `at` (an artifact's own `occurred_at`) —
    computed purely from `project_context.evaluation.ground_truth_state`,
    independent of which specific `GroundTruthTransition` a fake-mode
    caller may already know exactly. Returns `None` if nothing about this
    item actually changes at `at` relative to just before it (a pure
    repeated mention with no material content is instead flagged via
    `is_repeat_mention` — see callers).

    `owner_person_id` is resolved here via the *same*
    `people_repository.resolve_person` real reconciliation uses — never
    hardcoded — because `review_scripted_proposal`'s `treat_as_new`
    fallback (used whenever reconciliation's own classification isn't a
    clean `create`, e.g. it found a coincidental weak candidate and
    returned `conflict`) applies this value directly, bypassing
    reconciliation's own patch entirely. Passing `None` here would
    silently wipe a correct owner on every such fallback."""
    # `at` is a timestamp *string* one artifact occurred at; comparing
    # with `<` against other artifacts' occurred_at strings is exactly
    # `ground_truth_state`'s own "strictly before" contract, so passing
    # `at` unmodified as the cutoff would exclude this artifact's own
    # transition. A cutoff of "immediately after" is not expressible as
    # a clean ISO string bump, so this harness instead looks at the
    # transition list directly for the one whose earliest mention is at
    # exactly `at`.
    before = gts.state_at_cutoff(project, item, cutoff_at=at)
    matching = [
        t
        for t in item.transitions
        if any(m.artifact_id and _mention_occurs_at(project, m, at) for m in t.mentions)
    ]
    if not matching:
        return None
    transition = matching[-1]

    owner_person_id = None
    if transition.owner:
        resolution = people_repository.resolve_person(conn, name=transition.owner)
        if resolution.outcome == "resolved":
            owner_person_id = resolution.person_id

    return ExpectedOutcome(
        item_id=item.item_id,
        kind=item.kind,
        canonical_title=item.canonical_title,
        transition_type=transition.type,
        owner_person_id=owner_person_id,
        due_date=transition.due_date,
        status=transition.status,
        predecessor_item_id=transition.supersedes_item_id,
        is_repeat_mention=(
            transition.type is TransitionType.CREATE
            and before.exists
            and before.last_transition is not None
            and before.last_transition.transition_id == transition.transition_id
        ),
    )


def _mention_occurs_at(project: CorpusProject, mention, at: str) -> bool:
    return project.artifact_by_id(mention.artifact_id).occurred_at == at


# ---------------------------------------------------------------------------
# One artifact: ingest, extract, persist, reconcile, review.
# ---------------------------------------------------------------------------


def _build_extracted_observation(
    project: CorpusProject, artifact_id: str, ref, chunk_text: str, chunk_id: str
):
    item = project.item_by_id(ref.item_id)
    transition = next(t for t in item.transitions if t.transition_id == ref.transition_id)
    mention = next(m for m in transition.mentions if m.artifact_id == artifact_id)
    start = chunk_text.index(mention.statement)
    end = start + len(mention.statement)
    span = EvidenceSpan(chunk_id=chunk_id, char_start=start, char_end=end, quote=mention.statement)
    date_value = None
    if transition.due_date and transition.type in (
        TransitionType.CREATE,
        TransitionType.SUPERSEDE,
        TransitionType.UPDATE_DATE,
    ):
        from datetime import date

        date_value = date.fromisoformat(transition.due_date)
    return ExtractedObservation(
        kind=item.observation_kind,
        subject=item.canonical_title,
        statement=mention.statement,
        owner_name=mention.observed_owner,
        date_value=date_value,
        date_text=transition.due_date,
        explicitness="explicit",
        evidence=[span],
    )


def _ingest_and_extract_artifact(
    config: LedgerRunConfig,
    project: CorpusProject,
    project_id: str,
    artifact,
    chunk_target_chars: int,
    *,
    item_ledger_ids: dict[str, str],
    counts: dict[str, int],
) -> None:
    source_type, _suffix, _is_markdown = ARTIFACT_KIND_MAPPING[artifact.kind]
    upload = ManualFileUploadInput(
        title=artifact.title,
        source_type=source_type,
        occurred_at=_parse_occurred_at(artifact.occurred_at),
        author=artifact.author,
        filename=artifact.filename,
        data=artifact.raw_bytes,
    )
    ingest_result = submit_file_upload(
        config.conn,
        project_id,
        upload,
        evidence_dir=config.evidence_dir,
        max_upload_bytes=config.max_upload_bytes,
        chunk_target_chars=chunk_target_chars,
        chunk_overlap_ratio=0.0,
    )
    if not ingest_result.created_new_version:
        return  # idempotent resubmission — nothing new to extract/review

    plan = project.fact_plan.get(artifact.artifact_id, ())
    chunks = ingest_result.chunks

    if config.live_extraction_provider is not None:
        provider: LLMProvider = config.live_extraction_provider
    else:
        if len(chunks) != len(plan):
            raise ScriptedExtractionMismatchError(
                f"artifact {artifact.artifact_id!r}: {len(chunks)} real chunks vs "
                f"{len(plan)} fact_plan entries"
            )
        responses = []
        for chunk, ref in zip(chunks, plan, strict=True):
            if ref is None:
                responses.append(
                    ExtractionBatch(observations=[], source_contains_no_material_updates=True)
                )
            else:
                observation = _build_extracted_observation(
                    project, artifact.artifact_id, ref, chunk.text, chunk.id
                )
                responses.append(
                    ExtractionBatch(
                        observations=[observation], source_contains_no_material_updates=False
                    )
                )
        provider = FakeScriptedProvider(responses)

    run_result = extract_content(
        config.conn,
        project_id,
        ingest_result.content.id,
        provider=provider,
        model=config.model,
        reasoning_effort=config.reasoning_effort,
    )
    if run_result.status not in (ExtractionStatus.COMPLETED, ExtractionStatus.NO_MATERIAL_CONTENT):
        raise ScriptedExtractionMismatchError(
            f"artifact {artifact.artifact_id!r}: extraction status {run_result.status!r}: "
            f"{run_result.safe_error}"
        )

    persisted: list[str] = []
    for observation in run_result.accepted:
        obs_row, _links, _created = observations_service.persist_observation(
            config.conn,
            project_id,
            content_id=ingest_result.content.id,
            chunk_id=observation.evidence[0].chunk_id,
            extracted=observation,
        )
        persisted.append(obs_row.id)

    reconciliation_service.reconcile_pending_observations(config.conn, project_id)

    for observation_id, extracted in zip(persisted, run_result.accepted, strict=True):
        proposals = proposed_mutation_repository.list_for_observation(
            config.conn, project_id, observation_id
        )
        pending = [p for p in proposals if p.status is ProposedMutationStatus.PENDING]
        if not pending:
            continue
        proposal = pending[-1]

        item = _match_item(project, extracted)
        if item is None:
            reject_proposal(
                config.conn,
                project_id,
                proposal.id,
                reason_code="wrong_match",
                note="evaluation harness: no ground-truth item matched this observation",
            )
            counts["rejected"] += 1
            continue

        expected = _expected_outcome_for(config.conn, project, item, at=artifact.occurred_at)
        if expected is None:
            continue

        already_created = expected.item_id in item_ledger_ids
        is_repeat = expected.is_repeat_mention or (
            expected.transition_type is TransitionType.CREATE and already_created
        )
        expected = ExpectedOutcome(
            item_id=expected.item_id,
            kind=expected.kind,
            canonical_title=expected.canonical_title,
            transition_type=expected.transition_type,
            owner_person_id=expected.owner_person_id,
            due_date=expected.due_date,
            status=expected.status,
            predecessor_item_id=expected.predecessor_item_id,
            is_repeat_mention=is_repeat,
        )

        outcome = review_scripted_proposal(
            config.conn, project_id, proposal, expected=expected, item_ledger_ids=item_ledger_ids
        )
        if outcome.already_applied:
            continue
        if outcome.review.action is ReviewAction.EDIT_ACCEPT:
            counts["edited_accepted"] += 1
        else:
            counts["accepted_without_edit"] += 1


# ---------------------------------------------------------------------------
# Claim normalization for scoring.
# ---------------------------------------------------------------------------


def _evidence_ref_for_fact(
    conn: sqlite3.Connection, project_id: str, fact: BriefFact, real_to_corpus: dict[str, str]
) -> tuple[EvidenceRef, ...]:
    refs = []
    for link_id in fact.evidence_link_ids:
        link = evidence_link_repository.get_link(conn, project_id, link_id)
        if link is None:
            continue
        content = evidence_repository.get_content(conn, project_id, link.content_id)
        if content is None:
            continue
        corpus_artifact_id = real_to_corpus.get(content.artifact_id, content.artifact_id)
        refs.append(EvidenceRef(artifact_id=corpus_artifact_id, quote=link.quote))
    return tuple(refs)


def _claims_from_brief_result(
    conn: sqlite3.Connection, project_id: str, result, real_to_corpus: dict[str, str]
) -> tuple[ScoredClaim, ...]:
    fact_lookup = result.facts.fact_by_id()
    claims = []
    for claim in result.claims:
        if claim.validation_status is not ClaimValidationStatus.VALID:
            continue
        primary_fact = None
        for fact_id in claim.cited_fact_ids:
            candidate = fact_lookup.get(fact_id)
            if candidate is not None and candidate.ledger_item_id:
                primary_fact = candidate
                break
        claims.append(
            ScoredClaim(
                section=claim.section,
                text=claim.claim_text,
                claim_type=claim.claim_type.value,
                item_kind=primary_fact.kind if primary_fact else None,
                item_title=primary_fact.title if primary_fact else None,
                status=primary_fact.status if primary_fact else None,
                owner=primary_fact.owner_name if primary_fact else None,
                due_date=primary_fact.due_date if primary_fact else None,
                evidence=(
                    _evidence_ref_for_fact(conn, project_id, primary_fact, real_to_corpus)
                    if primary_fact
                    else ()
                ),
            )
        )
    return tuple(claims)


@dataclass(frozen=True)
class LedgerRunResult:
    project_key: str
    app_project_id: str
    checkpoints: tuple[CheckpointRunResult, ...]


def run_ledger_for_project(
    project: CorpusProject, chunk_target_chars: int, *, config: LedgerRunConfig
) -> LedgerRunResult:
    """Ingest `project`'s artifacts strictly in chronological order,
    generating a brief at each `Checkpoint`'s cutoff from the ledger
    state accepted so far (module docstring). `config.conn` must already
    have migrations applied; this function creates exactly one new
    `projects` row and never touches any other project's rows in the
    same connection (the cross-project-leakage test's whole premise)."""
    created = create_project(
        config.conn,
        ProjectCreateInput(name=project.name, objective=project.objective, stage=project.stage),
    )
    project_id = created.id

    seed_people(config.conn, project)

    artifacts_sorted = project.artifacts_sorted()
    checkpoints_sorted = sorted(project.checkpoints, key=lambda c: c.cutoff_at)

    real_to_corpus: dict[str, str] = {}
    item_ledger_ids: dict[str, str] = {}

    artifact_index = 0
    results: list[CheckpointRunResult] = []

    for checkpoint in checkpoints_sorted:
        counts = {"accepted_without_edit": 0, "edited_accepted": 0, "rejected": 0}
        while (
            artifact_index < len(artifacts_sorted)
            and artifacts_sorted[artifact_index].occurred_at < checkpoint.cutoff_at
        ):
            artifact = artifacts_sorted[artifact_index]
            before_ids = {a.id for a in evidence_repository.list_artifacts(config.conn, project_id)}
            _ingest_and_extract_artifact(
                config,
                project,
                project_id,
                artifact,
                chunk_target_chars,
                item_ledger_ids=item_ledger_ids,
                counts=counts,
            )
            after = evidence_repository.list_artifacts(config.conn, project_id)
            for row in after:
                if row.id not in before_ids:
                    real_to_corpus[row.id] = artifact.artifact_id
            artifact_index += 1

        results.append(
            _generate_checkpoint_brief(config, project_id, checkpoint, counts, real_to_corpus)
        )

    return LedgerRunResult(
        project_key=project.key, app_project_id=project_id, checkpoints=tuple(results)
    )


def _generate_checkpoint_brief(
    config: LedgerRunConfig,
    project_id: str,
    checkpoint: Checkpoint,
    counts: dict[str, int],
    real_to_corpus: dict[str, str],
) -> CheckpointRunResult:
    if checkpoint.brief_type is BriefTypeLiteral.CURRENT_PROJECT:
        result = generate_current_project_brief(
            config.conn,
            project_id,
            provider=config.brief_provider,
            model=config.model,
            reasoning_effort=config.reasoning_effort,
        )
    else:
        assert checkpoint.meeting_title is not None
        result = generate_meeting_prep_brief(
            config.conn,
            project_id,
            manual_title=checkpoint.meeting_title,
            manual_purpose=checkpoint.meeting_purpose,
            manual_scheduled_at=checkpoint.meeting_scheduled_at,
            participant_lines=checkpoint.participant_lines,
            cutoff_override=checkpoint.cutoff_at,
            provider=config.brief_provider,
            model=config.model,
            reasoning_effort=config.reasoning_effort,
        )
    claims = _claims_from_brief_result(config.conn, project_id, result, real_to_corpus)
    brief = result.brief
    return CheckpointRunResult(
        checkpoint_id=checkpoint.checkpoint_id,
        brief_type=checkpoint.brief_type.value,
        system="ledger",
        claims=claims,
        markdown=brief.markdown,
        input_tokens=brief.input_tokens or 0,
        output_tokens=brief.output_tokens or 0,
        estimated_cost_usd=brief.estimated_cost_usd or 0.0,
        latency_ms=brief.latency_ms or 0,
        accepted_without_edit=counts["accepted_without_edit"],
        edited_accepted=counts["edited_accepted"],
        rejected=counts["rejected"],
    )
