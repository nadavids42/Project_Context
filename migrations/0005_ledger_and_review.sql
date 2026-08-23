-- 0005_ledger_and_review.sql
--
-- The persistent domain model for a reviewable, versioned project
-- ledger: people/aliases, atomic observations, the current-ledger
-- projection and its append-only version history, evidence provenance,
-- reconciliation proposals, and human review/correction records. See
-- docs/Project_Context_Product_Plan_v1.md Section 9 ("Data model") for
-- field-by-field rationale, and Section 10 for the transition/status
-- vocabulary this schema exists to support.
--
-- Scope note: this migration establishes schema and invariants only.
-- No reconciliation (candidate scoring/action selection), review UI, or
-- brief generation is implemented here or by any repository built on
-- top of it in this step.
--
-- Modeling rules follow migrations/0001 exactly (ULID text ids, UTC ISO
-- 8601 timestamps, explicit project_id on every project-owned table, no
-- SQL-level cascading delete). Two deliberate exceptions, both called
-- out where they occur below:
--   * `people` and `person_aliases` are NOT project-owned — Section 9
--     describes them as canonical identity shared across projects
--     ("One person may have multiple email addresses; identity merging
--     is manual in MVP", Section 4). `project_people` is the
--     project-scoped join that carries a per-project stakeholder role.
--   * `evidence_links`, `proposed_mutations`, and `corrections` use a
--     polymorphic `target_type`/`target_id` pair with no SQL foreign
--     key on `target_id` (SQLite cannot express a conditional FK).
--     Section 9 itself says evidence-link span validity is a service
--     check, not a DB one; the same applies to target existence and
--     project-matching for every polymorphic reference in this file —
--     enforced by project_context.db.evidence_link_repository, not SQL.
--
-- Documented deviations from Section 9's literal field list, required by
-- the existing codebase (Prompt 5's project_context.llm.schemas):
--   1. `observations.statement` replaces the plan's separate
--      `predicate`/`object` columns. Prompt 5's ExtractedObservation
--      already models one atomic proposition as a single `statement`
--      string (mirrored from the product plan's own representative
--      Pydantic schema in Section 9); splitting it into predicate/object
--      would require inventing extraction behavior no prompt has asked
--      for. observations_fts (migration 0006) follows the same
--      substitution.
--   2. `observations.polarity` defaults to `'positive'` rather than
--      being populated by extraction — Prompt 5's schema has no polarity
--      field yet. The column exists per Section 9 and is reserved for a
--      future extraction enhancement.
--   3. `observations.schema_version` is additive: Section 9 lists
--      `model_id`/`prompt_version` on this table but Section 12.4
--      separately requires "Store schema version separately from prompt
--      version," which Prompt 5's ExtractionRunResult already tracks.
--      Dropping it here would lose provenance Prompt 5 established.
--   4. `corrections.actor` is additive: Section 9's table listing omits
--      it, but FR-016's acceptance criteria explicitly requires it
--      ("Correction row links project, item/proposal, model/prompt,
--      evidence, actor, reason, and timestamp").
--   5. Every table below has a surrogate ULID `id` primary key,
--      including `project_people` (whose Section 9 field list has no
--      `id` column). This matches Section 9's own modeling rule ("Use
--      UUIDv7/ULID-style text IDs for sortable application identities")
--      and how every other table in this repository is built, including
--      ones (`sources`) that are already uniquely identified by a
--      natural key.
--   6. Per-kind ledger status/transition vocabularies (which statuses
--      are valid for which item kind, and which (from_status,
--      transition_type) pairs are legal) are not fully enumerated
--      anywhere in the product plan text. project_context.domain.ledger
--      defines them explicitly as the single source of truth; the CHECK
--      constraint on ledger_items.status below mirrors it and must be
--      kept in sync by hand if either changes.

-- ---------------------------------------------------------------------
-- people / person_aliases: canonical identity, shared across projects
-- (not project-owned — see module docstring above).
-- ---------------------------------------------------------------------
CREATE TABLE people (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    primary_email TEXT,
    organization TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- "unique normalized email when present" (Section 9). A partial
-- expression index: SQLite indexes `lower(trim(primary_email))` only for
-- rows where it is non-null, so any number of people may have a NULL
-- primary_email without colliding.
CREATE UNIQUE INDEX uq_people_primary_email_normalized
    ON people (lower(trim(primary_email)))
    WHERE primary_email IS NOT NULL;

CREATE TABLE person_aliases (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES people (id),
    alias_type TEXT NOT NULL CHECK (alias_type IN ('email', 'name', 'speaker_label')),
    alias_value TEXT NOT NULL CHECK (length(trim(alias_value)) > 0),
    normalized_value TEXT NOT NULL CHECK (length(trim(normalized_value)) > 0),
    source TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_person_aliases_person ON person_aliases (person_id);

-- "unique (alias_type, normalized_value) only when safe; otherwise allow
-- ambiguity" (Section 9). An email alias is safe to treat as a unique
-- pointer to one person — enforced here. A name/speaker-label alias is
-- explicitly expected to be ambiguous (two people can share a first
-- name); resolving that ambiguity is project_context.db.people_repository
-- .resolve_person's job, not a uniqueness constraint.
CREATE UNIQUE INDEX uq_person_aliases_email_normalized
    ON person_aliases (normalized_value)
    WHERE alias_type = 'email';

-- ---------------------------------------------------------------------
-- project_people: the project-scoped stakeholder role join (Section 9).
-- ---------------------------------------------------------------------
CREATE TABLE project_people (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    person_id TEXT NOT NULL REFERENCES people (id),
    role TEXT,
    organization TEXT,
    is_internal INTEGER NOT NULL DEFAULT 0 CHECK (is_internal IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'unknown')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    evidence_link_id TEXT REFERENCES evidence_links (id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_project_people_project_person_role
    ON project_people (project_id, person_id, role);
CREATE INDEX idx_project_people_project_status ON project_people (project_id, status);

-- ---------------------------------------------------------------------
-- observations: atomic, immutable extracted propositions (Section 9;
-- Prompt 5's project_context.llm.schemas.ExtractedObservation is the
-- validated proposal shape this table stores once accepted). Only
-- `status` is ever updated after insert — every propositional field is
-- write-once, enforced by project_context.db.observation_repository
-- exposing no field-mutation function (Prompt 6: "Observations are
-- immutable atomic propositions").
-- ---------------------------------------------------------------------
CREATE TABLE observations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    content_id TEXT NOT NULL REFERENCES source_contents (id),
    chunk_id TEXT NOT NULL REFERENCES source_chunks (id),
    kind TEXT NOT NULL CHECK (kind IN (
        'update', 'decision', 'commitment', 'milestone',
        'risk', 'blocker', 'open_question', 'stakeholder'
    )),
    subject TEXT NOT NULL CHECK (length(trim(subject)) > 0),
    -- Deviation 1 (see header): one `statement` column, not
    -- `predicate`/`object`.
    statement TEXT NOT NULL CHECK (length(trim(statement)) > 0),
    owner_text TEXT,
    owner_person_id TEXT REFERENCES people (id),
    date_value TEXT,
    date_text TEXT,
    -- Deviation 2 (see header): defaulted, not yet extraction-populated.
    polarity TEXT NOT NULL DEFAULT 'positive' CHECK (polarity IN ('positive', 'negative')),
    explicitness TEXT NOT NULL CHECK (explicitness IN ('explicit', 'strongly_entailed')),
    model_id TEXT,
    prompt_version TEXT,
    -- Deviation 3 (see header): additive, tracked separately from
    -- prompt_version per Section 12.4.
    schema_version TEXT,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'valid'
        CHECK (status IN ('valid', 'invalid', 'duplicate', 'rejected', 'reconciled')),
    created_at TEXT NOT NULL
);

-- "unique semantic-source fingerprint" (Section 9) — the exact-duplicate
-- guard from Section 10.2 #3: hash of (content_id, kind, normalized
-- statement, sorted evidence spans). Computed in Python
-- (project_context.domain.observations.compute_fingerprint) so the
-- normalization rule lives in one place; this index only enforces it.
CREATE UNIQUE INDEX uq_observations_fingerprint ON observations (fingerprint);
CREATE INDEX idx_observations_project_kind_status ON observations (project_id, kind, status);
CREATE INDEX idx_observations_owner_date ON observations (owner_person_id, date_value);

-- ---------------------------------------------------------------------
-- ledger_items: the CURRENT projection only (Section 9: "Current ledger
-- rows are convenience projections. ledger_versions and reviews provide
-- history."). Never written to directly by anything but the
-- ledger-transition path this table's own append-only version history
-- backs — no such transaction exists yet (Prompt 8).
--
-- current_version_id is nullable for the same reason
-- source_artifacts.current_content_id is (migrations/0001): a
-- ledger_item must exist before its first ledger_version row can
-- reference it back, so creation is a two-step insert within one
-- transaction. SQLite resolves this mutual FK lazily, as already
-- verified for source_artifacts/source_contents.
-- ---------------------------------------------------------------------
CREATE TABLE ledger_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    kind TEXT NOT NULL CHECK (kind IN (
        'decision', 'commitment', 'milestone', 'risk', 'blocker', 'open_question', 'stakeholder'
    )),
    canonical_title TEXT NOT NULL CHECK (length(trim(canonical_title)) > 0),
    canonical_description TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('open', 'active', 'completed', 'resolved', 'canceled', 'superseded')),
    owner_person_id TEXT REFERENCES people (id),
    due_date TEXT,
    effective_at TEXT,
    current_version_id TEXT REFERENCES ledger_versions (id),
    confidence_band TEXT CHECK (confidence_band IS NULL OR confidence_band IN ('high', 'medium', 'low')),
    user_corrected INTEGER NOT NULL DEFAULT 0 CHECK (user_corrected IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Deviation 6 (see header): kind/status compatibility, mirroring
    -- project_context.domain.ledger.VALID_STATUSES_BY_KIND exactly.
    -- Keep both in sync by hand if either changes.
    CHECK (
        (kind = 'commitment' AND status IN ('open', 'active', 'completed', 'canceled', 'superseded'))
        OR (kind = 'decision' AND status IN ('active', 'canceled', 'superseded'))
        OR (kind = 'milestone' AND status IN ('open', 'active', 'completed', 'canceled', 'superseded'))
        OR (kind = 'risk' AND status IN ('open', 'active', 'resolved', 'canceled', 'superseded'))
        OR (kind = 'blocker' AND status IN ('open', 'active', 'resolved', 'canceled', 'superseded'))
        OR (kind = 'open_question' AND status IN ('open', 'resolved', 'canceled', 'superseded'))
        OR (kind = 'stakeholder' AND status IN ('active', 'canceled', 'superseded'))
    )
);

CREATE INDEX idx_ledger_items_project_kind_status ON ledger_items (project_id, kind, status);
CREATE INDEX idx_ledger_items_project_due_date ON ledger_items (project_id, due_date);
CREATE INDEX idx_ledger_items_owner_status ON ledger_items (owner_person_id, status);

-- ---------------------------------------------------------------------
-- ledger_versions: append-only values/transitions (Section 9, Section
-- 10.14: "Never update ledger_versions in place"). Each row is a full
-- point-in-time snapshot of the projection fields, so ledger_items can
-- be rebuilt/audited from history alone.
-- ---------------------------------------------------------------------
CREATE TABLE ledger_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    ledger_item_id TEXT NOT NULL REFERENCES ledger_items (id),
    version_no INTEGER NOT NULL CHECK (version_no >= 1),
    canonical_title TEXT NOT NULL,
    canonical_description TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('open', 'active', 'completed', 'resolved', 'canceled', 'superseded')),
    owner_person_id TEXT REFERENCES people (id),
    due_date TEXT,
    effective_at TEXT,
    confidence_band TEXT CHECK (confidence_band IS NULL OR confidence_band IN ('high', 'medium', 'low')),
    transition_type TEXT NOT NULL CHECK (transition_type IN (
        'create', 'update', 'complete', 'resolve', 'cancel', 'supersede', 'reopen', 'correct'
    )),
    from_version_id TEXT REFERENCES ledger_versions (id),
    observation_id TEXT REFERENCES observations (id),
    review_id TEXT REFERENCES reviews (id),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    superseded_by_version_id TEXT REFERENCES ledger_versions (id),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_ledger_versions_item_version_no ON ledger_versions (ledger_item_id, version_no);
CREATE INDEX idx_ledger_versions_project_valid_from ON ledger_versions (project_id, valid_from);

-- ---------------------------------------------------------------------
-- evidence_links: many-to-many provenance from an observation, ledger
-- item, ledger version, or (later) brief claim to exact evidence
-- (Section 9). `target_type`/`target_id` are polymorphic — see the
-- module docstring's second bullet on why there is no SQL FK on
-- target_id and why "brief_claim" is accepted by this CHECK today even
-- though no brief_claims table exists yet (Prompt 6: "can target...
-- later brief claims"). project_context.db.evidence_link_repository
-- rejects an actual insert against 'brief_claim' until that table
-- exists, since it cannot validate the target.
-- ---------------------------------------------------------------------
CREATE TABLE evidence_links (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    target_type TEXT NOT NULL
        CHECK (target_type IN ('observation', 'ledger_item', 'ledger_version', 'brief_claim')),
    target_id TEXT NOT NULL,
    content_id TEXT NOT NULL REFERENCES source_contents (id),
    chunk_id TEXT REFERENCES source_chunks (id),
    char_start INTEGER NOT NULL CHECK (char_start >= 0),
    char_end INTEGER NOT NULL CHECK (char_end > char_start),
    quote TEXT NOT NULL CHECK (length(trim(quote)) > 0),
    location_json TEXT,
    support_role TEXT NOT NULL
        CHECK (support_role IN ('supports', 'contradicts', 'context', 'completion', 'supersession')),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_evidence_links_target ON evidence_links (target_type, target_id);
CREATE INDEX idx_evidence_links_project_content ON evidence_links (project_id, content_id);

-- ---------------------------------------------------------------------
-- proposed_mutations: the reviewable reconciliation result (Section 9).
-- No reconciliation engine exists yet — this table and its repository
-- exist so the review/ledger schema is complete; nothing populates it
-- automatically in this step.
-- ---------------------------------------------------------------------
CREATE TABLE proposed_mutations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    observation_id TEXT NOT NULL REFERENCES observations (id),
    action TEXT NOT NULL CHECK (action IN (
        'create', 'no_op', 'add_evidence', 'update', 'complete', 'cancel', 'supersede', 'conflict'
    )),
    target_ledger_item_id TEXT REFERENCES ledger_items (id),
    proposed_patch_json TEXT,
    candidate_features_json TEXT,
    confidence_score REAL CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)),
    confidence_band TEXT CHECK (confidence_band IS NULL OR confidence_band IN ('high', 'medium', 'low')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'edited_accepted', 'rejected', 'expired')),
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

-- "one active proposal/observation" (Section 9): at most one *pending*
-- proposal may exist per observation at a time. Historical (resolved)
-- proposals for the same observation are not constrained by this index.
CREATE UNIQUE INDEX uq_proposed_mutations_one_pending_per_observation
    ON proposed_mutations (observation_id)
    WHERE status = 'pending';
CREATE INDEX idx_proposed_mutations_project_status_created
    ON proposed_mutations (project_id, status, created_at);

-- ---------------------------------------------------------------------
-- reviews: the human decision record (Section 9). At most one review
-- row per proposal — a review is a final decision act, not a queue.
-- ---------------------------------------------------------------------
CREATE TABLE reviews (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    proposal_id TEXT NOT NULL REFERENCES proposed_mutations (id),
    action TEXT NOT NULL CHECK (action IN (
        'accept', 'edit_accept', 'reject', 'mark_complete', 'mark_superseded', 'treat_as_new'
    )),
    before_json TEXT,
    after_json TEXT,
    reason_code TEXT,
    note TEXT,
    actor TEXT NOT NULL DEFAULT 'local-user',
    reviewed_at TEXT NOT NULL,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

CREATE UNIQUE INDEX uq_reviews_proposal ON reviews (proposal_id);
CREATE INDEX idx_reviews_project_reviewed_at ON reviews (project_id, reviewed_at);

-- ---------------------------------------------------------------------
-- corrections: explicit model/system error record (Section 9, FR-016).
-- Deviation 4 (see header): `actor` is additive, required by FR-016 but
-- absent from Section 9's own field list for this table.
-- ---------------------------------------------------------------------
CREATE TABLE corrections (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    target_type TEXT NOT NULL
        CHECK (target_type IN ('observation', 'ledger_item', 'ledger_version', 'proposed_mutation')),
    target_id TEXT NOT NULL,
    review_id TEXT REFERENCES reviews (id),
    field_name TEXT NOT NULL CHECK (length(trim(field_name)) > 0),
    original_json TEXT,
    corrected_json TEXT,
    reason_code TEXT NOT NULL CHECK (reason_code IN (
        'wrong_type', 'wrong_match', 'unsupported', 'wrong_owner',
        'wrong_date', 'wrong_status', 'wording', 'missing', 'other'
    )),
    materiality TEXT NOT NULL CHECK (materiality IN ('minor', 'material')),
    error_signature TEXT NOT NULL,
    model_id TEXT,
    prompt_version TEXT,
    actor TEXT NOT NULL DEFAULT 'local-user',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_corrections_project_error_signature ON corrections (project_id, error_signature);
CREATE INDEX idx_corrections_model_prompt ON corrections (model_id, prompt_version);
