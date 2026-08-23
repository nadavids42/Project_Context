-- 0008_briefs.sql
--
-- Reproducible, cited briefs (Section 9, tables `generated_briefs` and
-- `brief_claims`; Prompt 9; FR-024 through FR-026; ADR-011 "Claim
-- generation from ledger facts"). A brief is never freshly reinterpreted
-- from raw evidence at render time — `input_snapshot_json` freezes the
-- exact deterministic `BriefFact` payload a generation run saw, so the
-- same accepted ledger state always reproduces the same brief inputs.
--
-- Documented deviations from Section 9's literal field list, following
-- the same pattern already established in migrations/0005's header:
--   1. `brief_claims.ledger_item_id`/`ledger_version_id` are the single
--      *primary* citation Section 9 lists (indexed, used for "which
--      items does this brief talk about" queries). Section 9's
--      representative `BriefClaim` Pydantic schema separately allows a
--      claim to cite up to 10 `ledger_version_ids` — a claim comparing
--      an old and new date, for instance, legitimately spans two
--      versions. That fuller citation set is preserved additively in
--      `cited_fact_ids_json`: the opaque `BriefFact.fact_id` values the
--      claim actually referenced (see project_context.domain.briefs and
--      project_context.retrieval.briefs) — each resolves back to its own
--      ledger_item_id/ledger_version_id/evidence_link_ids server-side,
--      so nothing citable is lost, it is just keyed by the one ID space
--      the model itself is ever shown (Section 12.4: "Keep evidence IDs
--      opaque and supplied by the app").
--   2. `brief_claims.evidence_link_ids` (Section 9's BriefClaim schema)
--      is not a column here at all — `evidence_links.target_type`
--      already reserves `'brief_claim'` for exactly this many-to-many
--      relationship (migration 0005's header: "can target... later
--      brief claims"; project_context.db.evidence_link_repository
--      rejected inserting one until this table existed). This migration
--      is what unblocks that: a claim's cited evidence is `evidence_links`
--      rows with `target_type='brief_claim', target_id=brief_claims.id`,
--      not a redundant JSON list.
--   3. `schema_version` is additive (Section 12.4: "Store schema version
--      separately from prompt version"), matching observations'
--      identical deviation in migration 0005.
--
-- `input_tokens`/`output_tokens`/`estimated_cost_usd`/`latency_ms` and
-- `safe_error` are additive telemetry/diagnostic fields, mirroring
-- `project_context.llm.provider.StructuredResult` and
-- `project_context.services.extraction.ExtractionRunResult` — Section
-- 12.7: "Record per-call tokens/cost."

CREATE TABLE generated_briefs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    brief_type TEXT NOT NULL CHECK (brief_type IN ('current_project', 'meeting_preparation')),
    -- Meeting Preparation only (Section 9); NULL for a Current Project Brief.
    meeting_artifact_id TEXT REFERENCES source_artifacts (id),
    cutoff_at TEXT NOT NULL,
    input_snapshot_json TEXT NOT NULL,
    markdown TEXT,
    model_id TEXT,
    prompt_version TEXT,
    -- Deviation 3 (see header).
    schema_version TEXT,
    status TEXT NOT NULL DEFAULT 'generating'
        CHECK (status IN ('generating', 'valid', 'failed', 'superseded')),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    estimated_cost_usd REAL CHECK (estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    safe_error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_generated_briefs_project_type_created
    ON generated_briefs (project_id, brief_type, created_at DESC);

CREATE TABLE brief_claims (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    brief_id TEXT NOT NULL REFERENCES generated_briefs (id),
    section TEXT NOT NULL CHECK (length(trim(section)) > 0),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    claim_text TEXT NOT NULL CHECK (length(trim(claim_text)) > 0),
    claim_type TEXT NOT NULL CHECK (claim_type IN ('fact', 'inference', 'suggestion')),
    -- Deviation 1 (see header): primary citation only.
    ledger_item_id TEXT REFERENCES ledger_items (id),
    ledger_version_id TEXT REFERENCES ledger_versions (id),
    -- Deviation 1 (see header): the full opaque BriefFact citation set.
    cited_fact_ids_json TEXT NOT NULL,
    validation_status TEXT NOT NULL
        CHECK (validation_status IN ('valid', 'unsupported', 'invalid_reference')),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_brief_claims_brief_ordinal ON brief_claims (brief_id, ordinal);
CREATE INDEX idx_brief_claims_ledger_item ON brief_claims (ledger_item_id);
CREATE INDEX idx_brief_claims_project_brief ON brief_claims (project_id, brief_id);
