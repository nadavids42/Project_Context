"""Consolidated cross-project sentinel/leakage suite (Section 15,
"Privacy and cross-project leakage tests"; Prompt 16's explicit ask:
"an automated cross-project sentinel suite covering repositories, FTS,
reconciliation, evidence viewer authorization, brief fact building,
claim validation, exports, and model-request construction").

Method (Section 15): seed two projects with **identical names/terms**
and **distinct secret sentinel strings**, then exercise every listed
path and assert project B's sentinel never appears in anything
project A produces (and vice versa). Related, narrower isolation tests
already exist scattered across `tests/unit/test_*_repository.py`,
`test_evidence_service.py`, etc. — this module's job is to prove the
*combination* the product plan asks for in one place, using the exact
sentinel methodology Section 15 specifies, not to duplicate that
per-repository coverage.

`tests/evaluation/test_cross_project_sentinel.py` covers the same
methodology at the evaluation-harness level (three benchmark corpora,
brief markdown only); this module covers it at the real service layer,
across all eight required areas.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import (
    evidence_repository,
    ledger_repository,
    observation_repository,
    sources_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.projects import ProjectCreateInput
from project_context.llm.provider import StructuredResult, estimate_cost_usd
from project_context.llm.schemas import (
    BriefClaimOutput,
    BriefComposition,
    BriefSectionOutput,
    EvidenceSpan,
    ExtractedObservation,
    ObservationKind,
)
from project_context.retrieval.briefs import build_current_project_brief_facts
from project_context.services import evidence as evidence_service
from project_context.services.briefs import generate_current_project_brief
from project_context.services.observations import persist_observation
from project_context.services.projects import create_project
from project_context.services.reconciliation import reconcile_observation

_counter = itertools.count()

_SENTINEL_A = "SENTINELALPHA7734"
_SENTINEL_B = "SENTINELBRAVO2210"

# Deliberately identical between the two projects, per Section 15's
# "seed two projects with identical names/terms" — a title/subject
# match alone must never be enough to cross a project boundary.
_SHARED_PROJECT_NAME = "Acme Rollout"
_SHARED_ITEM_TITLE = "Send the final report"


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


def _seed_observation_and_accept(conn, project_id, *, sentinel: str):
    """Ingest one evidence artifact whose text embeds `sentinel`, extract
    one observation from it, and reconcile+accept it into a ledger item
    with the shared title — returns (ledger_item, observation)."""
    n = next(_counter)
    statement = f"{_SHARED_ITEM_TITLE} — reference {sentinel} — commitment text."
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = evidence_repository.insert_artifact(
        conn,
        project_id,
        source.id,
        external_id=f"text:{project_id}:{n}",
        artifact_type=ArtifactType.MANUAL_TEXT,
        title="Notes",
        author=None,
        occurred_at=None,
        external_url=None,
        source_type=None,
    )
    content = evidence_repository.insert_content(
        conn,
        project_id,
        artifact.id,
        sha256=hashlib.sha256(f"{n}:{statement}".encode()).hexdigest(),
        raw_storage_path=None,
        mime_type="text/plain",
        byte_size=len(statement),
        normalized_text=statement,
        parser_name="text",
        parser_version="1",
        parse_status=ParseStatus.PARSED,
        location_map=None,
        original_filename=None,
    )
    artifact = evidence_repository.set_current_content(conn, project_id, artifact.id, content.id)
    spec = ChunkSpec(
        ordinal=0,
        text=statement,
        char_start=0,
        char_end=len(statement),
        section_path=None,
        sha256=hashlib.sha256(f"{n}:{statement}:chunk".encode()).hexdigest(),
        token_estimate=len(statement) // 4,
    )
    (chunk,) = evidence_repository.insert_chunks(conn, project_id, content.id, [spec])
    extracted = ExtractedObservation(
        kind=ObservationKind.COMMITMENT,
        subject=_SHARED_ITEM_TITLE,
        statement=statement,
        owner_name=None,
        date_value=None,
        date_text=None,
        explicitness="explicit",
        evidence=[
            EvidenceSpan(chunk_id=chunk.id, char_start=0, char_end=len(statement), quote=statement)
        ],
    )
    observation, _links, _created = persist_observation(
        conn, project_id, content_id=content.id, chunk_id=chunk.id, extracted=extracted
    )
    from project_context.services.review import accept_proposal

    result = reconcile_observation(conn, project_id, observation.id)
    outcome = accept_proposal(conn, project_id, result.proposal.id)
    return outcome.ledger_item, observation, artifact


@pytest.fixture
def two_projects(conn, evidence_dir):
    """Two projects, same name, same ledger-item title, distinct
    sentinels — the exact Section 15 seed shape."""
    project_a = create_project(
        conn, ProjectCreateInput(name=_SHARED_PROJECT_NAME, objective="Ship the pilot")
    )
    project_b = create_project(
        conn, ProjectCreateInput(name=_SHARED_PROJECT_NAME, objective="Ship the pilot")
    )
    item_a, obs_a, artifact_a = _seed_observation_and_accept(
        conn, project_a.id, sentinel=_SENTINEL_A
    )
    item_b, obs_b, artifact_b = _seed_observation_and_accept(
        conn, project_b.id, sentinel=_SENTINEL_B
    )
    return {
        "project_a": project_a.id,
        "project_b": project_b.id,
        "item_a": item_a,
        "item_b": item_b,
        "obs_a": obs_a,
        "obs_b": obs_b,
        "artifact_a": artifact_a,
        "artifact_b": artifact_b,
    }


def _assert_no_cross_leak(haystack: str, *, own_sentinel: str, other_sentinel: str) -> None:
    assert other_sentinel not in haystack
    # Sanity check the fixture actually put the sentinel where expected —
    # a suite that can't find its own sentinel would pass vacuously.
    assert own_sentinel in haystack or own_sentinel == ""


# ---------------------------------------------------------------------------
# 1. Repositories
# ---------------------------------------------------------------------------


def test_repositories_never_return_the_other_projects_sentinel(conn, two_projects):
    project_a = two_projects["project_a"]

    artifacts = evidence_repository.list_artifacts(conn, project_a)
    contents = [
        evidence_repository.get_content(conn, project_a, a.current_content_id) for a in artifacts
    ]
    text_blob = " ".join(c.normalized_text or "" for c in contents if c is not None)
    _assert_no_cross_leak(text_blob, own_sentinel=_SENTINEL_A, other_sentinel=_SENTINEL_B)

    observations = observation_repository.list_observations_for_project(conn, project_a)
    obs_blob = " ".join(o.statement for o in observations)
    _assert_no_cross_leak(obs_blob, own_sentinel=_SENTINEL_A, other_sentinel=_SENTINEL_B)

    items = ledger_repository.list_items_for_project(conn, project_a)
    item_blob = " ".join(i.canonical_description or "" for i in items)
    # The ledger item's canonical fields don't carry the sentinel by
    # design (it lives in the observation/evidence text) — this assert
    # exists so a future change that *does* start copying observation
    # text onto the ledger item is still caught.
    assert _SENTINEL_B not in item_blob


# ---------------------------------------------------------------------------
# 2. FTS
# ---------------------------------------------------------------------------


def test_fts_search_never_crosses_project_boundary(conn, two_projects):
    project_a, project_b = two_projects["project_a"], two_projects["project_b"]

    # Each sentinel is findable — but only within its own project.
    assert evidence_repository.search_chunks(conn, project_a, _SENTINEL_A)
    assert evidence_repository.search_chunks(conn, project_a, _SENTINEL_B) == []
    assert evidence_repository.search_chunks(conn, project_b, _SENTINEL_B)
    assert evidence_repository.search_chunks(conn, project_b, _SENTINEL_A) == []

    assert observation_repository.search_observations(conn, project_a, _SENTINEL_A)
    assert observation_repository.search_observations(conn, project_a, _SENTINEL_B) == []

    # The shared, identical title *is* findable in both — proves this
    # isn't merely "different text never matches," it's real project
    # scoping despite an intentional title collision.
    assert ledger_repository.search_ledger_items(conn, project_a, "final report")
    assert ledger_repository.search_ledger_items(conn, project_b, "final report")


# ---------------------------------------------------------------------------
# 3. Reconciliation
# ---------------------------------------------------------------------------


def test_reconciliation_never_matches_a_candidate_from_another_project(conn, two_projects):
    """Project A and B each already have a ledger item titled
    _SHARED_ITEM_TITLE (identical text). A brand-new observation in
    project A with that same subject must be reconciled against
    project A's own item only — never proposed as an update to
    project B's identically-titled item."""
    project_a = two_projects["project_a"]
    _item, new_observation, _artifact = _seed_observation_and_accept(
        conn, project_a, sentinel=_SENTINEL_A
    )
    # _seed_observation_and_accept already reconciled+accepted once
    # above (in the fixture); reconcile a *second*, brand-new
    # observation now that project A already has one ledger item —
    # this should match A's own item (add_evidence/update), never B's.
    n = next(_counter)
    statement = f"{_SHARED_ITEM_TITLE} — reference {_SENTINEL_A} — follow-up text."
    source = sources_repository.ensure_manual_source(conn, project_a)
    artifact = evidence_repository.insert_artifact(
        conn,
        project_a,
        source.id,
        external_id=f"text:{project_a}:extra:{n}",
        artifact_type=ArtifactType.MANUAL_TEXT,
        title="Notes",
        author=None,
        occurred_at=None,
        external_url=None,
        source_type=None,
    )
    content = evidence_repository.insert_content(
        conn,
        project_a,
        artifact.id,
        sha256=hashlib.sha256(f"extra:{n}:{statement}".encode()).hexdigest(),
        raw_storage_path=None,
        mime_type="text/plain",
        byte_size=len(statement),
        normalized_text=statement,
        parser_name="text",
        parser_version="1",
        parse_status=ParseStatus.PARSED,
        location_map=None,
        original_filename=None,
    )
    spec = ChunkSpec(
        ordinal=0,
        text=statement,
        char_start=0,
        char_end=len(statement),
        section_path=None,
        sha256=hashlib.sha256(f"extra:{n}:{statement}:c".encode()).hexdigest(),
        token_estimate=len(statement) // 4,
    )
    (chunk,) = evidence_repository.insert_chunks(conn, project_a, content.id, [spec])
    extracted = ExtractedObservation(
        kind=ObservationKind.COMMITMENT,
        subject=_SHARED_ITEM_TITLE,
        statement=statement,
        owner_name=None,
        date_value=None,
        date_text=None,
        explicitness="explicit",
        evidence=[
            EvidenceSpan(chunk_id=chunk.id, char_start=0, char_end=len(statement), quote=statement)
        ],
    )
    observation, _links, _created = persist_observation(
        conn, project_a, content_id=content.id, chunk_id=chunk.id, extracted=extracted
    )

    result = reconcile_observation(conn, project_a, observation.id)

    item_a_id = two_projects["item_a"].id
    item_b_id = two_projects["item_b"].id
    assert result.proposal.target_ledger_item_id != item_b_id
    if result.proposal.target_ledger_item_id is not None:
        assert result.proposal.target_ledger_item_id == item_a_id


# ---------------------------------------------------------------------------
# 4. Evidence viewer authorization
# ---------------------------------------------------------------------------


def test_evidence_viewer_denies_a_foreign_artifact_id(conn, two_projects):
    project_a = two_projects["project_a"]
    artifact_b = two_projects["artifact_b"]

    detail = evidence_service.get_evidence_detail(conn, project_a, artifact_b.id)

    assert detail is None


# ---------------------------------------------------------------------------
# 5. Brief fact building
# ---------------------------------------------------------------------------


def test_brief_fact_building_never_includes_the_other_projects_sentinel(conn, two_projects):
    project_a = two_projects["project_a"]

    facts = build_current_project_brief_facts(conn, project_a)
    serialized = json.dumps([section.model_dump(mode="json") for section in facts.sections])

    _assert_no_cross_leak(serialized, own_sentinel="", other_sentinel=_SENTINEL_B)


# ---------------------------------------------------------------------------
# 6/7/8. Claim validation, exports (markdown), model-request construction
# ---------------------------------------------------------------------------


def test_brief_generation_pipeline_never_leaks_sentinel_into_request_or_output(conn, two_projects):
    """One `generate_current_project_brief` call for project A,
    inspected at every stage that touches text the model sees or the
    user sees: the constructed model request (`input_text` —
    "model-request construction"), the validated claims ("claim
    validation"), and the rendered Markdown ("exports")."""
    project_a = two_projects["project_a"]
    calls: list[str] = []

    def _fact_id(input_text: str, section_key: str) -> str | None:
        match = re.search(
            rf'<section key="{section_key}"[^>]*>\n(.*?)\n</section>', input_text, re.DOTALL
        )
        if match is None:
            return None
        facts = json.loads(match.group(1))
        return facts[0]["fact_id"] if facts else None

    class _RecordingProvider:
        def generate_structured(self, *, task, system, input_text, response_model, config):
            calls.append(input_text)
            fact_id = _fact_id(input_text, "open_commitments")
            claims = (
                [
                    BriefClaimOutput(
                        # Echoes the sentinel back, the way a real model
                        # composing prose from the supplied fact text might
                        # quote a distinctive phrase — proving the export
                        # path *can* carry project A's own sentinel through
                        # end to end, which is what makes "B never appears"
                        # below a meaningful assertion rather than a
                        # vacuous one.
                        text=f"The final report commitment ({_SENTINEL_A}) is open.",
                        claim_type="fact",
                        fact_ids=[fact_id],
                    )
                ]
                if fact_id
                else []
            )
            return StructuredResult(
                parsed=BriefComposition(
                    sections=[BriefSectionOutput(section="open_commitments", claims=claims)]
                ),
                provider="fake",
                model=config.model,
                request_id=None,
                input_tokens=10,
                output_tokens=5,
                latency_ms=1,
                estimated_cost_usd=estimate_cost_usd(config.model, 10, 5),
            )

    result = generate_current_project_brief(conn, project_a, provider=_RecordingProvider())

    assert calls, "provider was never called — nothing to check"
    for input_text in calls:
        assert _SENTINEL_B not in input_text
        assert _SENTINEL_A in input_text  # sanity: project A's own data *is* in its own request

    assert _SENTINEL_B not in (result.brief.markdown or "")
    assert _SENTINEL_A in (result.brief.markdown or "")

    # Claim validation: every claim the pipeline actually accepted must
    # resolve to a fact that itself only ever came from project A's own
    # `build_current_project_brief_facts` call above — there is no code
    # path here for a claim to cite an ID from another project's fact
    # set (opaque, per-run `fact_id`s — see project_context.retrieval.
    # briefs), so this asserts the *observable* half: no claim's
    # rendered text carries the foreign sentinel.
    for claim in result.claims:
        assert _SENTINEL_B not in claim.claim_text
