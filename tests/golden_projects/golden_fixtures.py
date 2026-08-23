"""Three compact synthetic golden mini-projects (Section 13; Prompt 9's
"golden/manual checkpoint").

Each ``GoldenProject`` is built by ``_build`` from one small ``_Flavor``
dataclass of domain-specific wording, so the three fixtures —
implementation, advisory, and launch work — share exactly one
structural shape (proven against the real deterministic reconciliation
classifiers in ``project_context.domain.reconciliation_language``) while
reading as three distinct projects. Every fact's wording is chosen to
trigger a specific, real classification deterministically:

- decision                        -> CREATE (decision)
- commitment (v1)                 -> CREATE (commitment), owner + due date
- commitment (v1) owner change    -> UPDATE ("ownership moves to ...")
- commitment (v1) due date change -> UPDATE ("... deadline moved to ...")
- commitment (v2)                 -> CREATE (commitment)
- commitment (v2) completion      -> COMPLETE ("... was sent to the client.")
- risk                            -> CREATE (risk)
- risk -> blocker                 -> SUPERSEDE ("... is now blocking work;
                                      we cannot proceed.")
- milestone                       -> CREATE (milestone)
- open question                   -> CREATE (open_question)
- irrelevant chatter               -> no observation at all (the fake
                                      extraction provider returns an
                                      empty, ``source_contains_no_
                                      material_updates`` batch for it,
                                      exactly as a real model is
                                      instructed to)

Evidence is split across three documents so every reconciliation pass
only ever targets one item per fact, avoiding any ordering ambiguity
between two proposals racing for the same item:

1. ``kickoff_vtt``   (a call-recording transcript): every "first
   mention" fact, plus the irrelevant chatter.
2. ``followup_md_1`` (meeting notes): the v1 commitment's owner change,
   and the v2 commitment's completion.
3. ``followup_md_2`` (meeting notes): the v1 commitment's due-date
   change, and the risk becoming a blocker.

Each ``GoldenFact`` carries the exact evidence quote it must be
traceable to end to end (Prompt 9: "exact expected spans") — the
fixture never asks a downstream step to infer a span; it states the one
that must appear.
"""

from __future__ import annotations

from dataclasses import dataclass

from project_context.llm.schemas import ObservationKind


@dataclass(frozen=True)
class GoldenFact:
    """One atomic fact this fixture expects to survive intact through
    extraction, reconciliation, and review — everything
    `project_context.llm.schemas.ExtractedObservation` needs, plus the
    ledger title it must end up filed under."""

    kind: ObservationKind
    subject: str
    statement: str
    owner_name: str | None = None
    date_value: str | None = None
    date_text: str | None = None


@dataclass(frozen=True)
class GoldenProject:
    key: str
    name: str
    objective: str
    stage: str
    kickoff_vtt: bytes
    followup_md_1: str
    followup_md_2: str
    decision_title: str
    commitment1_title: str
    commitment2_title: str
    risk_title: str
    blocker_title: str
    milestone_title: str
    question_title: str
    irrelevant_sentinel: str
    round1_facts: tuple[GoldenFact, ...]
    round2_facts: tuple[GoldenFact, ...]
    round3_facts: tuple[GoldenFact, ...]


@dataclass(frozen=True)
class _Flavor:
    key: str
    name: str
    objective: str
    stage: str
    decision_subject: str
    decision_statement: str
    c1_subject: str
    c1_owner: str
    c1_date: str
    c1_date_text: str
    c1_new_owner: str
    c1_new_date: str
    c1_new_date_text: str
    c2_subject: str
    c2_owner: str
    risk_subject: str
    risk_owner: str
    milestone_subject: str
    milestone_date: str
    milestone_date_text: str
    question_subject: str
    question_statement: str
    irrelevant_statement: str


def _vtt_bytes(turns: list[tuple[str, str]]) -> bytes:
    """Build a minimal, valid WEBVTT transcript, one cue per turn, each
    turn a separate ``<v Speaker>`` voice tag so the parser never merges
    two facts into one block (adjacent-same-speaker cues merge; every
    turn here alternates speaker to guarantee one block per fact)."""
    lines = ["WEBVTT", ""]
    start_seconds = 0
    for speaker, text in turns:
        end_seconds = start_seconds + 2
        start = f"00:00:{start_seconds:02d}.000"
        end = f"00:00:{end_seconds:02d}.000"
        lines.append(f"{start} --> {end}")
        lines.append(f"<v {speaker}>{text}</v>")
        lines.append("")
        start_seconds = end_seconds + 1
    return ("\n".join(lines)).encode("utf-8")


#: A floor every fact statement is padded up to (`_pad`) so that, whatever
#: a flavor's own wording length happens to be, the shortest possible
#: pair of adjacent blocks in one document is always longer than the
#: longest possible single block — the exact condition
#: `project_context.chunking.chunk_blocks` needs to place one fact per
#: chunk without ever merging two facts or hard-splitting one, for any
#: single `chunk_target_chars` value (`_CHUNK_TARGET_CHARS` below).
_STATEMENT_MIN_LEN = 80
_PAD_SUFFIX = " This was noted for the project record."

#: Verified (see tests/golden_projects/test_manual_vertical_slice.py's
#: chunking self-check) to sit strictly between the longest single fact
#: block and the shortest adjacent-pair sum across all three fixtures —
#: every document below chunks to exactly one fact per chunk at this
#: value.
CHUNK_TARGET_CHARS = 145


def _pad(statement: str) -> str:
    if len(statement) >= _STATEMENT_MIN_LEN:
        return statement
    return statement + _PAD_SUFFIX


def _build(flavor: _Flavor) -> GoldenProject:
    c1_create_statement = _pad(
        f"{flavor.c1_owner} will finish the {flavor.c1_subject.lower()} "
        f"by {flavor.c1_date_text}."
    )
    c2_create_statement = _pad(
        f"{flavor.c2_owner} will send the {flavor.c2_subject.lower()} by Friday."
    )
    risk_create_statement = _pad(
        f"{flavor.risk_owner} flagged a risk: {flavor.risk_subject.lower()} could delay the "
        "project."
    )
    milestone_create_statement = _pad(
        f"The {flavor.milestone_subject.lower()} milestone is targeted "
        f"for {flavor.milestone_date_text}."
    )

    decision_statement = _pad(flavor.decision_statement)
    question_statement = _pad(flavor.question_statement)
    irrelevant_statement = _pad(flavor.irrelevant_statement)

    kickoff_vtt = _vtt_bytes(
        [
            ("Alex", decision_statement),
            ("Priya", c1_create_statement),
            ("Alex", c2_create_statement),
            ("Priya", risk_create_statement),
            ("Alex", milestone_create_statement),
            ("Priya", question_statement),
            ("Alex", irrelevant_statement),
        ]
    )

    c1_owner_change_statement = _pad(
        f"{flavor.c1_subject}: ownership moves to {flavor.c1_new_owner}."
    )
    c2_completion_statement = _pad(f"The {flavor.c2_subject.lower()} was sent to the client.")
    followup_md_1 = f"{c1_owner_change_statement}\n\n{c2_completion_statement}\n"

    c1_date_change_statement = _pad(
        f"The {flavor.c1_subject.lower()} deadline moved to {flavor.c1_new_date_text}."
    )
    blocker_statement = _pad(
        f"{flavor.risk_owner} confirmed {flavor.risk_subject.lower()} is now blocking work; "
        "we cannot proceed."
    )
    followup_md_2 = f"{c1_date_change_statement}\n\n{blocker_statement}\n"

    round1_facts = (
        GoldenFact(ObservationKind.DECISION, flavor.decision_subject, decision_statement),
        GoldenFact(
            ObservationKind.COMMITMENT, flavor.c1_subject, c1_create_statement,
            owner_name=flavor.c1_owner, date_value=flavor.c1_date, date_text=flavor.c1_date_text,
        ),
        GoldenFact(
            ObservationKind.COMMITMENT, flavor.c2_subject, c2_create_statement,
            owner_name=flavor.c2_owner,
        ),
        GoldenFact(
            ObservationKind.RISK, flavor.risk_subject, risk_create_statement,
            owner_name=flavor.risk_owner,
        ),
        GoldenFact(
            ObservationKind.MILESTONE, flavor.milestone_subject, milestone_create_statement,
            date_value=flavor.milestone_date, date_text=flavor.milestone_date_text,
        ),
        GoldenFact(ObservationKind.OPEN_QUESTION, flavor.question_subject, question_statement),
    )
    round2_facts = (
        GoldenFact(
            ObservationKind.COMMITMENT, flavor.c1_subject, c1_owner_change_statement,
            owner_name=flavor.c1_new_owner,
        ),
        GoldenFact(ObservationKind.COMMITMENT, flavor.c2_subject, c2_completion_statement),
    )
    round3_facts = (
        GoldenFact(
            ObservationKind.COMMITMENT, flavor.c1_subject, c1_date_change_statement,
            date_value=flavor.c1_new_date, date_text=flavor.c1_new_date_text,
        ),
        GoldenFact(
            ObservationKind.BLOCKER, flavor.risk_subject, blocker_statement,
            owner_name=flavor.risk_owner,
        ),
    )

    return GoldenProject(
        key=flavor.key,
        name=flavor.name,
        objective=flavor.objective,
        stage=flavor.stage,
        kickoff_vtt=kickoff_vtt,
        followup_md_1=followup_md_1,
        followup_md_2=followup_md_2,
        decision_title=flavor.decision_subject,
        commitment1_title=flavor.c1_subject,
        commitment2_title=flavor.c2_subject,
        risk_title=flavor.risk_subject,
        blocker_title=flavor.risk_subject,
        milestone_title=flavor.milestone_subject,
        question_title=flavor.question_subject,
        irrelevant_sentinel=irrelevant_statement,
        round1_facts=round1_facts,
        round2_facts=round2_facts,
        round3_facts=round3_facts,
    )


_IMPLEMENTATION = _Flavor(
    key="implementation",
    name="Atlas Migration",
    objective="Migrate the billing datastore to PostgreSQL without downtime.",
    stage="Build",
    decision_subject="Datastore choice",
    decision_statement="We decided to use PostgreSQL for the new datastore.",
    c1_subject="Schema migration",
    c1_owner="Bob",
    c1_date="2026-08-10",
    c1_date_text="August 10th",
    c1_new_owner="Diego",
    c1_new_date="2026-08-20",
    c1_new_date_text="August 20th",
    c2_subject="Deployment checklist",
    c2_owner="Priya",
    risk_subject="Legacy exporter compatibility risk",
    risk_owner="Priya",
    milestone_subject="Migration cutover",
    milestone_date="2026-09-15",
    milestone_date_text="September 15th",
    question_subject="Rollback strategy",
    question_statement=(
        "It is still unclear what the rollback strategy should be if the migration fails."
    ),
    irrelevant_statement="By the way, the office coffee machine is broken again.",
)

_ADVISORY = _Flavor(
    key="advisory",
    name="Meridian Advisory Engagement",
    objective="Deliver finance-workstream recommendations to the client.",
    stage="Discovery",
    decision_subject="Engagement scope",
    decision_statement=(
        "We decided to scope the advisory engagement to the finance workstream only."
    ),
    c1_subject="Recommendations memo",
    c1_owner="Maria",
    c1_date="2026-09-03",
    c1_date_text="September 3rd",
    c1_new_owner="Sam",
    c1_new_date="2026-09-10",
    c1_new_date_text="September 10th",
    c2_subject="Interview summary",
    c2_owner="Elena",
    risk_subject="Client data access risk",
    risk_owner="Maria",
    milestone_subject="Phase one readout",
    milestone_date="2026-10-01",
    milestone_date_text="October 1st",
    question_subject="Engagement fee structure",
    question_statement=(
        "It is still unclear what the engagement fee structure should be for phase two."
    ),
    irrelevant_statement="By the way, the conference room booking system is down again.",
)

_LAUNCH = _Flavor(
    key="launch",
    name="Comet Launch",
    objective="Launch the Comet product on the web before mobile.",
    stage="Pre-launch",
    decision_subject="Launch channel",
    decision_statement="We decided to launch on the web app first, not mobile.",
    c1_subject="Press release draft",
    c1_owner="Jordan",
    c1_date="2026-08-25",
    c1_date_text="August 25th",
    c1_new_owner="Casey",
    c1_new_date="2026-08-30",
    c1_new_date_text="August 30th",
    c2_subject="Launch checklist",
    c2_owner="Jordan",
    risk_subject="App store review risk",
    risk_owner="Jordan",
    milestone_subject="Public launch",
    milestone_date="2026-09-05",
    milestone_date_text="September 5th",
    question_subject="Pricing tier",
    question_statement="It is still unclear what the pricing tier should be at launch.",
    irrelevant_statement="By the way, the office snack budget got cut again.",
)

IMPLEMENTATION_PROJECT = _build(_IMPLEMENTATION)
ADVISORY_PROJECT = _build(_ADVISORY)
LAUNCH_PROJECT = _build(_LAUNCH)

ALL_GOLDEN_PROJECTS: tuple[GoldenProject, ...] = (
    IMPLEMENTATION_PROJECT,
    ADVISORY_PROJECT,
    LAUNCH_PROJECT,
)
