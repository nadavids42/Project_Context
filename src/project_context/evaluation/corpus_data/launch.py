"""Project C — Product launch ("Comet Launch"): Section 13.2's required
traps — "risk becomes blocker, canceled milestone, stale document
contradicts newer meeting."

- **Risk becomes blocker**: `risk-appstore-review` is superseded by
  `blocker-appstore-review`, reusing the exact proven blocking-language
  pattern (`project_context.domain.reconciliation_language.
  BLOCKING_PHRASES`: "...is now blocking work; we cannot proceed.")
  `tests/golden_projects/golden_fixtures.py` already validates end to
  end for its own risk/blocker pair.
- **Canceled milestone**: `milestone-marketing-campaign-launch` is
  created, then explicitly canceled ("no longer needed").
- **Stale document contradicts newer meeting**:
  `milestone-public-launch` is first created from a week-one marketing
  brief *document* stating November 5th; three weeks later, the week-4
  standup *transcript* explicitly moves the date to November 12th. The
  document itself is never edited or removed — it stays in the corpus
  exactly as authored, so a system that does not track supersession
  correctly (Section 13.4's baseline, by construction) risks citing the
  stale November 5th date as current. The fake-mode baseline fixture
  (`_baseline_fake_claims`) deliberately does this at the final
  checkpoint — a directly scoreable materially-misleading-claim case.

17 artifacts across five simulated weeks (2026-10-05 through
2026-11-09): 4 meeting transcripts, 6 emails (one deliberately
ambiguous-assignment), 3 documents, 4 calendar events. 17 ground-truth
items, 27 material transitions.
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

CASEY, MORGAN, TAYLOR, JAMIE, AVERY = "Casey", "Morgan", "Taylor", "Jamie", "Avery"

_K = LedgerItemKind
_S = LedgerItemStatus
_T = TransitionType


def _artifacts() -> list[ArtifactSpec]:
    return [
        ArtifactSpec(
            artifact_id="launch-w1-kickoff-vtt",
            kind=ArtifactKind.MEETING_TRANSCRIPT,
            title="Comet Launch — Kickoff",
            occurred_at="2026-10-05T09:00:00Z",
            author="Casey",
            project_key="launch",
            blocks=[
                FactBlock(
                    "We decided to launch on the web app first, not mobile.",
                    FactSpec(
                        "decision-launch-channel",
                        _K.DECISION,
                        "Launch channel",
                        "channel-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                    speaker="Casey",
                ),
                FactBlock(
                    f"{CASEY} will finish the press release draft by October 19th.",
                    FactSpec(
                        "commitment-press-release",
                        _K.COMMITMENT,
                        "Press release draft",
                        "press-release-create",
                        _T.CREATE,
                        owner=CASEY,
                        due_date="2026-10-19",
                        status=_S.OPEN,
                    ),
                    speaker="Morgan",
                ),
                FactBlock(
                    f"{MORGAN} will finish the app store submission by October 15th.",
                    FactSpec(
                        "commitment-appstore-submission",
                        _K.COMMITMENT,
                        "App store submission",
                        "appstore-submission-create",
                        _T.CREATE,
                        owner=MORGAN,
                        due_date="2026-10-15",
                        status=_S.OPEN,
                    ),
                    speaker="Taylor",
                ),
                FactBlock(
                    "The beta rollout milestone is targeted for October 12th.",
                    FactSpec(
                        "milestone-beta-rollout",
                        _K.MILESTONE,
                        "Beta rollout",
                        "beta-rollout-create",
                        _T.CREATE,
                        due_date="2026-10-12",
                        status=_S.OPEN,
                    ),
                    speaker="Casey",
                ),
                FactBlock(
                    f"{MORGAN} flagged a risk: app store review could delay the project.",
                    FactSpec(
                        "risk-appstore-review",
                        _K.RISK,
                        "App store review risk",
                        "appstore-risk-create",
                        _T.CREATE,
                        owner=MORGAN,
                        status=_S.OPEN,
                    ),
                    speaker="Morgan",
                ),
                FactBlock(
                    "It is still unclear what the refund policy wording should be at launch.",
                    FactSpec(
                        "open-question-refund-policy",
                        _K.OPEN_QUESTION,
                        "Refund policy wording",
                        "refund-policy-create",
                        _T.CREATE,
                        status=_S.OPEN,
                    ),
                    speaker="Taylor",
                ),
                FactBlock(
                    "By the way, the office plants finally got watered this week.",
                    fact=None,
                    speaker="Casey",
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w1-marketing-brief-doc",
            kind=ArtifactKind.DOCUMENT,
            title="Comet Launch — Marketing Brief",
            occurred_at="2026-10-05T13:00:00Z",
            author="Taylor",
            project_key="launch",
            blocks=[
                FactBlock(
                    "The public launch milestone is targeted for November 5th.",
                    FactSpec(
                        "milestone-public-launch",
                        _K.MILESTONE,
                        "Public launch",
                        "public-launch-create",
                        _T.CREATE,
                        due_date="2026-11-05",
                        status=_S.OPEN,
                    ),
                ),
                FactBlock(
                    "The marketing campaign launch milestone is targeted for October 29th.",
                    FactSpec(
                        "milestone-marketing-campaign-launch",
                        _K.MILESTONE,
                        "Marketing campaign launch",
                        "marketing-campaign-create",
                        _T.CREATE,
                        due_date="2026-10-29",
                        status=_S.OPEN,
                    ),
                ),
                FactBlock(
                    f"{AVERY} is the executive sponsor for the Comet Launch project.",
                    FactSpec(
                        "stakeholder-avery",
                        _K.STAKEHOLDER,
                        "Avery",
                        "avery-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                ),
                FactBlock(
                    f"{JAMIE} is the support lead for the Comet Launch project.",
                    FactSpec(
                        "stakeholder-jamie",
                        _K.STAKEHOLDER,
                        "Jamie",
                        "jamie-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                ),
                FactBlock("This brief will be updated as launch plans firm up.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w1-checklist-email",
            kind=ArtifactKind.EMAIL,
            title="Launch checklist owner",
            occurred_at="2026-10-06T09:30:00Z",
            author="Casey",
            project_key="launch",
            blocks=[
                FactBlock(
                    f"{CASEY} will finish the launch checklist by October 26th.",
                    FactSpec(
                        "commitment-launch-checklist",
                        _K.COMMITMENT,
                        "Launch checklist",
                        "launch-checklist-create",
                        _T.CREATE,
                        owner=CASEY,
                        due_date="2026-10-26",
                        status=_S.OPEN,
                    ),
                ),
                FactBlock("Will circulate a draft for review by Friday.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w1-cal-standup",
            kind=ArtifactKind.CALENDAR_EVENT,
            title="Comet Launch — Weekly Standup",
            occurred_at="2026-10-08T09:00:00Z",
            project_key="launch",
            blocks=[
                FactBlock(
                    "Comet Launch Weekly Standup. Attendees: Casey, Morgan, Taylor. "
                    "Recurring Monday 9:00am.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w2-standup-vtt",
            kind=ArtifactKind.MEETING_TRANSCRIPT,
            title="Comet Launch — Week 2 Standup",
            occurred_at="2026-10-12T09:00:00Z",
            author="Casey",
            project_key="launch",
            blocks=[
                FactBlock(
                    "The app store submission was submitted and approved by Apple.",
                    FactSpec(
                        "commitment-appstore-submission",
                        _K.COMMITMENT,
                        "App store submission",
                        "appstore-submission-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                    speaker="Casey",
                ),
                FactBlock(
                    "The beta rollout milestone was deployed successfully.",
                    FactSpec(
                        "milestone-beta-rollout",
                        _K.MILESTONE,
                        "Beta rollout",
                        "beta-rollout-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                    speaker="Morgan",
                ),
                FactBlock(
                    f"{JAMIE} will finish the support runbook by November 2nd.",
                    FactSpec(
                        "commitment-support-runbook",
                        _K.COMMITMENT,
                        "Support runbook",
                        "support-runbook-create",
                        _T.CREATE,
                        owner=JAMIE,
                        due_date="2026-11-02",
                        status=_S.OPEN,
                    ),
                    speaker="Taylor",
                ),
                FactBlock(
                    f"{TAYLOR} flagged a risk: the third-party payment API could delay "
                    "the project.",
                    FactSpec(
                        "risk-third-party-api",
                        _K.RISK,
                        "Third-party payment API risk",
                        "third-party-api-risk-create",
                        _T.CREATE,
                        owner=TAYLOR,
                        status=_S.OPEN,
                    ),
                    speaker="Casey",
                ),
                FactBlock(
                    "By the way, the building fire drill is scheduled for next Tuesday.",
                    fact=None,
                    speaker="Morgan",
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w2-doc",
            kind=ArtifactKind.DOCUMENT,
            title="Comet Launch — Beta Feedback Summary",
            occurred_at="2026-10-14T10:00:00Z",
            author="Taylor",
            project_key="launch",
            blocks=[
                FactBlock(
                    "Beta feedback summary: overall reception was very positive, with no "
                    "material action items.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w2-cal-review",
            kind=ArtifactKind.CALENDAR_EVENT,
            title="Comet Launch — Readiness Review",
            occurred_at="2026-10-15T09:00:00Z",
            project_key="launch",
            blocks=[
                FactBlock(
                    "Comet Launch Readiness Review. Attendees: Casey, Morgan, Taylor, Jamie.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w3-standup-vtt",
            kind=ArtifactKind.MEETING_TRANSCRIPT,
            title="Comet Launch — Week 3 Standup",
            occurred_at="2026-10-19T09:00:00Z",
            author="Casey",
            project_key="launch",
            blocks=[
                FactBlock(
                    "The press release draft was sent to the press list.",
                    FactSpec(
                        "commitment-press-release",
                        _K.COMMITMENT,
                        "Press release draft",
                        "press-release-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                    speaker="Casey",
                ),
                FactBlock(
                    "The marketing campaign launch milestone is no longer needed since we "
                    "are folding it into the public launch announcement.",
                    FactSpec(
                        "milestone-marketing-campaign-launch",
                        _K.MILESTONE,
                        "Marketing campaign launch",
                        "marketing-campaign-cancel",
                        _T.UPDATE_STATUS,
                        status=_S.CANCELED,
                    ),
                    speaker="Morgan",
                ),
                FactBlock(
                    f"{MORGAN} confirmed the app store review risk is now blocking work; "
                    "we cannot proceed.",
                    FactSpec(
                        "blocker-appstore-review",
                        _K.BLOCKER,
                        "App store review risk",
                        "appstore-blocker-supersede",
                        _T.SUPERSEDE,
                        owner=MORGAN,
                        status=_S.OPEN,
                        supersedes_item_id="risk-appstore-review",
                    ),
                    speaker="Taylor",
                ),
                FactBlock(
                    f"{JAMIE} will finish the support team training by October 29th.",
                    FactSpec(
                        "commitment-support-training",
                        _K.COMMITMENT,
                        "Support team training",
                        "support-training-create",
                        _T.CREATE,
                        owner=JAMIE,
                        due_date="2026-10-29",
                        status=_S.OPEN,
                    ),
                    speaker="Casey",
                ),
                FactBlock(
                    f"{TAYLOR} will finish the launch FAQ document by October 30th.",
                    FactSpec(
                        "commitment-faq-doc",
                        _K.COMMITMENT,
                        "Launch FAQ document",
                        "faq-doc-create",
                        _T.CREATE,
                        owner=TAYLOR,
                        due_date="2026-10-30",
                        status=_S.OPEN,
                    ),
                    speaker="Morgan",
                ),
                FactBlock(
                    "By the way, the office wifi was flaky again this morning.",
                    fact=None,
                    speaker="Taylor",
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w3-ambiguous-email",
            kind=ArtifactKind.EMAIL,
            title="Quick flag re: Comet ticket",
            occurred_at="2026-10-21T16:00:00Z",
            author="Jamie",
            project_key="launch",
            ambiguous_assignment=True,
            blocks=[
                FactBlock(
                    "Quick note: saw a Comet ticket about billing, not sure if that's our "
                    "launch or the unrelated legacy Comet CRM the sales team still runs.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w4-standup-vtt",
            kind=ArtifactKind.MEETING_TRANSCRIPT,
            title="Comet Launch — Week 4 Standup",
            occurred_at="2026-10-26T09:00:00Z",
            author="Casey",
            project_key="launch",
            blocks=[
                FactBlock(
                    "The public launch milestone new target is November 12th.",
                    FactSpec(
                        "milestone-public-launch",
                        _K.MILESTONE,
                        "Public launch",
                        "public-launch-date",
                        _T.UPDATE_DATE,
                        due_date="2026-11-12",
                    ),
                    speaker="Casey",
                ),
                FactBlock(
                    "We decided to offer a single paid pricing tier at launch, no free tier.",
                    FactSpec(
                        "decision-pricing-tier",
                        _K.DECISION,
                        "Pricing tier",
                        "pricing-tier-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                    speaker="Morgan",
                ),
                FactBlock(
                    "By the way, the new coffee blend in the kitchen is a big hit.",
                    fact=None,
                    speaker="Taylor",
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w4-cal-signoff-prep",
            kind=ArtifactKind.CALENDAR_EVENT,
            title="Comet Launch — Sign-off Prep",
            occurred_at="2026-10-28T09:00:00Z",
            project_key="launch",
            blocks=[
                FactBlock(
                    "Comet Launch Sign-off Prep. Attendees: Casey, Morgan, Taylor, Avery.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w5-runbook-email",
            kind=ArtifactKind.EMAIL,
            title="Support runbook + refund policy updates",
            occurred_at="2026-11-02T10:00:00Z",
            author="Jamie",
            project_key="launch",
            blocks=[
                FactBlock(
                    "The support runbook is no longer needed since the vendor now provides one.",
                    FactSpec(
                        "commitment-support-runbook",
                        _K.COMMITMENT,
                        "Support runbook",
                        "support-runbook-cancel",
                        _T.UPDATE_STATUS,
                        status=_S.CANCELED,
                    ),
                ),
                FactBlock(
                    "The refund policy wording question was answered: full refunds within "
                    "14 days of purchase.",
                    FactSpec(
                        "open-question-refund-policy",
                        _K.OPEN_QUESTION,
                        "Refund policy wording",
                        "refund-policy-resolve",
                        _T.UPDATE_STATUS,
                        status=_S.RESOLVED,
                    ),
                ),
                FactBlock("Support team is ready either way.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w5-training-email",
            kind=ArtifactKind.EMAIL,
            title="Support training + FAQ — complete",
            occurred_at="2026-11-04T13:00:00Z",
            author="Taylor",
            project_key="launch",
            blocks=[
                FactBlock(
                    "The support team training is complete.",
                    FactSpec(
                        "commitment-support-training",
                        _K.COMMITMENT,
                        "Support team training",
                        "support-training-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                ),
                FactBlock(
                    "The launch FAQ document was sent to the client for review.",
                    FactSpec(
                        "commitment-faq-doc",
                        _K.COMMITMENT,
                        "Launch FAQ document",
                        "faq-doc-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                ),
                FactBlock("Great momentum heading into launch week.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w5-cal-final",
            kind=ArtifactKind.CALENDAR_EVENT,
            title="Comet Launch — Final Go/No-Go",
            occurred_at="2026-11-05T09:00:00Z",
            project_key="launch",
            blocks=[
                FactBlock(
                    "Comet Launch Final Go/No-Go. Attendees: Casey, Morgan, Taylor, Jamie, Avery.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w6-wrapup-email",
            kind=ArtifactKind.EMAIL,
            title="Launch week wrap-up",
            occurred_at="2026-11-06T09:00:00Z",
            author="Casey",
            project_key="launch",
            blocks=[
                FactBlock("Thanks everyone for a smooth launch week.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="launch-w6-final-doc",
            kind=ArtifactKind.DOCUMENT,
            title="Comet Launch — Launch Complete",
            occurred_at="2026-11-09T17:00:00Z",
            author="Casey",
            project_key="launch",
            blocks=[
                FactBlock(
                    "The public launch milestone is complete.",
                    FactSpec(
                        "milestone-public-launch",
                        _K.MILESTONE,
                        "Public launch",
                        "public-launch-complete",
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
            checkpoint_id="launch-cp-before-w2",
            cutoff_at="2026-10-12T09:00:00Z",
            brief_type=BriefTypeLiteral.MEETING_PREPARATION,
            meeting_title="Comet Launch — Week 2 Standup",
            meeting_scheduled_at="2026-10-12T09:00:00Z",
            participant_lines=("Casey", "Morgan", "Taylor"),
        ),
        Checkpoint(
            checkpoint_id="launch-cp-before-w3",
            cutoff_at="2026-10-19T09:00:00Z",
            brief_type=BriefTypeLiteral.MEETING_PREPARATION,
            meeting_title="Comet Launch — Week 3 Standup",
            meeting_scheduled_at="2026-10-19T09:00:00Z",
            participant_lines=("Casey", "Morgan", "Taylor", "Jamie"),
        ),
        Checkpoint(
            checkpoint_id="launch-cp-before-w4",
            cutoff_at="2026-10-26T09:00:00Z",
            brief_type=BriefTypeLiteral.MEETING_PREPARATION,
            meeting_title="Comet Launch — Week 4 Standup",
            meeting_scheduled_at="2026-10-26T09:00:00Z",
            participant_lines=("Casey", "Morgan", "Taylor", "Avery"),
            required_phrases=(),
            forbidden_phrases=(),
        ),
        Checkpoint(
            checkpoint_id="launch-cp-final",
            cutoff_at="2026-11-10T00:00:00Z",
            brief_type=BriefTypeLiteral.CURRENT_PROJECT,
        ),
    )


def _baseline_fake_claims() -> dict[str, tuple[BaselineScriptedClaim, ...]]:
    """Illustrates all three of Section 13.2's required traps for this
    project. Hand-authored, deliberately imperfect (see
    `project_context.evaluation.baseline_runner`'s module docstring)."""
    return {
        "launch-cp-before-w2": (
            BaselineScriptedClaim(
                section="open_commitments",
                text="Casey owns the press release draft, due October 19.",
                item_kind=_K.COMMITMENT,
                item_title="Press release draft",
                status=_S.OPEN,
                owner=CASEY,
                due_date="2026-10-19",
                evidence=(
                    (
                        "launch-w1-kickoff-vtt",
                        "Casey will finish the press release draft by October 19th.",
                    ),
                ),
            ),
        ),
        "launch-cp-before-w3": (
            # Risk-to-blocker miss: still calling it a risk after it
            # became a blocker.
            BaselineScriptedClaim(
                section="risks_and_blockers",
                text="App store review is an open risk.",
                item_kind=_K.RISK,
                item_title="App store review risk",
                status=_S.OPEN,
                owner=MORGAN,
                evidence=(
                    (
                        "launch-w1-kickoff-vtt",
                        "Morgan flagged a risk: app store review could delay the project.",
                    ),
                ),
            ),
        ),
        "launch-cp-before-w4": (
            # Canceled-milestone miss: still lists it as an active target.
            BaselineScriptedClaim(
                section="next_milestone",
                text="The marketing campaign launch milestone is targeted for October 29.",
                item_kind=_K.MILESTONE,
                item_title="Marketing campaign launch",
                status=_S.OPEN,
                due_date="2026-10-29",
                evidence=(
                    (
                        "launch-w1-marketing-brief-doc",
                        "The marketing campaign launch milestone is targeted for October 29th.",
                    ),
                ),
            ),
        ),
        "launch-cp-final": (
            # Stale-document-vs-newer-meeting trap: cites the marketing
            # brief's original November 5 date, missing the week-4
            # meeting's correction to November 12.
            BaselineScriptedClaim(
                section="next_milestone",
                text="The public launch milestone is targeted for November 5.",
                item_kind=_K.MILESTONE,
                item_title="Public launch",
                status=_S.OPEN,
                due_date="2026-11-05",
                evidence=(
                    (
                        "launch-w1-marketing-brief-doc",
                        "The public launch milestone is targeted for November 5th.",
                    ),
                ),
            ),
            BaselineScriptedClaim(
                section="risks_and_blockers",
                text="App store review is an open risk.",
                item_kind=_K.RISK,
                item_title="App store review risk",
                status=_S.OPEN,
                owner=MORGAN,
                evidence=(
                    (
                        "launch-w1-kickoff-vtt",
                        "Morgan flagged a risk: app store review could delay the project.",
                    ),
                ),
            ),
        ),
    }


def build() -> tuple[CorpusProject, int]:
    spec = ProjectSpec(
        key="launch",
        name="Comet Launch",
        objective="Launch the Comet product on the web before mobile.",
        stage="Pre-launch",
        artifacts=_artifacts(),
        checkpoints=_checkpoints(),
        baseline_fake_claims=_baseline_fake_claims(),
    )
    return assemble(spec)
