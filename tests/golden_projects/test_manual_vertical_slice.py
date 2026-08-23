"""End-to-end manual vertical slice (Prompt 9's "golden/manual
checkpoint"): create project -> ingest VTT/MD -> extract with a fake
provider -> reconcile -> review -> generate/validate a cited Current
Project Brief — run against all three golden mini-project fixtures
(tests/golden_projects/golden_fixtures.py), then proving idempotent reruns and
cross-project leakage-through-brief-generation.

Nothing here calls the network: extraction and brief composition both
use `FakeLLMProvider` (tests/fixtures/fake_llm_provider.py), queued with
responses built from the exact chunk text ingestion produced — never
invented text (Section 15: "Tests must use a fake/mock provider").
"""

from __future__ import annotations

from datetime import datetime

import pytest

from fixtures.fake_llm_provider import FakeLLMProvider
from golden_projects.golden_fixtures import (
    ALL_GOLDEN_PROJECTS,
    CHUNK_TARGET_CHARS,
    GoldenFact,
    GoldenProject,
)
from project_context.chunking import chunk_blocks
from project_context.db import (
    brief_repository,
    evidence_link_repository,
    evidence_repository,
    ledger_repository,
    observation_repository,
    people_repository,
    proposed_mutation_repository,
    sources_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.briefs import BriefStatus, BriefType, ClaimValidationStatus
from project_context.domain.evidence import EvidenceSourceType, ManualFileUploadInput
from project_context.domain.evidence_links import EvidenceLinkTargetType
from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus
from project_context.domain.people import AliasType
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.review import ProposedMutationAction
from project_context.llm.schemas import (
    BriefComposition,
    EvidenceSpan,
    ExtractedObservation,
    ExtractionBatch,
)
from project_context.parsers.txt_parser import parse_text
from project_context.parsers.vtt_parser import parse_vtt
from project_context.services import observations as observations_service
from project_context.services import reconciliation as reconciliation_service
from project_context.services import review as review_service
from project_context.services.briefs import generate_current_project_brief
from project_context.services.evidence import submit_file_upload
from project_context.services.extraction import ExtractionStatus, extract_content
from project_context.services.projects import create_project

_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def evidence_dir(tmp_path):
    directory = tmp_path / "evidence"
    directory.mkdir()
    return directory


# ---------------------------------------------------------------------------
# Fixture self-check: the exact chunking assumption every helper below
# relies on — one chunk per fact, never split, never merged.
# ---------------------------------------------------------------------------


def test_golden_fixtures_chunk_to_exactly_one_fact_per_chunk():
    for project in ALL_GOLDEN_PROJECTS:
        vtt_blocks = parse_vtt(project.kickoff_vtt).blocks
        vtt_chunks = chunk_blocks(vtt_blocks, target_chars=CHUNK_TARGET_CHARS, overlap_ratio=0.0)
        assert len(vtt_chunks) == len(vtt_blocks) == 7, project.key
        for markdown in (project.followup_md_1, project.followup_md_2):
            blocks = parse_text(markdown.encode("utf-8"), markdown=True).blocks
            chunks = chunk_blocks(blocks, target_chars=CHUNK_TARGET_CHARS, overlap_ratio=0.0)
            assert len(chunks) == len(blocks) == 2, project.key


# ---------------------------------------------------------------------------
# Pipeline helpers: real ingestion, a fake provider fed exact chunk text,
# real persistence, real reconciliation, real review.
# ---------------------------------------------------------------------------


def _observation_for(chunk, fact: GoldenFact) -> ExtractedObservation:
    """Build the observation a real extraction model *should* return for
    this chunk — quoting the exact span within the *real* chunk text
    (Prompt 9: "exact expected spans"), never a hand-typed offset."""
    start = chunk.text.index(fact.statement)
    end = start + len(fact.statement)
    span = EvidenceSpan(chunk_id=chunk.id, char_start=start, char_end=end, quote=fact.statement)
    return ExtractedObservation(
        kind=fact.kind,
        subject=fact.subject,
        statement=fact.statement,
        owner_name=fact.owner_name,
        date_value=fact.date_value,
        date_text=fact.date_text,
        explicitness="explicit",
        evidence=[span],
    )


def _responses_for(chunks, facts: tuple[GoldenFact, ...]) -> list[ExtractionBatch]:
    """One `ExtractionBatch` per chunk, in chunk order. `facts` may be
    shorter than `chunks` — the golden kickoff transcript's last chunk is
    always deliberately irrelevant chatter with no corresponding fact,
    exactly as a real model is expected to return an empty batch for it
    rather than inventing an observation."""
    responses = []
    for index, chunk in enumerate(chunks):
        if index < len(facts):
            observation = _observation_for(chunk, facts[index])
            responses.append(
                ExtractionBatch(
                    observations=[observation], source_contains_no_material_updates=False
                )
            )
        else:
            responses.append(
                ExtractionBatch(observations=[], source_contains_no_material_updates=True)
            )
    return responses


def _ingest_extract_persist(
    conn, project_id, evidence_dir, *, filename, source_type, occurred_at, data, facts
):
    upload = ManualFileUploadInput(
        title=filename, source_type=source_type, occurred_at=occurred_at,
        filename=filename, data=data,
    )
    ingest_result = submit_file_upload(
        conn, project_id, upload, evidence_dir=evidence_dir,
        max_upload_bytes=_MAX_UPLOAD_BYTES,
        chunk_target_chars=CHUNK_TARGET_CHARS, chunk_overlap_ratio=0.0,
    )
    responses = _responses_for(ingest_result.chunks, facts)
    provider = FakeLLMProvider(responses=list(responses))
    run_result = extract_content(conn, project_id, ingest_result.content.id, provider=provider)
    assert run_result.status is ExtractionStatus.COMPLETED, run_result.safe_error
    assert run_result.rejected == (), run_result.rejected
    assert len(run_result.accepted) == len(facts)

    for observation in run_result.accepted:
        observations_service.persist_observation(
            conn, project_id,
            content_id=ingest_result.content.id,
            chunk_id=observation.evidence[0].chunk_id,
            extracted=observation,
        )
    return ingest_result


def _reconcile_and_accept_all(conn, project_id):
    reconciliation_service.reconcile_pending_observations(conn, project_id)
    pending = proposed_mutation_repository.list_pending_for_project(conn, project_id)
    outcomes = []
    for proposal in pending:
        assert proposal.action is not ProposedMutationAction.CONFLICT, (
            proposal.action, proposal.candidate_features
        )
        outcomes.append(review_service.accept_proposal(conn, project_id, proposal.id))
    return outcomes


def _create_people_for(conn, project: GoldenProject) -> None:
    """Pre-create a `people` row (with a name alias) for every owner name
    this project's facts mention. Real reconciliation only recognizes an
    explicit reassignment ("ownership moves to ...") as a *material*
    owner change when the new name resolves to a known person
    (`_material_owner_conflict`) — exactly the real-world shape (a name
    reconciliation can't yet place is escalated, never silently
    trusted), so a realistic fixture must seed the same people a real
    project's contact list would already have, same as
    `tests/unit/test_reconciliation_service.py`'s `make_person`."""
    owner_names = {
        fact.owner_name
        for facts in (project.round1_facts, project.round2_facts, project.round3_facts)
        for fact in facts
        if fact.owner_name
    }
    for name in owner_names:
        person = people_repository.create_person(conn, display_name=name)
        people_repository.add_alias(conn, person.id, alias_type=AliasType.NAME, alias_value=name)


def _run_full_pipeline(conn, evidence_dir, project: GoldenProject) -> str:
    """Runs the entire manual vertical slice for one golden project —
    every fact accepted exactly as reconciliation classified it — and
    returns the resulting `project_id`."""
    _create_people_for(conn, project)
    created = create_project(
        conn,
        ProjectCreateInput(name=project.name, objective=project.objective, stage=project.stage),
    )
    project_id = created.id

    kickoff = _ingest_extract_persist(
        conn, project_id, evidence_dir,
        filename=f"{project.key}-kickoff.vtt", source_type=EvidenceSourceType.CALL_RECORDING,
        occurred_at=datetime(2026, 8, 1, 9, 0), data=project.kickoff_vtt,
        facts=project.round1_facts,
    )
    round1_outcomes = _reconcile_and_accept_all(conn, project_id)
    assert {o.proposal.action for o in round1_outcomes} == {ProposedMutationAction.CREATE}
    assert len(round1_outcomes) == len(project.round1_facts)

    _ingest_extract_persist(
        conn, project_id, evidence_dir,
        filename=f"{project.key}-standup-1.md", source_type=EvidenceSourceType.MEETING_NOTES,
        occurred_at=datetime(2026, 8, 12, 9, 0),
        data=project.followup_md_1.encode("utf-8"), facts=project.round2_facts,
    )
    round2_outcomes = _reconcile_and_accept_all(conn, project_id)
    assert {o.proposal.action for o in round2_outcomes} == {
        ProposedMutationAction.UPDATE, ProposedMutationAction.COMPLETE,
    }

    _ingest_extract_persist(
        conn, project_id, evidence_dir,
        filename=f"{project.key}-standup-2.md", source_type=EvidenceSourceType.MEETING_NOTES,
        occurred_at=datetime(2026, 8, 22, 9, 0),
        data=project.followup_md_2.encode("utf-8"), facts=project.round3_facts,
    )
    round3_outcomes = _reconcile_and_accept_all(conn, project_id)
    assert {o.proposal.action for o in round3_outcomes} == {
        ProposedMutationAction.UPDATE, ProposedMutationAction.SUPERSEDE,
    }

    assert kickoff.created_new_version is True
    return project_id


def _item_by_title(conn, project_id, kind: LedgerItemKind, title: str):
    matches = [
        item
        for item in ledger_repository.list_items_for_project(conn, project_id, kind=kind)
        if item.canonical_title == title
    ]
    assert len(matches) == 1, (kind, title, matches)
    return matches[0]


def _assert_ledger_state(conn, project_id, project: GoldenProject) -> None:
    decision = _item_by_title(conn, project_id, LedgerItemKind.DECISION, project.decision_title)
    assert decision.status is LedgerItemStatus.ACTIVE

    commitment1 = _item_by_title(
        conn, project_id, LedgerItemKind.COMMITMENT, project.commitment1_title
    )
    assert commitment1.status is LedgerItemStatus.OPEN
    c1_date_change_fact = next(f for f in project.round3_facts if f.date_value is not None)
    assert commitment1.due_date == c1_date_change_fact.date_value  # round-3's date wins

    commitment2 = _item_by_title(
        conn, project_id, LedgerItemKind.COMMITMENT, project.commitment2_title
    )
    assert commitment2.status is LedgerItemStatus.COMPLETED

    risk = _item_by_title(conn, project_id, LedgerItemKind.RISK, project.risk_title)
    assert risk.status is LedgerItemStatus.SUPERSEDED

    blocker = _item_by_title(conn, project_id, LedgerItemKind.BLOCKER, project.blocker_title)
    assert blocker.status is LedgerItemStatus.OPEN
    assert blocker.supersedes_item_id == risk.id
    assert risk.superseded_by_item_id == blocker.id

    milestone = _item_by_title(
        conn, project_id, LedgerItemKind.MILESTONE, project.milestone_title
    )
    assert milestone.status is LedgerItemStatus.OPEN

    question = _item_by_title(
        conn, project_id, LedgerItemKind.OPEN_QUESTION, project.question_title
    )
    assert question.status is LedgerItemStatus.OPEN

    # The irrelevant sentinel line must never have become project state.
    all_titles = " ".join(
        item.canonical_title + " " + (item.canonical_description or "")
        for item in ledger_repository.list_items_for_project(conn, project_id)
    )
    assert project.irrelevant_sentinel not in all_titles


def _assert_exact_evidence_span(conn, project_id, project: GoldenProject) -> None:
    """Prove one fact's evidence link — end to end from ingestion through
    the ledger — carries the exact span it was extracted from (Prompt 9:
    "exact expected spans")."""
    decision = _item_by_title(conn, project_id, LedgerItemKind.DECISION, project.decision_title)
    links = evidence_link_repository.list_for_target(
        conn, project_id, EvidenceLinkTargetType.LEDGER_ITEM, decision.id
    )
    assert len(links) == 1
    (link,) = links
    chunk = evidence_repository.get_chunk(conn, project_id, link.chunk_id)
    assert chunk is not None
    fact = project.round1_facts[0]
    assert fact.subject == project.decision_title
    expected_start = chunk.text.index(fact.statement)
    expected_end = expected_start + len(fact.statement)
    assert (link.char_start, link.char_end) == (expected_start, expected_end)
    assert link.quote == fact.statement


def _generate_and_validate_brief(conn, project_id, project: GoldenProject):
    provider = FakeLLMProvider(responses=[BriefComposition(sections=[])])
    result = generate_current_project_brief(conn, project_id, provider=provider)
    assert result.brief.status is BriefStatus.VALID
    assert len(provider.calls) == 1  # one call for every model-eligible, non-empty section

    for claim in result.claims:
        assert claim.validation_status is ClaimValidationStatus.VALID, claim

    markdown = result.brief.markdown
    assert markdown is not None
    assert project.decision_title in markdown
    assert project.commitment1_title in markdown
    assert project.milestone_title in markdown
    assert project.question_title in markdown
    assert project.blocker_title in markdown
    # The completed commitment must not appear as an open commitment.
    open_commitments_section = markdown.split("## Open Commitments")[1].split("## ")[0]
    assert project.commitment2_title not in open_commitments_section
    # The irrelevant sentinel must never reach the brief.
    assert project.irrelevant_sentinel not in markdown
    return result


# ---------------------------------------------------------------------------
# One test per golden project: the full slice, ledger state, exact
# spans, and a validated brief.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("project", ALL_GOLDEN_PROJECTS, ids=lambda p: p.key)
def test_manual_vertical_slice_end_to_end(conn, evidence_dir, project):
    project_id = _run_full_pipeline(conn, evidence_dir, project)
    _assert_ledger_state(conn, project_id, project)
    _assert_exact_evidence_span(conn, project_id, project)
    _generate_and_validate_brief(conn, project_id, project)


# ---------------------------------------------------------------------------
# Idempotency: rerunning ingestion/extraction/reconciliation must create
# no duplicate contents, observations, proposals, or ledger transitions.
# ---------------------------------------------------------------------------


def _counts(conn, project_id) -> dict[str, int]:
    def count(table: str) -> int:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?", (project_id,)
        ).fetchone()
        return row["n"]

    return {
        "source_contents": count("source_contents"),
        "observations": count("observations"),
        "proposed_mutations": count("proposed_mutations"),
        "ledger_versions": count("ledger_versions"),
    }


def test_rerun_ingestion_extraction_reconciliation_creates_no_duplicates(conn, evidence_dir):
    project = ALL_GOLDEN_PROJECTS[0]
    project_id = _run_full_pipeline(conn, evidence_dir, project)
    before = _counts(conn, project_id)
    assert before["source_contents"] == 3  # kickoff + 2 follow-ups
    assert before["observations"] == 10  # 6 + 2 + 2
    assert before["proposed_mutations"] == 10
    # 6 creates (round 1) + owner-change update + completion (round 2) +
    # date-change update + a supersession's *two* version rows — the old
    # item's closing version and the new successor item's version 1
    # (round 3).
    assert before["ledger_versions"] == 6 + 2 + 3

    # Rerun every ingestion with the exact same bytes and the exact same
    # (freshly queued, since FakeLLMProvider is consume-once) responses.
    for filename, source_type, occurred_at, data, facts in (
        (
            f"{project.key}-kickoff.vtt", EvidenceSourceType.CALL_RECORDING,
            datetime(2026, 8, 1, 9, 0), project.kickoff_vtt, project.round1_facts,
        ),
        (
            f"{project.key}-standup-1.md", EvidenceSourceType.MEETING_NOTES,
            datetime(2026, 8, 12, 9, 0),
            project.followup_md_1.encode("utf-8"), project.round2_facts,
        ),
        (
            f"{project.key}-standup-2.md", EvidenceSourceType.MEETING_NOTES,
            datetime(2026, 8, 22, 9, 0),
            project.followup_md_2.encode("utf-8"), project.round3_facts,
        ),
    ):
        ingest_result = _ingest_extract_persist(
            conn, project_id, evidence_dir,
            filename=filename, source_type=source_type, occurred_at=occurred_at,
            data=data, facts=facts,
        )
        assert ingest_result.created_new_version is False

    reconciliation_service.reconcile_pending_observations(conn, project_id)
    # Nothing is left pending review a second time.
    assert proposed_mutation_repository.list_pending_for_project(conn, project_id) == []

    after = _counts(conn, project_id)
    assert after == before

    # Regenerating the brief supersedes the old one and cites the exact
    # same underlying ledger items — not more, not fewer, no duplicates.
    first = _generate_and_validate_brief(conn, project_id, project)
    second = _generate_and_validate_brief(conn, project_id, project)

    refetched_first = brief_repository.get_brief(conn, project_id, first.brief.id)
    assert refetched_first.status is BriefStatus.SUPERSEDED
    refetched_second = brief_repository.get_brief(conn, project_id, second.brief.id)
    assert refetched_second.status is BriefStatus.VALID

    briefs = brief_repository.list_briefs_for_project(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT
    )
    assert len(briefs) == 2  # regenerating never leaves two VALID briefs behind
    assert sum(1 for b in briefs if b.status is BriefStatus.VALID) == 1

    first_item_ids = {c.ledger_item_id for c in first.claims if c.ledger_item_id}
    second_item_ids = {c.ledger_item_id for c in second.claims if c.ledger_item_id}
    assert first_item_ids == second_item_ids


# ---------------------------------------------------------------------------
# Cross-project leakage through the full pipeline, including brief
# generation — three real projects, never sharing a row.
# ---------------------------------------------------------------------------


def test_two_project_leakage_is_impossible_through_the_full_pipeline(conn, evidence_dir):
    project_ids = {
        project.key: _run_full_pipeline(conn, evidence_dir, project)
        for project in ALL_GOLDEN_PROJECTS
    }
    assert len(set(project_ids.values())) == 3  # sanity: three distinct projects

    briefs = {
        key: _generate_and_validate_brief(conn, project_ids[key], project)
        for key, project in ((p.key, p) for p in ALL_GOLDEN_PROJECTS)
    }

    all_item_ids: dict[str, set[str]] = {}
    all_observation_ids: dict[str, set[str]] = {}
    all_claim_item_ids: dict[str, set[str]] = {}
    for key, project_id in project_ids.items():
        all_item_ids[key] = {
            item.id for item in ledger_repository.list_items_for_project(conn, project_id)
        }
        all_observation_ids[key] = {
            obs.id
            for obs in observation_repository.list_observations_for_project(conn, project_id)
        }
        all_claim_item_ids[key] = {
            c.ledger_item_id for c in briefs[key].claims if c.ledger_item_id
        }
        # The manual source itself belongs to exactly this project.
        manual_source = sources_repository.get_manual_source(conn, project_id)
        assert manual_source is not None
        assert manual_source.project_id == project_id

    keys = list(project_ids)
    for i, key_a in enumerate(keys):
        for key_b in keys[i + 1 :]:
            assert all_item_ids[key_a].isdisjoint(all_item_ids[key_b])
            assert all_observation_ids[key_a].isdisjoint(all_observation_ids[key_b])
            assert all_claim_item_ids[key_a].isdisjoint(all_claim_item_ids[key_b])
