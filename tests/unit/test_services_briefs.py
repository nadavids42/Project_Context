"""Tests for Current Project Brief generation
(`project_context.services.briefs`; Section 5.8; FR-024/FR-026; Prompt 9).

Uses a reactive fake `LLMProvider` (not the shared queue-based
`FakeLLMProvider` fixture) because `project_context.retrieval.briefs`
mints fresh opaque `fact_id`s on every run — a test must react to the
*actual* fact_ids sent in one call's prompt, not guess them in advance.
Never touches the network (Section 15).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import evidence_link_repository, evidence_repository, sources_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.briefs import BriefStatus, ClaimValidationStatus
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.evidence_links import EvidenceLinkSupportRole, EvidenceLinkTargetType
from project_context.domain.ledger import LedgerItemKind
from project_context.domain.projects import ProjectCreateInput
from project_context.llm.provider import (
    LLMTimeoutError,
    StructuredResult,
    estimate_cost_usd,
)
from project_context.llm.schemas import BriefClaimOutput, BriefComposition, BriefSectionOutput
from project_context.services.briefs import generate_current_project_brief
from project_context.services.ledger import create_ledger_item
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
        conn, project_id, source.id, external_id=f"t:{project_id}:{n}",
        artifact_type=ArtifactType.MANUAL_TEXT, title="Notes", author=None,
        occurred_at=None, external_url="https://example.com/n", source_type=None,
    )
    content = evidence_repository.insert_content(
        conn, project_id, artifact.id, sha256=hashlib.sha256(f"{n}:{text}".encode()).hexdigest(),
        raw_storage_path=None, mime_type="text/plain", byte_size=len(text), normalized_text=text,
        parser_name="text", parser_version="1", parse_status=ParseStatus.PARSED,
        location_map=None, original_filename=None,
    )
    spec = ChunkSpec(
        ordinal=0, text=text, char_start=0, char_end=len(text), section_path=None,
        sha256=hashlib.sha256(f"{n}:{text}:c".encode()).hexdigest(), token_estimate=len(text) // 4,
    )
    (chunk,) = evidence_repository.insert_chunks(conn, project_id, content.id, [spec])
    return content, chunk


def make_evidenced_commitment(conn, project_id, *, title="Send the report", due_date="2026-09-01"):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title=title, due_date=due_date,
    )
    content, chunk = make_content_and_chunk(conn, project_id, text=f"{title} evidence.")
    evidence_link_repository.insert_link(
        conn, project_id, target_type=EvidenceLinkTargetType.LEDGER_ITEM, target_id=item.id,
        content_id=content.id, chunk_id=chunk.id, char_start=0, char_end=len(chunk.text),
        quote=chunk.text, support_role=EvidenceLinkSupportRole.SUPPORTS,
    )
    return item


@dataclass
class ScriptedProvider:
    """Reacts to the real prompt input rather than a pre-queued response
    — see module docstring."""

    handler: Callable[[str], BriefComposition]
    calls: list[str] = field(default_factory=list)

    def generate_structured(self, *, task, system, input_text, response_model, config):
        self.calls.append(input_text)
        composition = self.handler(input_text)
        return StructuredResult(
            parsed=composition, provider="fake", model=config.model, request_id=None,
            input_tokens=80, output_tokens=30, latency_ms=5,
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


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_valid_claim_is_rendered_with_citation_links(conn, project_id):
    make_evidenced_commitment(conn, project_id)

    def handler(input_text):
        fact_id = _fact_id(input_text, "open_commitments")
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="open_commitments",
                    claims=[
                        BriefClaimOutput(
                            text="The report is open, due Sep 1.",
                            claim_type="fact",
                            fact_ids=[fact_id],
                        )
                    ],
                )
            ]
        )

    provider = ScriptedProvider(handler=handler)
    result = generate_current_project_brief(conn, project_id, provider=provider)

    assert result.brief.status is BriefStatus.VALID
    assert len(provider.calls) == 1
    open_claims = _claims_by_section(result)["open_commitments"]
    assert any(c.validation_status is ClaimValidationStatus.VALID for c in open_claims)
    assert "The report is open, due Sep 1." in result.brief.markdown
    assert "[1](evidence?artifact_id=" in result.brief.markdown
    assert "[source](https://example.com/n)" in result.brief.markdown


def test_generation_supersedes_the_previous_valid_brief(conn, project_id):
    make_evidenced_commitment(conn, project_id)
    provider = ScriptedProvider(handler=lambda _: BriefComposition(sections=[]))

    first = generate_current_project_brief(conn, project_id, provider=provider)
    second = generate_current_project_brief(conn, project_id, provider=provider)

    from project_context.db import brief_repository

    refetched_first = brief_repository.get_brief(conn, project_id, first.brief.id)
    assert refetched_first.status is BriefStatus.SUPERSEDED
    assert second.brief.status is BriefStatus.VALID


# ---------------------------------------------------------------------------
# Empty project: no model call at all.
# ---------------------------------------------------------------------------


def test_fully_empty_project_never_calls_the_model(conn, project_id):
    provider = NeverCalledProvider()
    result = generate_current_project_brief(conn, project_id, provider=provider)

    assert result.brief.status is BriefStatus.VALID
    assert "No accepted open commitments." in result.brief.markdown
    assert "No accepted active decisions." in result.brief.markdown
    assert "No accepted upcoming milestone." in result.brief.markdown


def test_objective_and_current_stage_are_always_deterministic_never_sent_to_model(
    conn, project_id
):
    make_evidenced_commitment(conn, project_id)
    captured_inputs = []

    def handler(input_text):
        captured_inputs.append(input_text)
        return BriefComposition(sections=[])

    provider = ScriptedProvider(handler=handler)
    result = generate_current_project_brief(conn, project_id, provider=provider)

    assert "objective_and_scope" not in captured_inputs[0]
    assert "current_stage" not in captured_inputs[0]
    assert "Objective: Ship the pilot." in result.brief.markdown
    assert "Current stage: Build." in result.brief.markdown


# ---------------------------------------------------------------------------
# Claim validation: invalid references and unsupported facts.
# ---------------------------------------------------------------------------


def test_unresolvable_fact_id_is_omitted_and_deterministic_fallback_is_used(conn, project_id):
    make_evidenced_commitment(conn, project_id, title="Send the report")

    def handler(input_text):
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="open_commitments",
                    claims=[
                        BriefClaimOutput(
                            text="A made-up claim.", claim_type="fact", fact_ids=["totally-fake-id"]
                        )
                    ],
                )
            ]
        )

    provider = ScriptedProvider(handler=handler)
    result = generate_current_project_brief(conn, project_id, provider=provider)

    claims = _claims_by_section(result)["open_commitments"]
    invalid = [c for c in claims if c.validation_status is ClaimValidationStatus.INVALID_REFERENCE]
    valid = [c for c in claims if c.validation_status is ClaimValidationStatus.VALID]
    assert len(invalid) == 1
    assert invalid[0].claim_text == "A made-up claim."
    assert "A made-up claim." not in result.brief.markdown
    # The safety-net deterministic claim covers the real fact instead.
    assert len(valid) == 1
    assert "Send the report" in valid[0].claim_text
    assert "Send the report" in result.brief.markdown


def test_fact_id_from_a_different_section_is_rejected(conn, project_id):
    make_evidenced_commitment(conn, project_id)
    content, chunk = make_content_and_chunk(conn, project_id, text="Vendor delay is a risk.")
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.RISK, canonical_title="Vendor delay risk"
    )
    evidence_link_repository.insert_link(
        conn, project_id, target_type=EvidenceLinkTargetType.LEDGER_ITEM, target_id=item.id,
        content_id=content.id, chunk_id=chunk.id, char_start=0, char_end=len(chunk.text),
        quote=chunk.text, support_role=EvidenceLinkSupportRole.SUPPORTS,
    )

    def handler(input_text):
        # Cite a real fact_id, but from risks_and_blockers while claiming
        # to be part of open_commitments.
        risk_fact_id = _fact_id(input_text, "risks_and_blockers")
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="open_commitments",
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

    provider = ScriptedProvider(handler=handler)
    result = generate_current_project_brief(conn, project_id, provider=provider)

    open_claims = _claims_by_section(result)["open_commitments"]
    cross_section = [c for c in open_claims if c.claim_text == "Cross-section citation."]
    assert cross_section[0].validation_status is ClaimValidationStatus.INVALID_REFERENCE
    assert "Cross-section citation." not in result.brief.markdown


def test_fact_with_no_evidence_link_is_unsupported_and_omitted(conn, project_id):
    # Created directly, bypassing the review service that would normally
    # require at least one evidence link.
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="No-evidence commitment"
    )

    def handler(input_text):
        fact_id = _fact_id(input_text, "open_commitments")
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="open_commitments",
                    claims=[
                        BriefClaimOutput(
                            text="Claims about it.", claim_type="fact", fact_ids=[fact_id]
                        )
                    ],
                )
            ]
        )

    provider = ScriptedProvider(handler=handler)
    result = generate_current_project_brief(conn, project_id, provider=provider)

    claims = _claims_by_section(result)["open_commitments"]
    assert all(c.validation_status is ClaimValidationStatus.UNSUPPORTED for c in claims)
    assert "Claims about it." not in result.brief.markdown
    assert "No-evidence commitment" not in result.brief.markdown
    del item


def test_unknown_section_heading_from_the_model_is_ignored(conn, project_id):
    make_evidenced_commitment(conn, project_id)

    def handler(input_text):
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="editorial_notes",
                    claims=[
                        BriefClaimOutput(text="Off-topic.", claim_type="suggestion", fact_ids=[])
                    ],
                )
            ]
        )

    provider = ScriptedProvider(handler=handler)
    result = generate_current_project_brief(conn, project_id, provider=provider)

    assert result.brief.status is BriefStatus.VALID
    assert "Off-topic." not in result.brief.markdown
    # open_commitments had a real fact and got no matching response ->
    # the deterministic fallback still describes it.
    assert "Send the report" in result.brief.markdown


# ---------------------------------------------------------------------------
# Inference / suggestion claim types.
# ---------------------------------------------------------------------------


def test_inference_claim_renders_with_inference_label(conn, project_id):
    make_evidenced_commitment(conn, project_id, title="Send the report", due_date="2026-09-01")
    make_evidenced_commitment(conn, project_id, title="Review the report", due_date="2026-08-30")

    def handler(input_text):
        fact_ids = [f["fact_id"] for f in _facts_in_section(input_text, "open_commitments")]
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="open_commitments",
                    claims=[
                        BriefClaimOutput(
                            text="The review is due before the report that depends on it.",
                            claim_type="inference",
                            fact_ids=fact_ids,
                        )
                    ],
                )
            ]
        )

    provider = ScriptedProvider(handler=handler)
    result = generate_current_project_brief(conn, project_id, provider=provider)

    assert "**Inference:**" in result.brief.markdown
    claims = _claims_by_section(result)["open_commitments"]
    assert any(c.validation_status is ClaimValidationStatus.VALID for c in claims)


def test_suggestion_claim_renders_with_suggestion_label_and_needs_no_facts(conn, project_id):
    make_evidenced_commitment(conn, project_id)

    def handler(input_text):
        fact_id = _fact_id(input_text, "open_commitments")
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="open_commitments",
                    claims=[
                        BriefClaimOutput(text="Fact claim.", claim_type="fact", fact_ids=[fact_id]),
                        BriefClaimOutput(
                            text="Consider a status check-in.", claim_type="suggestion", fact_ids=[]
                        ),
                    ],
                )
            ]
        )

    provider = ScriptedProvider(handler=handler)
    result = generate_current_project_brief(conn, project_id, provider=provider)

    assert "**Suggestion:** Consider a status check-in." in result.brief.markdown


# ---------------------------------------------------------------------------
# Provider failure.
# ---------------------------------------------------------------------------


def test_provider_failure_marks_brief_failed_and_persists_no_claims(conn, project_id):
    make_evidenced_commitment(conn, project_id)
    provider = RaisingProvider(exc=LLMTimeoutError("timed out"))

    result = generate_current_project_brief(conn, project_id, provider=provider)

    assert result.brief.status is BriefStatus.FAILED
    assert result.brief.safe_error is not None
    assert result.brief.markdown is None
    assert result.claims == ()

    from project_context.db import brief_repository

    assert brief_repository.list_claims_for_brief(conn, project_id, result.brief.id) == []


# ---------------------------------------------------------------------------
# Reproducibility.
# ---------------------------------------------------------------------------


def test_input_snapshot_is_stored_and_matches_the_facts_used(conn, project_id):
    make_evidenced_commitment(conn, project_id)
    provider = ScriptedProvider(handler=lambda _: BriefComposition(sections=[]))
    result = generate_current_project_brief(conn, project_id, provider=provider)

    snapshot = result.brief.input_snapshot
    assert snapshot["project_id"] == project_id
    section_keys = {s["section"] for s in snapshot["sections"]}
    assert "open_commitments" in section_keys
    open_section = next(s for s in snapshot["sections"] if s["section"] == "open_commitments")
    assert open_section["facts"][0]["title"] == "Send the report"


# ---------------------------------------------------------------------------
# Cross-project leakage through full generation.
# ---------------------------------------------------------------------------


def test_two_project_leakage_through_brief_generation(conn):
    project_a = create_project(
        conn, ProjectCreateInput(name="Project A", objective="Shared wording")
    )
    project_b = create_project(
        conn, ProjectCreateInput(name="Project B", objective="Shared wording")
    )
    make_evidenced_commitment(conn, project_a.id, title="Send the report")
    make_evidenced_commitment(conn, project_b.id, title="Send the report")

    def handler(input_text):
        fact_id = _fact_id(input_text, "open_commitments")
        return BriefComposition(
            sections=[
                BriefSectionOutput(
                    section="open_commitments",
                    claims=[
                        BriefClaimOutput(
                            text="Send the report.", claim_type="fact", fact_ids=[fact_id]
                        )
                    ],
                )
            ]
        )

    result_a = generate_current_project_brief(
        conn, project_a.id, provider=ScriptedProvider(handler=handler)
    )
    result_b = generate_current_project_brief(
        conn, project_b.id, provider=ScriptedProvider(handler=handler)
    )

    assert result_a.brief.project_id == project_a.id
    assert result_b.brief.project_id == project_b.id
    claims_a = _claims_by_section(result_a)["open_commitments"]
    claims_b = _claims_by_section(result_b)["open_commitments"]
    ids_a = {c.ledger_item_id for c in claims_a if c.ledger_item_id}
    ids_b = {c.ledger_item_id for c in claims_b if c.ledger_item_id}
    assert ids_a.isdisjoint(ids_b)

    # A claim cannot even be *built* against another project's fact: an
    # attempt to cite project B's fact_id inside project A's generation
    # would fail to resolve, since project A's fact payload never
    # contains it (this is exercised directly at the retrieval layer in
    # test_retrieval_briefs.py's leakage test; here we confirm the two
    # real generated briefs never reference each other's ledger items).
    for claim in result_a.claims:
        if claim.ledger_item_id:
            item = None
            from project_context.db import ledger_repository

            item = ledger_repository.get_item(conn, project_a.id, claim.ledger_item_id)
            assert item is not None
            assert ledger_repository.get_item(conn, project_b.id, claim.ledger_item_id) is None
