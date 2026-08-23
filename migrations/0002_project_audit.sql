-- 0002_project_audit.sql
--
-- A minimal, append-only audit trail for project lifecycle actions
-- (create/update/archive/restore). See
-- docs/Project_Context_Product_Plan_v1.md FR-001 ("edits create an
-- audit entry") and Section 9 ("Modeling rules").
--
-- Narrowly scoped on purpose: `entity_type` currently allows only
-- 'project'. This is deliberately not the general-purpose `reviews` /
-- `corrections` audit trail described in Section 9 for ledger items —
-- those tables belong to the reconciliation/review milestone and are
-- out of scope here. Widening `entity_type` later is a new migration,
-- not an edit to this one.

CREATE TABLE audit_entries (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects (id),
    entity_type TEXT NOT NULL CHECK (entity_type IN ('project')),
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create', 'update', 'archive', 'restore')),
    before_json TEXT,
    after_json TEXT,
    actor TEXT NOT NULL DEFAULT 'local-user',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_audit_entries_project_created_at ON audit_entries (project_id, created_at);
CREATE INDEX idx_audit_entries_entity ON audit_entries (entity_type, entity_id, created_at);
