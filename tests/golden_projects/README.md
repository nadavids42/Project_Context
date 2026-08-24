# Golden projects

Two distinct things live in this directory:

- `golden_fixtures.py` + `test_manual_vertical_slice.py`: three **compact**
  synthetic mini-projects (~10 facts each) that exercise the manual
  ingest → extract → reconcile → review → brief vertical slice end to
  end. Fast unit-scale fixtures, not the evaluation benchmark.

- `benchmark_corpus/`: the three **full** synthetic benchmark projects
  required by Section 13 of the product plan — `implementation/`
  ("Atlas Migration"), `advisory/` ("Meridian Advisory Engagement"),
  and `launch/` ("Comet Launch"). Each holds:
  - `artifacts/` — the real bytes (`.vtt`/`.txt`/`.md`) every meeting
    transcript, email, document, and calendar-metadata note is made of;
  - `manifest.json` — artifact metadata/chronology, evaluation
    checkpoints, the scripted extraction fact-plan, and fake-mode
    baseline fixtures;
  - `ground_truth.json` — every atomic item and its field/status
    transitions over time, each with the exact evidence span it must be
    traceable to.

  These are **frozen, version-controlled files** — the authoring source
  of truth is `project_context.evaluation.corpus_data` (three Python
  modules, one per project); regenerate this directory only with
  `python scripts/build_benchmark_corpus.py`, and only when
  deliberately changing the corpus. See
  `src/project_context/evaluation/README.md` and Section 13 of
  `docs/Project_Context_Product_Plan_v1.md` for the full harness this
  corpus feeds (`project_context.evaluation`, `tests/evaluation/`,
  `scripts/run_evaluation.py`).

A release is blocked if current-state accuracy, evidence correctness,
leakage, or materially-misleading-claim rate regresses beyond Section
13.6's thresholds against this corpus — store the exact application
commit, model/configuration, and prompt/schema versions alongside any
report generated from it (`project_context.evaluation.reproducibility`
does this automatically).
