"""Tests for Fathom sync orchestration (Section 11.5; FR-004, FR-030,
FR-031; Prompt 13): `project_context.services.sync.sync_source` driven
by a `FathomConnector`/`FakeFathomApi`, `sync_fathom_project`'s
credential/rule wiring, `fathom_ingestion`'s primary/secondary chunking
and ambiguous-assignment handling, overlap dedupe, changed-transcript
reprocessing, partial-sync isolation, and cross-project leakage. Uses
`FakeFathomApi` and `FakeLLMProvider` — nothing here touches the
network."""

from __future__ import annotations

import json

import pytest

from fixtures.fake_fathom_api import FakeFathomApi
from project_context.connectors.fathom import FathomConnector
from project_context.credentials.service import CredentialService
from project_context.credentials.store import CredentialStore
from project_context.db import (
    evidence_repository,
    observation_repository,
    proposed_mutation_repository,
    sources_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import ArtifactAvailability, ArtifactType
from project_context.domain.fathom_matching import FathomMatchRules
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.sources import SourceHealthStatus, SourceKind
from project_context.domain.sync import SyncRunStatus
from project_context.llm.schemas import EvidenceSpan, ExtractedObservation, ExtractionBatch
from project_context.services import fathom_ingestion
from project_context.services import sync as sync_service
from project_context.services.projects import create_project


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


@pytest.fixture
def project_id(conn):
    return create_project(
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
    ).id


def _make_fathom_source(conn, project_id, *, boundary=None):
    boundary = boundary if boundary is not None else {"client_domain": "acme.com"}
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.FATHOM, display_name="Fathom API key"
    )
    return sources_repository.update_boundary(
        conn, project_id, source.id, boundary_json=json.dumps(boundary)
    )


def _connector(api, *, rules=None, created_after=None):
    rules = rules or FathomMatchRules(client_domain="acme.com")
    return FathomConnector(
        api_key="fake-api-key",
        rules=rules,
        created_after=created_after,
        http_transport=api,
        sleep=lambda _s: None,
        rand=lambda: 0.0,
    )


def _sync(conn, project_id, source_id, api, evidence_dir, *, extraction_provider=None, rules=None):
    return sync_service.sync_source(
        conn,
        project_id,
        source_id,
        connector=_connector(api, rules=rules),
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
        extraction_provider=extraction_provider,
        get_or_create_artifact_fn=fathom_ingestion.get_or_create_fathom_artifact,
        store_artifact_fn=fathom_ingestion.store_fathom_artifact,
    )


# --- validation ------------------------------------------------------------


def test_sync_source_raises_for_missing_boundary(conn, project_id, tmp_path):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.FATHOM, display_name="Fathom API key"
    )
    with pytest.raises(sync_service.SourceNotConfiguredError):
        sync_service.sync_source(
            conn,
            project_id,
            source.id,
            connector=_connector(FakeFathomApi()),
            evidence_dir=tmp_path,
            chunk_target_chars=4000,
            chunk_overlap_ratio=0.0,
            get_or_create_artifact_fn=fathom_ingestion.get_or_create_fathom_artifact,
            store_artifact_fn=fathom_ingestion.store_fathom_artifact,
        )


def test_sync_source_raises_for_empty_rule_set(conn, project_id, tmp_path):
    source = _make_fathom_source(conn, project_id, boundary={})
    with pytest.raises(sync_service.SourceNotConfiguredError):
        sync_service.sync_source(
            conn,
            project_id,
            source.id,
            connector=_connector(FakeFathomApi()),
            evidence_dir=tmp_path,
            chunk_target_chars=4000,
            chunk_overlap_ratio=0.0,
            get_or_create_artifact_fn=fathom_ingestion.get_or_create_fathom_artifact,
            store_artifact_fn=fathom_ingestion.store_fathom_artifact,
        )


def test_sync_source_marks_reauth_required_on_401(conn, project_id, tmp_path):
    source = _make_fathom_source(conn, project_id)
    api = FakeFathomApi()
    api.fail_next("list", times=99, status_code=401)
    run = _sync(conn, project_id, source.id, api, tmp_path)
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.REAUTH_REQUIRED


def test_sync_source_degrades_on_permission_error(conn, project_id, tmp_path):
    source = _make_fathom_source(conn, project_id)
    api = FakeFathomApi()
    api.fail_next("list", times=99, status_code=403)
    run = _sync(conn, project_id, source.id, api, tmp_path)
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.DEGRADED


def test_sync_source_fails_the_whole_run_on_a_persistent_5xx(conn, project_id, evidence_dir):
    """A listing-level failure (as opposed to one bad item) aborts
    discovery entirely, so nothing was discovered — `_final_status`
    correctly reports `FAILED`, not `PARTIAL` (Section 8: "One failed
    connector does not lose others" is about *other sources*, not
    about a single source's own unrecoverable listing failure). See
    `test_partial_sync_when_one_meetings_extraction_fails` below for a
    genuine partial-sync case."""
    source = _make_fathom_source(conn, project_id)
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}])
    api.fail_next("list", times=99, status_code=503)
    run = _sync(conn, project_id, source.id, api, evidence_dir)
    assert run.status is SyncRunStatus.FAILED
    assert run.failed_count == 1


def test_pagination_failure_on_a_later_page_does_not_lose_the_earlier_page(
    conn, project_id, evidence_dir
):
    """A failure fetching page 2 (after page 1's item was already
    discovered/stored) must not roll back what page 1 already
    committed — the run overall is reported `FAILED` (this source's own
    listing could not complete), but the already-stored artifact from
    page 1 remains, exactly as Section 8's "commit per source item/
    small batch so one bad artifact does not lose the run" requires."""
    source = _make_fathom_source(conn, project_id)

    class _FailFromSecondPageApi(FakeFathomApi):
        """`sync_source` itself makes two successful calls before
        pagination even begins to matter — `validate_config()`'s own
        light call, then `discover()`'s first page — so the third call
        (`discover()`'s *second* page) is the first one this fake fails,
        specifically to isolate a pagination-continuation failure from a
        first-call failure."""

        def _handle_list(self, params, headers):
            if len(self.list_calls) >= 2:
                from project_context.connectors.http import HttpResponse

                self.list_calls.append({"params": dict(params), "headers": dict(headers)})
                return HttpResponse(status_code=502, headers={}, content=b"{}")
            return super()._handle_list(params, headers)

    api = _FailFromSecondPageApi(page_size=1)  # page_size=1 forces two pages
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}])
    api.add_meeting("rec2", calendar_invitees=[{"email": "x@acme.com"}])

    run = _sync(conn, project_id, source.id, api, evidence_dir)
    assert run.status is SyncRunStatus.FAILED
    assert run.discovered_count == 1

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert len(artifacts) == 1  # page 1's artifact was committed, not rolled back


def test_partial_sync_when_one_meetings_extraction_fails(conn, project_id, evidence_dir):
    """Two matched meetings; extraction fails for exactly one of them.
    Section 8/FR-031: independent per-item failures must not roll back
    or block the other, and the run status must honestly reflect
    'partial', not 'failed' or 'completed'."""
    source = _make_fathom_source(conn, project_id)
    api = FakeFathomApi()
    api.add_meeting(
        "rec-good",
        calendar_invitees=[{"email": "x@acme.com"}],
        transcript=[{"speaker": {"display_name": "A"}, "text": "Fine text.", "timestamp": "0"}],
    )
    api.add_meeting(
        "rec-bad",
        calendar_invitees=[{"email": "x@acme.com"}],
        transcript=[
            {"speaker": {"display_name": "A"}, "text": "POISON-SENTINEL text.", "timestamp": "0"}
        ],
    )

    class _SelectivelyFailingProvider:
        def generate_structured(self, *, task, system, input_text, response_model, config):
            if "POISON-SENTINEL" in input_text:
                raise RuntimeError("simulated provider failure for this one item")
            from project_context.llm.provider import StructuredResult, estimate_cost_usd

            batch = ExtractionBatch(observations=[], source_contains_no_material_updates=True)
            return StructuredResult(
                parsed=batch,
                provider="fake",
                model=config.model,
                request_id=None,
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
                estimated_cost_usd=estimate_cost_usd(config.model, 1, 1),
            )

    run = _sync(
        conn,
        project_id,
        source.id,
        api,
        evidence_dir,
        extraction_provider=_SelectivelyFailingProvider(),
    )
    assert run.status is SyncRunStatus.PARTIAL
    assert run.discovered_count == 2
    assert run.failed_count == 1
    assert run.parsed_count == 2  # both stored; only extraction failed for one


# --- basic ingestion (no extraction provider) -------------------------------


def test_sync_ingests_a_matched_meeting_without_a_provider(conn, project_id, evidence_dir):
    source = _make_fathom_source(conn, project_id)
    api = FakeFathomApi()
    api.add_meeting(
        "rec1",
        title="Acme Kickoff",
        calendar_invitees=[{"email": "client@acme.com"}],
        transcript=[
            {
                "speaker": {"display_name": "Alice"},
                "text": "We shipped it.",
                "timestamp": "00:00:05",
            }
        ],
    )
    api.add_meeting("rec2", calendar_invitees=[{"email": "x@other.com"}])  # never matches

    run = _sync(conn, project_id, source.id, api, evidence_dir)

    assert run.status is SyncRunStatus.COMPLETED
    assert run.discovered_count == 1  # rec2 never enters the pipeline at all
    assert run.parsed_count == 1
    assert run.failed_count == 0

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type is ArtifactType.MEETING
    assert artifacts[0].availability is ArtifactAvailability.AVAILABLE
    assert "client_domain" in artifacts[0].match_reason

    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert "We shipped it." in content.normalized_text
    assert "Title: Acme Kickoff" in content.normalized_text


def test_missing_transcript_stores_metadata_but_extracts_nothing(conn, project_id, evidence_dir):
    source = _make_fathom_source(conn, project_id)
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}], transcript=[])

    class _ExplodingProvider:
        def generate_structured(self, **kwargs):
            raise AssertionError("extraction must never run for a transcript-less meeting")

    run = _sync(
        conn, project_id, source.id, api, evidence_dir, extraction_provider=_ExplodingProvider()
    )

    assert run.status is SyncRunStatus.COMPLETED
    assert run.parsed_count == 1
    assert run.extracted_count == 0
    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    chunks = evidence_repository.list_chunks_for_content(conn, project_id, content.id)
    assert chunks == []
    assert "Recording ID: rec1" in content.normalized_text


def test_summary_and_action_items_never_reach_extraction(conn, project_id, evidence_dir):
    """Prompt 13: Fathom-produced action items/summary are secondary
    source evidence, never automatically accepted commitments — enforced
    here by construction: they must never even reach the extraction
    provider as chunked text."""
    source = _make_fathom_source(conn, project_id)
    api = FakeFathomApi()
    api.add_meeting(
        "rec1",
        calendar_invitees=[{"email": "x@acme.com"}],
        transcript=[{"speaker": {"display_name": "A"}, "text": "We decided X.", "timestamp": "0"}],
        default_summary_markdown="Fathom summary sentinel.",
        action_items=[{"description": "Fathom action item sentinel", "completed": False}],
    )

    seen_texts: list[str] = []

    class _RecordingProvider:
        def generate_structured(self, *, task, system, input_text, response_model, config):
            seen_texts.append(input_text)
            from project_context.llm.provider import StructuredResult, estimate_cost_usd

            batch = ExtractionBatch(observations=[], source_contains_no_material_updates=True)
            return StructuredResult(
                parsed=batch,
                provider="fake",
                model=config.model,
                request_id=None,
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
                estimated_cost_usd=estimate_cost_usd(config.model, 1, 1),
            )

    _sync(conn, project_id, source.id, api, evidence_dir, extraction_provider=_RecordingProvider())

    assert seen_texts, "extraction should have run over the transcript chunk"
    for text in seen_texts:
        assert "Fathom summary sentinel" not in text
        assert "Fathom action item sentinel" not in text


# --- ambiguous assignment: scheduled_event tier alone -----------------------


def test_meeting_matched_only_by_scheduled_window_is_unassigned(conn, project_id, evidence_dir):
    boundary = {
        "scheduled_windows": [{"start": "2026-06-01T00:00:00Z", "end": "2026-06-02T00:00:00Z"}],
    }
    source = _make_fathom_source(conn, project_id, boundary=boundary)
    api = FakeFathomApi()
    api.add_meeting(
        "rec1",
        calendar_invitees=[{"email": "x@unrelated.com"}],
        recording_start_time="2026-06-01T12:00:00Z",
    )
    rules = FathomMatchRules.from_boundary(boundary)

    run = _sync(conn, project_id, source.id, api, evidence_dir, rules=rules)
    assert run.status is SyncRunStatus.COMPLETED
    assert run.needs_assignment_count == 1

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert artifacts[0].availability is ArtifactAvailability.UNASSIGNED
    # Still fully ingested/visible evidence, just not auto-assigned.
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert content is not None


def test_meeting_matched_by_client_domain_is_not_unassigned_even_with_a_window_configured(
    conn, project_id, evidence_dir
):
    boundary = {
        "client_domain": "acme.com",
        "scheduled_windows": [{"start": "2026-06-01T00:00:00Z", "end": "2026-06-02T00:00:00Z"}],
    }
    source = _make_fathom_source(conn, project_id, boundary=boundary)
    api = FakeFathomApi()
    api.add_meeting(
        "rec1",
        calendar_invitees=[{"email": "client@acme.com"}],
        recording_start_time="2026-06-01T12:00:00Z",
    )
    rules = FathomMatchRules.from_boundary(boundary)

    run = _sync(conn, project_id, source.id, api, evidence_dir, rules=rules)
    assert run.needs_assignment_count == 0
    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert artifacts[0].availability is ArtifactAvailability.AVAILABLE


# --- idempotency / overlap dedupe / changed transcript ----------------------


def test_rerun_of_the_same_meeting_produces_no_change_only(conn, project_id, evidence_dir):
    source = _make_fathom_source(conn, project_id)
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}])

    first = _sync(conn, project_id, source.id, api, evidence_dir)
    assert first.parsed_count == 1

    second = _sync(conn, project_id, source.id, api, evidence_dir)
    assert second.unchanged_count == 1
    assert second.parsed_count == 0
    assert second.discovered_count == 1

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert len(artifacts) == 1


def test_overlap_rescan_deduplicates_by_recording_id(conn, project_id, evidence_dir):
    """The same meeting appearing again inside a later sync's overlap
    window (Section 11.5: "48-hour overlap ... recording_id dedupe")
    must not create a second artifact or duplicate evidence."""
    source = _make_fathom_source(conn, project_id)
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}])
    _sync(conn, project_id, source.id, api, evidence_dir)

    # Simulate the next sync's overlap window re-listing the exact same
    # meeting (still present in the fake's full listing, as it always is).
    second = _sync(conn, project_id, source.id, api, evidence_dir)
    assert second.status is SyncRunStatus.COMPLETED
    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert len(artifacts) == 1
    contents = evidence_repository.list_contents_for_artifact(conn, project_id, artifacts[0].id)
    assert len(contents) == 1


def test_rerun_creates_a_new_version_when_transcript_changes(conn, project_id, evidence_dir):
    """A later transcript change (post-processing catching up, a later
    edit) must be discovered by the next overlap rescan — no webhook
    assumption (Prompt 13)."""
    source = _make_fathom_source(conn, project_id)
    api = FakeFathomApi()
    api.add_meeting("rec1", calendar_invitees=[{"email": "x@acme.com"}], transcript=[])

    first = _sync(conn, project_id, source.id, api, evidence_dir)
    assert first.parsed_count == 1
    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    first_content = evidence_repository.get_content(
        conn, project_id, artifacts[0].current_content_id
    )
    assert evidence_repository.list_chunks_for_content(conn, project_id, first_content.id) == []

    api.update_meeting(
        "rec1",
        transcript=[
            {"speaker": {"display_name": "A"}, "text": "Now it's ready.", "timestamp": "00:00:01"}
        ],
    )
    second = _sync(conn, project_id, source.id, api, evidence_dir)
    assert second.parsed_count == 1
    assert second.unchanged_count == 0

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    contents = evidence_repository.list_contents_for_artifact(conn, project_id, artifacts[0].id)
    assert len(contents) == 2  # both versions retained (FR-007)
    new_content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert "Now it's ready." in new_content.normalized_text
    chunks = evidence_repository.list_chunks_for_content(conn, project_id, new_content.id)
    assert len(chunks) == 1


# --- extraction + reconciliation wiring -------------------------------------


class _ReactiveProvider:
    def __init__(self, text: str):
        self._text = text

    def generate_structured(self, *, task, system, input_text, response_model, config):
        import re

        from project_context.llm.provider import StructuredResult, estimate_cost_usd

        chunk_id_match = re.search(r'chunk_id="([^"]+)"', input_text)
        chunk_text_match = re.search(
            r'<source_chunk id="[^"]+">\n(.*?)\n</source_chunk>', input_text, re.DOTALL
        )
        chunk_text = chunk_text_match.group(1)
        char_start = chunk_text.index(self._text)
        char_end = char_start + len(self._text)
        span = EvidenceSpan(
            chunk_id=chunk_id_match.group(1),
            char_start=char_start,
            char_end=char_end,
            quote=self._text,
        )
        observation = ExtractedObservation(
            kind="commitment",
            subject="Ship the report",
            statement=self._text,
            owner_name=None,
            explicitness="explicit",
            evidence=[span],
        )
        batch = ExtractionBatch(
            observations=[observation], source_contains_no_material_updates=False
        )
        return StructuredResult(
            parsed=batch,
            provider="fake",
            model=config.model,
            request_id=None,
            input_tokens=10,
            output_tokens=5,
            latency_ms=1,
            estimated_cost_usd=estimate_cost_usd(config.model, 10, 5),
        )


def test_sync_extracts_from_transcript_text(conn, project_id, evidence_dir):
    source = _make_fathom_source(conn, project_id)
    api = FakeFathomApi()
    text = "We will ship the report by Friday."
    api.add_meeting(
        "rec1",
        calendar_invitees=[{"email": "x@acme.com"}],
        transcript=[{"speaker": {"display_name": "A"}, "text": text, "timestamp": "00:00:01"}],
    )

    run = _sync(
        conn, project_id, source.id, api, evidence_dir, extraction_provider=_ReactiveProvider(text)
    )

    assert run.status is SyncRunStatus.COMPLETED
    assert run.proposed_count == 1
    observations = observation_repository.list_observations_for_project(conn, project_id)
    assert len(observations) == 1
    proposals = proposed_mutation_repository.list_pending_for_project(conn, project_id)
    assert len(proposals) == 1


# --- partial sync / cross-connector isolation -------------------------------


def test_fathom_sync_failure_does_not_affect_other_sources(
    conn, project_id, evidence_dir, tmp_path
):
    from fixtures.fake_drive_api import FakeDriveApi
    from project_context.connectors.drive import DriveConnector

    drive_source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    drive_source = sources_repository.update_boundary(
        conn, project_id, drive_source.id, boundary_json=json.dumps({"folder_id": "root"})
    )
    drive_api = FakeDriveApi()
    drive_api.files["root"] = {
        "id": "root",
        "mimeType": "application/vnd.google-apps.folder",
        "trashed": False,
    }
    drive_api.add_folder("root", [])
    drive_run = sync_service.sync_source(
        conn,
        project_id,
        drive_source.id,
        connector=DriveConnector(access_token="t", folder_id="root", http_transport=drive_api),
        evidence_dir=tmp_path,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    assert drive_run.status is SyncRunStatus.COMPLETED

    fathom_source = _make_fathom_source(conn, project_id)
    fathom_api = FakeFathomApi()
    fathom_api.fail_next("list", times=99, status_code=401)
    fathom_run = _sync(conn, project_id, fathom_source.id, fathom_api, evidence_dir)
    assert fathom_run.status is SyncRunStatus.FAILED

    refreshed_drive = sources_repository.get_source(conn, project_id, drive_source.id)
    assert refreshed_drive.health_status is SourceHealthStatus.HEALTHY


# --- cross-project isolation / leakage --------------------------------------


def test_fathom_evidence_is_isolated_per_project(conn, evidence_dir):
    project_a = create_project(conn, ProjectCreateInput(name="Project A", objective="A")).id
    project_b = create_project(conn, ProjectCreateInput(name="Project B", objective="B")).id

    source_a = _make_fathom_source(conn, project_a, boundary={"client_domain": "alpha.com"})
    api_a = FakeFathomApi()
    api_a.add_meeting(
        "rec1",
        calendar_invitees=[{"email": "x@alpha.com"}],
        transcript=[
            {
                "speaker": {"display_name": "A"},
                "text": "Secret sentinel Alpha content.",
                "timestamp": "0",
            }
        ],
    )
    _sync(
        conn,
        project_a,
        source_a.id,
        api_a,
        evidence_dir,
        rules=FathomMatchRules(client_domain="alpha.com"),
    )

    source_b = _make_fathom_source(conn, project_b, boundary={"client_domain": "beta.com"})
    api_b = FakeFathomApi()
    api_b.add_meeting(
        "rec2",
        calendar_invitees=[{"email": "x@beta.com"}],
        transcript=[
            {"speaker": {"display_name": "B"}, "text": "Unrelated Beta content.", "timestamp": "0"}
        ],
    )
    _sync(
        conn,
        project_b,
        source_b.id,
        api_b,
        evidence_dir,
        rules=FathomMatchRules(client_domain="beta.com"),
    )

    artifacts_b = evidence_repository.list_artifacts_for_source(conn, project_b, source_b.id)
    content_b = evidence_repository.get_content(conn, project_b, artifacts_b[0].current_content_id)
    assert "Secret sentinel Alpha" not in content_b.normalized_text

    artifacts_a = evidence_repository.list_artifacts_for_source(conn, project_a, source_a.id)
    assert evidence_repository.get_artifact(conn, project_b, artifacts_a[0].id) is None


def test_same_recording_id_in_two_projects_does_not_cross_link(conn, evidence_dir):
    """A meeting shared with two different clients could plausibly get
    the *same* `recording_id` matched by two independent projects'
    rules — each project's own artifact identity is still
    `(source_id, external_id)`, so this must produce two fully
    independent artifacts, not one shared/cross-linked row."""
    project_a = create_project(conn, ProjectCreateInput(name="Project A", objective="A")).id
    project_b = create_project(conn, ProjectCreateInput(name="Project B", objective="B")).id

    source_a = _make_fathom_source(conn, project_a, boundary={"client_domain": "alpha.com"})
    source_b = _make_fathom_source(conn, project_b, boundary={"client_domain": "alpha.com"})

    api = FakeFathomApi()
    api.add_meeting("rec-shared", calendar_invitees=[{"email": "x@alpha.com"}])

    _sync(
        conn,
        project_a,
        source_a.id,
        api,
        evidence_dir,
        rules=FathomMatchRules(client_domain="alpha.com"),
    )
    _sync(
        conn,
        project_b,
        source_b.id,
        api,
        evidence_dir,
        rules=FathomMatchRules(client_domain="alpha.com"),
    )

    artifacts_a = evidence_repository.list_artifacts_for_source(conn, project_a, source_a.id)
    artifacts_b = evidence_repository.list_artifacts_for_source(conn, project_b, source_b.id)
    assert artifacts_a[0].id != artifacts_b[0].id
    assert evidence_repository.get_artifact(conn, project_a, artifacts_b[0].id) is None


# --- secret/content redaction (Section 16; FR-032) --------------------------


def test_fathom_sync_never_logs_the_api_key_or_transcript_text(
    conn, project_id, evidence_dir, caplog
):
    import logging

    source = _make_fathom_source(conn, project_id)
    api = FakeFathomApi()
    secret_text = "SECRET-TEST-TRANSCRIPT-do-not-log-this-value"
    secret_key = "FAKE-TEST-API-KEY-do-not-log-this-value"
    api.add_meeting(
        "rec1",
        calendar_invitees=[{"email": "x@acme.com"}],
        transcript=[{"speaker": {"display_name": "A"}, "text": secret_text, "timestamp": "0"}],
    )

    connector = FathomConnector(
        api_key=secret_key,
        rules=FathomMatchRules(client_domain="acme.com"),
        http_transport=api,
        sleep=lambda _s: None,
        rand=lambda: 0.0,
    )
    with caplog.at_level(logging.DEBUG):
        sync_service.sync_source(
            conn,
            project_id,
            source.id,
            connector=connector,
            evidence_dir=evidence_dir,
            chunk_target_chars=4000,
            chunk_overlap_ratio=0.0,
            get_or_create_artifact_fn=fathom_ingestion.get_or_create_fathom_artifact,
            store_artifact_fn=fathom_ingestion.store_fathom_artifact,
        )

    for record in caplog.records:
        message = record.getMessage()
        as_dict = str(record.__dict__)
        for secret in (secret_key, secret_text):
            assert secret not in message
            assert secret not in as_dict


# --- sync_fathom_project: credential wiring ---------------------------------


@pytest.fixture
def credential_service(tmp_path):
    store = CredentialStore(credentials_dir=tmp_path / "creds", prefer_keyring=False)
    return CredentialService(store)


def test_sync_fathom_project_fails_cleanly_with_no_stored_credential(
    conn, project_id, evidence_dir, credential_service
):
    source = _make_fathom_source(conn, project_id)
    run = sync_service.sync_fathom_project(
        conn,
        project_id,
        source.id,
        credential_service=credential_service,
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    assert run.status is SyncRunStatus.FAILED


def test_sync_fathom_project_fails_gracefully_on_invalid_scheduled_window(
    conn, project_id, evidence_dir, credential_service
):
    source = _make_fathom_source(
        conn, project_id, boundary={"scheduled_windows": [{"start": "bad", "end": "also-bad"}]}
    )
    credential_service.connect(conn, project_id, source.id, secret="fake-api-key")

    run = sync_service.sync_fathom_project(
        conn,
        project_id,
        source.id,
        credential_service=credential_service,
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.DEGRADED


def test_sync_fathom_project_marks_reauth_required_on_invalid_key(
    conn, project_id, evidence_dir, credential_service, monkeypatch
):
    source = _make_fathom_source(conn, project_id)
    credential_service.connect(conn, project_id, source.id, secret="bad-api-key")

    class _AlwaysUnauthorizedTransport:
        def request(self, method, url, *, params=None, headers=None, timeout=30.0):
            from project_context.connectors.http import HttpResponse

            return HttpResponse(status_code=401, headers={}, content=b"{}")

    monkeypatch.setattr(
        sync_service, "RequestsHttpTransport", lambda: _AlwaysUnauthorizedTransport()
    )

    run = sync_service.sync_fathom_project(
        conn,
        project_id,
        source.id,
        credential_service=credential_service,
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.REAUTH_REQUIRED


def test_sync_fathom_project_computes_a_watermark_from_last_success(
    conn, project_id, evidence_dir, credential_service, monkeypatch
):
    source = _make_fathom_source(conn, project_id)
    credential_service.connect(conn, project_id, source.id, secret="fake-api-key")
    sources_repository.update_last_success(
        conn, project_id, source.id, last_success_at="2026-06-10T00:00:00Z"
    )

    captured: dict[str, object] = {}

    def _fake_connector(*, api_key, rules, created_after, http_transport):
        captured["created_after"] = created_after
        return FathomConnector(
            api_key=api_key,
            rules=rules,
            created_after=created_after,
            http_transport=FakeFathomApi(),
            sleep=lambda _s: None,
            rand=lambda: 0.0,
        )

    monkeypatch.setattr(sync_service, "FathomConnector", _fake_connector)

    sync_service.sync_fathom_project(
        conn,
        project_id,
        source.id,
        credential_service=credential_service,
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    # 48-hour overlap before 2026-06-10T00:00:00Z.
    assert captured["created_after"] == "2026-06-08T00:00:00Z"
