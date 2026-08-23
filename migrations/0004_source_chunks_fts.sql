-- 0004_source_chunks_fts.sql
--
-- FTS5 full-text index over source_chunks (Section 9, "FTS5 virtual
-- tables"). An external-content table: it stores no text of its own,
-- only a token index, keyed by `source_chunks.rowid` (SQLite's
-- implicit integer rowid — distinct from the application's own text
-- `id` primary key, which FTS5's `content_rowid` cannot use directly
-- since it requires an integer). Every table in this schema keeps its
-- rowid regardless of a TEXT primary key, so this is available for
-- free.
--
-- Kept synchronized with triggers rather than relying on every
-- repository call site to remember to write to it (Section 9: "Keep
-- FTS synchronized with triggers or explicit repository writes, and
-- test insert/update/delete behavior"). source_chunks rows are
-- currently only ever inserted (immutable per Section 9), but the
-- update/delete triggers are included for correctness and because a
-- future rebuild-on-reparse path will need them.
--
-- FTS5 query results carry only `rowid` plus indexed columns — never
-- `project_id` — so every caller MUST join back through
-- `source_chunks` to filter by project. See
-- project_context.db.evidence_repository.search_chunks, the only
-- sanctioned way to query this table.

CREATE VIRTUAL TABLE source_chunks_fts USING fts5(
    text,
    section_path,
    content='source_chunks',
    content_rowid='rowid'
);

CREATE TRIGGER trg_source_chunks_fts_ai AFTER INSERT ON source_chunks BEGIN
    INSERT INTO source_chunks_fts(rowid, text, section_path)
    VALUES (new.rowid, new.text, new.section_path);
END;

CREATE TRIGGER trg_source_chunks_fts_ad AFTER DELETE ON source_chunks BEGIN
    INSERT INTO source_chunks_fts(source_chunks_fts, rowid, text, section_path)
    VALUES ('delete', old.rowid, old.text, old.section_path);
END;

CREATE TRIGGER trg_source_chunks_fts_au AFTER UPDATE ON source_chunks BEGIN
    INSERT INTO source_chunks_fts(source_chunks_fts, rowid, text, section_path)
    VALUES ('delete', old.rowid, old.text, old.section_path);
    INSERT INTO source_chunks_fts(rowid, text, section_path)
    VALUES (new.rowid, new.text, new.section_path);
END;
