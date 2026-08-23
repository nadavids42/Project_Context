-- 0001_evidence_foundation.sql
--
-- Projects, source/connector configuration, evidence identity and
-- versioning, extraction chunks, and sync orchestration/audit tables.
-- See docs/Project_Context_Product_Plan_v1.md Section 9 ("Data model")
-- for field-by-field rationale, status vocabularies, and index intent.
--
-- Modeling rules applied throughout:
--   * IDs are application-generated ULID text (project_context.ids),
--     never a bare SQLite rowid.
--   * All timestamps are UTC ISO 8601 text (project_context.timeutil).
--   * Every project-owned table carries its own project_id, even where
--     it is derivable through a join, to keep isolation filters direct
--     (Section 16, "Cross-project isolation").
--   * Foreign keys use SQLite's default NO ACTION behavior — nothing in
--     this schema silently cascades a delete. Project deletion is a
--     later, explicit, previewed service (Section 16), not a DB trigger.

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    objective TEXT NOT NULL CHECK (length(trim(objective)) > 0),
    description TEXT,
    stage TEXT,
    client_name TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'on_hold', 'completed', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE INDEX idx_projects_status_updated_at ON projects (status, updated_at);

-- ---------------------------------------------------------------------
-- sources: connector/boundary configuration. Secrets never live here —
-- credential_ref is an opaque, nullable pointer to OS-keyring/encrypted
-- storage, resolved elsewhere (Section 16, "OAuth tokens and API keys").
-- ---------------------------------------------------------------------
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    kind TEXT NOT NULL
        CHECK (kind IN ('manual', 'drive', 'gmail', 'calendar', 'fathom')),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    external_account_id TEXT,
    boundary_json TEXT,
    credential_ref TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_success_at TEXT,
    last_cursor TEXT,
    health_status TEXT NOT NULL DEFAULT 'unconfigured'
        CHECK (health_status IN (
            'unconfigured', 'ready', 'syncing', 'healthy',
            'degraded', 'reauth_required', 'disabled'
        )),
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_sources_project_kind_enabled ON sources (project_id, kind, enabled);
CREATE UNIQUE INDEX uq_sources_project_kind_display_name
    ON sources (project_id, kind, display_name);

-- ---------------------------------------------------------------------
-- source_artifacts: stable identity of one email/file/event/meeting
-- across versions. current_content_id points at source_contents, which
-- is defined below and itself points back at source_artifacts via
-- artifact_id. SQLite resolves foreign keys lazily (by name, at DML
-- time, not at CREATE TABLE time), so this mutual reference is valid —
-- verified directly against sqlite3 before relying on it here.
-- ---------------------------------------------------------------------
CREATE TABLE source_artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    source_id TEXT NOT NULL REFERENCES sources (id),
    external_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL
        CHECK (artifact_type IN (
            'manual_text', 'file', 'email', 'calendar_event', 'meeting'
        )),
    title TEXT,
    author TEXT,
    occurred_at TEXT,
    external_url TEXT,
    assignment_method TEXT NOT NULL DEFAULT 'manual'
        CHECK (assignment_method IN ('manual', 'boundary_match', 'user_assigned')),
    availability TEXT NOT NULL DEFAULT 'available'
        CHECK (availability IN (
            'available', 'deleted_external', 'inaccessible', 'unassigned', 'rejected'
        )),
    current_content_id TEXT REFERENCES source_contents (id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- The external artifact identity constraint: one connector cannot
-- report the same external item twice as separate artifacts (FR-009).
CREATE UNIQUE INDEX uq_source_artifacts_source_external
    ON source_artifacts (source_id, external_id);
CREATE INDEX idx_source_artifacts_project_occurred_at
    ON source_artifacts (project_id, occurred_at);

-- ---------------------------------------------------------------------
-- source_contents: immutable artifact versions (no updated_at — a new
-- version is a new row, never an edit; see product principle 3,
-- "Sources are immutable"). A version is identified by the pair
-- (artifact_id, version_key); content is independently deduplicated by
-- (artifact_id, sha256) so re-ingesting byte-identical content cannot
-- create a second row (FR-005, FR-009).
-- ---------------------------------------------------------------------
CREATE TABLE source_contents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    artifact_id TEXT NOT NULL REFERENCES source_artifacts (id),
    version_key TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    raw_storage_path TEXT,
    mime_type TEXT,
    byte_size INTEGER CHECK (byte_size IS NULL OR byte_size >= 0),
    normalized_text TEXT,
    parser_name TEXT,
    parser_version TEXT,
    parse_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (parse_status IN (
            'pending', 'parsed', 'empty', 'ocr_required', 'unsupported', 'failed'
        )),
    location_map_json TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_source_contents_artifact_version
    ON source_contents (artifact_id, version_key);
CREATE UNIQUE INDEX uq_source_contents_artifact_sha256
    ON source_contents (artifact_id, sha256);
CREATE INDEX idx_source_contents_project_parse_status
    ON source_contents (project_id, parse_status);

-- ---------------------------------------------------------------------
-- source_chunks: stable extraction/retrieval units. Rebuildable from
-- content, but retained for prompt reproducibility (Section 9). Also
-- immutable — no updated_at.
-- ---------------------------------------------------------------------
CREATE TABLE source_chunks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    content_id TEXT NOT NULL REFERENCES source_contents (id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    text TEXT NOT NULL,
    char_start INTEGER NOT NULL CHECK (char_start >= 0),
    char_end INTEGER NOT NULL CHECK (char_end > char_start),
    token_estimate INTEGER CHECK (token_estimate IS NULL OR token_estimate >= 0),
    section_path TEXT,
    location_json TEXT,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_source_chunks_content_ordinal
    ON source_chunks (content_id, ordinal);
CREATE INDEX idx_source_chunks_project_content
    ON source_chunks (project_id, content_id);

-- ---------------------------------------------------------------------
-- sync_runs / sync_items: connector orchestration audit trail (FR-031).
-- Counts are explicit counters — Section 9 allows "counts JSON or
-- explicit counters"; explicit columns keep the health panel and tests
-- simple to query without a JSON layer this early.
--
-- sync_items carries two distinct status-like columns: `stage` is the
-- furthest pipeline stage reached for this item (Section 9's listed
-- vocabulary); `status` is a smaller outcome marker for that stage
-- (pending/ok/error) used together with `error_class` for FR-031's
-- partial-failure reporting. The plan's table lists both columns and an
-- index on each independently, which this schema follows.
-- ---------------------------------------------------------------------
CREATE TABLE sync_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'partial', 'failed', 'canceled')),
    trigger TEXT NOT NULL DEFAULT 'manual' CHECK (trigger IN ('manual')),
    discovered_count INTEGER NOT NULL DEFAULT 0 CHECK (discovered_count >= 0),
    unchanged_count INTEGER NOT NULL DEFAULT 0 CHECK (unchanged_count >= 0),
    parsed_count INTEGER NOT NULL DEFAULT 0 CHECK (parsed_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    proposed_count INTEGER NOT NULL DEFAULT 0 CHECK (proposed_count >= 0),
    needs_assignment_count INTEGER NOT NULL DEFAULT 0 CHECK (needs_assignment_count >= 0),
    correlation_id TEXT,
    app_version TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_sync_runs_project_started_at ON sync_runs (project_id, started_at DESC);
CREATE INDEX idx_sync_runs_status_started_at ON sync_runs (status, started_at);

CREATE TABLE sync_items (
    id TEXT PRIMARY KEY,
    sync_run_id TEXT NOT NULL REFERENCES sync_runs (id),
    source_id TEXT NOT NULL REFERENCES sources (id),
    artifact_id TEXT REFERENCES source_artifacts (id),
    external_id TEXT NOT NULL,
    stage TEXT NOT NULL
        CHECK (stage IN (
            'discovered', 'unchanged', 'downloaded', 'parsed',
            'extracted', 'failed', 'skipped'
        )),
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('pending', 'ok', 'error')),
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 0),
    error_class TEXT
        CHECK (error_class IS NULL OR error_class IN (
            'auth', 'permission', 'rate_limit', 'not_found',
            'parse', 'schema', 'provider', 'database', 'assignment'
        )),
    safe_error_message TEXT,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_sync_items_run_status ON sync_items (sync_run_id, status);
CREATE INDEX idx_sync_items_artifact_stage ON sync_items (artifact_id, stage);
