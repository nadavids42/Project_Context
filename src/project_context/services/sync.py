"""Manual `Sync Project` orchestration (Section 8's ingestion pipeline;
FR-009, FR-027, FR-031; Prompt 10).

`sync_source` is written against the generic `Connector` protocol so
adding Gmail/Calendar/Fathom later is additive; `sync_drive_project` is
the Drive-specific convenience entry point the UI calls, which mints a
fresh access token from the stored refresh token before constructing a
`DriveConnector`.

Every per-artifact step (fetch, store, extract) is isolated: one bad
artifact records a `failed` `sync_items` row and the loop continues —
"Independent source-item failures must not roll back successful items."
Storage and extraction are separately committed transactions, so an
extraction failure can never undo an already-stored, already-committed
piece of evidence.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from project_context.config import DEFAULT_OPENAI_MODEL
from project_context.connectors import google_oauth
from project_context.connectors.drive import DriveConnector
from project_context.connectors.errors import ConnectorError
from project_context.connectors.gmail import GmailConnector
from project_context.connectors.google_oauth import exchange_refresh_token
from project_context.connectors.http import RequestsHttpTransport
from project_context.connectors.protocol import ArtifactMetadata, Connector, ConnectorHealthStatus
from project_context.credentials.service import CredentialService
from project_context.db import evidence_repository, sources_repository, sync_repository
from project_context.db.connection import transaction
from project_context.domain.evidence import ArtifactAvailability, SourceArtifact
from project_context.domain.sources import Source, SourceHealthStatus, SourceKind
from project_context.domain.sync import (
    SyncErrorClass,
    SyncItemStage,
    SyncItemStatus,
    SyncRun,
    SyncRunStatus,
)
from project_context.llm.provider import LLMProvider, LLMProviderError
from project_context.observability import get_logger
from project_context.services import drive_ingestion, gmail_ingestion
from project_context.services import observations as observations_service
from project_context.services import reconciliation as reconciliation_service
from project_context.services.extraction import ExtractionStatus, extract_content

logger = get_logger(__name__)

#: A hard safety bound on pages-per-call, independent of any one
#: connector's own pagination — protects against a pathological/cyclic
#: folder tree or an unexpectedly long message list rather than a
#: realistic limit for any real project.
_MAX_PAGES_PER_SYNC = 2000

#: Section 11.3 / Prompt 11: "Use a stored last-success watermark with a
#: 48-hour overlap." Gmail's own `after:` search operator only accepts
#: day granularity, so the overlap is applied before truncating to a
#: date — see `_gmail_since_date`.
_GMAIL_OVERLAP = timedelta(hours=48)


class SourceNotConfiguredError(ValueError):
    """Raised when `sync_source`/`sync_drive_project` is asked to sync a
    source that does not exist, is disabled, or has no boundary
    (FR-002: "Sync refuses enabled connectors without a boundary")."""


@dataclass
class SyncCounts:
    """The UI's required progress/result counters (Prompt 10: "discovered,
    unchanged, downloaded, parsed, extracted, failed, unassigned")."""

    discovered: int = 0
    unchanged: int = 0
    downloaded: int = 0
    parsed: int = 0
    extracted: int = 0
    failed: int = 0
    #: Always 0 in this prompt — the optional intake-folder/unassigned
    #: routing feature (Section 11.2) was deliberately not built: this
    #: connector supports exactly one folder boundary per source, so
    #: every discovered artifact is unambiguously assigned to this
    #: project by construction. See the Prompt 10 report for why.
    unassigned: int = 0
    proposed: int = 0


# ---------------------------------------------------------------------------
# Drive-specific entry point.
# ---------------------------------------------------------------------------


def mint_drive_access_token(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    *,
    credential_service: CredentialService,
    google_client_id: str,
    google_client_secret: str,
) -> str | None:
    """Exchange the stored refresh token for a fresh, short-lived Drive
    access token through the one locked `CredentialService` (Section
    16). Returns `None` — after moving the source to `reauth_required`
    — if the refresh token is missing, invalid, or revoked; a caller
    with a `sources` row in hand can check `source.health_status`
    afterward rather than needing this function to raise. Shared by
    `sync_drive_project` and the Sources & Settings page's folder
    preview, so both mint a token exactly the same way."""
    access_token_holder: dict[str, str] = {}

    def _refresh_and_capture(current_refresh_token: str) -> str:
        access_token_holder["access_token"] = exchange_refresh_token(
            current_refresh_token, client_id=google_client_id, client_secret=google_client_secret
        )
        return current_refresh_token  # Google does not rotate the refresh token itself

    updated_source = credential_service.refresh(
        conn, project_id, source_id, refresh_fn=_refresh_and_capture
    )
    if updated_source.health_status is SourceHealthStatus.REAUTH_REQUIRED:
        return None
    return access_token_holder.get("access_token")


def sync_drive_project(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    *,
    credential_service: CredentialService,
    google_client_id: str,
    google_client_secret: str,
    evidence_dir: Path,
    chunk_target_chars: int,
    chunk_overlap_ratio: float,
    extraction_provider: LLMProvider | None = None,
    extraction_model: str = DEFAULT_OPENAI_MODEL,
) -> SyncRun:
    """Mint a fresh Drive access token from the stored refresh token,
    build a `DriveConnector`, and run the generic `sync_source`
    orchestration. If the refresh token is invalid/revoked, the source
    moves to `reauth_required` and a `failed` sync run is recorded — no
    other source is affected (Section 8)."""
    source = sources_repository.get_source(conn, project_id, source_id)
    if source is None:
        raise SourceNotConfiguredError(f"source {source_id!r} not found in project {project_id!r}")

    access_token = mint_drive_access_token(
        conn, project_id, source_id, credential_service=credential_service,
        google_client_id=google_client_id, google_client_secret=google_client_secret,
    )
    if access_token is None:
        run = sync_repository.insert_sync_run(conn, project_id)
        return sync_repository.finalize_sync_run(
            conn, project_id, run.id, status=SyncRunStatus.FAILED,
            discovered_count=0, unchanged_count=0, parsed_count=0, failed_count=1,
            proposed_count=0, needs_assignment_count=0,
        )

    boundary = _load_boundary(source)
    folder_id = boundary.get("folder_id", "") if boundary else ""
    connector = DriveConnector(
        access_token=access_token, folder_id=folder_id, http_transport=RequestsHttpTransport(),
    )
    return sync_source(
        conn, project_id, source_id, connector=connector, evidence_dir=evidence_dir,
        chunk_target_chars=chunk_target_chars, chunk_overlap_ratio=chunk_overlap_ratio,
        extraction_provider=extraction_provider, extraction_model=extraction_model,
    )


# ---------------------------------------------------------------------------
# Gmail-specific entry point (Section 11.3; Prompt 11).
# ---------------------------------------------------------------------------


def mint_gmail_access_token(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    *,
    credential_service: CredentialService,
    google_client_id: str,
    google_client_secret: str,
) -> str | None:
    """Same shape as `mint_drive_access_token`, requesting
    `GMAIL_READONLY_SCOPE` instead — see that function's docstring for
    the shared refresh/locking behavior."""
    access_token_holder: dict[str, str] = {}

    def _refresh_and_capture(current_refresh_token: str) -> str:
        access_token_holder["access_token"] = exchange_refresh_token(
            current_refresh_token, client_id=google_client_id, client_secret=google_client_secret,
            scopes=[google_oauth.GMAIL_READONLY_SCOPE],
        )
        return current_refresh_token

    updated_source = credential_service.refresh(
        conn, project_id, source_id, refresh_fn=_refresh_and_capture
    )
    if updated_source.health_status is SourceHealthStatus.REAUTH_REQUIRED:
        return None
    return access_token_holder.get("access_token")


def _gmail_since_date(last_success_at: str | None) -> str | None:
    """The configured query's `after:` watermark: `last_success_at`
    minus a 48-hour overlap, truncated to a day (Gmail's own `after:`
    search granularity — Section 11.3). `None` (no lower bound) on a
    first sync, or if the stored timestamp is unexpectedly unparseable
    — a missing watermark means "scan everything the boundary matches,"
    never a sync failure."""
    if not last_success_at:
        return None
    try:
        parsed = datetime.fromisoformat(last_success_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    watermark = parsed.astimezone(UTC) - _GMAIL_OVERLAP
    return watermark.strftime("%Y/%m/%d")


def sync_gmail_project(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    *,
    credential_service: CredentialService,
    google_client_id: str,
    google_client_secret: str,
    evidence_dir: Path,
    chunk_target_chars: int,
    chunk_overlap_ratio: float,
    extraction_provider: LLMProvider | None = None,
    extraction_model: str = DEFAULT_OPENAI_MODEL,
) -> SyncRun:
    """Mint a fresh Gmail access token from the stored refresh token,
    build a `GmailConnector` bounded to the configured label/query plus
    a 48-hour-overlap watermark derived from this source's own last
    successful sync, and run the generic `sync_source` orchestration
    with the Gmail-specific storage functions (Section 11.3; FR-028).
    If the refresh token is invalid/revoked, the source moves to
    `reauth_required` and a `failed` sync run is recorded — no other
    source, including a project's Drive source, is affected (Section 8:
    "Independent source-item failures must not roll back successful
    items"; Prompt 11: "Connector failure must produce partial sync
    status without affecting Drive/manual sources")."""
    source = sources_repository.get_source(conn, project_id, source_id)
    if source is None:
        raise SourceNotConfiguredError(f"source {source_id!r} not found in project {project_id!r}")

    access_token = mint_gmail_access_token(
        conn, project_id, source_id, credential_service=credential_service,
        google_client_id=google_client_id, google_client_secret=google_client_secret,
    )
    if access_token is None:
        run = sync_repository.insert_sync_run(conn, project_id)
        return sync_repository.finalize_sync_run(
            conn, project_id, run.id, status=SyncRunStatus.FAILED,
            discovered_count=0, unchanged_count=0, parsed_count=0, failed_count=1,
            proposed_count=0, needs_assignment_count=0,
        )

    boundary = _load_boundary(source) or {}
    connector = GmailConnector(
        access_token=access_token,
        label=boundary.get("label"),
        query=boundary.get("query"),
        since_date=_gmail_since_date(source.last_success_at),
        http_transport=RequestsHttpTransport(),
    )
    return sync_source(
        conn, project_id, source_id, connector=connector, evidence_dir=evidence_dir,
        chunk_target_chars=chunk_target_chars, chunk_overlap_ratio=chunk_overlap_ratio,
        extraction_provider=extraction_provider, extraction_model=extraction_model,
        get_or_create_artifact_fn=gmail_ingestion.get_or_create_gmail_artifact,
        store_artifact_fn=gmail_ingestion.store_gmail_artifact,
    )


# ---------------------------------------------------------------------------
# Generic orchestration.
# ---------------------------------------------------------------------------


#: The two connector-specific storage steps `_process_one_artifact`
#: needs — everything else in `sync_source` is already generic over the
#: `Connector` protocol. Defaults to the Drive functions so every
#: existing Drive call site (including every already-written test) is
#: unaffected; `sync_gmail_project` passes the Gmail equivalents.
GetOrCreateArtifactFn = Callable[[sqlite3.Connection, str, str, ArtifactMetadata], SourceArtifact]
StoreArtifactFn = Callable[..., drive_ingestion.DriveStoreResult]


def _boundary_is_configured(kind: SourceKind, boundary: dict | None) -> bool:
    """Each connector kind has its own shape of "a boundary is actually
    set" — Drive needs a non-empty `folder_id`; Gmail needs a non-empty
    `label` and/or `query` (Prompt 11: "accepts one Gmail label, one
    constrained query, or a documented combination")."""
    if not boundary:
        return False
    if kind is SourceKind.DRIVE:
        return bool(boundary.get("folder_id"))
    if kind is SourceKind.GMAIL:
        return bool(boundary.get("label")) or bool(boundary.get("query"))
    return True


def _load_boundary(source: Source) -> dict | None:
    return json.loads(source.boundary_json) if source.boundary_json else None


def _load_checkpoint(source: Source) -> dict | None:
    """A non-null `last_cursor` means the previous sync did not
    complete a full pass — resume from exactly where it stopped
    (Section 8: "Preserve a per-item checkpoint; rerun only failed/new
    items"). A completed sync always clears `last_cursor` back to
    null, so the next call starts a fresh full pass."""
    return json.loads(source.last_cursor) if source.last_cursor else None


def sync_source(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    *,
    connector: Connector,
    evidence_dir: Path,
    chunk_target_chars: int,
    chunk_overlap_ratio: float,
    extraction_provider: LLMProvider | None = None,
    extraction_model: str = DEFAULT_OPENAI_MODEL,
    get_or_create_artifact_fn: GetOrCreateArtifactFn = drive_ingestion.get_or_create_drive_artifact,
    store_artifact_fn: StoreArtifactFn = drive_ingestion.store_raw_artifact,
) -> SyncRun:
    source = sources_repository.get_source(conn, project_id, source_id)
    if source is None:
        raise SourceNotConfiguredError(f"source {source_id!r} not found in project {project_id!r}")
    if not source.enabled:
        raise SourceNotConfiguredError(f"source {source_id!r} is disabled")
    boundary = _load_boundary(source)
    if not _boundary_is_configured(source.kind, boundary):
        raise SourceNotConfiguredError(f"source {source_id!r} has no configured boundary")

    health = connector.validate_config()
    if health.status is not ConnectorHealthStatus.OK:
        _apply_connector_health(conn, project_id, source_id, health.status)
        run = sync_repository.insert_sync_run(conn, project_id)
        return sync_repository.finalize_sync_run(
            conn, project_id, run.id, status=SyncRunStatus.FAILED,
            discovered_count=0, unchanged_count=0, parsed_count=0, failed_count=1,
            proposed_count=0, needs_assignment_count=0,
        )

    run = sync_repository.insert_sync_run(conn, project_id)
    sources_repository.update_health(
        conn, project_id, source_id, health_status=SourceHealthStatus.SYNCING
    )

    counts = SyncCounts()
    seen_external_ids: set[str] = set()
    checkpoint = _load_checkpoint(source)
    started_from_root = checkpoint is None
    pages = 0

    while pages < _MAX_PAGES_PER_SYNC:
        pages += 1
        try:
            page = connector.discover(checkpoint)
        except ConnectorError as exc:
            counts.failed += 1
            _record_item(
                conn, project_id, run.id, source_id, None, f"connector:{source_id}",
                stage=SyncItemStage.FAILED, start=time.monotonic(),
                error_class=exc.error_class, safe_message=exc.safe_message,
            )
            break

        for metadata in page.artifacts:
            counts.discovered += 1
            seen_external_ids.add(metadata.external_id)
            _process_one_artifact(
                conn, project_id, run.id, source_id, connector, metadata, counts,
                evidence_dir=evidence_dir, chunk_target_chars=chunk_target_chars,
                chunk_overlap_ratio=chunk_overlap_ratio,
                extraction_provider=extraction_provider, extraction_model=extraction_model,
                get_or_create_artifact_fn=get_or_create_artifact_fn,
                store_artifact_fn=store_artifact_fn,
            )

        checkpoint = page.next_checkpoint
        sources_repository.update_last_cursor(
            conn, project_id, source_id,
            last_cursor=json.dumps(checkpoint) if checkpoint is not None else None,
        )
        if checkpoint is None:
            break

    if started_from_root and checkpoint is None:
        # A full, uninterrupted pass completed — safe to detect
        # vanished items. A resumed (partial-continuation) pass is
        # deliberately skipped here even if it reaches the end, since
        # its `seen_external_ids` would be missing whatever was seen
        # before the interruption, which would misreport still-present
        # items as deleted. Deletion detection simply waits for the
        # next uninterrupted full sync — documented in the Prompt 10
        # report as an accepted limitation, not a silent gap.
        _detect_vanished_items(
            conn, project_id, run.id, source_id, connector, seen_external_ids, counts
        )

    counts.proposed = _reconcile_new_observations(conn, project_id)

    status = _final_status(counts)
    finalized = sync_repository.finalize_sync_run(
        conn, project_id, run.id, status=status,
        discovered_count=counts.discovered, unchanged_count=counts.unchanged,
        downloaded_count=counts.downloaded, parsed_count=counts.parsed,
        extracted_count=counts.extracted, failed_count=counts.failed,
        proposed_count=counts.proposed, needs_assignment_count=counts.unassigned,
    )
    if status is not SyncRunStatus.FAILED:
        sources_repository.update_last_success(
            conn, project_id, source_id, last_success_at=finalized.ended_at or finalized.updated_at
        )
    sources_repository.update_health(
        conn, project_id, source_id,
        health_status=(
            SourceHealthStatus.HEALTHY if status is SyncRunStatus.COMPLETED
            else SourceHealthStatus.DEGRADED
        ),
    )
    return finalized


def _final_status(counts: SyncCounts) -> SyncRunStatus:
    if counts.failed == 0:
        return SyncRunStatus.COMPLETED
    if counts.failed >= counts.discovered:
        return SyncRunStatus.FAILED
    return SyncRunStatus.PARTIAL


def _apply_connector_health(
    conn: sqlite3.Connection, project_id: str, source_id: str, status: ConnectorHealthStatus
) -> None:
    mapping = {
        ConnectorHealthStatus.AUTH_ERROR: (SourceHealthStatus.REAUTH_REQUIRED, "auth"),
        ConnectorHealthStatus.PERMISSION_ERROR: (SourceHealthStatus.DEGRADED, "permission"),
        ConnectorHealthStatus.CONFIG_ERROR: (SourceHealthStatus.DEGRADED, "assignment"),
        ConnectorHealthStatus.ERROR: (SourceHealthStatus.DEGRADED, "provider"),
    }
    health_status, error_code = mapping.get(status, (SourceHealthStatus.DEGRADED, "provider"))
    sources_repository.update_health(
        conn, project_id, source_id, health_status=health_status, last_error_code=error_code
    )


def _record_item(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    source_id: str,
    artifact_id: str | None,
    external_id: str,
    *,
    stage: SyncItemStage,
    start: float,
    error_class: SyncErrorClass | None = None,
    safe_message: str | None = None,
) -> None:
    duration_ms = int((time.monotonic() - start) * 1000)
    status = SyncItemStatus.ERROR if stage is SyncItemStage.FAILED else SyncItemStatus.OK
    with transaction(conn):
        sync_repository.insert_sync_item(
            conn, project_id, sync_run_id=run_id, source_id=source_id, artifact_id=artifact_id,
            external_id=external_id, stage=stage, status=status, error_class=error_class,
            safe_error_message=safe_message, duration_ms=duration_ms,
        )


def _process_one_artifact(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    source_id: str,
    connector: Connector,
    metadata: ArtifactMetadata,
    counts: SyncCounts,
    *,
    evidence_dir: Path,
    chunk_target_chars: int,
    chunk_overlap_ratio: float,
    extraction_provider: LLMProvider | None,
    extraction_model: str,
    get_or_create_artifact_fn: GetOrCreateArtifactFn = drive_ingestion.get_or_create_drive_artifact,
    store_artifact_fn: StoreArtifactFn = drive_ingestion.store_raw_artifact,
) -> None:
    start = time.monotonic()
    artifact_id: str | None = None
    try:
        with transaction(conn):
            artifact = get_or_create_artifact_fn(conn, project_id, source_id, metadata)
        artifact_id = artifact.id

        # `is_unchanged` compares the artifact's current content
        # version_key against this discovery's version_marker — it has
        # no connector-specific behavior, so it is shared unmodified by
        # both Drive and Gmail.
        if drive_ingestion.is_unchanged(conn, project_id, artifact, metadata):
            counts.unchanged += 1
            _record_item(
                conn, project_id, run_id, source_id, artifact_id, metadata.external_id,
                stage=SyncItemStage.UNCHANGED, start=start,
            )
            return

        raw = connector.fetch(metadata)

        with transaction(conn):
            result = store_artifact_fn(
                conn, project_id, artifact, raw, evidence_dir=evidence_dir,
                chunk_target_chars=chunk_target_chars, chunk_overlap_ratio=chunk_overlap_ratio,
            )

        counts.downloaded += 1
        stage = SyncItemStage.DOWNLOADED
        if result.created_new_version:
            counts.parsed += 1
            stage = SyncItemStage.PARSED

            if extraction_provider is not None and result.chunks:
                extracted, stage = _extract_and_persist(
                    conn, project_id, result.content.id,
                    provider=extraction_provider, model=extraction_model,
                )
                if extracted:
                    counts.extracted += 1

        _record_item(
            conn, project_id, run_id, source_id, artifact_id, metadata.external_id,
            stage=stage, start=start,
        )
    except ConnectorError as exc:
        counts.failed += 1
        _record_item(
            conn, project_id, run_id, source_id, artifact_id, metadata.external_id,
            stage=SyncItemStage.FAILED, start=start,
            error_class=exc.error_class, safe_message=exc.safe_message,
        )
    except Exception as exc:  # noqa: BLE001 - one bad artifact must not abort the run
        counts.failed += 1
        logger.info(
            "sync_item_unexpected_error",
            extra={
                "project_id": project_id, "source_id": source_id,
                "error_class": type(exc).__name__,
            },
        )
        _record_item(
            conn, project_id, run_id, source_id, artifact_id, metadata.external_id,
            stage=SyncItemStage.FAILED, start=start,
            error_class=SyncErrorClass.DATABASE,
            safe_message="unexpected error while processing this item",
        )


def _extract_and_persist(
    conn: sqlite3.Connection,
    project_id: str,
    content_id: str,
    *,
    provider: LLMProvider,
    model: str,
) -> tuple[bool, SyncItemStage]:
    """Returns `(reached_extracted_stage, stage)`. A provider failure
    raises `LLMProviderError` so the caller records this one item as
    failed — the content/chunks it already stored (a separate, already-
    committed transaction) are unaffected."""
    run_result = extract_content(conn, project_id, content_id, provider=provider, model=model)
    if run_result.status is ExtractionStatus.COMPLETED:
        for observation in run_result.accepted:
            observations_service.persist_observation(
                conn, project_id, content_id=content_id,
                chunk_id=observation.evidence[0].chunk_id, extracted=observation,
                model_id=run_result.model, prompt_version=run_result.prompt_version,
                schema_version=run_result.schema_version,
            )
        return True, SyncItemStage.EXTRACTED
    if run_result.status is ExtractionStatus.NO_MATERIAL_CONTENT:
        return False, SyncItemStage.PARSED  # honest: nothing to extract, not a failure
    raise LLMProviderError(run_result.safe_error or "extraction failed")


def _detect_vanished_items(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    source_id: str,
    connector: Connector,
    seen_external_ids: set[str],
    counts: SyncCounts,
) -> None:
    check_availability = getattr(connector, "check_availability", None)
    if check_availability is None:
        return  # not a connector that supports this (only Drive does, today)
    known = evidence_repository.list_artifacts_for_source(conn, project_id, source_id)
    for artifact in known:
        if artifact.external_id in seen_external_ids:
            continue
        if artifact.availability is not ArtifactAvailability.AVAILABLE:
            continue  # already marked unavailable by an earlier sync
        start = time.monotonic()
        try:
            availability = check_availability(artifact.external_id)
        except ConnectorError as exc:
            counts.failed += 1
            _record_item(
                conn, project_id, run_id, source_id, artifact.id, artifact.external_id,
                stage=SyncItemStage.FAILED, start=start,
                error_class=exc.error_class, safe_message=exc.safe_message,
            )
            continue
        if availability is not ArtifactAvailability.AVAILABLE:
            with transaction(conn):
                evidence_repository.update_availability(
                    conn, project_id, artifact.id, availability=availability
                )
            _record_item(
                conn, project_id, run_id, source_id, artifact.id, artifact.external_id,
                stage=SyncItemStage.SKIPPED, start=start,
            )


def _reconcile_new_observations(conn: sqlite3.Connection, project_id: str) -> int:
    results = reconciliation_service.reconcile_pending_observations(conn, project_id)
    return sum(1 for result in results if result.created)
