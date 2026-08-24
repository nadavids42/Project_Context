# Evaluation harness

Implements Section 13 of `docs/Project_Context_Product_Plan_v1.md`: a
reproducible benchmark comparing the ledger (Project Context) system
against a simple recent-context-summary baseline on three synthetic
projects, to answer Section 13.1's question — does the persistent,
reviewed ledger improve current-state accuracy, evidence attribution,
and meeting-prep time enough to justify its overhead? This is a **product
decision instrument**, not a demo scoreboard: every threshold in the
generated report is an annotation, never a test assertion, and a
three-project synthetic sample is directional evidence, not statistical
proof (Section 13.6).

## Module map

| Module | Role |
|---|---|
| `schema.py` | Corpus/ground-truth Pydantic models (`CorpusProject`, `GroundTruthItem`/`GroundTruthTransition`, `Checkpoint`) |
| `corpus_data/` | The three authored corpora — `implementation.py`, `advisory.py`, `launch.py`, built on shared `builder.py` infrastructure |
| `corpus_text.py` | Parses a `CorpusArtifact`'s raw bytes with the same real parsers ingestion uses |
| `materialize.py` | Freezes/reads the corpora as version-controlled files under `tests/golden_projects/benchmark_corpus/` |
| `ground_truth_state.py` | The one function answering "what should this item look like right before this cutoff?" |
| `reviewer_protocol.py` | The scripted, ground-truth-driven reviewer the ledger runner applies to every proposal |
| `ledger_runner.py` | Runs the real ingest → extract → reconcile → review → brief pipeline incrementally, checkpoint by checkpoint |
| `baseline_schema.py` / `baseline_runner.py` | The recent-context baseline's own wire schema and one-shot-per-checkpoint runner |
| `runner_types.py` | Shared `ScoredClaim`/`CheckpointRunResult` shape both runners produce |
| `scoring.py` | Every Section 13.5 metric, with documented zero-denominator/ambiguous-exclusion handling |
| `reproducibility.py` | App commit/version, model/config, prompt/schema version capture |
| `report.py` | JSON + Markdown reports, Section 13.6 thresholds as annotations, the GO/ITERATE/STOP verdict |
| `cli.py` | Orchestrates one full run; `scripts/run_evaluation.py` is its CLI entrypoint |

## Running it

```bash
# Fake/deterministic mode (default): no network, no cost, CI-safe.
python scripts/run_evaluation.py

# Live-model mode: real API calls, costs money, requires OPENAI_API_KEY
# and interactive confirmation (or --yes-i-know-this-costs-money).
python scripts/run_evaluation.py --live
```

Output goes to `data/evaluation_runs/<mode>-<timestamp>/` (gitignored):
`report.json`, `report.md`, and the raw ledger/baseline predictions.

## Fake mode is a mechanism self-test, not a product decision

Fake mode never calls a model. The ledger's extraction is **scripted**
directly from the corpus's known fact plan (see `ledger_runner.
FakeScriptedProvider`); the baseline's output is a **hand-authored,
deliberately imperfect fixture** per checkpoint
(`CorpusProject.baseline_fake_claims`) illustrating a specific, realistic
failure mode each project's traps are designed to expose — never a
prediction of what a real model would say. Every report and CLI message
this harness produces says so explicitly. Only `--live` mode measures
real baseline behavior; treat any fake-mode verdict as proof the harness
computes every metric correctly and reproducibly, not as the Section 13
decision itself.

## What "reproducible" means here

Two fresh-database runs of the same frozen corpus produce identical
*claims* (text, section, kind/title/status/owner/due-date, and evidence
resolved to the corpus's own stable artifact IDs) — but **not**
byte-identical rendered Markdown, because the real application embeds
freshly-minted database row IDs (ULIDs) into evidence-viewer links on
every run. Score against `report.json`'s structured claim data (or
`raw_ledger_predictions.json`/`raw_baseline_predictions.json`), not
against a diff of two runs' Markdown.

## Known harness limitations (by design, not bugs)

- **`changes_since_previous` cannot be trusted for meeting-prep
  recall.** The real Meeting Preparation Brief's "changes since" section
  is driven by `ledger_versions.valid_from`, a **real wall-clock**
  timestamp set at review time — not this harness's simulated
  `occurred_at` timeline. `scoring._material_expected_items` therefore
  never requires a `meeting_preparation` checkpoint to surface a
  decision/milestone/stakeholder-kind item (which has no other
  qualifying section) or a just-completed commitment/risk/question
  purely through that section; anything the system additionally
  surfaces there is still scored (never penalized on precision, never
  flagged misleading if true) but never required for recall.
- **Live-mode item matching is a documented, best-effort
  simplification.** In fake mode, every observation traces back to its
  exact ground-truth transition via the corpus's fact plan — exact by
  construction. In live mode (`ledger_runner._match_item`), a real
  model's wording will not echo the corpus author's exactly, so matching
  falls back to `(kind, normalized title/alias)` equality; an
  unmatched observation is rejected outright rather than guessed at.
  This makes live-mode ledger scores a conservative floor, not a
  ceiling.
- **Project assignment error rate is reported as not applicable.** This
  harness (like the current application) assigns each artifact to its
  project deterministically at ingestion — there is no LLM-based
  auto-assignment step to measure (Section 12.1).
