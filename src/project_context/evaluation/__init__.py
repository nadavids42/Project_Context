"""The three-project benchmark and baseline-comparison evaluation harness
(docs/Project_Context_Product_Plan_v1.md Section 13; Section 14's W4.3;
Section 15's "Golden project datasets"; Section 19's "Proof required
before another user" / "Stop evidence"; Section 20's "Evaluation and
decision").

This package is a **product decision instrument**, not a demo scoreboard
(Prompt 15). It exists to answer Section 13.1's evaluation question —
"Does a persistent, reviewed ledger improve current-state accuracy,
evidence attribution, and meeting-preparation time enough to justify its
ingestion/review overhead relative to a simple recent-context summary?"
— on three fully synthetic, version-controlled corpora, and to report a
documented `GO` / `ITERATE` / `STOP` recommendation against Section
13.6's thresholds, never a tuned or gamed one.

Submodules, in dependency order:

- ``schema`` — the corpus/ground-truth Pydantic models (Section 13.3:
  "Store ground truth as versioned JSON/CSV separate from application
  outputs").
- ``corpus_data`` — the three authored ``CorpusProject`` builders
  (Section 13.2's required traps per project).
- ``materialize`` — writes/reads the frozen, version-controlled corpus
  files under ``tests/golden_projects/benchmark_corpus/`` (Section 13.7,
  step 1: "Freeze corpus, ground truth...").
- ``ground_truth_state`` — the one deterministic function
  (``state_at_cutoff``) both scoring and the ledger runner use to ask
  "what should this item look like right before this cutoff?" — a
  single source of truth so scoring can never silently disagree with
  what the ledger runner was told to ingest.
- ``reviewer_protocol`` — the scripted, ground-truth-driven reviewer
  (Section 13.4: "review proposals using a predefined reviewer
  protocol").
- ``ledger_runner`` / ``baseline_runner`` — the two compared systems
  (Section 13.4).
- ``baseline_schema`` — the baseline's own wire schema, deliberately
  shaped like the ledger's ``BriefFact``/``BriefClaim`` taxonomy so
  scoring can compare the two systems on equal terms (Section 13.4:
  "same output taxonomy").
- ``scoring`` — every metric in Section 13.5.
- ``reproducibility`` — app commit/version, model/config, prompt/schema
  version capture (Section 13.7, step 1; Section 15's golden-dataset
  "Store the exact application commit...").
- ``report`` — JSON + Markdown reports, Section 13.6's thresholds as
  annotations (never test assertions), and the final verdict.
- ``cli`` — orchestrates a full run; ``scripts/run_evaluation.py`` is
  its thin command-line entrypoint.
"""

from __future__ import annotations
