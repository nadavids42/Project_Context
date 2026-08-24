# Scripts

- `build_benchmark_corpus.py` — regenerates the three frozen benchmark
  corpora under `tests/golden_projects/benchmark_corpus/` from
  `project_context.evaluation.corpus_data`. Run only when deliberately
  changing the corpus itself.

- `run_evaluation.py` — runs the three-project evaluation harness
  (Section 13 of `docs/Project_Context_Product_Plan_v1.md`): both the
  ledger (Project Context) and recent-context baseline systems against
  all three frozen corpora, scores every required metric, and writes
  JSON + Markdown reports to `data/evaluation_runs/<mode>-<timestamp>/`
  (gitignored). Defaults to fake/deterministic mode (no network, no
  cost); pass `--live` for the real, paid evaluation described in
  Section 13.4 (requires `OPENAI_API_KEY` and interactive confirmation
  unless `--yes-i-know-this-costs-money` is also passed). See
  `python scripts/run_evaluation.py --help` and
  `src/project_context/evaluation/README.md`.

Seed golden fixtures / export a brief / purge a project (Section 8):
not yet implemented.
