"""Tests for Meeting Preparation Brief generation
(`project_context.services.meeting_prep`; Section 5.9; FR-025/FR-026;
Prompt 14).

Uses a reactive fake `LLMProvider` (not the shared queue-based
`FakeLLMProvider` fixture) because `project_context.retrieval.
meeting_prep` mints fresh opaque `fact_id`s on every run — a test must
react to the *actual* fact_ids sent in one call's prompt, not guess
them in advance. Never touches the network (Section 15)."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import (
    brief_repository,
    evidence_link_repository,
    evidence_repository,
    sources_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.briefs import BriefStatus, BriefType, ClaimValidationStatus
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.evidence_links import EvidenceLinkSupportRole, EvidenceLinkTargetType
from project_context.domain.ledger import LedgerItemKind
from project_context.domain.people import AliasType
from project_context.domain.projects import ProjectCreateInput
from project_context.llm.provider import LLMTimeoutError, StructuredResult, estimate_cost_usd
from project_context.llm.schemas import BriefClaimOutput, BriefComposition, BriefSectionOutput
from project_context.services.ledger import create_ledger_item
from project_context.services.meeting_prep import generate_meeting_prep_brief
from project_context.services.projects import create_project

_counter = itertools.count()


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def project_id(conn):
    return create_project(
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot", stage="Build")
    ).id


def make_content_and_chunk(conn, project_id, *, text):
    n = next(_counter)
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = evidence_repository.insert_artifact(
        conn,
        project_id,
        source.id,
        external_id=f"t:{project_id}:{n}",
        artifact_type=ArtifactType.MANUAL_TEXT,
        title="Notes",
        author=None,
        occurred_at=None,
        external_url="https://example.com/n",
        source_type=None,
    )
    content = evidence_repository.insert_content(
        conn,
        project_id,
        artifact.id,
        sha256=hashlib.sha256(f"{n}:{text}".encode()).hexdigest(),
        raw_storage_path=None,
        mime_type="text/plain",
        byte_size=len(text),
        normalized_text=text,
        parser_name="text",
        parser_version="1",
        parse_status=ParseStatus.PARSED,
        location_map=None,
        original_filename=None,
    )
    spec = ChunkSpec(
        ordinal=0,
        text=text,
        char_start=0,
        char_end=len(text),
        section_path=None,
        sha256=hashlib.sha256(f"{n}:{text}:c".encode()).hexdigest(),
        token_estimate=len(text) // 4,
    )
    (chunk,) = evidence_repository.insert_chunks(conn, project_id, content.id, [spec])
    return content, chunk


def make_evidenced_item(conn, project_id, *, kind, title, owner_person_id=None, due_date=None):
    """Attaches evidence to both the item *and* its current version —
    mirroring what `project_context.services.review`'s real accept
    transaction always does (it links both `LEDGER_ITEM` and
    `LEDGER_VERSION` for every accepted transition). Only the item-level
    link would leave a `changes_since_previous` transition fact
    evidence-less (`retrieval.brief_facts.transition_fact` reads
    version-scoped evidence specifically), which is a real, correctly
    enforced validation gap this fixture must not paper over."""
    item, v1 = create_ledger_item(
        conn,
        project_id,
        kind=kind,
        canonical_title=title,
        owner_person_id=owner_person_id,
        due_date=due_date,
    )
    content, chunk = make_content_and_chunk(conn, project_id, text=f"{title} evidence.")
    for target_type, target_id in (
        (EvidenceLinkTargetType.LEDGER_ITEM, item.id),
        (EvidenceLinkTargetType.LEDGER_VERSION, v1.id),
    ):
        evidence_link_repository.insert_link(
            conn,
            project_id,
            target_type=target_type,
            target_id=target_id,
            content_id=content.id,
            chunk_id=chunk.id,
            char_start=0,
            char_end=len(chunk.text),
            quote=chunk.text,
            support_role=EvidenceLinkSupportRole.SUPPORTS,
        )
    return item


@dataclass
class ScriptedProvider:
    handler: Callable[[str], BriefComposition]
    calls: list[str] = field(default_factory=list)

    def generate_structured(self, *, task, system, input_text, response_model, config):
        self.calls.append(input_text)
        composition = self.handler(input_text)
        return StructuredResult(
            parsed=composition,
            provider="fake",
            model=config.model,
            request_id=None,
            input_tokens=80,
            output_tokens=30,
            latency_ms=5,
            estimated_cost_usd=estimate_cost_usd(config.model, 80, 30),
        )


@dataclass
class RaisingProvider:
    exc: Exception
    calls: int = 0

    def generate_structured(self, **kwargs):
        self.calls += 1
        raise self.exc


@dataclass
class NeverCalledProvider:
    def generate_structured(self, **kwargs):
        raise AssertionError("provider should not have been called")


def _facts_in_section(input_text: str, section_key: str) -> list[dict]:
    match = re.search(
        rf'<section key="{section_key}"[^>]*>\n(.*?)\n</section>', input_text, re.DOTALL
    )
    assert match is not None, f"section {section_key!r} not found in prompt input"
    return json.loads(match.group(1))


def _fact_id(input_text: str, section_key: str, index: int = 0) -> str:
    return _facts_in_section(input_text, section_key)[index]["fact_id"]


def _claims_by_section(result) -> dict[str, list]:
    by_section: dict[str, list] = {}
    for claim in result.claims:
        by_section.setdefault(claim.section, []).append(claim)
    return by_section


def _sections_sent(input_text: str) -> set[str]:
    return set(re.findall(r'<section key="([^"]+)"', input_text))


def _markdown_section(markdown: str, heading: str) -> str:
    """The text of one `## <heading>` block, up to the next `## ` or end
    of document — lets a test assert about *that* section specifically.
    A ledger item legitimately appears as an independent fact in more
    than one section (e.g. its current state in `outstanding_commitments`
    *and* its own create-transition in `changes_since_previous`), so a
    bare whole-document substring search can find the wrong occurrence."""
    start = markdown.index(f"## {heading}")
    rest = markdown[start:]
    next_heading = rest.find("\n## ", 1)
    return rest if next_heading == -1 else rest[:next_heading]


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_valid_claim_is_rendered_with_citation_links(conn, project_id):
    make_evidenced_item(
        conn, project_id, kind=LedgerItemKind.RISK, title="Vendor delay", due_date="2026-09-01"
    )

    def handler(input_text):
        fact_id = _fact_id(input_text, "risks_and_blockers")
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="risks_and_blockers",
                    claims=[
                        BriefClaimOutput(
                            text="Vendor delay is an open risk.",
                            claim_type="fact",
                            fact_ids=[fact_id],
                        )
                    ],
                )
            ]
        )

    provider = ScriptedProvider(handler=handler)
    result = generate_meeting_prep_brief(
        conn, project_id, manual_title="Weekly sync", provider=provider
    )

    assert result.brief.status is BriefStatus.VALID
    assert result.brief.brief_type is BriefType.MEETING_PREPARATION
    risk_claims = _claims_by_section(result)["risks_and_blockers"]
    assert any(c.validation_status is ClaimValidationStatus.VALID for c in risk_claims)
    assert "Vendor delay is an open risk." in result.brief.markdown
    assert "[1](evidence?artifact_id=" in result.brief.markdown
    assert "[source](https://example.com/n)" in result.brief.markdown


def test_changes_since_previous_renders_a_real_transition_with_citation(conn, project_id):
    """An accepted create-transition, with its own version-level
    evidence (not just the item's), is a genuine — not placeholder —
    Changes Since Previous Meeting claim."""
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.RISK, title="Vendor delay")

    def handler(input_text):
        fact_id = _fact_id(input_text, "changes_since_previous")
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="changes_since_previous",
                    claims=[
                        BriefClaimOutput(
                            text="A new vendor delay risk was raised.", claim_type="fact",
                            fact_ids=[fact_id],
                        )
                    ],
                )
            ]
        )

    result = generate_meeting_prep_brief(
        conn, project_id, manual_title="Sync", provider=ScriptedProvider(handler=handler)
    )
    section = _markdown_section(result.brief.markdown, "Changes Since Previous Meeting")
    assert "A new vendor delay risk was raised." in section
    assert "No accepted changes" not in section
    assert "evidence?artifact_id=" in section


def test_inference_claim_renders_with_inference_label(conn, project_id):
    make_evidenced_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, title="Send the report",
        due_date="2026-09-01",
    )
    make_evidenced_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, title="Review the report",
        due_date="2026-08-30",
    )

    def handler(input_text):
        fact_ids = [f["fact_id"] for f in _facts_in_section(input_text, "outstanding_commitments")]
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="outstanding_commitments",
                    claims=[
                        BriefClaimOutput(
                            text="The review is due before the report that depends on it.",
                            claim_type="inference", fact_ids=fact_ids,
                        )
                    ],
                )
            ]
        )

    result = generate_meeting_prep_brief(
        conn, project_id, manual_title="Sync", provider=ScriptedProvider(handler=handler)
    )
    assert "**Inference:**" in result.brief.markdown
    claims = _claims_by_section(result)["outstanding_commitments"]
    assert any(c.validation_status is ClaimValidationStatus.VALID for c in claims)


def test_generation_supersedes_the_previous_valid_meeting_prep_brief(conn, project_id):
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.RISK, title="Vendor delay")
    provider = ScriptedProvider(handler=lambda _: BriefComposition(sections=[]))

    first = generate_meeting_prep_brief(conn, project_id, manual_title="Sync 1", provider=provider)
    second = generate_meeting_prep_brief(conn, project_id, manual_title="Sync 2", provider=provider)

    refetched_first = brief_repository.get_brief(conn, project_id, first.brief.id)
    assert refetched_first.status is BriefStatus.SUPERSEDED
    assert second.brief.status is BriefStatus.VALID


def test_meeting_purpose_is_always_deterministic_never_sent_to_model(conn, project_id):
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.RISK, title="Vendor delay")
    captured_inputs = []

    def handler(input_text):
        captured_inputs.append(input_text)
        return BriefComposition(sections=[])

    provider = ScriptedProvider(handler=handler)
    result = generate_meeting_prep_brief(
        conn,
        project_id,
        manual_title="Weekly sync",
        manual_purpose="Review the rollout.",
        provider=provider,
    )

    assert "meeting_purpose" not in _sections_sent(captured_inputs[0])
    assert "Purpose: Review the rollout." in result.brief.markdown


# ---------------------------------------------------------------------------
# Empty sections.
# ---------------------------------------------------------------------------


def test_fully_empty_project_never_calls_the_model(conn, project_id):
    provider = NeverCalledProvider()
    result = generate_meeting_prep_brief(
        conn, project_id, manual_title="Weekly sync", provider=provider
    )

    assert result.brief.status is BriefStatus.VALID
    assert "No accepted changes since the previous meeting/cutoff." in result.brief.markdown
    assert "No outstanding commitments." in result.brief.markdown
    assert "No decisions required." in result.brief.markdown
    assert "No risks or blockers requiring discussion." in result.brief.markdown
    assert "No unanswered questions." in result.brief.markdown
    assert "No suggested discussion topics." in result.brief.markdown


def test_suggested_topics_is_sent_when_other_sections_have_facts(conn, project_id):
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.RISK, title="Vendor delay")
    captured_inputs = []

    def handler(input_text):
        captured_inputs.append(input_text)
        return BriefComposition(sections=[])

    generate_meeting_prep_brief(
        conn, project_id, manual_title="Sync", provider=ScriptedProvider(handler=handler)
    )
    assert "suggested_topics" in _sections_sent(captured_inputs[0])
    section_facts = _facts_in_section(captured_inputs[0], "suggested_topics")
    assert section_facts == []


def test_suggestion_claim_needs_no_fact_ids(conn, project_id):
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.RISK, title="Vendor delay")

    def handler(input_text):
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="suggested_topics",
                    claims=[
                        BriefClaimOutput(
                            text="Discuss the vendor delay risk.",
                            claim_type="suggestion",
                            fact_ids=[],
                        )
                    ],
                )
            ]
        )

    result = generate_meeting_prep_brief(
        conn, project_id, manual_title="Sync", provider=ScriptedProvider(handler=handler)
    )
    assert "**Suggestion:** Discuss the vendor delay risk." in result.brief.markdown


# ---------------------------------------------------------------------------
# Outstanding commitments grouped by owner.
# ---------------------------------------------------------------------------


def test_outstanding_commitments_grouped_by_owner_and_unassigned(conn, project_id):
    from project_context.db import people_repository

    priya = people_repository.create_person(conn, display_name="Priya Shah")
    make_evidenced_item(
        conn,
        project_id,
        kind=LedgerItemKind.COMMITMENT,
        title="Send the report",
        owner_person_id=priya.id,
    )
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.COMMITMENT, title="Unassigned task")

    def handler(input_text):
        facts = _facts_in_section(input_text, "outstanding_commitments")
        claims = [
            BriefClaimOutput(text=f["title"], claim_type="fact", fact_ids=[f["fact_id"]])
            for f in facts
        ]
        return BriefComposition(
            sections=[BriefSectionOutput(section="outstanding_commitments", claims=claims)]
        )

    result = generate_meeting_prep_brief(
        conn, project_id, manual_title="Sync", provider=ScriptedProvider(handler=handler)
    )
    # Scoped to the Outstanding Commitments section specifically — the
    # same two commitments also legitimately appear as their own
    # create-transition facts under Changes Since Previous Meeting
    # (module-level `_markdown_section` docstring).
    section = _markdown_section(result.brief.markdown, "Outstanding Commitments")
    priya_index = section.index("**Priya Shah**")
    unassigned_index = section.index("**Unassigned**")
    send_report_index = section.index("Send the report")
    unassigned_task_index = section.index("Unassigned task")
    assert priya_index < send_report_index < unassigned_index < unassigned_task_index


def test_outstanding_commitments_empty_placeholder_is_not_grouped(conn, project_id):
    result = generate_meeting_prep_brief(
        conn,
        project_id,
        manual_title="Sync",
        provider=ScriptedProvider(handler=lambda _: BriefComposition(sections=[])),
    )
    assert "No outstanding commitments." in result.brief.markdown
    assert "**Unassigned**" not in result.brief.markdown


# ---------------------------------------------------------------------------
# Claim validation.
# ---------------------------------------------------------------------------


def test_unresolvable_fact_id_is_omitted_and_deterministic_fallback_is_used(conn, project_id):
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.RISK, title="Vendor delay")

    def handler(input_text):
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="risks_and_blockers",
                    claims=[
                        BriefClaimOutput(
                            text="A made-up claim.", claim_type="fact", fact_ids=["totally-fake-id"]
                        )
                    ],
                )
            ]
        )

    result = generate_meeting_prep_brief(
        conn, project_id, manual_title="Sync", provider=ScriptedProvider(handler=handler)
    )
    claims = _claims_by_section(result)["risks_and_blockers"]
    invalid = [c for c in claims if c.validation_status is ClaimValidationStatus.INVALID_REFERENCE]
    valid = [c for c in claims if c.validation_status is ClaimValidationStatus.VALID]
    assert len(invalid) == 1
    assert "A made-up claim." not in result.brief.markdown
    assert len(valid) == 1
    assert "Vendor delay" in valid[0].claim_text
    assert "Vendor delay" in result.brief.markdown


def test_fact_id_from_a_different_section_is_rejected(conn, project_id):
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.RISK, title="Vendor delay")
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.COMMITMENT, title="Send the report")

    def handler(input_text):
        risk_fact_id = _fact_id(input_text, "risks_and_blockers")
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="outstanding_commitments",
                    claims=[
                        BriefClaimOutput(
                            text="Cross-section citation.",
                            claim_type="fact",
                            fact_ids=[risk_fact_id],
                        )
                    ],
                ),
                BriefSectionOutput(section="risks_and_blockers", claims=[]),
            ]
        )

    result = generate_meeting_prep_brief(
        conn, project_id, manual_title="Sync", provider=ScriptedProvider(handler=handler)
    )
    claims = _claims_by_section(result)["outstanding_commitments"]
    cross_section = [c for c in claims if c.claim_text == "Cross-section citation."]
    assert cross_section[0].validation_status is ClaimValidationStatus.INVALID_REFERENCE
    assert "Cross-section citation." not in result.brief.markdown


def test_fact_with_no_evidence_link_is_unsupported_and_omitted(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.RISK, canonical_title="No-evidence risk"
    )

    def handler(input_text):
        fact_id = _fact_id(input_text, "risks_and_blockers")
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="risks_and_blockers",
                    claims=[
                        BriefClaimOutput(
                            text="Claims about it.", claim_type="fact", fact_ids=[fact_id]
                        )
                    ],
                )
            ]
        )

    result = generate_meeting_prep_brief(
        conn, project_id, manual_title="Sync", provider=ScriptedProvider(handler=handler)
    )
    claims = _claims_by_section(result)["risks_and_blockers"]
    assert all(c.validation_status is ClaimValidationStatus.UNSUPPORTED for c in claims)
    assert "No-evidence risk" not in result.brief.markdown
    del item


# ---------------------------------------------------------------------------
# Include/exclude before generation.
# ---------------------------------------------------------------------------


def test_excluded_fact_id_never_reaches_the_model_or_the_brief(conn, project_id):
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.RISK, title="Included risk")
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.RISK, title="Excluded risk")

    from project_context.retrieval.meeting_prep import build_meeting_prep_facts

    # The preview step's own facts object is what must flow into
    # generation — `excluded_fact_ids` are opaque IDs minted fresh on
    # every `build_meeting_prep_facts` call, so a *second* independent
    # call (as generate_meeting_prep_brief would do without `facts=`)
    # would mint different IDs and never actually match.
    preview = build_meeting_prep_facts(conn, project_id, manual_title="Sync")
    section = next(s for s in preview.sections if s.section == "risks_and_blockers")
    excluded_id = next(f.fact_id for f in section.facts if f.title == "Excluded risk")

    captured_inputs = []

    def handler(input_text):
        captured_inputs.append(input_text)
        return BriefComposition(sections=[])

    result = generate_meeting_prep_brief(
        conn,
        project_id,
        facts=preview,
        excluded_fact_ids=frozenset({excluded_id}),
        provider=ScriptedProvider(handler=handler),
    )
    facts_sent = _facts_in_section(captured_inputs[0], "risks_and_blockers")
    titles_sent = {f["title"] for f in facts_sent}
    assert titles_sent == {"Included risk"}
    # Scoped to Risks Requiring Discussion specifically: exclusion is
    # per-fact, not per-ledger-item — the same item's own create-
    # transition can still legitimately appear, un-excluded, under
    # Changes Since Previous Meeting (`_markdown_section` docstring).
    risks_section = _markdown_section(result.brief.markdown, "Risks Requiring Discussion")
    assert "Excluded risk" not in risks_section
    assert "Included risk" in risks_section
    assert "Included risk" in captured_inputs[0]
    snapshot_titles = {
        f["title"]
        for s in result.brief.input_snapshot["sections"]
        if s["section"] == "risks_and_blockers"
        for f in s["facts"]
    }
    assert snapshot_titles == {"Included risk"}


# ---------------------------------------------------------------------------
# Provider failure.
# ---------------------------------------------------------------------------


def test_provider_failure_marks_brief_failed_and_persists_no_claims(conn, project_id):
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.RISK, title="Vendor delay")
    provider = RaisingProvider(exc=LLMTimeoutError("timed out"))

    result = generate_meeting_prep_brief(conn, project_id, manual_title="Sync", provider=provider)

    assert result.brief.status is BriefStatus.FAILED
    assert result.brief.safe_error is not None
    assert result.brief.markdown is None
    assert result.claims == ()
    assert brief_repository.list_claims_for_brief(conn, project_id, result.brief.id) == []


# ---------------------------------------------------------------------------
# Cross-project leakage.
# ---------------------------------------------------------------------------


def test_two_project_leakage_through_brief_generation(conn):
    project_a = create_project(
        conn, ProjectCreateInput(name="Project A", objective="Shared wording")
    )
    project_b = create_project(
        conn, ProjectCreateInput(name="Project B", objective="Shared wording")
    )
    make_evidenced_item(conn, project_a.id, kind=LedgerItemKind.RISK, title="Shared title risk")
    make_evidenced_item(conn, project_b.id, kind=LedgerItemKind.RISK, title="Shared title risk")

    def handler(input_text):
        fact_id = _fact_id(input_text, "risks_and_blockers")
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="risks_and_blockers",
                    claims=[
                        BriefClaimOutput(
                            text="Shared title risk.", claim_type="fact", fact_ids=[fact_id]
                        )
                    ],
                )
            ]
        )

    result_a = generate_meeting_prep_brief(
        conn, project_a.id, manual_title="Sync A", provider=ScriptedProvider(handler=handler)
    )
    result_b = generate_meeting_prep_brief(
        conn, project_b.id, manual_title="Sync B", provider=ScriptedProvider(handler=handler)
    )

    ids_a = {c.ledger_item_id for c in result_a.claims if c.ledger_item_id}
    ids_b = {c.ledger_item_id for c in result_b.claims if c.ledger_item_id}
    assert ids_a.isdisjoint(ids_b)

    from project_context.db import ledger_repository

    for claim in result_a.claims:
        if claim.ledger_item_id:
            assert ledger_repository.get_item(conn, project_a.id, claim.ledger_item_id) is not None
            assert ledger_repository.get_item(conn, project_b.id, claim.ledger_item_id) is None


# ---------------------------------------------------------------------------
# End-to-end synthetic meeting-prep workflow.
# ---------------------------------------------------------------------------


def test_end_to_end_synthetic_meeting_prep_workflow(conn, project_id):
    """A realistic small project: a completed decision, an open
    commitment owned by one person, an unassigned commitment, a risk,
    an owned open question (decision required), and an unowned open
    question (unanswered) — one full generation, checked section by
    section."""
    from project_context.db import people_repository

    priya = people_repository.create_person(
        conn, display_name="Priya Shah", primary_email="priya@acme.com"
    )
    people_repository.add_alias(
        conn, priya.id, alias_type=AliasType.EMAIL, alias_value="priya@acme.com"
    )

    make_evidenced_item(
        conn,
        project_id,
        kind=LedgerItemKind.COMMITMENT,
        title="Send the requirements doc",
        owner_person_id=priya.id,
        due_date="2026-09-01",
    )
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.COMMITMENT, title="Schedule kickoff")
    make_evidenced_item(conn, project_id, kind=LedgerItemKind.RISK, title="Vendor delay risk")
    make_evidenced_item(
        conn,
        project_id,
        kind=LedgerItemKind.OPEN_QUESTION,
        title="Which vendor should we use?",
        owner_person_id=priya.id,
    )
    make_evidenced_item(
        conn,
        project_id,
        kind=LedgerItemKind.OPEN_QUESTION,
        title="Is the venue booked?",
    )

    def handler(input_text):
        sections = []
        for key in (
            "changes_since_previous",
            "outstanding_commitments",
            "decisions_required",
            "risks_and_blockers",
            "unanswered_questions",
        ):
            facts = _facts_in_section(input_text, key)
            claims = [
                BriefClaimOutput(text=f["title"], claim_type="fact", fact_ids=[f["fact_id"]])
                for f in facts
            ]
            if claims:
                sections.append(BriefSectionOutput(section=key, claims=claims))
        if "suggested_topics" in _sections_sent(input_text):
            sections.append(
                BriefSectionOutput(
                    section="suggested_topics",
                    claims=[
                        BriefClaimOutput(
                            text="Discuss the vendor delay risk before choosing a vendor.",
                            claim_type="suggestion",
                            fact_ids=[],
                        )
                    ],
                )
            )
        return BriefComposition(sections=sections)

    result = generate_meeting_prep_brief(
        conn,
        project_id,
        manual_title="Acme Weekly Sync",
        manual_purpose="Review open items.",
        manual_scheduled_at="2026-08-25T15:00:00Z",
        participant_lines=("Priya Shah <priya@acme.com>", "Unknown Person"),
        provider=ScriptedProvider(handler=handler),
    )

    assert result.brief.status is BriefStatus.VALID
    md = result.brief.markdown
    assert "Meeting Preparation Brief — Acme Weekly Sync" in md
    assert "Purpose: Review open items." in md
    assert "Send the requirements doc" in md
    assert "Schedule kickoff" in md
    assert "**Priya Shah**" in md
    assert "**Unassigned**" in md
    assert "Which vendor should we use?" in md  # decisions_required
    assert "Is the venue booked?" in md  # unanswered_questions
    assert "Vendor delay risk" in md
    assert "**Suggestion:**" in md
    assert result.brief.meeting_artifact_id is None
    assert result.brief.brief_type is BriefType.MEETING_PREPARATION
