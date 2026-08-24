"""Project B — Client advisory engagement ("Meridian Advisory
Engagement"): Section 13.2's required traps — "tentative vs final
decision, participant ambiguity, superseded recommendation."

- **Tentative vs final decision**: the kickoff transcript's tentative fee-
  structure discussion ("leaning toward... nothing is decided yet")
  deliberately produces *no* observation at all (`fact=None`) — the
  actual decision is only created three weeks later, worded as a real
  decision. A correct system reports no fee-structure decision until
  then; the fake-mode baseline fixture (`_baseline_fake_claims`)
  deliberately gets this wrong at the first checkpoint, citing the
  tentative line itself as if it supported a firm decision — a textbook
  materially-misleading-claim case (cited evidence that does not actually
  support the claim).
- **Participant ambiguity**: two real people, "Jordan Lee" (client) and
  "Jordan Ruiz" (internal), both exist; `commitment-discovery-scheduling`
  is assigned only to "Jordan" in the evidence, so the correct resolved
  owner is `None` (Section 12.9: "Do not guess between ambiguous
  names") — not a guess at either Jordan.
- **Superseded recommendation**: `decision-rollout-v1` (phased rollout)
  is superseded by `decision-rollout-v2` (parallel rollout) using
  explicit "instead of... we now recommend" language.

17 artifacts across six simulated weeks (2026-09-14 through
2026-10-23): 4 meeting transcripts, 6 emails (one deliberately
ambiguous-assignment), 3 documents, 4 calendar events. 19 ground-truth
items, 26 material transitions.
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
    AmbiguousAliasSeed,
    ArtifactKind,
    BaselineScriptedClaim,
    BriefTypeLiteral,
    Checkpoint,
    CorpusProject,
    TransitionType,
)

NAOMI, VICTOR, GRACE = "Naomi", "Victor", "Grace"
#: The two similarly-named real people behind the participant-ambiguity
#: trap. Only "Jordan" (first name alone) ever appears in evidence text.
JORDAN_LEE, JORDAN_RUIZ = "Jordan Lee", "Jordan Ruiz"

_K = LedgerItemKind
_S = LedgerItemStatus
_T = TransitionType


def _artifacts() -> list[ArtifactSpec]:
    return [
        ArtifactSpec(
            artifact_id="adv-w1-kickoff-vtt",
            kind=ArtifactKind.MEETING_TRANSCRIPT,
            title="Meridian Advisory — Kickoff",
            occurred_at="2026-09-14T09:00:00Z",
            author="Naomi",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "We decided to scope the advisory engagement to the finance workstream only.",
                    FactSpec(
                        "decision-scope",
                        _K.DECISION,
                        "Engagement scope",
                        "scope-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                    speaker="Naomi",
                ),
                FactBlock(
                    "We're leaning toward a fixed-fee model for the phase two engagement, "
                    "but nothing is decided yet.",
                    fact=None,
                    speaker="Victor",
                ),
                FactBlock(
                    f"{NAOMI} will finish the recommendations memo by September 19th.",
                    FactSpec(
                        "commitment-recommendations-memo",
                        _K.COMMITMENT,
                        "Recommendations memo",
                        "memo-create",
                        _T.CREATE,
                        owner=NAOMI,
                        due_date="2026-09-19",
                        status=_S.OPEN,
                    ),
                    speaker="Naomi",
                ),
                FactBlock(
                    f"{VICTOR} will finish the interview summary by September 18th.",
                    FactSpec(
                        "commitment-interview-summary",
                        _K.COMMITMENT,
                        "Interview summary",
                        "interview-summary-create",
                        _T.CREATE,
                        owner=VICTOR,
                        due_date="2026-09-18",
                        status=_S.OPEN,
                    ),
                    speaker="Victor",
                ),
                FactBlock(
                    "Jordan will schedule the discovery interviews by September 22nd.",
                    FactSpec(
                        "commitment-discovery-scheduling",
                        _K.COMMITMENT,
                        "Discovery interview scheduling",
                        "discovery-scheduling-create",
                        _T.CREATE,
                        owner=None,
                        due_date="2026-09-22",
                        status=_S.OPEN,
                        observed_owner_text="Jordan",
                    ),
                    speaker="Naomi",
                ),
                FactBlock(
                    "The phase one readout milestone is targeted for October 1st.",
                    FactSpec(
                        "milestone-phase-one-readout",
                        _K.MILESTONE,
                        "Phase one readout",
                        "phase-one-create",
                        _T.CREATE,
                        due_date="2026-10-01",
                        status=_S.OPEN,
                    ),
                    speaker="Victor",
                ),
                FactBlock(
                    "It is still unclear what the fee payment timeline should be for phase two.",
                    FactSpec(
                        "open-question-fee-timeline",
                        _K.OPEN_QUESTION,
                        "Fee payment timeline",
                        "fee-timeline-create",
                        _T.CREATE,
                        status=_S.OPEN,
                    ),
                    speaker="Naomi",
                ),
                FactBlock(
                    f"{VICTOR} is the lead associate on the Meridian Advisory engagement.",
                    FactSpec(
                        "stakeholder-victor",
                        _K.STAKEHOLDER,
                        "Victor",
                        "victor-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                    speaker="Victor",
                ),
                FactBlock(
                    "By the way, the espresso machine in the client's kitchen is quite good.",
                    fact=None,
                    speaker="Naomi",
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w1-charter-doc",
            kind=ArtifactKind.DOCUMENT,
            title="Meridian Advisory — Engagement Charter",
            occurred_at="2026-09-15T10:00:00Z",
            author="Naomi",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "We recommend a phased rollout starting with accounts receivable.",
                    FactSpec(
                        "decision-rollout-v1",
                        _K.DECISION,
                        "Rollout recommendation",
                        "rollout-v1-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                ),
                FactBlock(
                    f"{GRACE} is the client's primary contact for the Meridian Advisory "
                    "engagement.",
                    FactSpec(
                        "stakeholder-grace",
                        _K.STAKEHOLDER,
                        "Grace",
                        "grace-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                ),
                FactBlock(
                    f"{NAOMI} flagged a risk: client data access could delay the project.",
                    FactSpec(
                        "risk-client-data-access",
                        _K.RISK,
                        "Client data access risk",
                        "data-access-risk-create",
                        _T.CREATE,
                        owner=NAOMI,
                        status=_S.OPEN,
                    ),
                ),
                FactBlock("This charter will be revisited at the phase one readout.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w1-scope-email",
            kind=ArtifactKind.EMAIL,
            title="Scope sign-off",
            occurred_at="2026-09-16T09:30:00Z",
            author="Grace",
            project_key="advisory",
            blocks=[
                FactBlock(
                    f"{GRACE} will finish the scope sign-off by September 25th.",
                    FactSpec(
                        "commitment-client-signoff-scope",
                        _K.COMMITMENT,
                        "Scope sign-off",
                        "signoff-create",
                        _T.CREATE,
                        owner=GRACE,
                        due_date="2026-09-25",
                        status=_S.OPEN,
                    ),
                ),
                FactBlock("Thanks for the clear charter document.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w1-cal-standup",
            kind=ArtifactKind.CALENDAR_EVENT,
            title="Meridian Advisory — Weekly Sync",
            occurred_at="2026-09-18T09:00:00Z",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "Meridian Advisory Weekly Sync. Attendees: Naomi, Victor, Grace. "
                    "Recurring Monday 9:00am.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w2-standup-vtt",
            kind=ArtifactKind.MEETING_TRANSCRIPT,
            title="Meridian Advisory — Week 2 Sync",
            occurred_at="2026-09-21T09:00:00Z",
            author="Naomi",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "Recommendations memo: ownership moves to Victor.",
                    FactSpec(
                        "commitment-recommendations-memo",
                        _K.COMMITMENT,
                        "Recommendations memo",
                        "memo-owner",
                        _T.UPDATE_OWNER,
                        owner=VICTOR,
                    ),
                    speaker="Naomi",
                ),
                FactBlock(
                    f"{GRACE} will finish the workshop follow-up notes by October 2nd.",
                    FactSpec(
                        "commitment-workshop-followup",
                        _K.COMMITMENT,
                        "Workshop follow-up notes",
                        "workshop-followup-create",
                        _T.CREATE,
                        owner=GRACE,
                        due_date="2026-10-02",
                        status=_S.OPEN,
                    ),
                    speaker="Grace",
                ),
                FactBlock(
                    "The phase two kickoff milestone is targeted for October 20th.",
                    FactSpec(
                        "milestone-phase-two-kickoff",
                        _K.MILESTONE,
                        "Phase two kickoff",
                        "phase-two-create",
                        _T.CREATE,
                        due_date="2026-10-20",
                        status=_S.OPEN,
                    ),
                    speaker="Victor",
                ),
                FactBlock(
                    "It is still unclear what the vendor system access approval should be.",
                    FactSpec(
                        "open-question-vendor-access",
                        _K.OPEN_QUESTION,
                        "Vendor system access approval",
                        "vendor-access-create",
                        _T.CREATE,
                        status=_S.OPEN,
                    ),
                    speaker="Naomi",
                ),
                FactBlock(
                    "By the way, the client's building lobby was being renovated.",
                    fact=None,
                    speaker="Grace",
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w2-email",
            kind=ArtifactKind.EMAIL,
            title="Interview summary — approved",
            occurred_at="2026-09-23T14:00:00Z",
            author="Victor",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "The interview summary was approved by the client.",
                    FactSpec(
                        "commitment-interview-summary",
                        _K.COMMITMENT,
                        "Interview summary",
                        "interview-summary-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                ),
                FactBlock("Grace had great feedback on the format.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w2-cal-review",
            kind=ArtifactKind.CALENDAR_EVENT,
            title="Meridian Advisory — Progress Review",
            occurred_at="2026-09-24T09:00:00Z",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "Meridian Advisory Progress Review. Attendees: Naomi, Victor, Grace.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w3-standup-vtt",
            kind=ArtifactKind.MEETING_TRANSCRIPT,
            title="Meridian Advisory — Week 3 Sync",
            occurred_at="2026-09-28T09:00:00Z",
            author="Naomi",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "The phase one readout milestone new target is October 8th.",
                    FactSpec(
                        "milestone-phase-one-readout",
                        _K.MILESTONE,
                        "Phase one readout",
                        "phase-one-date",
                        _T.UPDATE_DATE,
                        due_date="2026-10-08",
                    ),
                    speaker="Naomi",
                ),
                FactBlock(
                    "It is still unclear what the follow-up meeting cadence should be.",
                    FactSpec(
                        "open-question-followup-cadence",
                        _K.OPEN_QUESTION,
                        "Follow-up meeting cadence",
                        "cadence-create",
                        _T.CREATE,
                        status=_S.OPEN,
                    ),
                    speaker="Victor",
                ),
                FactBlock(
                    "By the way, the client's office coffee is much better than ours.",
                    fact=None,
                    speaker="Naomi",
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w3-doc",
            kind=ArtifactKind.DOCUMENT,
            title="Meridian Advisory — Revised Rollout Recommendation",
            occurred_at="2026-09-30T11:00:00Z",
            author="Naomi",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "Instead of a phased rollout, we now recommend a full parallel rollout "
                    "across both accounts receivable and accounts payable.",
                    FactSpec(
                        "decision-rollout-v2",
                        _K.DECISION,
                        "Rollout recommendation (revised)",
                        "rollout-v2-supersede",
                        _T.SUPERSEDE,
                        status=_S.ACTIVE,
                        supersedes_item_id="decision-rollout-v1",
                    ),
                ),
                FactBlock(
                    "The recommendations memo was sent to the client for review.",
                    FactSpec(
                        "commitment-recommendations-memo",
                        _K.COMMITMENT,
                        "Recommendations memo",
                        "memo-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                ),
                FactBlock(
                    "This revision reflects feedback from the steering committee.", fact=None
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w3-ambiguous-email",
            kind=ArtifactKind.EMAIL,
            title="Quick flag re: Meridian",
            occurred_at="2026-10-01T16:00:00Z",
            author="Grace",
            project_key="advisory",
            ambiguous_assignment=True,
            blocks=[
                FactBlock(
                    "Quick note: saw a Meridian invoice, not sure if that's our engagement "
                    "or the unrelated Meridian software license renewal finance tracks.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w4-standup-vtt",
            kind=ArtifactKind.MEETING_TRANSCRIPT,
            title="Meridian Advisory — Week 4 Sync",
            occurred_at="2026-10-05T09:00:00Z",
            author="Naomi",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "We decided to use a fixed-fee model for the phase two engagement.",
                    FactSpec(
                        "decision-fee-structure",
                        _K.DECISION,
                        "Engagement fee structure",
                        "fee-structure-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                    speaker="Naomi",
                ),
                FactBlock(
                    "We decided to use the existing BI platform for the analytics tooling.",
                    FactSpec(
                        "decision-tooling",
                        _K.DECISION,
                        "Analytics tooling choice",
                        "tooling-create",
                        _T.CREATE,
                        status=_S.ACTIVE,
                    ),
                    speaker="Victor",
                ),
                FactBlock(
                    "The scope sign-off was approved by the client.",
                    FactSpec(
                        "commitment-client-signoff-scope",
                        _K.COMMITMENT,
                        "Scope sign-off",
                        "signoff-complete",
                        _T.UPDATE_STATUS,
                        status=_S.COMPLETED,
                    ),
                    speaker="Grace",
                ),
                FactBlock(
                    "The final engagement readout milestone is targeted for November 1st.",
                    FactSpec(
                        "milestone-final-readout",
                        _K.MILESTONE,
                        "Final engagement readout",
                        "final-readout-create",
                        _T.CREATE,
                        due_date="2026-11-01",
                        status=_S.OPEN,
                    ),
                    speaker="Naomi",
                ),
                FactBlock(
                    "By the way, the parking validation stickers ran out at the front desk.",
                    fact=None,
                    speaker="Victor",
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w4-cal-readout-prep",
            kind=ArtifactKind.CALENDAR_EVENT,
            title="Meridian Advisory — Readout Prep",
            occurred_at="2026-10-07T09:00:00Z",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "Meridian Advisory Readout Prep. Attendees: Naomi, Victor, Grace.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w5-vendor-email",
            kind=ArtifactKind.EMAIL,
            title="Vendor access — resolved",
            occurred_at="2026-10-12T10:00:00Z",
            author="Naomi",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "The vendor system access approval question was answered: IT granted "
                    "read-only access as of October 10th.",
                    FactSpec(
                        "open-question-vendor-access",
                        _K.OPEN_QUESTION,
                        "Vendor system access approval",
                        "vendor-access-resolve",
                        _T.UPDATE_STATUS,
                        status=_S.RESOLVED,
                    ),
                ),
                FactBlock("This unblocks the data pull for phase two.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w5-cadence-email",
            kind=ArtifactKind.EMAIL,
            title="Follow-up cadence — resolved",
            occurred_at="2026-10-14T10:00:00Z",
            author="Victor",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "The follow-up meeting cadence question was answered: biweekly syncs "
                    "starting in November.",
                    FactSpec(
                        "open-question-followup-cadence",
                        _K.OPEN_QUESTION,
                        "Follow-up meeting cadence",
                        "cadence-resolve",
                        _T.UPDATE_STATUS,
                        status=_S.RESOLVED,
                    ),
                ),
                FactBlock("Calendar holds will go out next week.", fact=None),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w5-cal-final",
            kind=ArtifactKind.CALENDAR_EVENT,
            title="Meridian Advisory — Phase One Readout",
            occurred_at="2026-10-15T09:00:00Z",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "Meridian Advisory Phase One Readout. Attendees: Naomi, Victor, Grace.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w6-wrapup-email",
            kind=ArtifactKind.EMAIL,
            title="Phase one wrap-up",
            occurred_at="2026-10-19T09:00:00Z",
            author="Naomi",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "Thanks everyone for a strong phase one readout.",
                    fact=None,
                ),
            ],
        ),
        ArtifactSpec(
            artifact_id="adv-w6-final-doc",
            kind=ArtifactKind.DOCUMENT,
            title="Meridian Advisory — Phase One Summary",
            occurred_at="2026-10-23T17:00:00Z",
            author="Naomi",
            project_key="advisory",
            blocks=[
                FactBlock(
                    "Phase two planning will continue based on the revised rollout recommendation.",
                    fact=None,
                ),
                FactBlock("The team will reconvene after the November holidays.", fact=None),
            ],
        ),
    ]


def _checkpoints() -> tuple[Checkpoint, ...]:
    return (
        Checkpoint(
            checkpoint_id="adv-cp-before-w2",
            cutoff_at="2026-09-21T09:00:00Z",
            brief_type=BriefTypeLiteral.MEETING_PREPARATION,
            meeting_title="Meridian Advisory — Week 2 Sync",
            meeting_scheduled_at="2026-09-21T09:00:00Z",
            participant_lines=("Naomi", "Victor", "Grace"),
        ),
        Checkpoint(
            checkpoint_id="adv-cp-before-w3",
            cutoff_at="2026-09-28T09:00:00Z",
            brief_type=BriefTypeLiteral.MEETING_PREPARATION,
            meeting_title="Meridian Advisory — Week 3 Sync",
            meeting_scheduled_at="2026-09-28T09:00:00Z",
            participant_lines=("Naomi", "Victor", "Grace"),
        ),
        Checkpoint(
            checkpoint_id="adv-cp-before-w4",
            cutoff_at="2026-10-05T09:00:00Z",
            brief_type=BriefTypeLiteral.MEETING_PREPARATION,
            meeting_title="Meridian Advisory — Week 4 Sync",
            meeting_scheduled_at="2026-10-05T09:00:00Z",
            participant_lines=("Naomi", "Victor", "Grace"),
        ),
        Checkpoint(
            checkpoint_id="adv-cp-final",
            cutoff_at="2026-10-24T00:00:00Z",
            brief_type=BriefTypeLiteral.CURRENT_PROJECT,
        ),
    )


def _baseline_fake_claims() -> dict[str, tuple[BaselineScriptedClaim, ...]]:
    """Illustrates all three of Section 13.2's required traps for this
    project — see the module docstring for which. As with
    `project_context.evaluation.corpus_data.implementation`, this is a
    hand-authored, deliberately imperfect fixture, not a model
    prediction (see `project_context.evaluation.baseline_runner`)."""
    return {
        "adv-cp-before-w2": (
            # Tentative-vs-final trap: cites the tentative line as if it
            # were a firm decision — evidence correctness failure.
            BaselineScriptedClaim(
                section="decisions",
                text="The team decided to use a fixed-fee model for the phase two engagement.",
                item_kind=_K.DECISION,
                item_title="Engagement fee structure",
                status=_S.ACTIVE,
                evidence=(
                    (
                        "adv-w1-kickoff-vtt",
                        "We're leaning toward a fixed-fee model for the phase two engagement, "
                        "but nothing is decided yet.",
                    ),
                ),
            ),
            BaselineScriptedClaim(
                section="decisions",
                text="The engagement is scoped to the finance workstream only.",
                item_kind=_K.DECISION,
                item_title="Engagement scope",
                status=_S.ACTIVE,
                evidence=(
                    (
                        "adv-w1-kickoff-vtt",
                        "We decided to scope the advisory engagement to the finance "
                        "workstream only.",
                    ),
                ),
            ),
        ),
        "adv-cp-before-w3": (
            # Recency miss (owner) + guesses between the two ambiguous
            # "Jordan"s instead of leaving the owner unresolved.
            BaselineScriptedClaim(
                section="open_commitments",
                text="Naomi owns the recommendations memo.",
                item_kind=_K.COMMITMENT,
                item_title="Recommendations memo",
                status=_S.OPEN,
                owner=NAOMI,
                evidence=(
                    (
                        "adv-w1-kickoff-vtt",
                        "Naomi will finish the recommendations memo by September 19th.",
                    ),
                ),
            ),
            BaselineScriptedClaim(
                section="open_commitments",
                text="Jordan Ruiz will schedule the discovery interviews.",
                item_kind=_K.COMMITMENT,
                item_title="Discovery interview scheduling",
                status=_S.OPEN,
                owner=JORDAN_RUIZ,
                evidence=(
                    (
                        "adv-w1-kickoff-vtt",
                        "Jordan will schedule the discovery interviews by September 22nd.",
                    ),
                ),
            ),
        ),
        "adv-cp-before-w4": (
            # Superseded-recommendation trap: still cites the phased
            # rollout, missing the "instead of... we now recommend" revision.
            BaselineScriptedClaim(
                section="decisions",
                text="The current recommendation is a phased rollout starting with AR.",
                item_kind=_K.DECISION,
                item_title="Rollout recommendation",
                status=_S.ACTIVE,
                evidence=(
                    (
                        "adv-w1-charter-doc",
                        "We recommend a phased rollout starting with accounts "
                        "receivable.",
                    ),
                ),
            ),
        ),
        "adv-cp-final": (
            BaselineScriptedClaim(
                section="decisions",
                text="The current recommendation is a phased rollout starting with AR.",
                item_kind=_K.DECISION,
                item_title="Rollout recommendation",
                status=_S.ACTIVE,
                evidence=(
                    (
                        "adv-w1-charter-doc",
                        "We recommend a phased rollout starting with accounts "
                        "receivable.",
                    ),
                ),
            ),
            BaselineScriptedClaim(
                section="open_commitments",
                text="Naomi owns the recommendations memo.",
                item_kind=_K.COMMITMENT,
                item_title="Recommendations memo",
                status=_S.OPEN,
                owner=NAOMI,
                evidence=(
                    (
                        "adv-w1-kickoff-vtt",
                        "Naomi will finish the recommendations memo by September 19th.",
                    ),
                ),
            ),
        ),
    }


def build() -> tuple[CorpusProject, int]:
    spec = ProjectSpec(
        key="advisory",
        name="Meridian Advisory Engagement",
        objective="Deliver finance-workstream recommendations to the client.",
        stage="Discovery",
        artifacts=_artifacts(),
        checkpoints=_checkpoints(),
        baseline_fake_claims=_baseline_fake_claims(),
        ambiguous_aliases=(
            AmbiguousAliasSeed(shared_alias="Jordan", full_names=(JORDAN_LEE, JORDAN_RUIZ)),
        ),
    )
    return assemble(spec)
