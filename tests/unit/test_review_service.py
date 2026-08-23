"""Tests for the human review transaction (Section 5.4/5.6/5.7; Section
10.14; FR-013 through FR-021; Prompt 8) —
`project_context.services.review`, end to end against real SQLite.

Covers: every review action's happy path, transaction rollback on a
mid-transition failure, double-submit idempotence, stale-review
detection, invalid-transition rejection, date/owner correction creation,
supersession linkage and history, rejection leaving the ledger
untouched, queue-state persistence across a simulated app restart,
cross-project reference rejection, and current-projection/latest-version
agreement after every action.
"""

from __future__ import annotations

import hashlib
import itertools

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import (
    correction_repository,
    evidence_link_repository,
    evidence_repository,
    ledger_repository,
    people_repository,
    proposed_mutation_repository,
    review_repository,
    sources_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.evidence_links import EvidenceLinkSupportRole, EvidenceLinkTargetType
from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus, LedgerStatusError
from project_context.domain.people import AliasType
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.review import ProposalEdit, ProposedMutationStatus, ReviewAction
from project_context.llm.schemas import EvidenceSpan, ExtractedObservation, ObservationKind
from project_context.services import review as review_service
from project_context.services.ledger import create_ledger_item
from project_context.services.observations import persist_observation
from project_context.services.projects import create_project
from project_context.services.reconciliation import reconcile_observation

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
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
    ).id


# --- shared fixtures (mirrors tests/unit/test_reconciliation_service.py) ---


def make_observation(
    conn,
    project_id,
    *,
    statement,
    subject,
    kind="commitment",
    owner_text=None,
    date_value=None,
    date_text=None,
    explicitness="explicit",
):
    n = next(_counter)
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = evidence_repository.insert_artifact(
        conn, project_id, source.id,
        external_id=f"text:{project_id}:{n}",
        artifact_type=ArtifactType.MANUAL_TEXT,
        title="Notes", author=None, occurred_at=None, external_url=None, source_type=None,
    )
    content = evidence_repository.insert_content(
        conn, project_id, artifact.id,
        sha256=hashlib.sha256(f"{n}:{statement}".encode()).hexdigest(),
        raw_storage_path=None, mime_type="text/plain", byte_size=len(statement),
        normalized_text=statement, parser_name="text", parser_version="1",
        parse_status=ParseStatus.PARSED, location_map=None, original_filename=None,
    )
    spec = ChunkSpec(
        ordinal=0, text=statement, char_start=0, char_end=len(statement), section_path=None,
        sha256=hashlib.sha256(f"{n}:{statement}:chunk".encode()).hexdigest(),
        token_estimate=len(statement) // 4,
    )
    (chunk,) = evidence_repository.insert_chunks(conn, project_id, content.id, [spec])
    extracted = ExtractedObservation(
        kind=ObservationKind(kind),
        subject=subject,
        statement=statement,
        owner_name=owner_text,
        date_value=date_value,
        date_text=date_text,
        explicitness=explicitness,
        evidence=[
            EvidenceSpan(chunk_id=chunk.id, char_start=0, char_end=len(statement), quote=statement)
        ],
    )
    observation, links, _created = persist_observation(
        conn, project_id, content_id=content.id, chunk_id=chunk.id, extracted=extracted
    )
    return observation, content, chunk, links


def make_person(conn, *, display_name):
    person = people_repository.create_person(conn, display_name=display_name)
    people_repository.add_alias(
        conn, person.id, alias_type=AliasType.NAME, alias_value=display_name
    )
    return person


def reconcile(conn, project_id, observation):
    return reconcile_observation(conn, project_id, observation.id)


def ledger_snapshot(conn, project_id):
    items = ledger_repository.list_items_for_project(conn, project_id)
    version_count = sum(
        len(ledger_repository.list_versions_for_item(conn, project_id, item.id)) for item in items
    )
    return len(items), version_count


# ---------------------------------------------------------------------------
# Happy paths: one per review action.
# ---------------------------------------------------------------------------


def test_accept_create_proposal(conn, project_id):
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="Priya will send the report.",
        owner_text="Priya",
    )
    result = reconcile(conn, project_id, observation)
    assert result.proposal.action.value == "create"

    outcome = review_service.accept_proposal(conn, project_id, result.proposal.id)

    assert outcome.review.action is ReviewAction.ACCEPT
    assert outcome.ledger_item is not None
    assert outcome.ledger_item.canonical_title == "Send the report"
    assert outcome.ledger_item.status is LedgerItemStatus.OPEN
    assert outcome.ledger_version.version_no == 1
    assert outcome.proposal.status is ProposedMutationStatus.ACCEPTED
    # Evidence linked to both the item and the specific version.
    item_links = evidence_link_repository.list_for_target(
        conn, project_id, EvidenceLinkTargetType.LEDGER_ITEM, outcome.ledger_item.id
    )
    version_links = evidence_link_repository.list_for_target(
        conn, project_id, EvidenceLinkTargetType.LEDGER_VERSION, outcome.ledger_version.id
    )
    assert len(item_links) == 1
    assert len(version_links) == 1


def test_accept_update_due_date(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT,
        canonical_title="Send the report", canonical_description="Priya will send the report.",
        due_date="2026-08-28",
    )
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report",
        statement="Priya said the report deadline moved to September 4th.",
        date_value="2026-09-04", date_text="September 4th",
    )
    result = reconcile(conn, project_id, observation)
    assert result.proposal.action.value == "update"

    outcome = review_service.accept_proposal(conn, project_id, result.proposal.id)

    assert outcome.ledger_item.id == item.id
    assert outcome.ledger_item.due_date == "2026-09-04"
    assert outcome.ledger_version.version_no == 2
    assert outcome.ledger_version.transition_type.value == "update"


def test_accept_complete(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT,
        canonical_title="Send the report", canonical_description="Priya will send the report.",
    )
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="The report was sent to the client.",
    )
    result = reconcile(conn, project_id, observation)
    assert result.proposal.action.value == "complete"

    outcome = review_service.accept_proposal(conn, project_id, result.proposal.id)

    assert outcome.ledger_item.status is LedgerItemStatus.COMPLETED
    completion_links = [
        link
        for link in evidence_link_repository.list_for_target(
            conn, project_id, EvidenceLinkTargetType.LEDGER_VERSION, outcome.ledger_version.id
        )
    ]
    assert all(link.support_role.value == "completion" for link in completion_links)


def test_accept_cancel(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT,
        canonical_title="Send the report", canonical_description="Priya will send the report.",
    )
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report",
        statement="This commitment is cancelled; we won't proceed with the report.",
    )
    result = reconcile(conn, project_id, observation)
    assert result.proposal.action.value == "cancel"

    outcome = review_service.accept_proposal(conn, project_id, result.proposal.id)

    assert outcome.ledger_item.status is LedgerItemStatus.CANCELED


def test_accept_add_evidence_writes_no_ledger_version(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT,
        canonical_title="Send the report", canonical_description="Priya will send the report.",
        owner_person_id=None, due_date="2026-08-28",
    )
    first_content, first_chunk = _content_and_chunk_only(
        conn, project_id, "Priya will send the report by Friday."
    )
    evidence_link_repository.insert_link(
        conn, project_id,
        target_type=EvidenceLinkTargetType.LEDGER_ITEM, target_id=item.id,
        content_id=first_content.id, chunk_id=first_chunk.id,
        char_start=0, char_end=len(first_chunk.text), quote=first_chunk.text,
        support_role=EvidenceLinkSupportRole.SUPPORTS,
    )
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="Priya will send the report.",
        date_value="2026-08-28",
    )
    result = reconcile(conn, project_id, observation)
    assert result.proposal.action.value == "add_evidence"

    before_count, before_versions = ledger_snapshot(conn, project_id)
    outcome = review_service.accept_proposal(conn, project_id, result.proposal.id)
    after_count, after_versions = ledger_snapshot(conn, project_id)

    assert (before_count, before_versions) == (after_count, after_versions)
    assert outcome.ledger_version is None
    assert outcome.ledger_item.id == item.id


def _content_and_chunk_only(conn, project_id, text):
    n = next(_counter)
    source = sources_repository.ensure_manual_source(conn, project_id)
    artifact = evidence_repository.insert_artifact(
        conn, project_id, source.id, external_id=f"extra:{project_id}:{n}",
        artifact_type=ArtifactType.MANUAL_TEXT, title="Notes", author=None,
        occurred_at=None, external_url=None, source_type=None,
    )
    content = evidence_repository.insert_content(
        conn, project_id, artifact.id, sha256=hashlib.sha256(f"{n}:{text}".encode()).hexdigest(),
        raw_storage_path=None, mime_type="text/plain", byte_size=len(text),
        normalized_text=text, parser_name="text", parser_version="1",
        parse_status=ParseStatus.PARSED, location_map=None, original_filename=None,
    )
    spec = ChunkSpec(
        ordinal=0, text=text, char_start=0, char_end=len(text), section_path=None,
        sha256=hashlib.sha256(f"{n}:{text}:c".encode()).hexdigest(), token_estimate=len(text) // 4,
    )
    (chunk,) = evidence_repository.insert_chunks(conn, project_id, content.id, [spec])
    return content, chunk


def test_edit_and_accept_overrides_wording_kind_owner_due_date_status(conn, project_id):
    priya = make_person(conn, display_name="Priya")
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="Someone will send the report.",
    )
    result = reconcile(conn, project_id, observation)
    assert result.proposal.action.value == "create"

    edits = ProposalEdit(
        kind=LedgerItemKind.MILESTONE,
        canonical_title="Send the final report",
        canonical_description="Corrected description.",
        owner_person_id=priya.id,
        due_date="2026-10-01",
    )
    outcome = review_service.edit_and_accept_proposal(conn, project_id, result.proposal.id, edits)

    assert outcome.review.action is ReviewAction.EDIT_ACCEPT
    assert outcome.proposal.status is ProposedMutationStatus.EDITED_ACCEPTED
    assert outcome.ledger_item.kind is LedgerItemKind.MILESTONE
    assert outcome.ledger_item.canonical_title == "Send the final report"
    assert outcome.ledger_item.owner_person_id == priya.id
    assert outcome.ledger_item.due_date == "2026-10-01"
    # CREATE always starts a new item at its kind's own initial status —
    # not an arbitrary caller-chosen one (domain.ledger.validate_transition
    # fixes it); MILESTONE's initial status is OPEN.
    assert outcome.ledger_item.status is LedgerItemStatus.OPEN
    assert outcome.ledger_item.user_corrected is True


def test_mark_complete_forces_completion_regardless_of_proposal_action(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT,
        canonical_title="Send the report", canonical_description="Priya will send the report.",
        due_date="2026-08-28",
    )
    # An UPDATE(due_date) proposal, not a completion — the human decides
    # it should actually be marked complete instead.
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report",
        statement="Priya said the report deadline moved to September 4th.",
        date_value="2026-09-04", date_text="September 4th",
    )
    result = reconcile(conn, project_id, observation)
    assert result.proposal.action.value == "update"

    outcome = review_service.mark_complete(conn, project_id, result.proposal.id)

    assert outcome.review.action is ReviewAction.MARK_COMPLETE
    assert outcome.ledger_item.status is LedgerItemStatus.COMPLETED
    assert outcome.ledger_version.transition_type.value == "complete"


def test_mark_superseded_forces_supersession(conn, project_id):
    priya = make_person(conn, display_name="Priya")
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.DECISION,
        canonical_title="Use vendor A for hosting", owner_person_id=priya.id, due_date="2026-09-01",
    )
    observation, *_ = make_observation(
        conn, project_id, kind="decision", subject="Use vendor A for hosting",
        statement="The vendor decision needs another look.",
        owner_text="Priya", date_value="2026-09-01",
    )
    result = reconcile(conn, project_id, observation)

    outcome = review_service.mark_superseded(conn, project_id, result.proposal.id)

    assert outcome.review.action is ReviewAction.MARK_SUPERSEDED
    assert outcome.predecessor_item.status is LedgerItemStatus.SUPERSEDED
    assert outcome.predecessor_item.superseded_by_item_id == outcome.ledger_item.id
    assert outcome.ledger_item.supersedes_item_id == outcome.predecessor_item.id


def test_treat_as_new_ignores_matched_target(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report",
    )
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="Priya will send the report.",
    )
    result = reconcile(conn, project_id, observation)
    # A near-exact subject match should find `item` as a candidate for a
    # plain accept; treat_as_new must ignore it regardless.
    outcome = review_service.treat_as_new(
        conn, project_id, result.proposal.id, kind=LedgerItemKind.COMMITMENT,
    )

    assert outcome.review.action is ReviewAction.TREAT_AS_NEW
    assert outcome.ledger_item.id != item.id
    before_count, _ = ledger_snapshot(conn, project_id)
    assert before_count == 2  # the pre-existing item plus the new one


def test_reject_leaves_ledger_unchanged(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report",
        canonical_description="Priya will send the report.",
    )
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="The report was sent to the client.",
    )
    result = reconcile(conn, project_id, observation)

    before = ledger_snapshot(conn, project_id)
    outcome = review_service.reject_proposal(
        conn, project_id, result.proposal.id, reason_code="wrong_match", note="not confirmed"
    )
    after = ledger_snapshot(conn, project_id)

    assert before == after
    assert outcome.review.action is ReviewAction.REJECT
    assert outcome.proposal.status is ProposedMutationStatus.REJECTED
    assert outcome.ledger_item is None
    assert outcome.ledger_version is None
    # The observation/proposal record itself is preserved, not deleted.
    preserved = proposed_mutation_repository.get_proposal(conn, project_id, result.proposal.id)
    assert preserved is not None
    assert preserved.status is ProposedMutationStatus.REJECTED


def test_accept_conflict_proposal_is_refused(conn, project_id):
    priya = make_person(conn, display_name="Priya")
    item_a, _ = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report",
        canonical_description="Priya will send the report.", owner_person_id=priya.id,
        due_date="2026-08-28",
    )
    item_b, _ = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report",
        canonical_description="Priya will send the report.", owner_person_id=priya.id,
        due_date="2026-08-28",
    )
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="Priya will send the report.",
        owner_text="Priya", date_value="2026-08-28",
    )
    result = reconcile(conn, project_id, observation)
    assert result.proposal.action.value == "conflict"

    with pytest.raises(review_service.ReviewActionNotApplicableError):
        review_service.accept_proposal(conn, project_id, result.proposal.id)

    # A human can still resolve it explicitly by redirecting to one
    # candidate via edit_and_accept.
    outcome = review_service.edit_and_accept_proposal(
        conn, project_id, result.proposal.id,
        ProposalEdit(target_ledger_item_id=item_a.id, status=LedgerItemStatus.ACTIVE),
    )
    assert outcome.ledger_item.id == item_a.id
    del item_b


# ---------------------------------------------------------------------------
# Idempotence / double-submit.
# ---------------------------------------------------------------------------


def test_double_submit_accept_is_idempotent(conn, project_id):
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="Priya will send the report.",
    )
    result = reconcile(conn, project_id, observation)

    first = review_service.accept_proposal(conn, project_id, result.proposal.id)
    second = review_service.accept_proposal(conn, project_id, result.proposal.id)

    assert first.already_applied is False
    assert second.already_applied is True
    assert first.review.id == second.review.id
    assert len(review_repository.list_for_project(conn, project_id)) == 1
    item_count, version_count = ledger_snapshot(conn, project_id)
    assert (item_count, version_count) == (1, 1)


def test_double_submit_reject_is_idempotent(conn, project_id):
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="Priya will send the report.",
    )
    result = reconcile(conn, project_id, observation)

    first = review_service.reject_proposal(conn, project_id, result.proposal.id)
    second = review_service.reject_proposal(conn, project_id, result.proposal.id)

    assert first.already_applied is False
    assert second.already_applied is True
    assert first.review.id == second.review.id
    assert len(review_repository.list_for_project(conn, project_id)) == 1


# ---------------------------------------------------------------------------
# Staleness.
# ---------------------------------------------------------------------------


def test_stale_review_is_rejected(conn, project_id):
    item, v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report",
        canonical_description="Priya will send the report.",
    )
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report",
        statement="Priya said the report deadline moved to September 4th.",
        date_value="2026-09-04", date_text="September 4th",
    )
    result = reconcile(conn, project_id, observation)

    with pytest.raises(review_service.StaleReviewError):
        review_service.accept_proposal(
            conn, project_id, result.proposal.id, expected_target_version_id="a-stale-version-id"
        )
    # Nothing was written.
    item_count, version_count = ledger_snapshot(conn, project_id)
    assert (item_count, version_count) == (1, 1)
    assert proposed_mutation_repository.get_proposal(
        conn, project_id, result.proposal.id
    ).status is ProposedMutationStatus.PENDING


def test_matching_expected_version_succeeds(conn, project_id):
    item, v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report",
        canonical_description="Priya will send the report.", due_date="2026-08-28",
    )
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report",
        statement="Priya said the report deadline moved to September 4th.",
        date_value="2026-09-04", date_text="September 4th",
    )
    result = reconcile(conn, project_id, observation)

    outcome = review_service.accept_proposal(
        conn, project_id, result.proposal.id, expected_target_version_id=item.current_version_id
    )
    assert outcome.ledger_item.due_date == "2026-09-04"


# ---------------------------------------------------------------------------
# Invalid transitions / rollback.
# ---------------------------------------------------------------------------


def test_invalid_transition_rolls_back_the_whole_review(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.RISK, canonical_title="Vendor delay risk",
    )
    observation, *_ = make_observation(
        conn, project_id, kind="risk", subject="Vendor delay risk",
        statement="Vendor delay risk noted.",
    )
    result = reconcile(conn, project_id, observation)

    before_reviews = len(review_repository.list_for_project(conn, project_id))
    before_corrections = len(correction_repository.list_for_project(conn, project_id))
    before_ledger = ledger_snapshot(conn, project_id)

    # COMPLETED is not a valid status for a risk item.
    with pytest.raises(LedgerStatusError):
        review_service.mark_complete(conn, project_id, result.proposal.id)

    assert len(review_repository.list_for_project(conn, project_id)) == before_reviews
    assert len(correction_repository.list_for_project(conn, project_id)) == before_corrections
    assert ledger_snapshot(conn, project_id) == before_ledger
    proposal = proposed_mutation_repository.get_proposal(conn, project_id, result.proposal.id)
    assert proposal.status is ProposedMutationStatus.PENDING


# ---------------------------------------------------------------------------
# Corrections.
# ---------------------------------------------------------------------------


def test_owner_correction_is_recorded_and_flags_user_corrected(conn, project_id):
    priya = make_person(conn, display_name="Priya")
    diego = make_person(conn, display_name="Diego")
    sam = make_person(conn, display_name="Sam")
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report",
        canonical_description="Priya will send the report.", owner_person_id=priya.id,
    )
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report",
        statement="The report: ownership moves to Diego.", owner_text="Diego",
    )
    result = reconcile(conn, project_id, observation)
    assert result.proposal.proposed_patch["owner_person_id"] == diego.id

    outcome = review_service.edit_and_accept_proposal(
        conn, project_id, result.proposal.id, ProposalEdit(owner_person_id=sam.id)
    )

    assert outcome.ledger_item.owner_person_id == sam.id
    assert outcome.ledger_item.user_corrected is True
    owner_corrections = [c for c in outcome.corrections if c.field_name == "owner_person_id"]
    assert len(owner_corrections) == 1
    correction = owner_corrections[0]
    assert correction.reason_code.value == "wrong_owner"
    assert correction.materiality.value == "material"
    assert correction.original == {"value": diego.id}
    assert correction.corrected == {"value": sam.id}
    stored = correction_repository.list_for_project(conn, project_id)
    assert len(stored) == 1


def test_due_date_correction_is_recorded(conn, project_id):
    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report",
        canonical_description="Priya will send the report.", due_date="2026-08-28",
    )
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report",
        statement="Priya said the report deadline moved to September 4th.",
        date_value="2026-09-04", date_text="September 4th",
    )
    result = reconcile(conn, project_id, observation)
    assert result.proposal.proposed_patch["due_date"] == "2026-09-04"

    outcome = review_service.edit_and_accept_proposal(
        conn, project_id, result.proposal.id, ProposalEdit(due_date="2026-09-10")
    )

    assert outcome.ledger_item.due_date == "2026-09-10"
    date_corrections = [c for c in outcome.corrections if c.field_name == "due_date"]
    assert len(date_corrections) == 1
    assert date_corrections[0].reason_code.value == "wrong_date"
    assert date_corrections[0].original == {"value": "2026-09-04"}
    assert date_corrections[0].corrected == {"value": "2026-09-10"}


def test_accept_without_edits_records_no_corrections(conn, project_id):
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="Priya will send the report.",
    )
    result = reconcile(conn, project_id, observation)
    outcome = review_service.accept_proposal(conn, project_id, result.proposal.id)
    assert outcome.corrections == ()
    assert outcome.ledger_item.user_corrected is False


# ---------------------------------------------------------------------------
# Supersession linkage and history.
# ---------------------------------------------------------------------------


def test_supersession_links_predecessor_and_successor_bidirectionally(conn, project_id):
    priya = make_person(conn, display_name="Priya")
    old_item, old_v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.DECISION, canonical_title="Use vendor A for hosting",
        owner_person_id=priya.id, due_date="2026-09-01",
    )
    observation, *_ = make_observation(
        conn, project_id, kind="decision", subject="Use vendor B for hosting",
        statement="We will use vendor B instead of vendor A for hosting.",
        owner_text="Priya", date_value="2026-09-01",
    )
    result = reconcile(conn, project_id, observation)
    assert result.proposal.action.value == "supersede"

    outcome = review_service.accept_proposal(conn, project_id, result.proposal.id)
    new_item = outcome.ledger_item

    assert outcome.predecessor_item.id == old_item.id
    assert outcome.predecessor_item.status is LedgerItemStatus.SUPERSEDED
    assert outcome.predecessor_item.superseded_by_item_id == new_item.id
    assert new_item.supersedes_item_id == old_item.id

    # Version-level linkage too: the old item's final version points at
    # the new item's first version.
    old_versions = ledger_repository.list_versions_for_item(conn, project_id, old_item.id)
    assert old_versions[-1].superseded_by_version_id == outcome.ledger_version.id
    assert outcome.ledger_version.version_no == 1

    # Both items independently resolvable via get_item — a history/
    # evidence drawer can navigate either direction.
    refetched_old = ledger_repository.get_item(conn, project_id, old_item.id)
    refetched_new = ledger_repository.get_item(conn, project_id, new_item.id)
    assert refetched_old.superseded_by_item_id == refetched_new.id
    assert refetched_new.supersedes_item_id == refetched_old.id


def test_supersession_preserves_old_evidence_and_history(conn, project_id):
    priya = make_person(conn, display_name="Priya")
    old_item, old_v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.DECISION, canonical_title="Use vendor A for hosting",
        owner_person_id=priya.id, due_date="2026-09-01",
    )
    old_content, old_chunk = _content_and_chunk_only(
        conn, project_id, "We chose vendor A for hosting."
    )
    evidence_link_repository.insert_link(
        conn, project_id, target_type=EvidenceLinkTargetType.LEDGER_ITEM, target_id=old_item.id,
        content_id=old_content.id, chunk_id=old_chunk.id,
        char_start=0, char_end=len(old_chunk.text),
        quote=old_chunk.text, support_role=EvidenceLinkSupportRole.SUPPORTS,
    )
    observation, *_ = make_observation(
        conn, project_id, kind="decision", subject="Use vendor B for hosting",
        statement="We will use vendor B instead of vendor A for hosting.",
        owner_text="Priya", date_value="2026-09-01",
    )
    result = reconcile(conn, project_id, observation)
    review_service.accept_proposal(conn, project_id, result.proposal.id)

    # The pre-existing evidence link to the old item is untouched.
    old_item_links = evidence_link_repository.list_for_target(
        conn, project_id, EvidenceLinkTargetType.LEDGER_ITEM, old_item.id
    )
    assert any(link.content_id == old_content.id for link in old_item_links)
    # Plus a new supersession-role link recording *why* it was superseded.
    supersession_links = [
        link for link in old_item_links if link.support_role.value == "supersession"
    ]
    assert len(supersession_links) == 1
    # The old item's full version history is intact (create + supersede).
    old_versions = ledger_repository.list_versions_for_item(conn, project_id, old_item.id)
    assert [v.transition_type.value for v in old_versions] == ["create", "supersede"]
    assert old_versions[0].valid_to is not None  # closed when version 2 was appended


# ---------------------------------------------------------------------------
# Queue-state persistence across a simulated app restart.
# ---------------------------------------------------------------------------


def test_queue_state_survives_a_simulated_restart(tmp_path, migrations_dir):
    db_path = tmp_path / "restart.db"
    conn1 = connect(db_path)
    run_migrations(conn1, migrations_dir)
    project = create_project(conn1, ProjectCreateInput(name="Acme Rollout", objective="Ship it"))

    obs_a, *_ = make_observation(
        conn1, project.id, subject="Send the report", statement="Priya will send it.",
    )
    obs_b, *_ = make_observation(
        conn1, project.id, subject="Renew the contract", statement="Diego will renew it.",
    )
    result_a = reconcile(conn1, project.id, obs_a)
    result_b = reconcile(conn1, project.id, obs_b)
    review_service.accept_proposal(conn1, project.id, result_a.proposal.id)
    # result_b stays pending.
    conn1.close()

    # A fresh connection to the same file stands in for an app restart.
    conn2 = connect(db_path)
    try:
        pending = proposed_mutation_repository.list_pending_for_project(conn2, project.id)
        assert [p.id for p in pending] == [result_b.proposal.id]

        accepted = [
            p
            for p in review_repository.list_for_project(conn2, project.id)
        ]
        assert len(accepted) == 1

        queue = review_service.list_review_queue(conn2, project.id)
        assert [card.proposal.id for card in queue] == [result_b.proposal.id]

        # The accepted CREATE produced an item findable after reconnect too.
        items = ledger_repository.list_items_for_project(conn2, project.id)
        assert len(items) == 1
        assert items[0].canonical_title == "Send the report"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Cross-project reference rejection.
# ---------------------------------------------------------------------------


def test_cross_project_target_redirect_is_rejected(conn, project_id):
    other_project = create_project(conn, ProjectCreateInput(name="Other", objective="Other work"))
    other_item, _ = create_ledger_item(
        conn, other_project.id, kind=LedgerItemKind.COMMITMENT, canonical_title="Unrelated item",
    )
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="Priya will send the report.",
    )
    result = reconcile(conn, project_id, observation)

    with pytest.raises(review_service.CrossProjectReferenceError):
        review_service.edit_and_accept_proposal(
            conn, project_id, result.proposal.id,
            ProposalEdit(target_ledger_item_id=other_item.id, status=LedgerItemStatus.ACTIVE),
        )
    # Nothing was written to either project's ledger.
    assert ledger_snapshot(conn, project_id) == (0, 0)
    assert ledger_snapshot(conn, other_project.id) == (1, 1)


def test_proposal_from_a_different_project_is_not_found(conn, project_id):
    other_project = create_project(conn, ProjectCreateInput(name="Other", objective="Other work"))
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="Priya will send the report.",
    )
    result = reconcile(conn, project_id, observation)

    with pytest.raises(review_service.ProposalNotFoundError):
        review_service.accept_proposal(conn, other_project.id, result.proposal.id)


def test_evidence_link_id_from_another_observation_is_rejected(conn, project_id):
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="Priya will send the report.",
    )
    other_observation, *_ = make_observation(
        conn, project_id, subject="Renew the contract", statement="Diego will renew it.",
    )
    other_result = reconcile(conn, project_id, other_observation)
    other_links = evidence_link_repository.list_for_target(
        conn, project_id, EvidenceLinkTargetType.OBSERVATION, other_observation.id
    )

    result = reconcile(conn, project_id, observation)
    with pytest.raises(review_service.ReviewValidationError):
        review_service.edit_and_accept_proposal(
            conn, project_id, result.proposal.id,
            ProposalEdit(evidence_link_ids=(other_links[0].id,)),
        )
    del other_result


def test_evidence_selection_requires_at_least_one_link(conn, project_id):
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement="Priya will send the report.",
    )
    result = reconcile(conn, project_id, observation)
    with pytest.raises(review_service.ReviewValidationError):
        review_service.edit_and_accept_proposal(
            conn, project_id, result.proposal.id, ProposalEdit(evidence_link_ids=())
        )


# ---------------------------------------------------------------------------
# Current projection agrees with the latest version after every action.
# ---------------------------------------------------------------------------


def _assert_projection_matches_latest_version(conn, project_id, item_id):
    item = ledger_repository.get_item(conn, project_id, item_id)
    versions = ledger_repository.list_versions_for_item(conn, project_id, item_id)
    latest = versions[-1]
    assert item.current_version_id == latest.id
    assert item.status == latest.status
    assert item.canonical_title == latest.canonical_title
    assert item.canonical_description == latest.canonical_description
    assert item.owner_person_id == latest.owner_person_id
    assert item.due_date == latest.due_date
    # Exactly one version has no valid_to (the current one).
    open_ended = [v for v in versions if v.valid_to is None]
    assert open_ended == [latest]


@pytest.mark.parametrize(
    "build_case",
    [
        "create",
        "update",
        "complete",
        "cancel",
    ],
)
def test_projection_matches_latest_version_after_each_action(conn, project_id, build_case):
    if build_case == "create":
        observation, *_ = make_observation(
            conn, project_id, subject="Send the report", statement="Priya will send the report.",
        )
        result = reconcile(conn, project_id, observation)
        outcome = review_service.accept_proposal(conn, project_id, result.proposal.id)
        _assert_projection_matches_latest_version(conn, project_id, outcome.ledger_item.id)
        return

    item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.COMMITMENT, canonical_title="Send the report",
        canonical_description="Priya will send the report.", due_date="2026-08-28",
    )
    statements = {
        "update": (
            "Priya said the report deadline moved to September 4th.",
            {"date_value": "2026-09-04", "date_text": "September 4th"},
        ),
        "complete": ("The report was sent to the client.", {}),
        "cancel": ("This commitment is cancelled; we won't proceed with the report.", {}),
    }
    statement, extra = statements[build_case]
    observation, *_ = make_observation(
        conn, project_id, subject="Send the report", statement=statement, **extra
    )
    result = reconcile(conn, project_id, observation)
    outcome = review_service.accept_proposal(conn, project_id, result.proposal.id)
    _assert_projection_matches_latest_version(conn, project_id, item.id)
    assert outcome.ledger_item.id == item.id


def test_projection_matches_latest_version_for_both_items_after_supersede(conn, project_id):
    priya = make_person(conn, display_name="Priya")
    old_item, _v1 = create_ledger_item(
        conn, project_id, kind=LedgerItemKind.DECISION, canonical_title="Use vendor A for hosting",
        owner_person_id=priya.id, due_date="2026-09-01",
    )
    observation, *_ = make_observation(
        conn, project_id, kind="decision", subject="Use vendor B for hosting",
        statement="We will use vendor B instead of vendor A for hosting.",
        owner_text="Priya", date_value="2026-09-01",
    )
    result = reconcile(conn, project_id, observation)
    outcome = review_service.accept_proposal(conn, project_id, result.proposal.id)

    _assert_projection_matches_latest_version(conn, project_id, old_item.id)
    _assert_projection_matches_latest_version(conn, project_id, outcome.ledger_item.id)
