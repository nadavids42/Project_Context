"""Tests for Gmail sync orchestration (Section 11.3; FR-004, FR-028,
FR-031; Prompt 11): `project_context.services.sync.sync_source` driven
by a `GmailConnector`/`FakeGmailApi`, plus `sync_gmail_project`'s
watermark/credential wiring and `gmail_ingestion`'s quote-boundary
chunking. Uses `FakeGmailApi` and `FakeLLMProvider` — nothing here
touches the network."""

from __future__ import annotations

import json

import pytest

from fixtures.fake_gmail_api import FakeGmailApi
from project_context.connectors.gmail import GmailConnector
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
from project_context.domain.evidence import ArtifactType
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.sources import SourceHealthStatus, SourceKind
from project_context.domain.sync import SyncRunStatus
from project_context.llm.schemas import EvidenceSpan, ExtractedObservation, ExtractionBatch
from project_context.services import gmail_ingestion
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


def _make_gmail_source(conn, project_id, *, label="Acme", query=None):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.GMAIL, display_name="Gmail label/query"
    )
    return sources_repository.update_boundary(
        conn,
        project_id,
        source.id,
        boundary_json=json.dumps({"label": label, "query": query}),
    )


def _connector(api, *, label="Acme", query=None):
    return GmailConnector(
        access_token="fake-token",
        label=label,
        query=query,
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
    label="Acme",
    query=None,
):
    return sync_service.sync_source(
        conn,
        project_id,
        source_id,
        connector=_connector(api, label=label, query=query),
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
        extraction_provider=extraction_provider,
        get_or_create_artifact_fn=gmail_ingestion.get_or_create_gmail_artifact,
        store_artifact_fn=gmail_ingestion.store_gmail_artifact,
    )


# --- validation ----------------------------------------------------------


def test_sync_source_raises_for_missing_boundary(conn, project_id, tmp_path):
    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.GMAIL, display_name="Gmail label/query"
    )
    with pytest.raises(sync_service.SourceNotConfiguredError):
        sync_service.sync_source(
            conn,
            project_id,
            source.id,
            connector=_connector(FakeGmailApi()),
            evidence_dir=tmp_path,
            chunk_target_chars=4000,
            chunk_overlap_ratio=0.0,
            get_or_create_artifact_fn=gmail_ingestion.get_or_create_gmail_artifact,
            store_artifact_fn=gmail_ingestion.store_gmail_artifact,
        )


def test_sync_source_marks_reauth_required_on_401(conn, project_id, tmp_path):
    source = _make_gmail_source(conn, project_id)
    api = FakeGmailApi()
    api.fail_next("list", times=99, status_code=401)
    run = _sync(conn, project_id, source.id, api, tmp_path)
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.REAUTH_REQUIRED


def test_sync_source_degrades_on_permission_error(conn, project_id, tmp_path):
    source = _make_gmail_source(conn, project_id)
    api = FakeGmailApi()
    api.fail_next("list", times=99, status_code=403)
    run = _sync(conn, project_id, source.id, api, tmp_path)
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.DEGRADED


def test_sync_source_degrades_on_malformed_query(conn, project_id, tmp_path):
    source = _make_gmail_source(conn, project_id, label=None, query="((( bad")
    api = FakeGmailApi()
    api.fail_next("list", times=99, status_code=400)
    run = _sync(conn, project_id, source.id, api, tmp_path, label=None, query="((( bad")
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.DEGRADED


# --- basic ingestion (no extraction provider) ------------------------------


def test_sync_ingests_a_new_message_without_a_provider(conn, project_id, evidence_dir):
    source = _make_gmail_source(conn, project_id)
    api = FakeGmailApi()
    api.add_message("m1", subject="Kickoff", plain_text="Let's meet Friday.")

    run = _sync(conn, project_id, source.id, api, evidence_dir)

    assert run.status is SyncRunStatus.COMPLETED
    assert run.discovered_count == 1
    assert run.parsed_count == 1
    assert run.failed_count == 0

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type is ArtifactType.EMAIL
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert "Let's meet Friday." in content.normalized_text
    assert "Subject: Kickoff" in content.normalized_text

    assert observation_repository.list_observations_for_project(conn, project_id) == []


def test_sync_marks_partial_when_one_message_fails_but_another_succeeds(
    conn, project_id, evidence_dir
):
    source = _make_gmail_source(conn, project_id)
    api = FakeGmailApi()
    api.add_message("m1", plain_text="Good message.")
    api.add_message("m2", malformed_plain_data=True)

    run = _sync(conn, project_id, source.id, api, evidence_dir)

    assert run.status is SyncRunStatus.PARTIAL
    assert run.discovered_count == 2
    assert run.parsed_count == 1
    assert run.failed_count == 1

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert len(artifacts) == 2  # both artifact identities recorded; only one has content


def test_gmail_sync_failure_does_not_affect_other_sources(conn, project_id, evidence_dir, tmp_path):
    """FR-031 / Prompt 11: "Connector failure must produce partial sync
    status without affecting Drive/manual sources." A Gmail 401 must
    not touch a project's independently configured Drive source."""
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

    gmail_source = _make_gmail_source(conn, project_id)
    gmail_api = FakeGmailApi()
    gmail_api.fail_next("list", times=99, status_code=401)
    gmail_run = _sync(conn, project_id, gmail_source.id, gmail_api, evidence_dir)
    assert gmail_run.status is SyncRunStatus.FAILED

    refreshed_drive = sources_repository.get_source(conn, project_id, drive_source.id)
    assert refreshed_drive.health_status is SourceHealthStatus.HEALTHY


# --- extraction + reconciliation wiring ------------------------------------


def _extraction_batch_for(
    text: str, chunk_id: str, *, char_start: int, char_end: int
) -> ExtractionBatch:
    span = EvidenceSpan(chunk_id=chunk_id, char_start=char_start, char_end=char_end, quote=text)
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
    """A fake provider that reads the real `chunk_id` *and* the target
    sentence's real offset out of whatever chunk `extract_content`
    actually sent — a Gmail chunk always has the normalized header
    block ahead of the body, so (unlike a bare manual/Drive text
    fixture) the target sentence is not at chunk offset 0."""

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
        batch = _extraction_batch_for(
            self._text, chunk_id_match.group(1), char_start=char_start, char_end=char_end
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


def test_sync_with_provider_extracts_and_reconciles(conn, project_id, evidence_dir):
    source = _make_gmail_source(conn, project_id)
    api = FakeGmailApi()
    text = "Priya will send the report by Friday."
    api.add_message("m1", plain_text=text)

    run = _sync(
        conn,
        project_id,
        source.id,
        api,
        evidence_dir,
        extraction_provider=_ReactiveProvider(text),
    )

    assert run.status is SyncRunStatus.COMPLETED
    assert run.proposed_count == 1
    observations = observation_repository.list_observations_for_project(conn, project_id)
    assert len(observations) == 1
    proposals = proposed_mutation_repository.list_pending_for_project(conn, project_id)
    assert len(proposals) == 1


def test_ordinary_sync_makes_zero_llm_calls_even_with_api_key_set(
    conn, project_id, evidence_dir, monkeypatch
):
    """The privacy/sync correction: Sync Project must never construct an
    LLM provider or call an LLM, regardless of `OPENAI_API_KEY` — see
    the equivalent Drive test in `test_services_sync.py` for the full
    rationale."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-not-a-real-key")

    def _fail_if_constructed(self, *args, **kwargs):
        raise AssertionError("sync_source must never construct an LLM provider")

    from project_context.llm.openai_provider import OpenAIProvider

    monkeypatch.setattr(OpenAIProvider, "__init__", _fail_if_constructed)

    source = _make_gmail_source(conn, project_id)
    api = FakeGmailApi()
    api.add_message("m1", plain_text="Priya will send the report by Friday.")

    # extraction_provider defaults to None
    run = _sync(conn, project_id, source.id, api, evidence_dir)

    assert run.status is SyncRunStatus.COMPLETED
    assert run.extracted_count == 0
    assert observation_repository.list_observations_for_project(conn, project_id) == []


def test_quoted_history_is_not_sent_to_extraction(conn, project_id, evidence_dir):
    """Prompt 11: "Avoid re-extracting an entire historical thread for
    each new reply." The old quoted commitment must not appear in any
    chunk fed to the extraction provider, even though it remains in the
    stored evidence text."""
    source = _make_gmail_source(conn, project_id)
    api = FakeGmailApi()
    new_reply = "Sounds good, I will follow up Monday."
    quoted_old_commitment = "Priya will send the report by Friday."
    body = (
        f"{new_reply}\n\nOn Mon, Aug 10, 2026 at 9:00 AM Priya <priya@example.com> wrote:\n"
        f"> {quoted_old_commitment}"
    )
    api.add_message("m1", plain_text=body)

    class _RecordingProvider:
        def __init__(self):
            self.seen_inputs: list[str] = []

        def generate_structured(self, *, task, system, input_text, response_model, config):
            from project_context.llm.provider import StructuredResult, estimate_cost_usd

            self.seen_inputs.append(input_text)
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

    provider = _RecordingProvider()
    run = _sync(conn, project_id, source.id, api, evidence_dir, extraction_provider=provider)
    assert run.status is SyncRunStatus.COMPLETED

    assert any(new_reply in seen for seen in provider.seen_inputs)
    assert not any(quoted_old_commitment in seen for seen in provider.seen_inputs)

    # But the complete text, including the quoted history, is retained
    # as the stored evidence (Prompt 11: "retaining the complete
    # imported normalized body as evidence/versioned content").
    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    content = evidence_repository.get_content(conn, project_id, artifacts[0].current_content_id)
    assert quoted_old_commitment in content.normalized_text
    assert new_reply in content.normalized_text


# --- idempotency: unchanged reruns -----------------------------------------


def test_rerun_with_no_new_message_produces_no_change_only(conn, project_id, evidence_dir):
    source = _make_gmail_source(conn, project_id)
    api = FakeGmailApi()
    api.add_message("m1", plain_text="Hello.")

    first = _sync(conn, project_id, source.id, api, evidence_dir)
    assert first.parsed_count == 1

    second = _sync(conn, project_id, source.id, api, evidence_dir)
    assert second.unchanged_count == 1
    assert second.parsed_count == 0
    assert second.discovered_count == 1

    artifacts = evidence_repository.list_artifacts_for_source(conn, project_id, source.id)
    assert len(artifacts) == 1


def test_rerun_within_overlap_window_creates_no_duplicate_observations(
    conn, project_id, evidence_dir
):
    """FR-009 / Prompt 11: "A repeated sync with no new message must
    create no content/observation/proposal duplicates" — simulated here
    by a message still appearing in a second sync's list results (as it
    would inside the 48-hour overlap window) without ever changing."""
    source = _make_gmail_source(conn, project_id)
    api = FakeGmailApi()
    text = "Priya will send the report by Friday."
    api.add_message("m1", plain_text=text)

    first = _sync(
        conn,
        project_id,
        source.id,
        api,
        evidence_dir,
        extraction_provider=_ReactiveProvider(text),
    )
    assert first.proposed_count == 1

    # Second sync: same message still listed (overlap window), nothing
    # new — must not create a second observation/proposal.
    second = _sync(
        conn,
        project_id,
        source.id,
        api,
        evidence_dir,
        extraction_provider=_ReactiveProvider(text),
    )
    assert second.unchanged_count == 1
    assert second.proposed_count == 0

    observations = observation_repository.list_observations_for_project(conn, project_id)
    assert len(observations) == 1
    proposals = proposed_mutation_repository.list_pending_for_project(conn, project_id)
    assert len(proposals) == 1


# --- cross-project isolation -------------------------------------------


def test_gmail_evidence_is_isolated_per_project(conn, evidence_dir):
    project_a = create_project(conn, ProjectCreateInput(name="Project A", objective="A")).id
    project_b = create_project(conn, ProjectCreateInput(name="Project B", objective="B")).id

    source_a = _make_gmail_source(conn, project_a)
    api_a = FakeGmailApi()
    api_a.add_message("m1", plain_text="Secret sentinel Alpha content.")
    _sync(conn, project_a, source_a.id, api_a, evidence_dir)

    source_b = _make_gmail_source(conn, project_b)
    api_b = FakeGmailApi()
    api_b.add_message("m2", plain_text="Unrelated Beta content.")
    _sync(conn, project_b, source_b.id, api_b, evidence_dir)

    artifacts_b = evidence_repository.list_artifacts_for_source(conn, project_b, source_b.id)
    assert len(artifacts_b) == 1
    content_b = evidence_repository.get_content(conn, project_b, artifacts_b[0].current_content_id)
    assert "Secret sentinel Alpha" not in content_b.normalized_text

    # A foreign artifact ID must not resolve under the wrong project.
    artifacts_a = evidence_repository.list_artifacts_for_source(conn, project_a, source_a.id)
    assert evidence_repository.get_artifact(conn, project_b, artifacts_a[0].id) is None


# --- secret/content redaction (Section 16; FR-032) --------------------------


def test_gmail_sync_never_logs_the_access_token_subject_or_body(
    conn, project_id, evidence_dir, caplog
):
    import logging

    source = _make_gmail_source(conn, project_id)
    api = FakeGmailApi()
    secret_body = "SECRET-TEST-EMAIL-BODY-do-not-log-this-value"
    secret_subject = "SECRET-TEST-SUBJECT-do-not-log-this-value"
    secret_token = "FAKE-TEST-ACCESS-TOKEN-do-not-log-this-value"
    api.add_message("m1", subject=secret_subject, plain_text=secret_body)

    connector = GmailConnector(
        access_token=secret_token,
        label="Acme",
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
            get_or_create_artifact_fn=gmail_ingestion.get_or_create_gmail_artifact,
            store_artifact_fn=gmail_ingestion.store_gmail_artifact,
        )

    for record in caplog.records:
        message = record.getMessage()
        as_dict = str(record.__dict__)
        for secret in (secret_token, secret_body, secret_subject):
            assert secret not in message
            assert secret not in as_dict


# --- sync_gmail_project: watermark + credential wiring ----------------------


@pytest.fixture
def credential_service(tmp_path):
    store = CredentialStore(credentials_dir=tmp_path / "creds", prefer_keyring=False)
    return CredentialService(store)


def test_sync_gmail_project_marks_reauth_required_when_refresh_fails(
    conn, project_id, evidence_dir, credential_service, monkeypatch
):
    source = _make_gmail_source(conn, project_id)
    credential_service.connect(conn, project_id, source.id, secret="refresh-token")

    def fake_exchange(*args, **kwargs):
        from project_context.credentials.service import TokenRefreshError

        raise TokenRefreshError("revoked")

    monkeypatch.setattr(sync_service, "exchange_refresh_token", fake_exchange)

    run = sync_service.sync_gmail_project(
        conn,
        project_id,
        source.id,
        credential_service=credential_service,
        google_client_id="cid",
        google_client_secret="csecret",
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    assert run.status is SyncRunStatus.FAILED
    refreshed = sources_repository.get_source(conn, project_id, source.id)
    assert refreshed.health_status is SourceHealthStatus.REAUTH_REQUIRED


def test_gmail_since_date_applies_a_48_hour_overlap():
    from datetime import UTC, datetime

    from project_context.services.sync import _gmail_since_date

    last_success = "2026-08-20T10:00:00.000000Z"
    since = _gmail_since_date(last_success)
    parsed_last_success = datetime(2026, 8, 20, 10, 0, 0, tzinfo=UTC)
    expected_watermark = parsed_last_success - sync_service._GMAIL_OVERLAP
    assert since == expected_watermark.strftime("%Y/%m/%d")


def test_gmail_since_date_is_none_on_first_sync():
    from project_context.services.sync import _gmail_since_date

    assert _gmail_since_date(None) is None


# --- gmail_ingestion: quote-boundary chunking (unit-level) ------------------


def test_store_gmail_artifact_excludes_quoted_history_from_chunks(conn, project_id, evidence_dir):
    from project_context.connectors.protocol import ArtifactMetadata, RawArtifact
    from project_context.domain.evidence import ArtifactType, AssignmentMethod, EvidenceSourceType

    source = _make_gmail_source(conn, project_id)
    new_reply = "Sounds good, I will follow up Monday."
    quoted = "Priya will send the report by Friday."
    body_text = (
        f"{new_reply}\n\nOn Mon, Aug 10, 2026 at 9:00 AM Priya <priya@example.com> wrote:\n"
        f"> {quoted}"
    )
    full_text = (
        "Subject: Re: Report\nFrom: bob@example.com\nDate: (unknown date)\n"
        f"Message-ID: m1\nThread-ID: t1\n\n{body_text}"
    )

    metadata = ArtifactMetadata(
        external_id="m1",
        title="Re: Report",
        artifact_type=ArtifactType.EMAIL,
        source_type=EvidenceSourceType.EMAIL,
        version_marker="gmail:m1",
    )
    artifact = evidence_repository.insert_artifact(
        conn,
        project_id,
        source.id,
        external_id="m1",
        artifact_type=ArtifactType.EMAIL,
        title="Re: Report",
        author="bob@example.com",
        occurred_at=None,
        external_url=None,
        source_type=EvidenceSourceType.EMAIL,
        assignment_method=AssignmentMethod.BOUNDARY_MATCH,
    )
    raw = RawArtifact(
        metadata=metadata,
        data=full_text.encode("utf-8"),
        mime_type="text/plain",
        filename="gmail-m1.txt",
    )

    result = gmail_ingestion.store_gmail_artifact(
        conn,
        project_id,
        artifact,
        raw,
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )

    assert quoted not in "".join(chunk.text for chunk in result.chunks)
    assert new_reply in "".join(chunk.text for chunk in result.chunks)
    assert quoted in result.content.normalized_text
