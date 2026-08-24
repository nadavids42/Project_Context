"""Project A — Software implementation ("Atlas Migration"): Section
13.2's required traps — "repeated action wording, one reassignment, two
due-date changes, one completion" — on the ``commitment-schema-migration``
and ``commitment-deployment-checklist`` items (see their docstrings-in-
code below for exactly which trap lives where).

17 artifacts across six simulated weeks (2026-08-03 through 2026-09-14):
4 meeting transcripts, 6 emails (one deliberately ambiguous-assignment),
3 documents, 4 calendar events. 17 ground-truth items, 29 material
transitions.
"""

from __future__ import annotations

from project_context.domain.ledger import LedgerItemKind, LedgerItemStatus
from project_context.evaluation.corpus_data.builder import (
    ArtifactSpec,
    FactBlock,
    FactSpec,
    ProjectSpec,
    assemble,
)
from project_context.evaluation.schema import (
    ArtifactKind,
    BaselineScriptedClaim,
    BriefTypeLiteral,
    Checkpoint,
    CorpusProject,
    TransitionType,
)

#: Plain first names, matching the proven golden-fixture convention of a
#: single name appearing verbatim in every statement that cites it.
BOB, PRIYA, DIEGO, ELENA, SAM = "Bob", "Priya", "Diego", "Elena", "Sam"

_K = LedgerItemKind
_S = LedgerItemStatus
_T = TransitionType


def _artifacts() -> list[ArtifactSpec]:
    return [
        ArtifactSpec(
            artifact_id="impl-w1-kickoff-vtt",
            kind=ArtifactKind.MEETING_TRANSCRIPT,
            title="Atlas Migration — Kickoff",
            occurred_at="2026-08-03T09:00:00Z",
            author="Priya",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "We decided to use PostgreSQL as the new billing datastore.",
                    FactSpec(
                        "decision-datastore",
                        _K.DECISION,
                        "Datastore choice",
                        "decision-datastore-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                    speaker="Priya",
                ),
                FactBlock(
                    f"{BOB} will finish the schema migration script by August 14th.",
                    FactSpec(
                        "commitment-schema-migration",
                        _K.COMMITMENT,
                        "Schema migration script",
                        "schema-migration-create",
                        _T.CREATE,
                        owner=BOB,
                        due_date="2026-08-14",
                        status=_S.OPEN,
                    ),
                    speaker="Bob",
                ),
                FactBlock(
                    f"{PRIYA} will finish the deployment checklist by August 14th.",
                    FactSpec(
                        "commitment-deployment-checklist",
                        _K.COMMITMENT,
                        "Deployment checklist",
                        "deployment-checklist-create",
                        _T.CREATE,
                        owner=PRIYA,
                        due_date="2026-08-14",
                        status=_S.OPEN,
                    ),
                    speaker="Priya",
                ),
                FactBlock(
                    f"{DIEGO} will finish the load testing report by August 28th.",
                    FactSpec(
                        "commitment-load-testing-report",
                        _K.COMMITMENT,
                        "Load testing report",
                        "load-testing-create",
                        _T.CREATE,
                        owner=DIEGO,
                        due_date="2026-08-28",
                        status=_S.OPEN,
                    ),
                    speaker="Diego",
                ),
                FactBlock(
                    "The migration cutover milestone is targeted for September 15th.",
                    FactSpec(
                        "milestone-migration-cutover",
                        _K.MILESTONE,
                        "Migration cutover",
                        "cutover-create",
                        _T.CREATE,
                        due_date="2026-09-15",
                        status=_S.OPEN,
                    ),
                    speaker="Priya",
                ),
                FactBlock(
                    "It is still unclear what the rollback strategy should be if the "
                    "migration fails.",
                    FactSpec(
                        "open-question-rollback-strategy",
                        _K.OPEN_QUESTION,
                        "Rollback strategy",
                        "rollback-create",
                        _T.CREATE,
                        status=_S.OPEN,
                    ),
                    speaker="Bob",
                ),
                FactBlock(
                    "By the way, the office coffee machine is broken again.",
                    fact=None,
                    speaker="Diego",
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w1-charter-doc",
            kind=ArtifactKind.DOCUMENT,
            title="Atlas Migration — Project Charter",
            occurred_at="2026-08-04T10:00:00Z",
            author="Priya",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "The staging rollout milestone is targeted for August 21st.",
                    FactSpec(
                        "milestone-staging-rollout",
                        _K.MILESTONE,
                        "Staging rollout",
                        "staging-create",
                        _T.CREATE,
                        due_date="2026-08-21",
                        status=_S.OPEN,
                    ),
                ),
                FactBlock(
                    f"{ELENA} is the client executive sponsor for the Atlas Migration project.",
                    FactSpec(
                        "stakeholder-elena",
                        _K.STAKEHOLDER,
                        "Elena",
                        "elena-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                ),
                FactBlock(
                    f"{PRIYA} flagged a risk: legacy exporter compatibility could delay "
                    "the project.",
                    FactSpec(
                        "risk-legacy-exporter",
                        _K.RISK,
                        "Legacy exporter compatibility risk",
                        "legacy-exporter-create",
                        _T.CREATE,
                        owner=PRIYA,
                        status=_S.OPEN,
                    ),
                ),
                FactBlock("This charter will be revisited at the midpoint review.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w1-priya-email",
            kind=ArtifactKind.EMAIL,
            title="Re: deployment checklist",
            occurred_at="2026-08-05T09:30:00Z",
            author="Priya",
            project_key="implementation",
            blocks=[
                FactBlock(
                    f"{PRIYA} will finish the deployment checklist by August 14th.",
                    FactSpec(
                        "commitment-deployment-checklist",
                        _K.COMMITMENT,
                        "Deployment checklist",
                        "deployment-checklist-create",
                        _T.CREATE,
                        owner=PRIYA,
                        due_date="2026-08-14",
                        status=_S.OPEN,
                    ),
                ),
                FactBlock(
                    "Also, does anyone know if the printer on the third floor is fixed yet?",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w1-cal-standup",
            kind=ArtifactKind.CALENDAR_EVENT,
            title="Atlas Migration — Weekly Standup",
            occurred_at="2026-08-07T09:00:00Z",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "Atlas Migration Weekly Standup. Attendees: Bob, Priya, Diego. "
                    "Recurring Monday 9:00am sync.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w2-standup-vtt",
            kind=ArtifactKind.MEETING_TRANSCRIPT,
            title="Atlas Migration — Week 2 Standup",
            occurred_at="2026-08-10T09:00:00Z",
            author="Priya",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "Schema migration script: ownership moves to Diego.",
                    FactSpec(
                        "commitment-schema-migration",
                        _K.COMMITMENT,
                        "Schema migration script",
                        "schema-migration-owner",
                        _T.UPDATE_OWNER,
                        owner=DIEGO,
                    ),
                    speaker="Priya",
                ),
                FactBlock(
                    f"{PRIYA} will finish the runbook documentation by September 4th.",
                    FactSpec(
                        "commitment-runbook-documentation",
                        _K.COMMITMENT,
                        "Runbook documentation",
                        "runbook-create",
                        _T.CREATE,
                        owner=PRIYA,
                        due_date="2026-09-04",
                        status=_S.OPEN,
                    ),
                    speaker="Bob",
                ),
                FactBlock(
                    f"{DIEGO} flagged a risk: vendor API rate limits could delay the project.",
                    FactSpec(
                        "risk-vendor-rate-limits",
                        _K.RISK,
                        "Vendor API rate limits",
                        "vendor-risk-create",
                        _T.CREATE,
                        owner=DIEGO,
                        status=_S.OPEN,
                    ),
                    speaker="Diego",
                ),
                FactBlock(
                    f"{SAM} is now the QA lead for the Atlas Migration project.",
                    FactSpec(
                        "stakeholder-sam",
                        _K.STAKEHOLDER,
                        "Sam",
                        "sam-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                    speaker="Priya",
                ),
                FactBlock(
                    "The deployment checklist was approved by the client.",
                    FactSpec(
                        "commitment-deployment-checklist",
                        _K.COMMITMENT,
                        "Deployment checklist",
                        "deployment-checklist-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                    speaker="Bob",
                ),
                FactBlock(
                    "By the way, the elevator in the east wing is out of service again.",
                    fact=None,
                    speaker="Diego",
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w2-diego-email",
            kind=ArtifactKind.EMAIL,
            title="Schema migration — new target date",
            occurred_at="2026-08-12T15:00:00Z",
            author="Diego",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "The schema migration script deadline moved to August 21st.",
                    FactSpec(
                        "commitment-schema-migration",
                        _K.COMMITMENT,
                        "Schema migration script",
                        "schema-migration-date1",
                        _T.UPDATE_DATE,
                        due_date="2026-08-21",
                    ),
                ),
                FactBlock(
                    "Also, heads up that the shared drive was reorganized yesterday.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w2-cal-review",
            kind=ArtifactKind.CALENDAR_EVENT,
            title="Atlas Migration — Mid-sprint Review",
            occurred_at="2026-08-13T09:00:00Z",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "Atlas Migration Mid-sprint Review. Attendees: Bob, Priya, Diego, Sam.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w3-standup-vtt",
            kind=ArtifactKind.MEETING_TRANSCRIPT,
            title="Atlas Migration — Week 3 Standup",
            occurred_at="2026-08-17T09:00:00Z",
            author="Priya",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "The production sign-off milestone is targeted for September 10th.",
                    FactSpec(
                        "milestone-production-signoff",
                        _K.MILESTONE,
                        "Production sign-off",
                        "production-signoff-create",
                        _T.CREATE,
                        due_date="2026-09-10",
                        status=_S.OPEN,
                    ),
                    speaker="Priya",
                ),
                FactBlock(
                    f"{BOB} will finish the data validation script by September 1st.",
                    FactSpec(
                        "commitment-data-validation-script",
                        _K.COMMITMENT,
                        "Data validation script",
                        "data-validation-create",
                        _T.CREATE,
                        owner=BOB,
                        due_date="2026-09-01",
                        status=_S.OPEN,
                    ),
                    speaker="Bob",
                ),
                FactBlock(
                    f"{DIEGO} is now responsible for deciding the on-call rotation "
                    "ownership question.",
                    FactSpec(
                        "open-question-oncall-rotation",
                        _K.OPEN_QUESTION,
                        "On-call rotation ownership",
                        "oncall-create",
                        _T.CREATE,
                        owner=DIEGO,
                        status=_S.OPEN,
                    ),
                    speaker="Priya",
                ),
                FactBlock(
                    f"{ELENA} will finish the API contract sign-off by August 25th.",
                    FactSpec(
                        "commitment-api-contract-signoff",
                        _K.COMMITMENT,
                        "API contract sign-off",
                        "api-signoff-create",
                        _T.CREATE,
                        owner=ELENA,
                        due_date="2026-08-25",
                        status=_S.OPEN,
                    ),
                    speaker="Diego",
                ),
                FactBlock(
                    "By the way, the vending machine on this floor ran out of snacks again.",
                    fact=None,
                    speaker="Bob",
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w3-staging-doc",
            kind=ArtifactKind.DOCUMENT,
            title="Atlas Migration — Staging Rollout Notes",
            occurred_at="2026-08-19T11:00:00Z",
            author="Diego",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "The staging rollout milestone was deployed successfully.",
                    FactSpec(
                        "milestone-staging-rollout",
                        _K.MILESTONE,
                        "Staging rollout",
                        "staging-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                ),
                FactBlock("Smoke tests passed with no notable regressions.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w3-ambiguous-email",
            kind=ArtifactKind.EMAIL,
            title="Quick flag re: Atlas ticket",
            occurred_at="2026-08-20T16:00:00Z",
            author="Sam",
            project_key="implementation",
            ambiguous_assignment=True,
            blocks=[
                FactBlock(
                    "Quick note: saw an Atlas ticket about dashboard capacity, not sure if "
                    "that's our migration or the unrelated internal Atlas analytics tool.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w4-standup-vtt",
            kind=ArtifactKind.MEETING_TRANSCRIPT,
            title="Atlas Migration — Week 4 Standup",
            occurred_at="2026-08-24T09:00:00Z",
            author="Priya",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "We decided to run the production cutover during the Saturday night "
                    "maintenance window.",
                    FactSpec(
                        "decision-cutover-window",
                        _K.DECISION,
                        "Cutover maintenance window",
                        "cutover-window-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                    speaker="Priya",
                ),
                FactBlock(
                    "The schema migration script deadline moved to August 28th.",
                    FactSpec(
                        "commitment-schema-migration",
                        _K.COMMITMENT,
                        "Schema migration script",
                        "schema-migration-date2",
                        _T.UPDATE_DATE,
                        due_date="2026-08-28",
                    ),
                    speaker="Diego",
                ),
                FactBlock(
                    "The migration cutover milestone new target is September 8th.",
                    FactSpec(
                        "milestone-migration-cutover",
                        _K.MILESTONE,
                        "Migration cutover",
                        "cutover-date",
                        _T.UPDATE_DATE,
                        due_date="2026-09-08",
                    ),
                    speaker="Priya",
                ),
                FactBlock(
                    f"{PRIYA} confirmed the legacy exporter compatibility risk is now resolved "
                    "after the vendor patch shipped.",
                    FactSpec(
                        "risk-legacy-exporter",
                        _K.RISK,
                        "Legacy exporter compatibility risk",
                        "legacy-exporter-resolve",
                        _T.UPDATE_STATUS,
                        status=_S.RESOLVED,
                    ),
                    speaker="Diego",
                ),
                FactBlock(
                    "By the way, the parking garage badge readers are down again.",
                    fact=None,
                    speaker="Bob",
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w4-cal-cutover-prep",
            kind=ArtifactKind.CALENDAR_EVENT,
            title="Atlas Migration — Cutover Prep Session",
            occurred_at="2026-08-26T09:00:00Z",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "Atlas Migration Cutover Prep Session. Attendees: Bob, Priya, Diego, Elena.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w5-priya-email",
            kind=ArtifactKind.EMAIL,
            title="Runbook + rollback strategy updates",
            occurred_at="2026-08-31T10:00:00Z",
            author="Priya",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "The runbook documentation is no longer needed since the vendor now "
                    "provides one.",
                    FactSpec(
                        "commitment-runbook-documentation",
                        _K.COMMITMENT,
                        "Runbook documentation",
                        "runbook-cancel",
                        _T.UPDATE_STATUS,
                        status=_S.CANCELED,
                    ),
                ),
                FactBlock(
                    "The rollback strategy question was answered: revert to the read replica "
                    "and replay the write-ahead log.",
                    FactSpec(
                        "open-question-rollback-strategy",
                        _K.OPEN_QUESTION,
                        "Rollback strategy",
                        "rollback-resolve",
                        _T.UPDATE_STATUS,
                        status=_S.RESOLVED,
                    ),
                ),
                FactBlock("Thanks everyone for the hard work this sprint.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w5-schema-signoff-email",
            kind=ArtifactKind.EMAIL,
            title="Schema migration script — signed off",
            occurred_at="2026-09-02T13:00:00Z",
            author="Diego",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "The schema migration script was sent to the client for review and signed off.",
                    FactSpec(
                        "commitment-schema-migration",
                        _K.COMMITMENT,
                        "Schema migration script",
                        "schema-migration-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                ),
                FactBlock("Nice milestone to close out the week.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w5-cal-final-review",
            kind=ArtifactKind.CALENDAR_EVENT,
            title="Atlas Migration — Final Review",
            occurred_at="2026-09-04T09:00:00Z",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "Atlas Migration Final Review. Attendees: Bob, Priya, Diego, Elena, Sam.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w6-data-validation-email",
            kind=ArtifactKind.EMAIL,
            title="Data validation script — deployed",
            occurred_at="2026-09-08T09:00:00Z",
            author="Bob",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "The data validation script was deployed to staging.",
                    FactSpec(
                        "commitment-data-validation-script",
                        _K.COMMITMENT,
                        "Data validation script",
                        "data-validation-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                ),
                FactBlock("No issues found in the first overnight run.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="impl-w6-cutover-complete-doc",
            kind=ArtifactKind.DOCUMENT,
            title="Atlas Migration — Cutover Complete",
            occurred_at="2026-09-14T17:00:00Z",
            author="Priya",
            project_key="implementation",
            blocks=[
                FactBlock(
                    "The migration cutover milestone is complete.",
                    FactSpec(
                        "milestone-migration-cutover",
                        _K.MILESTONE,
                        "Migration cutover",
                        "cutover-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                ),
                FactBlock("The team will reconvene next quarter for the retro.", fact=None),
            ],
        ),
    ]


def _checkpoints() -> tuple[Checkpoint, ...]:
    return (
        Checkpoint(
            checkpoint_id="impl-cp-before-w2",
            cutoff_at="2026-08-10T09:00:00Z",
            brief_type=BriefTypeLiteral.MEETING_PREPARATION,
            meeting_title="Atlas Migration — Week 2 Standup",
            meeting_scheduled_at="2026-08-10T09:00:00Z",
            participant_lines=("Bob", "Priya", "Diego"),
        ),
        Checkpoint(
            checkpoint_id="impl-cp-before-w3",
            cutoff_at="2026-08-17T09:00:00Z",
            brief_type=BriefTypeLiteral.MEETING_PREPARATION,
            meeting_title="Atlas Migration — Week 3 Standup",
            meeting_scheduled_at="2026-08-17T09:00:00Z",
            participant_lines=("Bob", "Priya", "Diego", "Sam"),
        ),
        Checkpoint(
            checkpoint_id="impl-cp-before-w4",
            cutoff_at="2026-08-24T09:00:00Z",
            brief_type=BriefTypeLiteral.MEETING_PREPARATION,
            meeting_title="Atlas Migration — Week 4 Standup",
            meeting_scheduled_at="2026-08-24T09:00:00Z",
            participant_lines=("Bob", "Priya", "Diego", "Elena"),
        ),
        Checkpoint(
            checkpoint_id="impl-cp-final",
            cutoff_at="2026-09-15T00:00:00Z",
            brief_type=BriefTypeLiteral.CURRENT_PROJECT,
        ),
    )


def _baseline_fake_claims() -> dict[str, tuple[BaselineScriptedClaim, ...]]:
    """Hand-authored, deliberately imperfect claims illustrating a
    recent-context baseline's core weakness on this project: it loses
    track of who owns the schema migration and what its due date is once
    the reassignment/date-change evidence ages out of view (Section
    13.2's "one reassignment, two due-date changes" traps). See
    `project_context.evaluation.baseline_runner`'s module docstring for
    why fake-mode baseline output is scripted-illustrative, not derived
    from ground truth."""
    return {
        "impl-cp-before-w2": (
            BaselineScriptedClaim(
                section="open_commitments",
                text="Bob owns the schema migration script, due August 14.",
                item_kind=_K.COMMITMENT,
                item_title="Schema migration script",
                status=_S.OPEN,
                owner=BOB,
                due_date="2026-08-14",
                evidence=(
                    (
                        "impl-w1-kickoff-vtt",
                        "Bob will finish the schema migration script by August 14th.",
                    ),
                ),
            ),
            BaselineScriptedClaim(
                section="open_commitments",
                text="Priya owns the deployment checklist, due August 14.",
                item_kind=_K.COMMITMENT,
                item_title="Deployment checklist",
                status=_S.OPEN,
                owner=PRIYA,
                due_date="2026-08-14",
                evidence=(
                    (
                        "impl-w1-kickoff-vtt",
                        "Priya will finish the deployment checklist by August 14th.",
                    ),
                ),
            ),
        ),
        "impl-cp-before-w3": (
            # Recent-context miss: the reassignment/date-change email is
            # already out of the window, so the baseline still reports
            # the stale owner and the stale due date.
            BaselineScriptedClaim(
                section="open_commitments",
                text="Bob owns the schema migration script, due August 14.",
                item_kind=_K.COMMITMENT,
                item_title="Schema migration script",
                status=_S.OPEN,
                owner=BOB,
                due_date="2026-08-14",
                evidence=(
                    (
                        "impl-w1-kickoff-vtt",
                        "Bob will finish the schema migration script by August 14th.",
                    ),
                ),
            ),
            BaselineScriptedClaim(
                section="recent_changes",
                text="The deployment checklist was approved by the client.",
                item_kind=_K.COMMITMENT,
                item_title="Deployment checklist",
                status=_S.COMPLETED,
                evidence=(
                    ("impl-w2-standup-vtt", "The deployment checklist was approved by the client."),
                ),
            ),
        ),
        "impl-cp-before-w4": (
            BaselineScriptedClaim(
                section="open_commitments",
                text="Diego owns the schema migration script, due August 21.",
                item_kind=_K.COMMITMENT,
                item_title="Schema migration script",
                status=_S.OPEN,
                owner=DIEGO,
                due_date="2026-08-21",
                evidence=(
                    (
                        "impl-w2-diego-email",
                        "The schema migration script deadline moved to August 21st.",
                    ),
                ),
            ),
        ),
        "impl-cp-final": (
            # Still shows the schema migration as open with the
            # once-reassigned owner and the first extended date — the
            # second date change and the final completion both fell
            # outside whatever the model most recently saw.
            BaselineScriptedClaim(
                section="open_commitments",
                text="Diego owns the schema migration script, due August 21.",
                item_kind=_K.COMMITMENT,
                item_title="Schema migration script",
                status=_S.OPEN,
                owner=DIEGO,
                due_date="2026-08-21",
                evidence=(
                    (
                        "impl-w2-diego-email",
                        "The schema migration script deadline moved to August 21st.",
                    ),
                ),
            ),
            BaselineScriptedClaim(
                section="recent_changes",
                text="The migration cutover milestone is complete.",
                item_kind=_K.MILESTONE,
                item_title="Migration cutover",
                status=_S.COMPLETED,
                evidence=(
                    (
                        "impl-w6-cutover-complete-doc",
                        "The migration cutover milestone is complete.",
                    ),
                ),
            ),
        ),
    }


def build() -> tuple[CorpusProject, int]:
    spec = ProjectSpec(
        key="implementation",
        name="Atlas Migration",
        objective="Migrate the billing datastore to PostgreSQL without downtime.",
        stage="Build",
        artifacts=_artifacts(),
        checkpoints=_checkpoints(),
        baseline_fake_claims=_baseline_fake_claims(),
    )
    return assemble(spec)
