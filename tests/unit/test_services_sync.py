"""Tests for `project_context.services.sync`: manual `Sync Project`
orchestration — discovery, change detection, per-item isolation,
partial sync/resume, idempotent reprocessing, extraction/reconciliation
wiring, deletion detection, and cross-project misuse (Section 8;
FR-009, FR-027, FR-031; Prompt 10). Uses `FakeDriveApi` and
`FakeLLMProvider` — nothing here touches the network."""

from __future__ import annotations

import json

import pytest

from fixtures.fake_drive_api import FakeDriveApi
from project_context.connectors.drive import DriveConnector
from project_context.credentials.service import CredentialService, TokenRefreshError
from project_context.credentials.store import CredentialStore
from project_context.db import (
    evidence_repository,
    observation_repository,
    proposed_mutation_repository,
    sources_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import ArtifactAvailability
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.sources import SourceHealthStatus, SourceKind
from project_context.domain.sync import SyncRunStatus
from project_context.llm.schemas import EvidenceSpan, ExtractedObservation, ExtractionBatch
from project_context.services import sync as sync_service
from project_context.services.projects import create_project

_ROOT = "root-folder"


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


def _make_drive_source(conn, project_id, *, folder_id=_ROOT):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    return sources_repository.update_boundary(
        conn, project_id, source.id, boundary_json=json.dumps({"folder_id": folder_id})
    )


def _api(*, folder_id=_ROOT) -> FakeDriveApi:
    """A `FakeDriveApi` with the root folder's own metadata already
    registered — every `sync_source` call validates the boundary
    folder itself (a `files.get`) before ever listing its children."""
    api = FakeDriveApi()
    api.files[folder_id] = {
        "id": folder_id,
        "mimeType": "application/vnd.google-apps.folder",
        "trashed": False,
    }
    return api


def _connector(api, *, folder_id=_ROOT):
    return DriveConnector(
        access_token="fake-token",
        folder_id=folder_id,
        http_transport=api,
        sleep=lambda _s: None,
        rand=lambda: 0.0,
    )


def _sync(
    conn,
    project_id,
    source_id,
    api,
    evidence_dir,
    *,
    extraction_provider=None,
    folder_id=_ROOT,
):
    return sync_service.sync_source(
        conn,
        project_id,
        source_id,
        connector=_connector(api, folder_id=folder_id),
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
        extraction_provider=extraction_provider,
    )


# --- validation --------------------------------------------------------


def test_sync_source_raises_for_missing_source(conn, project_id, tmp_path):
    with pytest.raises(sync_service.SourceNotConfiguredError):
        sync_service.sync_source(
            conn,
            project_id,
            "does-not-exist",
            connector=_connector(FakeDriveApi()),
            evidence_dir=tmp_path,
            chunk_target_chars=4000,
            chunk_overlap_ratio=0.0,
        )


def test_sync_source_raises_for_missing_boundary(conn, project_id, tmp_path):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    with pytest.raises(sync_service.SourceNotConfiguredError):
        sync_service.sync_source(
            conn,
            project_id,
            source.id,
            connector=_connector(FakeDriveApi()),
            evidence_dir=tmp_path,
            chunk_target_chars=4000,
            chunk_overlap_ratio=0.0,
        )


def test_sync_source_raises_for_disabled_source(conn, project_id, tmp_path):
    source = _make_drive_source(conn, project_id)
    sources_repository.set_enabled(conn, project_id, source.id, enabled=False)
    with pytest.raises(sync_service.SourceNotConfiguredError):
        sync_service.sync_source(
            conn,
            project_id,
            source.id,
            connector=_connector(FakeDriveApi()),
            evidence_dir=tmp_path,
            chunk_target_chars=4000,
            chunk_overlap_ratio=0.0,
        )


def test_sync_source_marks_reauth_required_when_folder_check_401s(conn, project_id, tmp_path):
    source = _make_drive_source(conn, project_id)
    api = _api()
    api.fail_next(f"get:{_ROOT}", times=99, status_code=401)
    run = sync_service.sync_source(
        conn,
        project_id,
        source.id,
        connector=_connector(api),
        evidence_dir=tmp_path,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.REAUTH_REQUIRED


# --- basic ingestion (no extraction provider) ----------------------------


def test_sync_ingests_a_new_text_file_without_a_provider(conn, project_id, evidence_dir):
    source = _make_drive_source(conn, project_id)
    api = _api()
    api.add_folder(
        _ROOT,
        [api.add_file("f1", name="notes.txt", mime_type="text/plain", content=b"Some notes.")],
    )

    run = sync_service.sync_source(
        conn,
        project_id,
        source.id,
        connector=_connector(api),
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )

    assert run.status is SyncRunStatus.COMPLETED
    assert run.discovered_count == 1
    assert run.parsed_count == 1
    assert run.failed_count == 0

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert len(artifacts) == 1
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert content.normalized_text == "Some notes."
    assert content.version_key == "2026-08-01T00:00:00.000Z"  # FakeDriveApi's default modified_time

    assert observation_repository.list_observations_for_project(conn, project_id) == []


# --- extraction + reconciliation wiring ----------------------------------


def _extraction_batch_for(text: str, chunk_id: str) -> ExtractionBatch:
    span = EvidenceSpan(chunk_id=chunk_id, char_start=0, char_end=len(text), quote=text)
    observation = ExtractedObservation(
        kind="commitment",
        subject="Send the report",
        statement=text,
        owner_name=None,
        explicitness="explicit",
        evidence=[span],
    )
    return ExtractionBatch(observations=[observation], source_contains_no_material_updates=False)


class _ReactiveProvider:
    """A fake provider that reads the real `chunk_id` out of whatever
    prompt `extract_content` actually sends — chunk IDs are freshly
    generated per sync, so a plain queued fake can't know them ahead of
    time (the same pattern used throughout the brief-generation tests)."""

    def __init__(self, text: str):
        self._text = text
        self.calls: list[str] = []

    def generate_structured(self, *, task, system, input_text, response_model, config):
        import re

        from project_context.llm.provider import StructuredResult, estimate_cost_usd

        match = re.search(r'chunk_id="([^"]+)"', input_text)
        batch = _extraction_batch_for(self._text, match.group(1))
        self.calls.append(input_text)
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


def test_sync_with_provider_extracts_and_reconciles(conn, project_id, evidence_dir):
    source = _make_drive_source(conn, project_id)
    api = _api()
    text = "Priya will send the report by Friday."
    api.add_folder(
        _ROOT, [api.add_file("f1", name="notes.txt", mime_type="text/plain", content=text.encode())]
    )

    run = _sync(
        conn, project_id, source.id, api, evidence_dir, extraction_provider=_ReactiveProvider(text)
    )

    assert run.status is SyncRunStatus.COMPLETED
    assert run.parsed_count == 1
    assert run.proposed_count == 1

    observations = observation_repository.list_observations_for_project(conn, project_id)
    assert len(observations) == 1
    proposals = proposed_mutation_repository.list_pending_for_project(conn, project_id)
    assert len(proposals) == 1


def test_ordinary_sync_makes_zero_llm_calls_even_with_api_key_set(
    conn, project_id, evidence_dir, monkeypatch
):
    """The privacy/sync correction: Sync Project must never construct an
    LLM provider or call an LLM, regardless of `OPENAI_API_KEY`. This
    generic-orchestration test proves it at the `sync_source` level
    (`extraction_provider` left at its `None` default, exactly as every
    UI Sync Project button now calls it) — no LLM provider class is ever
    instantiated."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-not-a-real-key")

    def _fail_if_constructed(self, *args, **kwargs):
        raise AssertionError("sync_source must never construct an LLM provider")

    from project_context.llm.openai_provider import OpenAIProvider

    monkeypatch.setattr(OpenAIProvider, "__init__", _fail_if_constructed)

    source = _make_drive_source(conn, project_id)
    api = _api()
    text = "Priya will send the report by Friday."
    api.add_folder(
        _ROOT, [api.add_file("f1", name="notes.txt", mime_type="text/plain", content=text.encode())]
    )

    # extraction_provider defaults to None
    run = _sync(conn, project_id, source.id, api, evidence_dir)

    assert run.status is SyncRunStatus.COMPLETED
    assert run.extracted_count == 0
    assert observation_repository.list_observations_for_project(conn, project_id) == []


# --- idempotency: unchanged reruns, changed content --------------------


def test_rerun_with_unchanged_content_produces_no_change_only(conn, project_id, evidence_dir):
    source = _make_drive_source(conn, project_id)
    api = _api()
    api.add_folder(
        _ROOT, [api.add_file("f1", name="notes.txt", mime_type="text/plain", content=b"Hello.")]
    )

    first = _sync(conn, project_id, source.id, api, evidence_dir)
    assert first.parsed_count == 1

    second = _sync(conn, project_id, source.id, api, evidence_dir)
    assert second.unchanged_count == 1
    assert second.parsed_count == 0
    assert second.discovered_count == 1

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert len(artifacts) == 1
    contents = evidence_repository.list_contents_for_artifact(conn, project_id, artifacts[0].id)
    assert len(contents) == 1  # no duplicate version


def test_changed_modified_time_creates_a_new_content_version(conn, project_id, evidence_dir):
    source = _make_drive_source(conn, project_id)
    api = _api()
    metadata = api.add_file("f1", name="notes.txt", mime_type="text/plain", content=b"Version one.")
    api.add_folder(_ROOT, [metadata])

    _sync(conn, project_id, source.id, api, evidence_dir)

    api.files["f1"]["modifiedTime"] = "2026-09-01T00:00:00.000Z"
    api.folders[_ROOT][0]["modifiedTime"] = "2026-09-01T00:00:00.000Z"
    api.file_bytes["f1"] = b"Version two, changed."

    second = _sync(conn, project_id, source.id, api, evidence_dir)
    assert second.parsed_count == 1
    assert second.unchanged_count == 0

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    contents = evidence_repository.list_contents_for_artifact(conn, project_id, artifacts[0].id)
    assert len(contents) == 2
    current = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert current.normalized_text == "Version two, changed."


def test_same_artifact_processed_twice_is_fully_idempotent_end_to_end(
    conn, project_id, evidence_dir
):
    source = _make_drive_source(conn, project_id)
    api = _api()
    text = "Priya will send the report by Friday."
    api.add_folder(
        _ROOT, [api.add_file("f1", name="notes.txt", mime_type="text/plain", content=text.encode())]
    )

    provider1 = _ReactiveProvider(text)
    provider2 = _ReactiveProvider(text)
    _sync(conn, project_id, source.id, api, evidence_dir, extraction_provider=provider1)
    _sync(conn, project_id, source.id, api, evidence_dir, extraction_provider=provider2)

    observations = observation_repository.list_observations_for_project(conn, project_id)
    assert len(observations) == 1
    proposals = proposed_mutation_repository.list_pending_for_project(conn, project_id)
    assert len(proposals) == 1
    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    contents = evidence_repository.list_contents_for_artifact(conn, project_id, artifacts[0].id)
    assert len(contents) == 1


# --- per-item isolation, partial sync -------------------------------------


def test_one_failed_item_does_not_roll_back_a_successful_sibling(conn, project_id, evidence_dir):
    source = _make_drive_source(conn, project_id)
    api = _api()
    api.add_folder(
        _ROOT,
        [
            api.add_file("good", name="good.txt", mime_type="text/plain", content=b"Fine."),
            # "bad" has no content registered -> 404 on fetch.
            api.add_file("bad", name="bad.txt", mime_type="text/plain"),
        ],
    )

    run = _sync(conn, project_id, source.id, api, evidence_dir)

    assert run.status is SyncRunStatus.PARTIAL
    assert run.discovered_count == 2
    assert run.failed_count == 1
    assert run.parsed_count == 1

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    good = [a for a in artifacts if a.external_id == "good"]
    assert len(good) == 1
    assert good[0].current_content_id is not None


def test_partial_sync_persists_a_checkpoint_and_a_later_call_resumes(
    conn, project_id, evidence_dir
):
    source = _make_drive_source(conn, project_id)
    api = _api()
    api.add_folder(
        _ROOT,
        [
            api.add_file("f1", name="a.txt", mime_type="text/plain", content=b"A."),
            api.add_file("f2", name="b.txt", mime_type="text/plain", content=b"B."),
        ],
    )
    api.set_page_size(_ROOT, 1)
    # The second files.list page (continuing folder listing) fails hard
    # and persistently — enough consecutive 5xx responses to exhaust
    # `request_with_retry`'s own bounded retry, so this is a genuine
    # connector-level failure, not one it transparently retries away.
    original_request = api.request
    failing = {"active": True}

    def flaky_request(method, url, **kwargs):
        has_page_token = kwargs.get("params", {}).get("pageToken")
        if failing["active"] and url.endswith("/files") and has_page_token:
            from project_context.connectors.http import HttpResponse

            return HttpResponse(status_code=500, headers={}, content=b"{}")
        return original_request(method, url, **kwargs)

    api.request = flaky_request  # type: ignore[method-assign]

    connector = DriveConnector(
        access_token="t",
        folder_id=_ROOT,
        http_transport=api,
        sleep=lambda _s: None,
        rand=lambda: 0.0,
    )
    run1 = sync_service.sync_source(
        conn,
        project_id,
        source.id,
        connector=connector,
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    assert run1.discovered_count == 1  # only the first page's item before the failure

    refreshed_source = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed_source.last_cursor is not None

    # The transport recovers, and a fresh connector resumes.
    failing["active"] = False
    connector2 = DriveConnector(
        access_token="t",
        folder_id=_ROOT,
        http_transport=api,
        sleep=lambda _s: None,
        rand=lambda: 0.0,
    )
    run2 = sync_service.sync_source(
        conn,
        project_id,
        source.id,
        connector=connector2,
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    assert run2.discovered_count == 1  # only the second item, resumed rather than restarted
    assert run2.status is SyncRunStatus.COMPLETED

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert {a.external_id for a in artifacts} == {"f1", "f2"}


# --- trashed/deleted detection --------------------------------------------


def test_a_file_removed_from_the_folder_is_marked_deleted_external_not_erased(
    conn, project_id, evidence_dir
):
    source = _make_drive_source(conn, project_id)
    api = _api()
    metadata = api.add_file("f1", name="a.txt", mime_type="text/plain", content=b"Content.")
    api.add_folder(_ROOT, [metadata])
    _sync(conn, project_id, source.id, api, evidence_dir)

    # File is trashed/removed: no longer listed in the folder, and a
    # direct metadata GET reports trashed=true.
    api.folders[_ROOT] = []
    api.files["f1"]["trashed"] = True

    run = _sync(conn, project_id, source.id, api, evidence_dir)
    assert run.status is SyncRunStatus.COMPLETED

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert artifacts[0].availability is ArtifactAvailability.DELETED_EXTERNAL
    # Evidence itself is retained, not erased.
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert content.normalized_text == "Content."


# --- secret redaction (Section 16; FR-032) --------------------------------


def test_sync_never_logs_the_access_token(conn, project_id, evidence_dir, caplog):
    import logging

    source = _make_drive_source(conn, project_id)
    api = _api()
    api.add_folder(
        _ROOT, [api.add_file("f1", name="a.txt", mime_type="text/plain", content=b"Hi.")]
    )
    secret_token = "FAKE-TEST-ACCESS-TOKEN-do-not-log-this-value"

    connector = DriveConnector(
        access_token=secret_token,
        folder_id=_ROOT,
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
        )

    for record in caplog.records:
        assert secret_token not in record.getMessage()
        assert secret_token not in str(record.__dict__)


# --- cross-project misuse --------------------------------------------------


def test_syncing_a_source_id_from_a_different_project_is_rejected(conn, project_id, evidence_dir):
    other_project_id = create_project(
        conn, ProjectCreateInput(name="Other", objective="Other work")
    ).id
    source = _make_drive_source(conn, project_id)

    with pytest.raises(sync_service.SourceNotConfiguredError):
        sync_service.sync_source(
            conn,
            other_project_id,
            source.id,
            connector=_connector(FakeDriveApi()),
            evidence_dir=evidence_dir,
            chunk_target_chars=4000,
            chunk_overlap_ratio=0.0,
        )


# --- sync_drive_project (credential wiring) --------------------------------


def test_sync_drive_project_marks_reauth_required_on_a_revoked_refresh_token(
    conn, project_id, tmp_path, monkeypatch
):
    source = _make_drive_source(conn, project_id)
    store = CredentialStore(credentials_dir=tmp_path / "creds", prefer_keyring=False)
    credential_service = CredentialService(store)
    credential_service.connect(conn, project_id, source.id, secret="refresh-token")

    def fake_exchange(refresh_token, *, client_id, client_secret):
        raise TokenRefreshError("revoked")

    monkeypatch.setattr(sync_service, "exchange_refresh_token", fake_exchange)

    run = sync_service.sync_drive_project(
        conn,
        project_id,
        source.id,
        credential_service=credential_service,
        google_client_id="cid",
        google_client_secret="csecret",
        evidence_dir=tmp_path,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.REAUTH_REQUIRED
