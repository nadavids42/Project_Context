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

- `backup.py` — `backup`/`restore`/`verify` subcommands for local
  database + evidence backups (Section 17). Copies the live SQLite
  database via SQLite's own online backup API and every currently
  referenced content-addressed evidence object into a fresh timestamped
  directory under a destination you choose (expected to already be on
  encrypted storage — this script does not encrypt anything itself).
  `restore` refuses to overwrite an existing database unless `--force`
  is passed. See `python scripts/backup.py --help` and
  `src/project_context/backup.py`.

- `secure_delete_maintenance.py` — explicit, optional local
  secure-delete maintenance (Section 16): `PRAGMA secure_delete=ON`
  plus `VACUUM` against the configured database, with an interactive
  confirmation prompt (skip with `--yes`). Never run automatically —
  see `src/project_context/db/maintenance.py`'s docstring for exactly
  what this does and does not guarantee (it is not a substitute for
  full-disk encryption or a certified data-destruction procedure).

Project deletion itself (previewed, exactly-confirmed, full purge —
Section 16) is a UI/service action, not a script — see **Delete a
project** in the main README and
`src/project_context/services/project_deletion.py`. Seeding golden
fixtures happens through `build_benchmark_corpus.py` above; there is no
separate seed script.
