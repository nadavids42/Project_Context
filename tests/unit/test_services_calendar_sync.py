"""Tests for Calendar sync orchestration (Section 11.4; FR-004, FR-029,
FR-031; Prompt 12): `project_context.services.sync.sync_source` driven
by a `CalendarConnector`/`FakeCalendarApi`, `sync_calendar_project`'s
credential/rule wiring, and `calendar_ingestion`'s description-only
chunking. Uses `FakeCalendarApi` and `FakeLLMProvider` — nothing here
touches the network."""

from __future__ import annotations

import json

import pytest

from fixtures.fake_calendar_api import FakeCalendarApi
from project_context.connectors.calendar import CalendarConnector
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
from project_context.domain.calendar_matching import CalendarMatchRules
from project_context.domain.evidence import ArtifactAvailability, ArtifactType
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.sources import SourceHealthStatus, SourceKind
from project_context.domain.sync import SyncRunStatus
from project_context.llm.schemas import EvidenceSpan, ExtractedObservation, ExtractionBatch
from project_context.services import calendar_ingestion
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


def _make_calendar_source(conn, project_id, *, boundary=None):
    boundary = boundary if boundary is not None else {"project_name_terms": ["Acme Rollout"]}
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.CALENDAR, display_name="Calendar rules"
    )
    return sources_repository.update_boundary(
        conn, project_id, source.id, boundary_json=json.dumps(boundary)
    )


def _connector(api, *, rules=None):
    rules = rules or CalendarMatchRules(project_name_terms=("Acme Rollout",))
    return CalendarConnector(
        access_token="fake-token", rules=rules, http_transport=api,
        sleep=lambda _s: None, rand=lambda: 0.0,
    )


def _sync(conn, project_id, source_id, api, evidence_dir, *, extraction_provider=None, rules=None):
    return sync_service.sync_source(
        conn, project_id, source_id, connector=_connector(api, rules=rules),
        evidence_dir=evidence_dir, chunk_target_chars=4000, chunk_overlap_ratio=0.0,
        extraction_provider=extraction_provider,
        get_or_create_artifact_fn=calendar_ingestion.get_or_create_calendar_artifact,
        store_artifact_fn=calendar_ingestion.store_calendar_artifact,
    )


# --- validation ----------------------------------------------------------


def test_sync_source_raises_for_missing_boundary(conn, project_id, tmp_path):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.CALENDAR, display_name="Calendar rules"
    )
    with pytest.raises(sync_service.SourceNotConfiguredError):
        sync_service.sync_source(
            conn, project_id, source.id, connector=_connector(FakeCalendarApi()),
            evidence_dir=tmp_path, chunk_target_chars=4000, chunk_overlap_ratio=0.0,
            get_or_create_artifact_fn=calendar_ingestion.get_or_create_calendar_artifact,
            store_artifact_fn=calendar_ingestion.store_calendar_artifact,
        )


def test_sync_source_raises_for_empty_rule_set(conn, project_id, tmp_path):
    source = _make_calendar_source(conn, project_id, boundary={})
    with pytest.raises(sync_service.SourceNotConfiguredError):
        sync_service.sync_source(
            conn, project_id, source.id, connector=_connector(FakeCalendarApi()),
            evidence_dir=tmp_path, chunk_target_chars=4000, chunk_overlap_ratio=0.0,
            get_or_create_artifact_fn=calendar_ingestion.get_or_create_calendar_artifact,
            store_artifact_fn=calendar_ingestion.store_calendar_artifact,
        )


def test_sync_source_marks_reauth_required_on_401(conn, project_id, tmp_path):
    source = _make_calendar_source(conn, project_id)
    api = FakeCalendarApi()
    api.fail_next("list", times=99, status_code=401)
    run = _sync(conn, project_id, source.id, api, tmp_path)
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.REAUTH_REQUIRED


def test_sync_source_degrades_on_permission_error(conn, project_id, tmp_path):
    source = _make_calendar_source(conn, project_id)
    api = FakeCalendarApi()
    api.fail_next("list", times=99, status_code=403)
    run = _sync(conn, project_id, source.id, api, tmp_path)
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.DEGRADED


# --- basic ingestion (no extraction provider) ------------------------------


def test_sync_ingests_a_matched_event_without_a_provider(conn, project_id, evidence_dir):
    source = _make_calendar_source(conn, project_id)
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync", description="We decided to ship in July.")
    api.add_event("evt2", summary="Personal dentist appt")

    run = _sync(conn, project_id, source.id, api, evidence_dir)

    assert run.status is SyncRunStatus.COMPLETED
    assert run.discovered_count == 1  # evt2 never enters the pipeline at all
    assert run.parsed_count == 1
    assert run.failed_count == 0

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type is ArtifactType.CALENDAR_EVENT
    assert artifacts[0].match_rule == "project_name_term"
    assert "Acme Rollout" in artifacts[0].match_reason

    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert "We decided to ship in July." in content.normalized_text
    assert "Title: Acme Rollout sync" in content.normalized_text


def test_event_metadata_alone_cannot_produce_a_project_state_observation(
    conn, project_id, evidence_dir
):
    """Prompt 12: "It must not create a decision, commitment,
    completion, or risk merely because an event exists." A matched
    event with no description must reach extraction with zero chunks —
    the extraction provider must never even be called for it."""
    source = _make_calendar_source(conn, project_id)
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync")  # no description at all

    class _ExplodingProvider:
        def generate_structured(self, **kwargs):
            raise AssertionError("extraction must never run for a description-less event")

    run = _sync(
        conn, project_id, source.id, api, evidence_dir,
        extraction_provider=_ExplodingProvider(),
    )

    assert run.status is SyncRunStatus.COMPLETED
    assert run.parsed_count == 1
    assert run.extracted_count == 0
    assert observation_repository.list_observations_for_project(conn, project_id) == []

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    chunks = evidence_repository.list_chunks_for_content(conn, project_id, content.id)
    assert chunks == []
    # But the metadata itself (title, organizer, timing) is still fully
    # visible as stored evidence — it just never reaches extraction.
    assert "Title: Acme Rollout sync" in content.normalized_text


def test_metadata_header_is_never_chunked_even_with_a_description_present(
    conn, project_id, evidence_dir
):
    source = _make_calendar_source(conn, project_id)
    api = FakeCalendarApi()
    api.add_event(
        "evt1", summary="Acme Rollout sync", description="Priya will send the report.",
        organizer_email="sensitive-organizer@example.com",
    )
    run = _sync(conn, project_id, source.id, api, evidence_dir)
    assert run.status is SyncRunStatus.COMPLETED

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    chunks = evidence_repository.list_chunks_for_content(conn, project_id, content.id)
    chunk_text = "".join(c.text for c in chunks)
    assert "Priya will send the report." in chunk_text
    assert "sensitive-organizer@example.com" not in chunk_text
    assert "Title:" not in chunk_text
    # Full metadata remains in the stored evidence, unmodified.
    assert "sensitive-organizer@example.com" in content.normalized_text


# --- extraction + reconciliation wiring ------------------------------------


class _ReactiveProvider:
    """Reads the real `chunk_id` and the target sentence's real offset
    out of whatever chunk `extract_content` actually sent."""

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
            chunk_id=chunk_id_match.group(1), char_start=char_start, char_end=char_end,
            quote=self._text,
        )
        observation = ExtractedObservation(
            kind="commitment", subject="Send the report", statement=self._text,
            owner_name=None, explicitness="explicit", evidence=[span],
        )
        batch = ExtractionBatch(
            observations=[observation], source_contains_no_material_updates=False
        )
        return StructuredResult(
            parsed=batch, provider="fake", model=config.model, request_id=None,
            input_tokens=10, output_tokens=5, latency_ms=1,
            estimated_cost_usd=estimate_cost_usd(config.model, 10, 5),
        )


def test_sync_extracts_only_from_explicit_description_text(conn, project_id, evidence_dir):
    source = _make_calendar_source(conn, project_id)
    api = FakeCalendarApi()
    text = "Priya will send the report by Friday."
    api.add_event("evt1", summary="Acme Rollout sync", description=text)

    run = _sync(
        conn, project_id, source.id, api, evidence_dir, extraction_provider=_ReactiveProvider(text)
    )

    assert run.status is SyncRunStatus.COMPLETED
    assert run.proposed_count == 1
    observations = observation_repository.list_observations_for_project(conn, project_id)
    assert len(observations) == 1
    proposals = proposed_mutation_repository.list_pending_for_project(conn, project_id)
    assert len(proposals) == 1


# --- idempotency / bounded-window rescanning --------------------------------


def test_rerun_of_the_same_bounded_window_produces_no_change_only(conn, project_id, evidence_dir):
    source = _make_calendar_source(conn, project_id)
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync")

    first = _sync(conn, project_id, source.id, api, evidence_dir)
    assert first.parsed_count == 1

    second = _sync(conn, project_id, source.id, api, evidence_dir)
    assert second.unchanged_count == 1
    assert second.parsed_count == 0
    assert second.discovered_count == 1

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert len(artifacts) == 1


def test_rerun_updates_content_when_event_updated_timestamp_changes(
    conn, project_id, evidence_dir
):
    source = _make_calendar_source(conn, project_id)
    api = FakeCalendarApi()
    api.add_event(
        "evt1", summary="Acme Rollout sync", description="v1",
        updated="2026-05-01T00:00:00.000Z",
    )

    first = _sync(conn, project_id, source.id, api, evidence_dir)
    assert first.parsed_count == 1

    api.update_event("evt1", description="v2", updated="2026-05-02T00:00:00.000Z")
    second = _sync(conn, project_id, source.id, api, evidence_dir)
    assert second.parsed_count == 1
    assert second.unchanged_count == 0

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    contents = evidence_repository.list_contents_for_artifact(
        conn, project_id, artifacts[0].id
    )
    assert len(contents) == 2  # both versions retained (FR-007)


# --- cancelled/vanished events -------------------------------------------


def test_cancelled_event_marks_availability_without_erasing_evidence(
    conn, project_id, evidence_dir
):
    source = _make_calendar_source(conn, project_id)
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync", description="Notes.")
    first = _sync(conn, project_id, source.id, api, evidence_dir)
    assert first.status is SyncRunStatus.COMPLETED

    api.update_event("evt1", status="cancelled")
    second = _sync(conn, project_id, source.id, api, evidence_dir)
    assert second.status is SyncRunStatus.COMPLETED

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert artifacts[0].availability is ArtifactAvailability.DELETED_EXTERNAL
    # Evidence itself is retained, not erased.
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert "Notes." in content.normalized_text


def test_event_that_falls_out_of_rules_is_marked_unavailable(conn, project_id, evidence_dir):
    source = _make_calendar_source(conn, project_id)
    api = FakeCalendarApi()
    api.add_event("evt1", summary="Acme Rollout sync")
    first = _sync(conn, project_id, source.id, api, evidence_dir)
    assert first.status is SyncRunStatus.COMPLETED

    api.update_event("evt1", summary="Renamed — no longer relevant")
    second = _sync(conn, project_id, source.id, api, evidence_dir)
    assert second.status is SyncRunStatus.COMPLETED

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert artifacts[0].availability is ArtifactAvailability.DELETED_EXTERNAL


# --- partial sync / cross-connector isolation -------------------------------


def test_calendar_sync_failure_does_not_affect_other_sources(
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
        "id": "root", "mimeType": "application/vnd.google-apps.folder", "trashed": False,
    }
    drive_api.add_folder("root", [])
    drive_run = sync_service.sync_source(
        conn, project_id, drive_source.id,
        connector=DriveConnector(access_token="t", folder_id="root", http_transport=drive_api),
        evidence_dir=tmp_path, chunk_target_chars=4000, chunk_overlap_ratio=0.0,
    )
    assert drive_run.status is SyncRunStatus.COMPLETED

    calendar_source = _make_calendar_source(conn, project_id)
    calendar_api = FakeCalendarApi()
    calendar_api.fail_next("list", times=99, status_code=401)
    calendar_run = _sync(conn, project_id, calendar_source.id, calendar_api, evidence_dir)
    assert calendar_run.status is SyncRunStatus.FAILED

    refreshed_drive = sources_repository.get_source(conn, project_id, drive_source.id)
    assert refreshed_drive.health_status is SourceHealthStatus.HEALTHY


# --- cross-project isolation / authorization --------------------------------


def test_calendar_evidence_is_isolated_per_project(conn, evidence_dir):
    project_a = create_project(conn, ProjectCreateInput(name="Project A", objective="A")).id
    project_b = create_project(conn, ProjectCreateInput(name="Project B", objective="B")).id

    source_a = _make_calendar_source(
        conn, project_a, boundary={"project_name_terms": ["Alpha"]}
    )
    api_a = FakeCalendarApi()
    api_a.add_event("evt1", summary="Alpha sync", description="Secret sentinel Alpha content.")
    _sync(
        conn, project_a, source_a.id, api_a, evidence_dir,
        rules=CalendarMatchRules(project_name_terms=("Alpha",)),
    )

    source_b = _make_calendar_source(
        conn, project_b, boundary={"project_name_terms": ["Beta"]}
    )
    api_b = FakeCalendarApi()
    api_b.add_event("evt2", summary="Beta sync", description="Unrelated Beta content.")
    _sync(
        conn, project_b, source_b.id, api_b, evidence_dir,
        rules=CalendarMatchRules(project_name_terms=("Beta",)),
    )

    artifacts_b = evidence_repository.list_artifacts_for_source(conn, project_b, source_b.id)
    content_b = evidence_repository.get_content(conn, project_b, artifacts_b[0].current_content_id)
    assert "Secret sentinel Alpha" not in content_b.normalized_text

    artifacts_a = evidence_repository.list_artifacts_for_source(conn, project_a, source_a.id)
    assert evidence_repository.get_artifact(conn, project_b, artifacts_a[0].id) is None


# --- secret/content redaction (Section 16; FR-032) --------------------------


def test_calendar_sync_never_logs_the_access_token_or_description(
    conn, project_id, evidence_dir, caplog
):
    import logging

    source = _make_calendar_source(conn, project_id)
    api = FakeCalendarApi()
    secret_description = "SECRET-TEST-DESCRIPTION-do-not-log-this-value"
    secret_token = "FAKE-TEST-ACCESS-TOKEN-do-not-log-this-value"
    api.add_event("evt1", summary="Acme Rollout sync", description=secret_description)

    connector = CalendarConnector(
        access_token=secret_token, rules=CalendarMatchRules(project_name_terms=("Acme Rollout",)),
        http_transport=api, sleep=lambda _s: None, rand=lambda: 0.0,
    )
    with caplog.at_level(logging.DEBUG):
        sync_service.sync_source(
            conn, project_id, source.id, connector=connector, evidence_dir=evidence_dir,
            chunk_target_chars=4000, chunk_overlap_ratio=0.0,
            get_or_create_artifact_fn=calendar_ingestion.get_or_create_calendar_artifact,
            store_artifact_fn=calendar_ingestion.store_calendar_artifact,
        )

    for record in caplog.records:
        message = record.getMessage()
        as_dict = str(record.__dict__)
        for secret in (secret_token, secret_description):
            assert secret not in message
            assert secret not in as_dict


# --- sync_calendar_project: credential + rule wiring ------------------------


@pytest.fixture
def credential_service(tmp_path):
    store = CredentialStore(credentials_dir=tmp_path / "creds", prefer_keyring=False)
    return CredentialService(store)


def test_sync_calendar_project_marks_reauth_required_when_refresh_fails(
    conn, project_id, evidence_dir, credential_service, monkeypatch
):
    source = _make_calendar_source(conn, project_id)
    credential_service.connect(conn, project_id, source.id, secret="refresh-token")

    def fake_exchange(*args, **kwargs):
        from project_context.credentials.service import TokenRefreshError

        raise TokenRefreshError("revoked")

    monkeypatch.setattr(sync_service, "exchange_refresh_token", fake_exchange)

    run = sync_service.sync_calendar_project(
        conn, project_id, source.id, credential_service=credential_service,
        google_client_id="cid", google_client_secret="csecret",
        evidence_dir=evidence_dir, chunk_target_chars=4000, chunk_overlap_ratio=0.0,
    )
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.REAUTH_REQUIRED


def test_sync_calendar_project_fails_gracefully_on_invalid_regex(
    conn, project_id, evidence_dir, credential_service, monkeypatch
):
    source = _make_calendar_source(
        conn, project_id, boundary={"include_regex": "(unclosed"}
    )
    credential_service.connect(conn, project_id, source.id, secret="refresh-token")
    # Isolate this test to the invalid-regex failure path — never a
    # real token exchange over the network (Section 15).
    monkeypatch.setattr(sync_service, "exchange_refresh_token", lambda *a, **k: "fake-access-token")

    run = sync_service.sync_calendar_project(
        conn, project_id, source.id, credential_service=credential_service,
        google_client_id="cid", google_client_secret="csecret",
        evidence_dir=evidence_dir, chunk_target_chars=4000, chunk_overlap_ratio=0.0,
    )
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.DEGRADED
