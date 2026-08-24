"""Secret/log content scan (Prompt 16: "Add a secret/log scan for API
keys, OAuth tokens, authorization headers, email/source bodies,
evidence quotes, and synthetic sentinel content").

Each category already has direct, narrower coverage elsewhere:

- API keys / OAuth tokens / authorization headers:
  `tests/unit/test_connectors_http.py::test_retry_never_logs_the_authorization_header_value`,
  `tests/unit/test_services_sync.py::test_sync_never_logs_the_access_token`,
  `tests/unit/test_credentials_service.py`, `tests/unit/test_credentials_store.py`.
- Static, committed-file secret patterns:
  `tests/unit/test_secret_scan.py`.

This module is the piece those don't cover: **runtime log records**
produced by the real extraction -> reconciliation -> review -> brief
pipeline, scanned for source/evidence body text and a synthetic
sentinel planted specifically to be leak-checkable — never a real
secret, since this suite runs no network/LLM calls (Section 15: "Tests
must use a fake/mock provider and never call the network")."""

from __future__ import annotations

import hashlib
import itertools
import logging

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import evidence_repository, sources_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.projects import ProjectCreateInput
from project_context.llm.provider import StructuredResult, estimate_cost_usd
from project_context.llm.schemas import (
    BriefComposition,
    BriefSectionOutput,
    EvidenceSpan,
    ExtractedObservation,
    ObservationKind,
)
from project_context.services import review as review_service
from project_context.services.briefs import generate_current_project_brief
from project_context.services.observations import persist_observation
from project_context.services.projects import create_project
from project_context.services.reconciliation import reconcile_observation

_counter = itertools.count()

#: A synthetic marker planted in evidence text specifically so this
#: suite can assert it never appears in a log record — never a real
#: secret shape (contrast tests/unit/test_secret_scan.py's real-shaped
#: patterns), just a distinctive, greppable token.
_SENTINEL = "LOGSCANSENTINEL99182"
#: The full "source body" — stands in for an email/meeting-note body;
#: this exact string is what should be readable in the evidence viewer
#: but never in a log line.
_SOURCE_BODY = (
    f"Confidential kickoff notes ({_SENTINEL}): the launch date moved to "
    "September 4th per the client's legal team, budget details attached."
)


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def project_id(conn):
    return create_project(
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
    ).id


def _ingest_and_extract(conn, project_id):
    """Ingest `_SOURCE_BODY` as one manual-text artifact, extract one
    real observation from it (via a fake LLM provider whose own
    `input_text`/response are never logged by design — this asserts
    that), reconcile, and accept it — returns nothing; callers inspect
    `caplog` afterward."""
    n = next(_counter)
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = evidence_repository.insert_artifact(
        conn,
        project_id,
        source.id,
        external_id=f"text:{project_id}:{n}",
        artifact_type=ArtifactType.MANUAL_TEXT,
        title="Kickoff notes",
        author=None,
        occurred_at=None,
        external_url=None,
        source_type=None,
    )
    content = evidence_repository.insert_content(
        conn,
        project_id,
        artifact.id,
        sha256=hashlib.sha256(f"{n}:{_SOURCE_BODY}".encode()).hexdigest(),
        raw_storage_path=None,
        mime_type="text/plain",
        byte_size=len(_SOURCE_BODY),
        normalized_text=_SOURCE_BODY,
        parser_name="text",
        parser_version="1",
        parse_status=ParseStatus.PARSED,
        location_map=None,
        original_filename=None,
    )
    evidence_repository.set_current_content(conn, project_id, artifact.id, content.id)
    spec = ChunkSpec(
        ordinal=0,
        text=_SOURCE_BODY,
        char_start=0,
        char_end=len(_SOURCE_BODY),
        section_path=None,
        sha256=hashlib.sha256(f"{n}:{_SOURCE_BODY}:c".encode()).hexdigest(),
        token_estimate=len(_SOURCE_BODY) // 4,
    )
    (chunk,) = evidence_repository.insert_chunks(conn, project_id, content.id, [spec])
    extracted = ExtractedObservation(
        kind=ObservationKind.COMMITMENT,
        subject="Launch date commitment",
        statement=_SOURCE_BODY,
        owner_name=None,
        date_value="2026-09-04",
        date_text="September 4th",
        explicitness="explicit",
        evidence=[
            EvidenceSpan(
                chunk_id=chunk.id, char_start=0, char_end=len(_SOURCE_BODY), quote=_SOURCE_BODY
            )
        ],
    )
    observation, _links, _created = persist_observation(
        conn, project_id, content_id=content.id, chunk_id=chunk.id, extracted=extracted
    )
    result = reconcile_observation(conn, project_id, observation.id)
    review_service.accept_proposal(conn, project_id, result.proposal.id)


def _assert_records_are_clean(records: list[logging.LogRecord]) -> None:
    for record in records:
        message = record.getMessage()
        assert _SENTINEL not in message, f"sentinel leaked into log message: {message!r}"
        assert _SOURCE_BODY not in message, f"source body leaked into log message: {message!r}"
        record_dict = str(record.__dict__)
        assert _SENTINEL not in record_dict, f"sentinel leaked into log record: {record_dict!r}"
        assert _SOURCE_BODY not in record_dict, (
            f"source body leaked into log record: {record_dict!r}"
        )


def test_ingest_extract_reconcile_review_never_logs_the_source_body_or_sentinel(
    caplog, conn, project_id
):
    with caplog.at_level(logging.DEBUG):
        _ingest_and_extract(conn, project_id)

    _assert_records_are_clean(caplog.records)


def test_brief_generation_never_logs_the_source_body_or_sentinel(caplog, conn, project_id):
    _ingest_and_extract(conn, project_id)

    class _Provider:
        def generate_structured(self, *, task, system, input_text, response_model, config):
            return StructuredResult(
                parsed=BriefComposition(sections=[BriefSectionOutput(section="risks", claims=[])]),
                provider="fake",
                model=config.model,
                request_id=None,
                input_tokens=10,
                output_tokens=5,
                latency_ms=1,
                estimated_cost_usd=estimate_cost_usd(config.model, 10, 5),
            )

    with caplog.at_level(logging.DEBUG):
        generate_current_project_brief(conn, project_id, provider=_Provider())

    _assert_records_are_clean(caplog.records)


def test_extraction_failure_path_never_logs_the_source_body(caplog, conn, project_id):
    """Section 16: "Exception reporting must redact... evidence quotes"
    — including on the *failure* path, not only the happy path."""
    from project_context.llm.provider import LLMClientError
    from project_context.services.extraction import extract_content

    n = next(_counter)
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = evidence_repository.insert_artifact(
        conn,
        project_id,
        source.id,
        external_id=f"text:{project_id}:fail:{n}",
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
        sha256=hashlib.sha256(f"fail:{n}:{_SOURCE_BODY}".encode()).hexdigest(),
        raw_storage_path=None,
        mime_type="text/plain",
        byte_size=len(_SOURCE_BODY),
        normalized_text=_SOURCE_BODY,
        parser_name="text",
        parser_version="1",
        parse_status=ParseStatus.PARSED,
        location_map=None,
        original_filename=None,
    )
    evidence_repository.set_current_content(conn, project_id, artifact.id, content.id)
    spec = ChunkSpec(
        ordinal=0,
        text=_SOURCE_BODY,
        char_start=0,
        char_end=len(_SOURCE_BODY),
        section_path=None,
        sha256=hashlib.sha256(f"fail:{n}:{_SOURCE_BODY}:c".encode()).hexdigest(),
        token_estimate=len(_SOURCE_BODY) // 4,
    )
    evidence_repository.insert_chunks(conn, project_id, content.id, [spec])

    class _RaisingProvider:
        def generate_structured(self, **kwargs):
            raise LLMClientError("synthetic client error for this test")

    with caplog.at_level(logging.DEBUG):
        extract_content(conn, project_id, content.id, provider=_RaisingProvider(), model="x")

    _assert_records_are_clean(caplog.records)
