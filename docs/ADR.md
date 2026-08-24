# Architecture Decision Record — index

The twelve key architecture decisions behind Project Context, as
identified by the product plan (`Project_Context_Product_Plan_v1.md`,
Section 20, "Key architectural decisions"). This file is the promised
ADR index (Section 20's build checklist: "Add an ADR/index capturing
the twelve key architecture decisions") — each entry states the
decision, why it was made, what it rules out, and where to look if it
needs revisiting.

None of these are re-litigated by this checkpoint (Prompt 16). They are
recorded here, unchanged, as the load-bearing constraints the rest of
the codebase — and this stabilization pass — was built against.

---

## ADR-001 — SQLite + FTS5, no vector database

**Decision:** Persistence is one local SQLite file (WAL mode) with
FTS5 virtual tables (`source_chunks_fts`, `ledger_items_fts`,
`observations_fts`) for lexical search. No vector store, no embeddings.

**Rationale:** A structured project ledger plus a bounded per-project
corpus (Section 8) makes lexical/metadata retrieval sufficient until
measured otherwise — Cortex's own postmortem (Section 4/18) warns
against unmeasured memory-system complexity.

**Rules out (for now):** semantic/paraphrase recall across dissimilar
wording; ranking by embedding similarity.

**Revisit when:** Section 19.5's expansion gate is met — "Candidate
recall failure defined in Section 8 is reproduced and an A/B experiment
clears the improvement threshold." Not before.

**See:** `migrations/0004_source_chunks_fts.sql`,
`migrations/0006_ledger_fts.sql`,
`src/project_context/db/evidence_repository.py::search_chunks`.

---

## ADR-002 — Immutable evidence and observations

**Decision:** `source_contents`, `source_chunks`, and `observations`
rows are write-once. A changed source creates a new `source_contents`
version, never an edit; an observation's propositional fields are
never mutated after insert (only `status` changes).

**Rationale:** Reproducibility, provenance, audit, and correction
analysis all depend on being able to point at the exact bytes/text a
claim came from, forever.

**Rules out:** in-place correction of ingested text; deduplication by
overwrite instead of a new, dedicated row.

**See:** `migrations/0001_evidence_foundation.sql`,
`migrations/0005_ledger_and_review.sql` header,
`src/project_context/db/observation_repository.py` (no field-mutation
function is exposed, deliberately).

---

## ADR-003 — Current ledger projection + append-only versions

**Decision:** `ledger_items` holds only the current, denormalized state
of each item; `ledger_versions` is an append-only history of every
transition, each a full point-in-time snapshot.

**Rationale:** Fast "what's true right now" queries (every brief, every
list view) without losing temporal truth — a project's history is
rebuildable from `ledger_versions` alone.

**Rules out:** treating `ledger_items` as the source of truth for
anything beyond "what does the app show right now."

**See:** `migrations/0005_ledger_and_review.sql`,
`src/project_context/services/ledger.py`.

---

## ADR-004 — Deterministic reconciliation first

**Decision:** Candidate generation, scoring, and action classification
(create/no_op/add_evidence/update/complete/cancel/supersede/conflict)
are pure, deterministic Python (`services/reconciliation.py`,
`domain/reconciliation*.py`) — no LLM call decides a ledger transition.

**Rationale:** Section 10's own framing: transitions must be testable,
auditable, and reproducible; an LLM-decided state machine cannot offer
the same guarantee, and this is the exact kind of unmeasured complexity
the Cortex postmortem (Section 18) warns against.

**Rules out:** a model choosing *which* ledger item an observation
updates, or *which* action to take — it only proposes atomic facts
(ADR-005 below draws that line precisely).

**See:** `src/project_context/services/reconciliation.py`,
`tests/unit/test_reconciliation_domain.py`,
`tests/unit/test_reconciliation_service.py` (table-driven cases).

---

## ADR-005 — LLM proposals, never writes

**Decision:** The LLM only ever produces `ExtractedObservation` rows
(extraction) and `BriefClaimOutput`/`BriefComposition` rows (brief
composition) — both validated, both requiring either human review
(observations, via `proposed_mutations`) or deterministic claim
validation against pre-built facts (briefs, via
`services/briefs.py`'s claim-validation pass) before anything reaches
the ledger or a user.

**Rationale:** Hallucination containment and user trust (Section 12.8)
— every fact a user sees traces to accepted ledger state and a
verified evidence span, never directly to raw model output.

**Rules out:** any code path where a model response is written to
`ledger_items`/`ledger_versions` without going through review; any
brief claim rendered without passing claim validation.

**See:** `src/project_context/services/extraction.py`
(`validate_observation`), `src/project_context/services/briefs.py`
(module docstring: "Every model-composed claim is validated against
the exact fact payload").

---

## ADR-006 — One provider/model behind a protocol

**Decision:** `project_context.llm.provider.LLMProvider` is a small
Protocol; `OpenAIProvider` is the only implementation. Every service
that calls a model depends on the protocol, never the OpenAI SDK
directly.

**Rationale:** Model replaceability without routing complexity — Section
12.5's "Provider abstraction." Tests use `FakeLLMProvider`
exclusively (Section 15: "never call the network").

**Rules out:** multi-provider routing/fallback logic; any service
importing `openai` directly.

**See:** `src/project_context/llm/provider.py`,
`src/project_context/llm/openai_provider.py`,
`tests/fixtures/fake_llm_provider.py`.

---

## ADR-007 — Manual sync

**Decision:** Every connector sync is a user-clicked "Sync Project"
button. No background jobs, no webhooks, no polling scheduler.

**Rationale:** Avoids event infrastructure entirely and keeps
debugging/evaluation simple and reproducible (Section 8) — a run either
happened because you clicked it, or it didn't happen.

**Rules out:** any "last synced N minutes ago, refreshing..." behavior;
Fathom/Gmail push notifications.

**Revisit when:** Section 19.5's gate — "Polling delay or sync duration
materially harms >=3 users; hosted security/operations already exist."

**See:** `src/project_context/services/sync.py`,
`migrations/0001_evidence_foundation.sql`'s `sync_runs.trigger` CHECK
(`'manual'` is the only legal value).

---

## ADR-008 — External read-only

**Decision:** Every external API scope requested (Drive, Gmail,
Calendar, Fathom) is read-only. No code path ever calls a write/modify/
send endpoint on any connector.

**Rationale:** Limits blast radius and the OAuth permission surface
(Section 16, "Least privilege and external read-only access") — a bug
in this application cannot corrupt or send anything on your behalf.

**Rules out:** Gmail send/modify/label-write; Calendar event creation;
Drive file creation; any Fathom write.

**See:** README's per-connector setup sections (each states its exact
scope), `src/project_context/connectors/*.py`.

---

## ADR-009 — Local single-user deployment

**Decision:** Runs as one Streamlit process on `127.0.0.1`, one SQLite
file, one implicit user (`local-user` in every audit/review/correction
row) — no authentication layer, no multi-tenancy.

**Rationale:** Fits the prototype's actual scope and sensitive-data
risk far better than premature SaaS infrastructure (Section 16's
"Acceptable prototype vs commercial minimum" table) — building auth for
one person is pure overhead with no safety benefit yet.

**Rules out:** any notion of "which user did this" beyond the fixed
`DEFAULT_ACTOR` placeholder; hosting this build publicly.

**See:** `src/project_context/services/projects.py::DEFAULT_ACTOR`,
Section 16 of the product plan.

---

## ADR-010 — Existing Zoom-to-Drive route, no native Zoom

**Decision:** No Zoom API integration exists or is planned in this
build. Zoom content reaches this application only via whatever an
existing, externally-configured Zoom-to-Drive export workflow already
places in a Drive folder this app is already syncing.

**Rationale:** Reuses a working system instead of building a second
OAuth app, navigating host/admin permission questions, and taking on
recording-download security/webhook operations for a connector that
isn't the core hypothesis.

**Rules out:** a "Connect Zoom" button anywhere in this application.

**See:** README's "[Zoom-to-Drive compatibility](../README.md#zoom-to-drive-compatibility)",
`src/project_context/domain/zoom_hints.py`.

---

## ADR-011 — Claim generation from ledger facts

**Decision:** Brief generation builds a deterministic `BriefFact` set
first (`retrieval/briefs.py`, `retrieval/meeting_prep.py`), then asks
the model to compose claims *only* by citing those pre-built,
opaque-ID facts — never by re-reading raw evidence/chunks itself.

**Rationale:** Prevents the brief-composition model from reinterpreting
the whole corpus on its own terms; keeps every claim traceable to a
fact this application itself selected and validated (ADR-005).

**Rules out:** sending raw evidence chunks directly into the brief
composition prompt; a claim citing anything other than a supplied
`fact_id`.

**See:** `src/project_context/retrieval/brief_facts.py`,
`src/project_context/services/briefs.py`.

---

## ADR-012 — Connector feature flags and a fixed cut line

**Decision:** Drive/Gmail/Calendar/Fathom are each behind their own
`PROJECT_CONTEXT_FEATURE_*_ENABLED` flag, defaulting to `false`. Manual
ingestion never depends on any of them. A fixed cut order exists
(Gmail first, then Calendar) if time/verification budget runs out.

**Rationale:** Protects the core hypothesis (ledger + review + briefs)
and the four-week/50-hour boundary — a connector can be disabled
entirely without touching the application's actual thesis.

**Rules out:** any connector becoming a hard dependency of core
ledger/review/brief functionality.

**Verification (Prompt 16):** every connector's "disabled by default"
state is exercised directly —
`tests/unit/test_ui_sources_settings_page.py`,
`test_ui_sources_settings_gmail.py`,
`test_ui_sources_settings_calendar.py`,
`test_ui_sources_settings_fathom.py` each render the full Sources &
Settings page with that connector's flag left at its default (off) and
assert no exception; `tests/unit/test_smoke.py` imports every
submodule, including every connector, with no side effects.

**See:** `.env.example`, `src/project_context/config.py`.
