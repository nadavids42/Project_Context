# Release / checkpoint note — Prompt 16 stabilization

**Date:** 2026-08-24
**Scope:** Final first-version stabilization pass (product plan Section
15-20). No new product features were added — this pass closed
reliability, privacy, deletion/retention, backup/recovery, and
documentation gaps in the prototype built by Prompts 1-15.

**Status: `READY FOR LIMITED DOGFOOD`** — see the accompanying response
for full rationale. Not `READY FOR BENCHMARK` (the live, paid
three-project evaluation described in Section 13 has not been run this
session — see "Benchmark status" below) and not production/commercial-
ready (this remains a local, single-user prototype per ADR-009).

---

## Enabled by default

- Project lifecycle: create/edit/archive/restore, and now **delete**
  (previewed, exact-confirmation, full purge — new this checkpoint).
- Manual evidence ingestion: paste text or upload TXT/MD/DOCX/PDF/VTT.
- Evidence viewer, content-addressed storage, immutable versions.
- Structured atomic extraction (requires `OPENAI_API_KEY`; UI shows an
  actionable error, not a crash, without it).
- Deterministic reconciliation (create/no_op/add_evidence/update/
  complete/cancel/supersede/conflict) and full review workflow.
- Versioned, append-only ledger with corrections tracking.
- Current Project Brief and Meeting Preparation Brief, both with
  claim validation and a Markdown export/download button.

## Optional, disabled by default (feature-flagged)

- Google Drive sync (`PROJECT_CONTEXT_FEATURE_DRIVE_ENABLED`)
- Gmail sync (`PROJECT_CONTEXT_FEATURE_GMAIL_ENABLED`) — restricted
  Google scope; first connector to cut under time/verification budget.
- Calendar matching (`PROJECT_CONTEXT_FEATURE_CALENDAR_ENABLED`) —
  sensitive Google scope; second connector to cut.
- Fathom polling (`PROJECT_CONTEXT_FEATURE_FATHOM_ENABLED`) — API key,
  no Google OAuth.
- Zoom-to-Drive compatibility (no flag — passive; works automatically
  once Drive is enabled and pointed at an existing Zoom export folder).

Every one of the four flags defaults `false`; manual ingestion and the
whole ledger/review/brief path work fully with all four left unset.
Verified this checkpoint: each connector's page section renders with
no exception in its default-disabled state
(`tests/unit/test_ui_sources_settings_*.py`), and every submodule
including every connector imports with no side effects
(`tests/unit/test_smoke.py`).

## Explicitly excluded (by design, not by omission)

Native Zoom, multi-user/RBAC, hosted/authenticated deployment,
embeddings/vector DB, agent loops, background sync/webhooks, model
routing, billing, public onboarding — see `docs/ADR.md` and the product
plan Section 20's "Explicitly deferred" list. None of these were added
or started this checkpoint, per this prompt's explicit instruction.

## New this checkpoint (stabilization, not new product features)

- **Project deletion** (`services/project_deletion.py`,
  `db/project_deletion_repository.py`): preview with real counts, exact
  typed-name confirmation, full purge of every project-owned row/FTS
  entry, credential disconnect, and content-addressed byte cleanup that
  correctly preserves bytes still referenced by another project.
- **`DatabaseBusyError`** (`db/connection.py`): SQLite lock contention
  now surfaces as a typed error with a safe, actionable message instead
  of a raw driver exception, caught centrally in `app.py` via
  `ui/navigation.run_navigation_with_busy_guard` — verified against
  real two-connection lock contention, not a mocked exception.
- **Filesystem permission hardening**: `data/`, `data/evidence/`, and
  the SQLite database file (plus WAL/SHM sidecars) are now chmod
  0700/0600 on every startup, self-healing a pre-existing looser mode.
- **Local backup/restore** (`backup.py`, `scripts/backup.py`): online
  SQLite backup API + referenced-evidence-only copy + manifest, with a
  restore smoke test against temporary directories.
- **Secure-delete maintenance** (`db/maintenance.py`,
  `scripts/secure_delete_maintenance.py`): optional, explicit,
  never-automatic `PRAGMA secure_delete=ON` + `VACUUM`, with documented
  limitations.
- **Consolidated cross-project sentinel suite** (`tests/security/`):
  repositories, FTS, reconciliation, evidence-viewer authorization,
  brief fact building, claim validation, exports, and model-request
  construction, all exercised with the Section 15 "identical names,
  distinct sentinel" method — plus a runtime log-content scan.
- **Dependency vulnerability review**: `cryptography` was found on an
  older pinned range (`>=42,<46`, resolving to 45.0.7) with several
  disclosed advisories (PYSEC-2026-35/36/2141/3552/3553/3554,
  GHSA-537c-gmf6-5ccf) via `pip-audit` against this project's *locked*
  dependency set — bumped to `>=46.0.6,<51` (resolved: 50.0.0) and
  re-locked; full suite re-verified green.
- **Removed dead code**: `ui/pages/not_built.py` (unreferenced by
  navigation since every page it was a placeholder for now exists).
- **Fixed stale documentation/UI text**: the privacy banner claimed
  "this build does not send anything anywhere" (false — extraction has
  sent data to the configured LLM provider since Prompt 5); the
  Activity page claimed "automated sync is not built" (false since
  Prompt 10); the README's intro claimed the evaluation harness "is not
  implemented yet" (false since the most recent prior commit);
  `scripts/README.md` and `tests/integration/README.md`/
  `tests/prompt_regression/README.md` described work as not-started
  that had, in whole or in part, already been done.
- **Documentation**: this file, `docs/ADR.md` (twelve key decisions),
  `docs/MANUAL_ACCEPTANCE_CHECKLIST.md`, and substantial README
  additions (Delete a project, Backup and restore, Secure-delete
  maintenance, Security scanning, parser-limits/no-OCR/no-transcription
  callouts).

## Test counts (this checkpoint's final run)

```
1279 passed, 0 failed, 0 skipped   (pytest, full suite)
  tests/unit/          1200
  tests/security/          9   (new this checkpoint)
  tests/evaluation/        64
  tests/golden_projects/    6
```

Baseline at the start of this checkpoint (before any stabilization
changes): **1235 passed**. Net +44 tests, all net-new coverage for
this checkpoint's hardening work (deletion, backup/restore, DB
busy/locked handling, filesystem permissions, cross-project sentinel
suite, log scan) plus two small symmetry fixes (a missing
"Drive disabled by default" UI test, a `not_built.py` removal).

`ruff check .`: **all checks passed** (0 errors), before and after this
checkpoint. `ruff format --check .`: **now passes** (276/276 files
formatted) — 75 files (mostly pre-existing, from ruff-version drift
predating this checkpoint) were reformatted; every diff verified
whitespace/line-wrapping only, confirmed by a full green test re-run
immediately after.

No `mypy`/type checker is configured in this repository (`pyproject.toml`
declares no `[tool.mypy]` section and no type-checking dev dependency)
— there is no type-check gate to run. Adding one is a reasonable future
P2 item, not attempted here (would require fixing whatever it finds
across ~15 prior prompts' worth of code, which is new-feature-adjacent
scope this checkpoint explicitly excludes).

## Benchmark status (Section 13)

The three-project evaluation harness itself is fully implemented and
was re-run in **fake/deterministic mode** (no network, no cost) after
every change in this checkpoint, most recently producing
`data/evaluation_runs/fake-2026-08-24T214515725106Z/`:

- Verdict: `ITERATE` (11 of 12 evaluable go-criteria thresholds pass;
  `edit_free_acceptance_rate` observed 61.4%, needed >= 65%).
- Cross-project leakage: **0**.
- This is explicitly **a mechanism self-test, not the product
  decision** the harness's own report states plainly: fake mode's
  baseline output is a hand-authored illustrative fixture, not a real
  model prediction.

**The live, paid evaluation (`--live`, requires `OPENAI_API_KEY` and
real API spend) was not run this session** — per this prompt's
instruction that optional connectors/live model calls stay mocked
unless explicitly authorized, and no such authorization or API key was
provided. No go/iterate/stop product decision has been made against
real model output. That live run is the concrete next action for
`READY FOR BENCHMARK` status (see the accompanying response).

## Remaining known issues

### P0 — none identified

No P0 acceptance-criteria gap was found during this checkpoint's audit
of Sections 15-20 against the actual codebase.

### P1

- **No frozen prompt-regression corpus.** Section 15 asks for 20-30
  compact, frozen source-chunk/expected-output cases across specific
  scenario types (positive/negative/injection/similar-names/etc.).
  Related coverage exists scattered across
  `tests/unit/test_llm_prompts.py` and `test_extraction_service.py`,
  but not as the single consolidated, versioned asset Section 15
  describes. Documented honestly in
  `tests/prompt_regression/README.md` rather than fabricated under
  time pressure.
- **No dedicated Unassigned Evidence review queue.** Ambiguous
  connector matches (e.g. Fathom's lowest-confidence tier) are stored
  correctly, visible in that project's Evidence list, and excluded from
  automatic ledger assignment — but there is no dedicated, filtered UI
  to browse/triage them as a queue. Pre-existing (product plan Section
  20 lists this explicitly under P1, "complete if P0 is stable inside
  50 hours") — not a regression from this checkpoint.
- **`pytest` disclosed advisory (PYSEC-2026-1845), fix in 9.0.3.**
  Currently pinned `>=8.0,<9`. Left as a deliberate, tracked issue: a
  dev-only tool (never runs against real user data or in the shipped
  application), and a major-version bump deserves its own dedicated
  verification pass rather than a same-session drive-by upgrade this
  late in a stabilization checkpoint.
- **Live evaluation not yet run** (see "Benchmark status" above) — no
  product go/iterate/stop decision exists yet.

### P2

- **`SyncErrorClass.ASSIGNMENT`** is defined in the schema/enum
  (Section 8's full error-class list) but is never actually raised by
  any current connector. Investigated during this checkpoint: every
  existing "ambiguous assignment" case (e.g. Fathom's lowest-confidence
  match tier) is deliberately handled as a distinct, successful
  `unassigned` outcome, not an error (Section 11.5: "Ambiguity goes to
  unassigned/manual review") — so there is currently no genuine
  "assignment failure" code path to wire it into. Left reserved rather
  than forced into a manufactured use.
- **No repository-wide type checker** — see "Test counts" above.
- Known parser limitation: Zoom's actual VTT export uses a plain-text
  `"Speaker Name: "` prefix instead of WebVTT `<v>` tags, so
  per-speaker turn boundaries are lost for that one source (full text
  is still imported and evidence-linkable) — documented in the README's
  Zoom-to-Drive section since before this checkpoint; unchanged here.

## Privacy/security limitations that must not be overlooked

- This remains a **local, single-user prototype** (ADR-009) — no
  authentication layer exists. Anyone with access to this OS user
  account has full access to every project.
- Full-disk encryption is a **user responsibility**, not something this
  application provides or verifies — see Section 16's "Encryption."
- The secure-delete maintenance path reduces but does not certify-erase
  previously deleted data (SSD wear-leveling, copy-on-write filesystem
  snapshots, and any existing backup/copy are entirely outside its
  control — see `db/maintenance.py`'s docstring).
- Backups are unencrypted file copies to a destination *you* choose;
  this application does not encrypt them and cannot verify your chosen
  destination actually is encrypted storage.
- Google Drive/Gmail request **restricted** scopes and Calendar
  requests a **sensitive** scope — appropriate only for a private,
  unverified, test-mode OAuth client you run yourself, never for
  distributing this application to anyone else without funding Google's
  verification process first (Section 16, "Google verification
  reality").
- `OPENAI_API_KEY` is read directly from the environment, not through
  the credential-store subsystem that covers every other secret — a
  documented, deliberate interim exception (see README's Configuration
  section), not an oversight.
