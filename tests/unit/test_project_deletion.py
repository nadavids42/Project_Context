"""Tests for `project_context.services.project_deletion` (Section 16,
"Deletion and retention"): preview counts, exact-confirmation
requirement, full-purge row/FTS/content-byte/credential cleanup, and
cross-project isolation of all of the above.

Seeds a realistic project through the same real service pipeline other
unit tests use (observation -> reconciliation -> review -> ledger, plus
a correction, a brief, a sync run, a stakeholder, and a connected
credential) so this exercises the actual delete path against every
project-owned table, not a hand-picked subset.
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import UTC, datetime

import pytest

from project_context.chunking import ChunkSpec
from project_context.credentials.service import CredentialService
from project_context.credentials.store import CredentialStore
from project_context.db import (
    people_repository,
    sources_repository,
    sync_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import (
    ArtifactType,
    EvidenceSourceType,
    ManualTextInput,
    ParseStatus,
)
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.review import ProposalEdit
from project_context.domain.sources import SourceKind
from project_context.domain.sync import SyncItemStage
from project_context.llm.provider import StructuredResult, estimate_cost_usd
from project_context.llm.schemas import (
    BriefComposition,
    BriefSectionOutput,
    EvidenceSpan,
    ExtractedObservation,
    ObservationKind,
)
from project_context.services import evidence as evidence_service
from project_context.services import review as review_service
from project_context.services.briefs import generate_current_project_brief
from project_context.services.observations import persist_observation
from project_context.services.project_deletion import (
    DeletionConfirmationError,
    delete_project,
    preview_delete_project,
)
from project_context.services.projects import ProjectNotFoundError, create_project
from project_context.services.reconciliation import reconcile_observation

_counter = itertools.count()


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
def credential_service(tmp_path):
    store = CredentialStore(credentials_dir=tmp_path / "credentials", prefer_keyring=False)
    return CredentialService(store)


def _make_project(conn, name="Acme Rollout"):
    return create_project(conn, ProjectCreateInput(name=name, objective="Ship the pilot"))


def _make_observation(conn, project_id, *, statement, subject, owner_text="Priya"):
    from project_context.db import evidence_repository

    n = next(_counter)
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
        subject=subject,
        statement=statement,
        owner_name=owner_text,
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
    return observation


def _seed_full_project(conn, evidence_dir, credential_service, *, name="Acme Rollout") -> str:
    """Populate every project-owned table for one project via the real
    service pipeline: observation -> reconciliation -> review (with a
    correction) -> ledger; a stakeholder; a sync run/item; a connected
    Drive credential; and a generated brief."""
    project = _make_project(conn, name=name)
    project_id = project.id

    observation = _make_observation(
        conn, project_id, subject="Send the report", statement="Priya will send the report."
    )
    result = reconcile_observation(conn, project_id, observation.id)
    person = people_repository.create_person(conn, display_name="Priya")
    people_repository.upsert_project_person(conn, project_id, person.id, role="Lead")
    review_service.edit_and_accept_proposal(
        conn,
        project_id,
        result.proposal.id,
        ProposalEdit(canonical_title="Send the final report", owner_person_id=person.id),
        reason_code="wording",
    )

    source = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    )
    credential_service.connect(conn, project_id, source.id, secret="fake-refresh-token")
    conn.commit()

    run = sync_repository.insert_sync_run(conn, project_id)
    sync_repository.insert_sync_item(
        conn,
        project_id,
        sync_run_id=run.id,
        source_id=source.id,
        artifact_id=None,
        external_id="ext-1",
        stage=SyncItemStage.DISCOVERED,
    )
    conn.commit()

    def handler(input_text):
        return BriefComposition(sections=[BriefSectionOutput(section="risks", claims=[])])

    class _Provider:
        def generate_structured(self, *, task, system, input_text, response_model, config):
            return StructuredResult(
                parsed=handler(input_text),
                provider="fake",
                model=config.model,
                request_id=None,
                input_tokens=10,
                output_tokens=5,
                latency_ms=1,
                estimated_cost_usd=estimate_cost_usd(config.model, 10, 5),
            )

    generate_current_project_brief(conn, project_id, provider=_Provider())
    return project_id


def _table_counts_for_project(conn, project_id) -> dict[str, int]:
    tables = [
        "sources",
        "source_artifacts",
        "source_contents",
        "source_chunks",
        "sync_runs",
        "observations",
        "ledger_items",
        "ledger_versions",
        "evidence_links",
        "proposed_mutations",
        "reviews",
        "corrections",
        "project_people",
        "generated_briefs",
        "brief_claims",
        "audit_entries",
    ]
    counts = {
        table: conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE project_id = ?",
            (project_id,),  # noqa: S608
        ).fetchone()[0]
        for table in tables
    }
    counts["sync_items"] = conn.execute(
        "SELECT COUNT(*) FROM sync_items WHERE sync_run_id IN "
        "(SELECT id FROM sync_runs WHERE project_id = ?)",
        (project_id,),
    ).fetchone()[0]
    return counts


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def test_preview_counts_match_actual_seeded_rows(conn, evidence_dir, credential_service):
    project_id = _seed_full_project(conn, evidence_dir, credential_service)

    preview = preview_delete_project(conn, project_id)
    actual = _table_counts_for_project(conn, project_id)

    assert preview.sources == actual["sources"] == 2  # manual + drive
    assert preview.observations == actual["observations"] == 1
    assert preview.ledger_items == actual["ledger_items"] == 1
    assert preview.ledger_versions == actual["ledger_versions"] == 1
    assert preview.reviews == actual["reviews"] == 1
    assert preview.corrections == actual["corrections"] == 1
    assert preview.proposed_mutations == actual["proposed_mutations"] == 1
    assert preview.project_people == actual["project_people"] == 1
    assert preview.sync_runs == actual["sync_runs"] == 1
    assert preview.sync_items == actual["sync_items"] == 1
    assert preview.generated_briefs == actual["generated_briefs"] == 1
    assert preview.evidence_links >= 1
    assert preview.source_contents >= 1
    assert preview.source_chunks >= 1
    assert preview.orphaned_content_objects == preview.source_contents


def test_preview_raises_for_unknown_project(conn):
    with pytest.raises(ProjectNotFoundError):
        preview_delete_project(conn, "nonexistent")


def test_preview_never_writes(conn, evidence_dir, credential_service):
    project_id = _seed_full_project(conn, evidence_dir, credential_service)
    before = _table_counts_for_project(conn, project_id)

    preview_delete_project(conn, project_id)
    preview_delete_project(conn, project_id)

    assert _table_counts_for_project(conn, project_id) == before


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def test_delete_rejects_wrong_confirmation_text_and_deletes_nothing(
    conn, evidence_dir, credential_service
):
    project_id = _seed_full_project(conn, evidence_dir, credential_service, name="Acme Rollout")
    before = _table_counts_for_project(conn, project_id)

    with pytest.raises(DeletionConfirmationError):
        delete_project(
            conn,
            project_id,
            confirmation_text="acme rollout",  # wrong case
            evidence_dir=evidence_dir,
            credential_service=credential_service,
        )
    with pytest.raises(DeletionConfirmationError):
        delete_project(
            conn,
            project_id,
            confirmation_text="Something else",
            evidence_dir=evidence_dir,
            credential_service=credential_service,
        )

    assert _table_counts_for_project(conn, project_id) == before
    assert conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()


def test_delete_raises_for_unknown_project(conn, evidence_dir, credential_service):
    with pytest.raises(ProjectNotFoundError):
        delete_project(
            conn,
            "nonexistent",
            confirmation_text="anything",
            evidence_dir=evidence_dir,
            credential_service=credential_service,
        )


# ---------------------------------------------------------------------------
# Full purge
# ---------------------------------------------------------------------------


def test_delete_removes_every_project_owned_row(conn, evidence_dir, credential_service):
    project_id = _seed_full_project(conn, evidence_dir, credential_service)

    delete_project(
        conn,
        project_id,
        confirmation_text="Acme Rollout",
        evidence_dir=evidence_dir,
        credential_service=credential_service,
    )

    assert conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None
    counts = _table_counts_for_project(conn, project_id)
    assert all(count == 0 for count in counts.values()), counts


def test_delete_removes_fts_entries(conn, evidence_dir, credential_service):
    from project_context.db import evidence_repository, ledger_repository, observation_repository

    project_id = _seed_full_project(conn, evidence_dir, credential_service)
    # Sanity: findable before delete.
    assert observation_repository.search_observations(conn, project_id, "report")
    assert ledger_repository.search_ledger_items(conn, project_id, "report")
    assert evidence_repository.search_chunks(conn, project_id, "report")

    delete_project(
        conn,
        project_id,
        confirmation_text="Acme Rollout",
        evidence_dir=evidence_dir,
        credential_service=credential_service,
    )

    assert observation_repository.search_observations(conn, project_id, "report") == []
    assert ledger_repository.search_ledger_items(conn, project_id, "report") == []
    assert evidence_repository.search_chunks(conn, project_id, "report") == []


def test_delete_disconnects_and_deletes_credential_material(conn, evidence_dir, credential_service):
    project_id = _seed_full_project(conn, evidence_dir, credential_service)
    source = sources_repository.get_source_by_kind(conn, project_id, SourceKind.DRIVE)
    credential_ref = source.credential_ref
    assert credential_ref is not None
    assert credential_service.get_secret(conn, project_id, source.id) == "fake-refresh-token"

    delete_project(
        conn,
        project_id,
        confirmation_text="Acme Rollout",
        evidence_dir=evidence_dir,
        credential_service=credential_service,
    )

    # The secret itself is gone from the credential store, independent
    # of the (now also-deleted) sources row.
    assert credential_service._store.get_secret(credential_ref) is None


def test_delete_removes_orphaned_content_bytes(conn, evidence_dir, credential_service):
    from project_context import evidence_store

    project_id = _make_project(conn, name="Solo Project").id
    result = evidence_service.submit_manual_text(
        conn,
        project_id,
        ManualTextInput(
            title="Notes",
            text="Unique content for this project only.",
            source_type=EvidenceSourceType.MEETING_NOTES,
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    sha256 = result.content.sha256
    assert evidence_store.content_exists(evidence_dir, sha256)

    delete_project(
        conn,
        project_id,
        confirmation_text="Solo Project",
        evidence_dir=evidence_dir,
        credential_service=credential_service,
    )

    assert not evidence_store.content_exists(evidence_dir, sha256)


def test_delete_preserves_content_bytes_still_referenced_by_another_project(
    conn, evidence_dir, credential_service
):
    from project_context import evidence_store

    project_a = _make_project(conn, name="Project A").id
    project_b = _make_project(conn, name="Project B").id
    shared_text = "This exact text is uploaded into two different projects."

    result_a = evidence_service.submit_manual_text(
        conn,
        project_a,
        ManualTextInput(
            title="Notes",
            text=shared_text,
            source_type=EvidenceSourceType.MEETING_NOTES,
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    result_b = evidence_service.submit_manual_text(
        conn,
        project_b,
        ManualTextInput(
            title="Notes",
            text=shared_text,
            source_type=EvidenceSourceType.MEETING_NOTES,
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        evidence_dir=evidence_dir,
        chunk_target_chars=4000,
        chunk_overlap_ratio=0.0,
    )
    assert result_a.content.sha256 == result_b.content.sha256
    sha256 = result_a.content.sha256

    delete_project(
        conn,
        project_a,
        confirmation_text="Project A",
        evidence_dir=evidence_dir,
        credential_service=credential_service,
    )

    # Still referenced by project B — must not be deleted.
    assert evidence_store.content_exists(evidence_dir, sha256)
    from project_context.db import evidence_repository

    artifacts = evidence_repository.list_artifacts(conn, project_b)
    assert artifacts and artifacts[0].current_content_id is not None


def test_delete_is_isolated_from_other_projects(conn, evidence_dir, credential_service):
    project_a = _seed_full_project(conn, evidence_dir, credential_service, name="Project A")
    project_b = _seed_full_project(conn, evidence_dir, credential_service, name="Project B")
    before_b = _table_counts_for_project(conn, project_b)

    delete_project(
        conn,
        project_a,
        confirmation_text="Project A",
        evidence_dir=evidence_dir,
        credential_service=credential_service,
    )

    assert conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_b,)).fetchone()
    assert _table_counts_for_project(conn, project_b) == before_b


def test_delete_does_not_touch_shared_people_or_person_aliases(
    conn, evidence_dir, credential_service
):
    project_id = _seed_full_project(conn, evidence_dir, credential_service)
    person = people_repository.get_person_by_email(conn, "nonexistent@example.com")
    assert person is None  # sanity: this test's person has no email
    all_people_before = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    assert all_people_before >= 1

    delete_project(
        conn,
        project_id,
        confirmation_text="Acme Rollout",
        evidence_dir=evidence_dir,
        credential_service=credential_service,
    )

    all_people_after = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    assert all_people_after == all_people_before  # people/aliases are not project-owned
