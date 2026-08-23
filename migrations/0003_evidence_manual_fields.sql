-- 0003_evidence_manual_fields.sql
--
-- Two small additive columns needed by manual evidence ingestion
-- (FR-005/FR-006), narrowly scoped to this feature rather than folded
-- into an existing loosely-typed JSON blob:
--
--   * source_artifacts.source_type — a user-facing descriptive tag
--     ("meeting notes", "email", ...), distinct from the structural
--     `artifact_type` column (manual_text/file/email/...), which is
--     about *how* the artifact was ingested, not what it is.
--   * source_contents.original_filename — preserved as-is for uploaded
--     files (Section 15: "Preserve original filename and MIME
--     metadata"); NULL for pasted manual text, which has no filename.
--
-- Both are nullable so existing rows (there are none yet in practice,
-- but the migration must still be valid against a populated table) are
-- unaffected; SQLite's ALTER TABLE ADD COLUMN accepts a CHECK
-- constraint here because it evaluates to true for every existing row
-- (NULL satisfies the `IS NULL` branch).

ALTER TABLE source_artifacts ADD COLUMN source_type TEXT
    CHECK (source_type IS NULL OR source_type IN (
        'meeting_notes', 'email', 'document', 'chat_transcript', 'call_recording', 'other'
    ));

ALTER TABLE source_contents ADD COLUMN original_filename TEXT;
