"""Table-driven reconciliation scenario suite (Section 10; FR-017 through
FR-020; Prompt 7's required case list) — end to end against real SQLite,
through `project_context.services.reconciliation.reconcile_observation`.

Every case is a `(setup, assertions)` pair collected in `CASES` and run by
one parametrized test. The harness itself asserts the one invariant every
case shares — reconciliation never mutates `ledger_items`/`ledger_versions`
— so no individual case has to remember it.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from project_context.chunking import ChunkSpec
from project_context.db import (
    evidence_link_repository,
    evidence_repository,
    ledger_repository,
    observation_repository,
    people_repository,
    proposed_mutation_repository,
    sources_repository,
)
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.evidence import ArtifactType, ParseStatus
from project_context.domain.evidence_links import EvidenceLinkSupportRole, EvidenceLinkTargetType
from project_context.domain.ledger import (
    ConfidenceBand,
    LedgerItemKind,
    LedgerItemStatus,
    LedgerTransitionType,
)
from project_context.domain.people import AliasType
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.review import ProposedMutationAction
from project_context.services.ledger import append_ledger_version, create_ledger_item
from project_context.services.projects import create_project
from project_context.services.reconciliation import ReconciliationResult, reconcile_observation

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


# --- shared fixtures ---------------------------------------------------------


def make_project(conn, name="Second Project"):
    return create_project(conn, ProjectCreateInput(name=name, objective="Other work")).id


def make_content_and_chunk(conn, project_id, *, text):
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
        sha256=hashlib.sha256(f"{n}:{text}".encode()).hexdigest(),
        raw_storage_path=None,
        mime_type="text/plain",
        byte_size=len(text),
        normalized_text=text,
        parser_name="text",
        parser_version="1",
        parse_status=ParseStatus.PARSED,
        location_map=None,
        original_filename=None,
    )
    spec = ChunkSpec(
        ordinal=0,
        text=text,
        char_start=0,
        char_end=len(text),
        section_path=None,
        sha256=hashlib.sha256(f"{n}:{text}:chunk".encode()).hexdigest(),
        token_estimate=len(text) // 4,
    )
    (chunk,) = evidence_repository.insert_chunks(conn, project_id, content.id, [spec])
    return content, chunk


def make_observation(
    conn,
    project_id,
    *,
    statement,
    subject,
    kind="commitment",
    owner_text=None,
    owner_person_id=None,
    date_value=None,
    date_text=None,
    explicitness="explicit",
):
    content, chunk = make_content_and_chunk(conn, project_id, text=statement)
    observation, _created = observation_repository.insert_observation(
        conn,
        project_id,
        content_id=content.id,
        chunk_id=chunk.id,
        kind=kind,
        subject=subject,
        statement=statement,
        evidence_spans=[(chunk.id, 0, len(statement))],
        owner_text=owner_text,
        owner_person_id=owner_person_id,
        date_value=date_value,
        date_text=date_text,
        explicitness=explicitness,
    )
    return observation, content, chunk


def make_person(conn, *, display_name, email=None):
    person = people_repository.create_person(conn, display_name=display_name, primary_email=email)
    people_repository.add_alias(
        conn, person.id, alias_type=AliasType.NAME, alias_value=display_name
    )
    if email:
        people_repository.add_alias(conn, person.id, alias_type=AliasType.EMAIL, alias_value=email)
    return person


def link_evidence_to_item(conn, project_id, ledger_item_id, content, chunk):
    evidence_link_repository.insert_link(
        conn,
        project_id,
        target_type=EvidenceLinkTargetType.LEDGER_ITEM,
        target_id=ledger_item_id,
        content_id=content.id,
        chunk_id=chunk.id,
        char_start=0,
        char_end=len(chunk.text),
        quote=chunk.text,
        support_role=EvidenceLinkSupportRole.SUPPORTS,
    )


def ledger_snapshot(conn, project_id) -> dict[str, Any]:
    """Everything that would change if reconciliation ever wrote a ledger
    transition — used to assert zero direct ledger mutation."""
    items = ledger_repository.list_items_for_project(conn, project_id)
    version_count = sum(
        len(ledger_repository.list_versions_for_item(conn, project_id, item.id)) for item in items
    )
    return {
        "item_count": len(items),
        "version_count": version_count,
        "projections": {item.id: (item.status, item.current_version_id) for item in items},
    }


@dataclass(frozen=True)
class Case:
    name: str
    setup: Callable[[Any, str], dict[str, Any]]
    assertions: Callable[[Any, str, dict[str, Any], ReconciliationResult], None]


CASES: list[Case] = []


def register(name: str) -> Callable[[Callable], Callable]:
    def decorator(build_and_check: Callable) -> Callable:
        setup_fn, assertions_fn = build_and_check()
        CASES.append(Case(name=name, setup=setup_fn, assertions=assertions_fn))
        return build_and_check

    return decorator


# ---------------------------------------------------------------------------
# 1. new item — no candidates exist anywhere in the project.
# ---------------------------------------------------------------------------


@register("new_item")
def _case_new_item():
    def setup(conn, project_id):
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="Priya will send the report by Friday.",
            owner_text="Priya",
            date_text="Friday",
        )
        return {"observation_id": observation.id}

    def check(conn, project_id, ctx, result):
        assert result.classification.action is ProposedMutationAction.CREATE
        assert result.classification.target_ledger_item_id is None
        assert result.classification.proposed_patch["kind"] == "commitment"
        assert result.classification.escalate is False
        assert result.proposal.confidence_band == ConfidenceBand.HIGH.value

    return setup, check


# ---------------------------------------------------------------------------
# 2. exact repeat — reconciling the same observation twice is idempotent.
# ---------------------------------------------------------------------------


@register("exact_repeat")
def _case_exact_repeat():
    def setup(conn, project_id):
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="Priya will send the report by Friday.",
            owner_text="Priya",
        )
        first = reconcile_observation(conn, project_id, observation.id)
        return {"observation_id": observation.id, "first_proposal_id": first.proposal.id}

    def check(conn, project_id, ctx, result):
        assert result.created is False
        assert result.proposal.id == ctx["first_proposal_id"]
        all_proposals = proposed_mutation_repository.list_for_observation(
            conn, project_id, ctx["observation_id"]
        )
        assert len(all_proposals) == 1

    return setup, check


# ---------------------------------------------------------------------------
# 3. second supporting source — a different observation, same proposition,
#    from content not yet linked to the existing item: ADD_EVIDENCE.
# ---------------------------------------------------------------------------


@register("second_supporting_source")
def _case_second_supporting_source():
    def setup(conn, project_id):
        priya = make_person(conn, display_name="Priya")
        item, _v1 = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Priya will send the report.",
            owner_person_id=priya.id,
            due_date="2026-08-28",
        )
        first_content, first_chunk = make_content_and_chunk(
            conn, project_id, text="Priya will send the report by Friday."
        )
        link_evidence_to_item(conn, project_id, item.id, first_content, first_chunk)

        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="Priya will send the report.",
            owner_text="Priya",
            date_value="2026-08-28",
        )
        return {"observation_id": observation.id, "item_id": item.id}

    def check(conn, project_id, ctx, result):
        assert result.classification.action is ProposedMutationAction.ADD_EVIDENCE
        assert result.classification.target_ledger_item_id == ctx["item_id"]
        top = result.match_outcome.top
        assert top is not None
        assert top.features.owner_match == 1.0
        assert top.features.subject_token_similarity >= 0.75

    return setup, check


# ---------------------------------------------------------------------------
# 4. changed due date — explicit change language, material date difference.
# ---------------------------------------------------------------------------


@register("changed_due_date")
def _case_changed_due_date():
    def setup(conn, project_id):
        priya = make_person(conn, display_name="Priya")
        item, _v1 = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Priya will send the report.",
            owner_person_id=priya.id,
            due_date="2026-08-28",
        )
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="Priya said the report deadline moved to September 4th.",
            owner_text="Priya",
            date_value="2026-09-04",
            date_text="September 4th",
        )
        return {"observation_id": observation.id, "item_id": item.id}

    def check(conn, project_id, ctx, result):
        assert result.classification.action is ProposedMutationAction.UPDATE
        assert result.classification.target_ledger_item_id == ctx["item_id"]
        assert result.classification.proposed_patch["due_date"] == "2026-09-04"
        assert result.classification.escalate is True
        assert "changed_due_date" in result.classification.escalation_reasons

    return setup, check


# ---------------------------------------------------------------------------
# 5. changed owner — explicit assignment language, not assistance.
# ---------------------------------------------------------------------------


@register("changed_owner")
def _case_changed_owner():
    def setup(conn, project_id):
        priya = make_person(conn, display_name="Priya")
        diego = make_person(conn, display_name="Diego")
        item, _v1 = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Priya will send the report.",
            owner_person_id=priya.id,
        )
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="The report: ownership moves to Diego.",
            owner_text="Diego",
        )
        return {"observation_id": observation.id, "item_id": item.id, "diego_id": diego.id}

    def check(conn, project_id, ctx, result):
        assert result.classification.action is ProposedMutationAction.UPDATE
        assert result.classification.target_ledger_item_id == ctx["item_id"]
        assert result.classification.proposed_patch["owner_person_id"] == ctx["diego_id"]
        assert "changed_owner" in result.classification.escalation_reasons

    return setup, check


# ---------------------------------------------------------------------------
# 6. explicit completion.
# ---------------------------------------------------------------------------


@register("explicit_completion")
def _case_explicit_completion():
    def setup(conn, project_id):
        item, _v1 = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Priya will send the report.",
        )
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="The report was sent to the client.",
        )
        return {"observation_id": observation.id, "item_id": item.id}

    def check(conn, project_id, ctx, result):
        assert result.classification.action is ProposedMutationAction.COMPLETE
        assert result.classification.target_ledger_item_id == ctx["item_id"]
        assert result.classification.proposed_patch == {"status": "completed"}
        assert result.classification.escalate is True

    return setup, check


# ---------------------------------------------------------------------------
# 7. false/future completion — must not be treated as completion.
# ---------------------------------------------------------------------------


@register("false_future_completion")
def _case_false_future_completion():
    def setup(conn, project_id):
        item, _v1 = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Priya will send the report.",
        )
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="Priya will send the report next week.",
        )
        return {"observation_id": observation.id, "item_id": item.id}

    def check(conn, project_id, ctx, result):
        assert result.classification.action is not ProposedMutationAction.COMPLETE
        assert result.classification.action is ProposedMutationAction.ADD_EVIDENCE
        assert result.classification.target_ledger_item_id == ctx["item_id"]

    return setup, check


# ---------------------------------------------------------------------------
# 8. cancellation.
# ---------------------------------------------------------------------------


@register("cancellation")
def _case_cancellation():
    def setup(conn, project_id):
        item, _v1 = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Priya will send the report.",
        )
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="This commitment is cancelled; we won't proceed with the report.",
        )
        return {"observation_id": observation.id, "item_id": item.id}

    def check(conn, project_id, ctx, result):
        assert result.classification.action is ProposedMutationAction.CANCEL
        assert result.classification.target_ledger_item_id == ctx["item_id"]
        assert result.classification.proposed_patch == {"status": "canceled"}
        assert result.classification.escalate is True

    return setup, check


# ---------------------------------------------------------------------------
# 9. delay that is not cancellation.
# ---------------------------------------------------------------------------


@register("delay_not_cancellation")
def _case_delay_not_cancellation():
    def setup(conn, project_id):
        item, _v1 = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Send the report. The report is delayed and still pending.",
        )
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="Send the report. The report is delayed and still pending.",
        )
        return {"observation_id": observation.id, "item_id": item.id}

    def check(conn, project_id, ctx, result):
        assert result.classification.action is not ProposedMutationAction.CANCEL
        assert result.classification.action is ProposedMutationAction.ADD_EVIDENCE
        assert any("delay" in reason for reason in result.classification.reasons)

    return setup, check


# ---------------------------------------------------------------------------
# 10. supersession — a new decision explicitly replaces an old one.
# ---------------------------------------------------------------------------


@register("supersession")
def _case_supersession():
    def setup(conn, project_id):
        priya = make_person(conn, display_name="Priya")
        item, _v1 = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.DECISION,
            canonical_title="Use vendor A for hosting",
            owner_person_id=priya.id,
            due_date="2026-09-01",
        )
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            kind="decision",
            subject="Use vendor B for hosting",
            statement="We will use vendor B instead of vendor A for hosting.",
            owner_text="Priya",
            date_value="2026-09-01",
        )
        return {"observation_id": observation.id, "item_id": item.id}

    def check(conn, project_id, ctx, result):
        assert result.classification.action is ProposedMutationAction.SUPERSEDE
        assert result.classification.target_ledger_item_id == ctx["item_id"]
        assert result.classification.proposed_patch["supersedes_ledger_item_id"] == ctx["item_id"]
        assert result.classification.escalate is True

    return setup, check


# ---------------------------------------------------------------------------
# 11. risk becoming a blocker — cross-kind match forces SUPERSEDE.
# ---------------------------------------------------------------------------


@register("risk_becoming_blocker")
def _case_risk_becoming_blocker():
    def setup(conn, project_id):
        priya = make_person(conn, display_name="Priya")
        item, _v1 = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.RISK,
            canonical_title="Vendor delay risk",
            owner_person_id=priya.id,
            due_date="2026-09-01",
        )
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            kind="blocker",
            subject="Vendor delay risk",
            statement="Vendor delay risk is now blocking work; we cannot proceed.",
            owner_text="Priya",
            date_value="2026-09-01",
        )
        return {"observation_id": observation.id, "item_id": item.id}

    def check(conn, project_id, ctx, result):
        assert result.classification.action is ProposedMutationAction.SUPERSEDE
        assert result.classification.target_ledger_item_id == ctx["item_id"]
        assert result.classification.proposed_patch["kind"] == "blocker"
        assert "item_kind_change_requires_supersession" in result.classification.escalation_reasons

    return setup, check


# ---------------------------------------------------------------------------
# 12. two close candidates — margin too small to trust either.
# ---------------------------------------------------------------------------


@register("two_close_candidates")
def _case_two_close_candidates():
    def setup(conn, project_id):
        priya = make_person(conn, display_name="Priya")
        item_a, _ = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Priya will send the report.",
            owner_person_id=priya.id,
            due_date="2026-08-28",
        )
        item_b, _ = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Priya will send the report.",
            owner_person_id=priya.id,
            due_date="2026-08-28",
        )
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="Priya will send the report.",
            owner_text="Priya",
            date_value="2026-08-28",
        )
        return {"observation_id": observation.id, "item_ids": {item_a.id, item_b.id}}

    def check(conn, project_id, ctx, result):
        assert result.classification.action is ProposedMutationAction.CONFLICT
        assert result.classification.target_ledger_item_id is None
        assert "multiple_candidates_within_score_margin" in result.classification.escalation_reasons
        candidate_ids = {c.ledger_item.id for c in result.match_outcome.ranked}
        assert ctx["item_ids"] <= candidate_ids

    return setup, check


# ---------------------------------------------------------------------------
# 13. corrected-field reversal — contradicts a human-corrected owner field.
# ---------------------------------------------------------------------------


@register("corrected_field_reversal")
def _case_corrected_field_reversal():
    def setup(conn, project_id):
        priya = make_person(conn, display_name="Priya")
        diego = make_person(conn, display_name="Diego")
        item, _v1 = create_ledger_item(
            conn,
            project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Priya will send the report.",
        )
        # A human already corrected the owner to Priya.
        append_ledger_version(
            conn,
            project_id,
            item.id,
            transition_type=LedgerTransitionType.CORRECT,
            status=LedgerItemStatus.OPEN,
            owner_person_id=priya.id,
            user_corrected=True,
        )
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="The report: ownership moves to Diego.",
            owner_text="Diego",
        )
        return {"observation_id": observation.id, "item_id": item.id, "diego_id": diego.id}

    def check(conn, project_id, ctx, result):
        assert "corrected_field_disagreement" in result.classification.escalation_reasons
        assert result.classification.escalate is True
        assert result.proposal.confidence_band == ConfidenceBand.LOW.value

    return setup, check


# ---------------------------------------------------------------------------
# 14. same wording in another project — no cross-project candidates.
# ---------------------------------------------------------------------------


@register("same_wording_in_another_project")
def _case_same_wording_in_another_project():
    def setup(conn, project_id):
        other_project_id = make_project(conn)
        other_priya = make_person(conn, display_name="Priya")
        create_ledger_item(
            conn,
            other_project_id,
            kind=LedgerItemKind.COMMITMENT,
            canonical_title="Send the report",
            canonical_description="Priya will send the report.",
            owner_person_id=other_priya.id,
            due_date="2026-08-28",
        )
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="Priya will send the report by Friday.",
            owner_text="Priya",
            date_value="2026-08-28",
        )
        return {"observation_id": observation.id}

    def check(conn, project_id, ctx, result):
        assert result.match_outcome.tier == "none"
        assert result.match_outcome.ranked == ()
        assert result.classification.action is ProposedMutationAction.CREATE

    return setup, check


# ---------------------------------------------------------------------------
# 15. no candidate — an `update` observation with nothing to update.
# ---------------------------------------------------------------------------


@register("no_candidate")
def _case_no_candidate():
    def setup(conn, project_id):
        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            kind="update",
            subject="That item",
            statement="That item is now due September 10th.",
            date_value="2026-09-10",
            date_text="September 10th",
        )
        return {"observation_id": observation.id}

    def check(conn, project_id, ctx, result):
        assert result.classification.action is ProposedMutationAction.CONFLICT
        assert result.classification.target_ledger_item_id is None
        assert (
            "update_observation_without_resolvable_target"
            in result.classification.escalation_reasons
        )

    return setup, check


# ---------------------------------------------------------------------------
# 16. multiple people with similar names — ambiguous owner resolution.
# ---------------------------------------------------------------------------


@register("multiple_people_with_similar_names")
def _case_multiple_people_similar_names():
    def setup(conn, project_id):
        person_a = people_repository.create_person(conn, display_name="Sam Alvarez")
        people_repository.add_alias(conn, person_a.id, alias_type=AliasType.NAME, alias_value="Sam")
        person_b = people_repository.create_person(conn, display_name="Sam Okafor")
        people_repository.add_alias(conn, person_b.id, alias_type=AliasType.NAME, alias_value="Sam")

        observation, _content, _chunk = make_observation(
            conn,
            project_id,
            subject="Send the report",
            statement="Sam will send the report.",
            owner_text="Sam",
        )
        return {"observation_id": observation.id, "person_ids": {person_a.id, person_b.id}}

    def check(conn, project_id, ctx, result):
        assert result.resolved_owner.outcome == "ambiguous"
        assert set(result.resolved_owner.candidate_person_ids) == ctx["person_ids"]
        assert result.classification.action is ProposedMutationAction.CREATE
        assert result.classification.proposed_patch["owner_person_id"] is None
        assert "ambiguous_owner" in result.classification.escalation_reasons
        assert result.classification.escalate is True

    return setup, check


# ---------------------------------------------------------------------------
# Test harness.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_reconciliation_scenarios(conn, project_id, case: Case):
    ctx = case.setup(conn, project_id)
    before = ledger_snapshot(conn, project_id)

    result = reconcile_observation(conn, project_id, ctx["observation_id"])

    after = ledger_snapshot(conn, project_id)
    assert before == after, "reconciliation must never mutate ledger_items/ledger_versions"

    case.assertions(conn, project_id, ctx, result)


def test_every_required_scenario_is_present():
    required = {
        "new_item",
        "exact_repeat",
        "second_supporting_source",
        "changed_due_date",
        "changed_owner",
        "explicit_completion",
        "false_future_completion",
        "cancellation",
        "delay_not_cancellation",
        "supersession",
        "risk_becoming_blocker",
        "two_close_candidates",
        "corrected_field_reversal",
        "same_wording_in_another_project",
        "no_candidate",
        "multiple_people_with_similar_names",
    }
    assert required <= {c.name for c in CASES}


# ---------------------------------------------------------------------------
# A few additional orchestration-level checks not implied by the scenario
# table above.
# ---------------------------------------------------------------------------


def test_reconcile_pending_observations_processes_every_valid_observation(conn, project_id):
    from project_context.services.reconciliation import reconcile_pending_observations

    make_observation(conn, project_id, subject="Send the report", statement="Priya will send it.")
    make_observation(
        conn, project_id, subject="Renew the contract", statement="Diego will renew it."
    )

    results = reconcile_pending_observations(conn, project_id)

    assert len(results) == 2
    assert all(r.created for r in results)
    assert {r.proposal.action for r in results} == {ProposedMutationAction.CREATE}


def test_reconcile_pending_observations_is_idempotent_across_runs(conn, project_id):
    from project_context.services.reconciliation import reconcile_pending_observations

    make_observation(conn, project_id, subject="Send the report", statement="Priya will send it.")

    first = reconcile_pending_observations(conn, project_id)
    second = reconcile_pending_observations(conn, project_id)

    assert len(first) == 1
    # The observation is now `reconciled`, not `valid`, so a second run
    # finds nothing left pending.
    assert second == []
    all_proposals = proposed_mutation_repository.list_pending_for_project(conn, project_id)
    assert len(all_proposals) == 1
