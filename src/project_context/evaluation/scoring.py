"""Every metric in Section 13.5, computed from a `ProjectRunResult`
against `CorpusProject` ground truth via `project_context.evaluation.
ground_truth_state`.

**Matching a claim to a ground-truth item.** Neither system's claims
carry an opaque ground-truth ID (they can't — ground truth is authored
independently of both systems, exactly as Section 13.3 requires). Every
metric here therefore starts from `match_claims_to_items`: a claim with
`item_kind`/`item_title` set is matched to the one `GroundTruthItem`
whose `kind` and `canonical_title`/one of its `aliases` normalize-equal
the claim's own — deterministic, auditable, and identical for both
systems (the same function scores the ledger's already-precise titles
and the baseline's own free-text titles).

**Zero-denominator and ambiguous handling.** Every ratio in this module
returns a `Metric` whose `.value` is `None`, never a division error or a
silently-substituted 0/1, when its denominator is zero — Section 13.3:
"Ambiguous items are labeled and excluded from strict precision/recall
or scored separately." A ground-truth transition authored with
`Materiality.AMBIGUOUS` is excluded from every metric's denominator
entirely (Section 13.6's "trap" transitions, `Materiality.TRAP`, are
still material and fully scored — the label is for report readability,
never a scoring exemption).

**"Materially misleading" is the one metric requiring judgment this
harness cannot fully automate.** Section 13.7 step 6: "blind human
adjudication where semantic equivalence is required." What this module
*can* do deterministically: flag a claim whose cited evidence is a real,
verbatim quote (so it is not simply "unsupported") but whose asserted
item state contradicts ground truth's expected state at that checkpoint
— e.g. claiming a decision was made while ground truth says it was still
tentative, or reporting a superseded recommendation as current. Every
such flagged instance is reported individually with expected vs. actual
(Prompt 15's explicit requirement), not folded into a single opaque
rate, precisely so a human adjudicator can confirm or overturn each one
before this harness's numbers are treated as final for a real pilot
decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from project_context.domain.ledger import LedgerItemStatus
from project_context.evaluation import ground_truth_state as gts
from project_context.evaluation.runner_types import CheckpointRunResult, ScoredClaim
from project_context.evaluation.schema import Checkpoint, CorpusProject, Materiality, TransitionType

_TERMINAL_TRANSITION_TYPES = (TransitionType.UPDATE_STATUS, TransitionType.SUPERSEDE)


@dataclass(frozen=True)
class Metric:
    """One ratio metric. `.value` is `None` exactly when `denominator`
    is 0 — never divide-by-zero, never a silently substituted value."""

    numerator: int
    denominator: int

    @property
    def value(self) -> float | None:
        return (self.numerator / self.denominator) if self.denominator else None


def zero_metric() -> Metric:
    return Metric(0, 0)


def add_metrics(a: Metric, b: Metric) -> Metric:
    return Metric(a.numerator + b.numerator, a.denominator + b.denominator)


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


@dataclass(frozen=True)
class MisleadingClaim:
    """One individually-reported unsupported or materially-misleading
    claim (module docstring; Prompt 15's "Highlight every materially
    misleading or unsupported claim individually with its expected/
    actual evidence")."""

    checkpoint_id: str
    system: str
    claim_text: str
    section: str
    kind: str  # "unsupported" | "materially_misleading"
    item_title: str | None
    expected: str
    actual: str


def _item_index(project: CorpusProject) -> dict[tuple[str, str], str]:
    """(kind, normalized title-or-alias) -> item_id, for every item and
    every alias — the one lookup table every claim match goes through."""
    index: dict[tuple[str, str], str] = {}
    for item in project.items:
        index.setdefault((item.kind.value, _normalize(item.canonical_title)), item.item_id)
        for alias in item.aliases:
            index.setdefault((item.kind.value, _normalize(alias)), item.item_id)
    return index


def _merge_claims_for_item(claim_list: list[ScoredClaim]) -> ScoredClaim:
    """A brief routinely mentions the same item from more than one
    section (e.g. a transition in "Recent Changes" that omits the owner
    a separate "Open Commitments" claim states) — scoring the *first*
    matching claim alone would wrongly dock a correct brief for one
    section's deliberate brevity. Merge every matching claim's fields,
    first non-`None` value wins per field, and union their evidence."""
    first = claim_list[0]
    status = next((c.status for c in claim_list if c.status), None)
    owner = next((c.owner for c in claim_list if c.owner), None)
    due_date = next((c.due_date for c in claim_list if c.due_date), None)
    evidence = tuple(dict.fromkeys(e for c in claim_list for e in c.evidence))
    return ScoredClaim(
        section=first.section,
        text=" | ".join(c.text for c in claim_list),
        claim_type=first.claim_type,
        item_kind=first.item_kind,
        item_title=first.item_title,
        status=status,
        owner=owner,
        due_date=due_date,
        evidence=evidence,
    )


def match_claims_to_items(
    project: CorpusProject, claims: tuple[ScoredClaim, ...]
) -> tuple[dict[str, ScoredClaim], int]:
    """`(item_id -> merged claim across every matching claim — see
    `_merge_claims_for_item`, count of claims asserting an item that
    matched no known ground-truth item at all)` — the second number is
    true hallucination (a title/kind pair ground truth has never heard
    of), kept separate from "matched a real item that shouldn't exist
    yet" (scored via `material_expected` membership)."""
    index = _item_index(project)
    grouped: dict[str, list[ScoredClaim]] = {}
    unmatched_count = 0
    for claim in claims:
        if not claim.item_kind or not claim.item_title:
            continue
        item_id = index.get((claim.item_kind, _normalize(claim.item_title)))
        if item_id is None:
            unmatched_count += 1
        else:
            grouped.setdefault(item_id, []).append(claim)
    matched = {item_id: _merge_claims_for_item(group) for item_id, group in grouped.items()}
    return matched, unmatched_count


_TERMINAL_STATUSES = frozenset(
    {
        LedgerItemStatus.COMPLETED,
        LedgerItemStatus.CANCELED,
        LedgerItemStatus.RESOLVED,
        LedgerItemStatus.SUPERSEDED,
    }
)

#: Kinds `project_context.domain.meeting_prep.MEETING_PREP_SECTIONS`
#: gives a *current-state* section to (`outstanding_commitments`,
#: `decisions_required`/`unanswered_questions`, `risks_and_blockers`).
#: `decision`, `milestone`, and `stakeholder` items have no such section
#: — a Meeting Preparation Brief only ever surfaces them via
#: `changes_since_previous` (Section 5.9's own scope, not a harness rule).
_MEETING_PREP_CURRENT_STATE_KINDS = frozenset({"commitment", "open_question", "risk", "blocker"})


def _material_expected_items(
    project: CorpusProject, cutoff_at: str, *, brief_type: str
) -> dict[str, gts.ItemState]:
    """Every item ground truth expects a brief of `brief_type` to
    surface as of `cutoff_at`, excluding one whose *only* visible
    transition is `Materiality.AMBIGUOUS` (module docstring).

    For a `current_project` brief (Section 5.8: shows full current
    state), every existing item is expected regardless of status. For a
    `meeting_preparation` brief (Section 5.9: forward-looking —
    outstanding commitments, open risks/questions — no decisions/
    milestones/stakeholders section at all), an item counts toward
    recall only if its kind has a current-state section *and* its status
    is non-terminal.

    A `meeting_preparation` brief's remaining section,
    `changes_since_previous`, is deliberately **never** used to expand
    this set, even though it can legitimately carry any kind/status: that
    section is driven by `project_context.db.ledger_repository.
    list_versions_since`'s real `ledger_versions.valid_from` wall-clock
    timestamp, not this harness's simulated `occurred_at` timeline — a
    review applied *today* against a corpus dated in the past or future
    does not reliably fall on either side of a simulated checkpoint
    cutoff. Anything the system additionally surfaces through that
    section is still scored (it costs nothing on precision — see
    `score_checkpoint`'s broad-existence check — and is never flagged
    misleading if true); it simply cannot be required for recall without
    the harness scoring its own wall-clock artifact rather than the
    system."""
    all_states = gts.all_states_at_cutoff(project, cutoff_at=cutoff_at)
    result = {}
    for item_id, state in all_states.items():
        if not state.exists:
            continue
        if (
            state.last_transition is not None
            and state.last_transition.materiality is Materiality.AMBIGUOUS
        ):
            continue
        if brief_type == "meeting_preparation":
            in_current_state_section = (
                state.kind.value in _MEETING_PREP_CURRENT_STATE_KINDS
                and state.status not in _TERMINAL_STATUSES
            )
            if not in_current_state_section:
                continue
        result[item_id] = state
    return result


def _fields_match(claim: ScoredClaim, state: gts.ItemState) -> dict[str, bool | None]:
    """`None` means "ground truth has no value to check" (excluded from
    that field's denominator); `True`/`False` otherwise."""
    result: dict[str, bool | None] = {}
    result["kind"] = claim.item_kind == state.kind.value if claim.item_kind else False
    result["status"] = (
        None
        if state.status is None
        else (claim.status == state.status.value if claim.status else False)
    )
    result["owner"] = (
        None
        if state.owner is None
        else (_normalize(claim.owner) == _normalize(state.owner) if claim.owner else False)
    )
    result["due_date"] = (
        None
        if state.due_date is None
        else (claim.due_date == state.due_date if claim.due_date else False)
    )
    return result


@dataclass(frozen=True)
class CheckpointScore:
    checkpoint_id: str
    brief_type: str
    system: str
    item_precision: Metric
    item_recall: Metric
    field_accuracy_kind: Metric
    field_accuracy_status: Metric
    field_accuracy_owner: Metric
    field_accuracy_due_date: Metric
    evidence_correctness: Metric
    unsupported_claim_rate: Metric
    transition_accuracy: Metric
    current_state_accuracy: Metric
    misleading: tuple[MisleadingClaim, ...]
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    latency_ms: int
    accepted_without_edit: int
    edited_accepted: int
    rejected: int


def score_checkpoint(
    project: CorpusProject, checkpoint: Checkpoint, run: CheckpointRunResult
) -> CheckpointScore:
    material_expected = _material_expected_items(
        project, checkpoint.cutoff_at, brief_type=checkpoint.brief_type.value
    )
    matched, unmatched_count = match_claims_to_items(project, run.claims)

    # Precision credits any claim matched to an item that genuinely
    # exists per ground truth by this cutoff (the broad check, not
    # `material_expected`'s narrower recall scope) — mentioning a true
    # fact outside one brief type's usual sections (e.g. an old decision
    # surfacing again via "changes since") is not a precision fault;
    # only a claim about an item that does not exist yet, or that
    # matches no known item at all, is.
    correct_and_expected = sum(
        1
        for item_id in matched
        if gts.state_at_cutoff(
            project, project.item_by_id(item_id), cutoff_at=checkpoint.cutoff_at
        ).exists
    )
    item_precision = Metric(correct_and_expected, len(matched) + unmatched_count)
    item_recall = Metric(
        sum(1 for item_id in material_expected if item_id in matched), len(material_expected)
    )

    field_totals = {
        "kind": zero_metric(),
        "status": zero_metric(),
        "owner": zero_metric(),
        "due_date": zero_metric(),
    }
    transition_metric = zero_metric()
    misleading: list[MisleadingClaim] = []

    for item_id, state in material_expected.items():
        claim = matched.get(item_id)
        if claim is None:
            continue
        fields = _fields_match(claim, state)
        for name, outcome in fields.items():
            if outcome is None:
                continue
            field_totals[name] = Metric(
                field_totals[name].numerator + (1 if outcome else 0),
                field_totals[name].denominator + 1,
            )
        if (
            state.last_transition is not None
            and state.last_transition.type in _TERMINAL_TRANSITION_TYPES
        ):
            status_ok = fields.get("status")
            if status_ok is not None:
                transition_metric = Metric(
                    transition_metric.numerator + (1 if status_ok else 0),
                    transition_metric.denominator + 1,
                )
        if fields.get("status") is False or fields.get("owner") is False:
            expected_status = state.status.value if state.status else "n/a"
            expected_desc = f"status={expected_status}, owner={state.owner or 'n/a'}"
            actual_desc = f"status={claim.status or 'n/a'}, owner={claim.owner or 'n/a'}"
            if claim.evidence:
                misleading.append(
                    MisleadingClaim(
                        checkpoint_id=checkpoint.checkpoint_id,
                        system=run.system,
                        claim_text=claim.text,
                        section=claim.section,
                        kind="materially_misleading",
                        item_title=claim.item_title,
                        expected=expected_desc,
                        actual=actual_desc,
                    )
                )

    # Claims asserting a real item that ground truth says does not exist
    # *at all* yet (e.g. the tentative-decision trap) — the other half of
    # "materially misleading": correctly matched to a real item, but
    # premature. Uses the broad `state_at_cutoff` existence check, not
    # membership in `material_expected` — an item outside this
    # checkpoint's brief-type recall scope (module docstring's
    # decision/milestone/stakeholder-kind and terminal-status narrowing)
    # is not misleading merely for being mentioned; it is scored as an
    # unscored true fact, neither a recall credit nor a precision fault.
    for item_id, claim in matched.items():
        if item_id in material_expected:
            continue
        broad_state = gts.state_at_cutoff(
            project, project.item_by_id(item_id), cutoff_at=checkpoint.cutoff_at
        )
        if broad_state.exists:
            continue
        if claim.evidence:
            misleading.append(
                MisleadingClaim(
                    checkpoint_id=checkpoint.checkpoint_id,
                    system=run.system,
                    claim_text=claim.text,
                    section=claim.section,
                    kind="materially_misleading",
                    item_title=claim.item_title,
                    expected="item does not exist yet at this cutoff",
                    actual=f"claimed as {claim.status or 'present'}",
                )
            )

    # "Material factual claims" (Section 13.5) — excludes self-evidencing
    # project/meeting-meta claims ("Objective: ...", "Purpose: ...") and
    # deterministic "no accepted items" placeholders, neither of which
    # assert anything about a tracked item and neither of which requires
    # (or can even carry) a supporting quote by the app's own design
    # (`project_context.domain.briefs.BriefFactType.PROJECT_META`/
    # `MEETING_META` are exempted from evidence at the application layer
    # itself, not just here).
    factual_claims = [
        c for c in run.claims if c.claim_type in ("fact", "inference") and c.item_kind is not None
    ]
    with_evidence = [c for c in factual_claims if c.evidence]
    evidence_correctness = Metric(len(with_evidence), len(factual_claims))
    unsupported = [c for c in factual_claims if not c.evidence]
    unsupported_claim_rate = Metric(len(unsupported), len(factual_claims))
    for claim in unsupported:
        misleading.append(
            MisleadingClaim(
                checkpoint_id=checkpoint.checkpoint_id,
                system=run.system,
                claim_text=claim.text,
                section=claim.section,
                kind="unsupported",
                item_title=claim.item_title,
                expected="a verbatim supporting quote",
                actual="no valid cited evidence",
            )
        )

    current_state_numerator = sum(
        1
        for item_id, state in material_expected.items()
        if item_id in matched
        and all(v is not False for v in _fields_match(matched[item_id], state).values())
    )
    current_state_accuracy = Metric(current_state_numerator, len(material_expected))

    return CheckpointScore(
        checkpoint_id=checkpoint.checkpoint_id,
        brief_type=checkpoint.brief_type.value,
        system=run.system,
        item_precision=item_precision,
        item_recall=item_recall,
        field_accuracy_kind=field_totals["kind"],
        field_accuracy_status=field_totals["status"],
        field_accuracy_owner=field_totals["owner"],
        field_accuracy_due_date=field_totals["due_date"],
        evidence_correctness=evidence_correctness,
        unsupported_claim_rate=unsupported_claim_rate,
        transition_accuracy=transition_metric,
        current_state_accuracy=current_state_accuracy,
        misleading=tuple(misleading),
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        estimated_cost_usd=run.estimated_cost_usd,
        latency_ms=run.latency_ms,
        accepted_without_edit=run.accepted_without_edit,
        edited_accepted=run.edited_accepted,
        rejected=run.rejected,
    )


@dataclass(frozen=True)
class ProjectScore:
    project_key: str
    system: str
    checkpoints: tuple[CheckpointScore, ...]

    def pooled(self) -> PooledMetrics:
        item_precision = zero_metric()
        item_recall = zero_metric()
        field_kind = zero_metric()
        field_status = zero_metric()
        field_owner = zero_metric()
        field_due_date = zero_metric()
        evidence_correctness = zero_metric()
        unsupported_claim_rate = zero_metric()
        transition_accuracy = zero_metric()
        current_state_accuracy = zero_metric()
        for cp in self.checkpoints:
            item_precision = add_metrics(item_precision, cp.item_precision)
            item_recall = add_metrics(item_recall, cp.item_recall)
            field_kind = add_metrics(field_kind, cp.field_accuracy_kind)
            field_status = add_metrics(field_status, cp.field_accuracy_status)
            field_owner = add_metrics(field_owner, cp.field_accuracy_owner)
            field_due_date = add_metrics(field_due_date, cp.field_accuracy_due_date)
            evidence_correctness = add_metrics(evidence_correctness, cp.evidence_correctness)
            unsupported_claim_rate = add_metrics(unsupported_claim_rate, cp.unsupported_claim_rate)
            transition_accuracy = add_metrics(transition_accuracy, cp.transition_accuracy)
            current_state_accuracy = add_metrics(current_state_accuracy, cp.current_state_accuracy)
        return PooledMetrics(
            item_precision=item_precision,
            item_recall=item_recall,
            field_accuracy_kind=field_kind,
            field_accuracy_status=field_status,
            field_accuracy_owner=field_owner,
            field_accuracy_due_date=field_due_date,
            evidence_correctness=evidence_correctness,
            unsupported_claim_rate=unsupported_claim_rate,
            transition_accuracy=transition_accuracy,
            current_state_accuracy=current_state_accuracy,
            total_input_tokens=sum(cp.input_tokens for cp in self.checkpoints),
            total_output_tokens=sum(cp.output_tokens for cp in self.checkpoints),
            total_cost_usd=sum(cp.estimated_cost_usd for cp in self.checkpoints),
            total_latency_ms=sum(cp.latency_ms for cp in self.checkpoints),
            accepted_without_edit=sum(cp.accepted_without_edit for cp in self.checkpoints),
            edited_accepted=sum(cp.edited_accepted for cp in self.checkpoints),
            rejected=sum(cp.rejected for cp in self.checkpoints),
            misleading=tuple(m for cp in self.checkpoints for m in cp.misleading),
        )


@dataclass(frozen=True)
class PooledMetrics:
    item_precision: Metric
    item_recall: Metric
    field_accuracy_kind: Metric
    field_accuracy_status: Metric
    field_accuracy_owner: Metric
    field_accuracy_due_date: Metric
    evidence_correctness: Metric
    unsupported_claim_rate: Metric
    transition_accuracy: Metric
    current_state_accuracy: Metric
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    total_latency_ms: int
    accepted_without_edit: int
    edited_accepted: int
    rejected: int
    misleading: tuple[MisleadingClaim, ...]

    @property
    def field_accuracy_combined(self) -> Metric:
        return add_metrics(
            add_metrics(self.field_accuracy_kind, self.field_accuracy_status),
            add_metrics(self.field_accuracy_owner, self.field_accuracy_due_date),
        )

    @property
    def acceptance_rate(self) -> Metric:
        total = self.accepted_without_edit + self.edited_accepted + self.rejected
        return Metric(self.accepted_without_edit + self.edited_accepted, total)

    @property
    def edit_free_acceptance_rate(self) -> Metric:
        total = self.accepted_without_edit + self.edited_accepted + self.rejected
        return Metric(self.accepted_without_edit, total)


def score_project(
    project: CorpusProject, checkpoints_run: dict[str, CheckpointRunResult], *, system: str
) -> ProjectScore:
    scored = [
        score_checkpoint(project, checkpoint, checkpoints_run[checkpoint.checkpoint_id])
        for checkpoint in sorted(project.checkpoints, key=lambda c: c.cutoff_at)
        if checkpoint.checkpoint_id in checkpoints_run
    ]
    return ProjectScore(project_key=project.key, system=system, checkpoints=tuple(scored))


# ---------------------------------------------------------------------------
# Cross-project sentinel / assignment checks (Section 13.5's assignment
# error rate and cross-project leakage; Section 15's "Privacy and
# cross-project leakage tests").
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeakageResult:
    leaked_sentinels: tuple[str, ...]
    checked_projects: int


def check_cross_project_leakage(
    outputs_by_project: dict[str, tuple[str, ...]], sentinels_by_project: dict[str, tuple[str, ...]]
) -> LeakageResult:
    """`outputs_by_project[key]` is every rendered text blob (markdown,
    raw claim text) produced *for* project `key`; `sentinels_by_project`
    is every other project's unique sentinel string. Zero leakage means
    no project's own output ever contains another project's sentinel."""
    leaked: list[str] = []
    for project_key, texts in outputs_by_project.items():
        combined = "\n".join(texts)
        for other_key, sentinels in sentinels_by_project.items():
            if other_key == project_key:
                continue
            for sentinel in sentinels:
                if sentinel in combined:
                    leaked.append(
                        f"{other_key}'s sentinel {sentinel!r} found in {project_key}'s output"
                    )
    return LeakageResult(leaked_sentinels=tuple(leaked), checked_projects=len(outputs_by_project))


__all__ = [
    "CheckpointScore",
    "LeakageResult",
    "MisleadingClaim",
    "Metric",
    "PooledMetrics",
    "ProjectScore",
    "add_metrics",
    "check_cross_project_leakage",
    "match_claims_to_items",
    "score_checkpoint",
    "score_project",
    "zero_metric",
]
