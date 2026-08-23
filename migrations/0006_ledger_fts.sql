-- 0006_ledger_fts.sql
--
-- FTS5 full-text indexes over the current ledger projection and
-- observations (Section 9, "FTS5 virtual tables"; Prompt 6: "Add FTS5
-- for current ledger title/description and observations"). Same
-- external-content pattern as migrations/0004_source_chunks_fts.sql:
-- these store no text of their own, only a token index keyed by the
-- indexed table's `rowid`.
--
-- Every FTS5 query result carries only `rowid` plus indexed columns —
-- never `project_id` — so callers MUST join back through the indexed
-- table and filter by `project_id = ?`. See
-- project_context.db.ledger_repository.search_ledger_items and
-- project_context.db.observation_repository.search_observations, the
-- only sanctioned ways to query these tables.
--
-- observations_fts indexes `statement`, not `predicate`/`object` —
-- following migration 0005's documented deviation 1 (observations has
-- one `statement` column, matching Prompt 5's extraction schema).
--
-- observations is immutable after insert (Prompt 6 invariant), so only
-- insert/delete triggers are needed — no row's subject/statement text
-- ever changes, only its `status` column, which observations_fts does
-- not index. ledger_items *is* updated in place as new versions become
-- current, so it gets the same insert/update/delete trigger set already
-- established for source_chunks_fts in migration 0004.

CREATE VIRTUAL TABLE ledger_items_fts USING fts5(
    canonical_title,
    canonical_description,
    content='ledger_items',
    content_rowid='rowid'
);

CREATE TRIGGER trg_ledger_items_fts_ai AFTER INSERT ON ledger_items BEGIN
    INSERT INTO ledger_items_fts(rowid, canonical_title, canonical_description)
    VALUES (new.rowid, new.canonical_title, new.canonical_description);
END;

CREATE TRIGGER trg_ledger_items_fts_ad AFTER DELETE ON ledger_items BEGIN
    INSERT INTO ledger_items_fts(ledger_items_fts, rowid, canonical_title, canonical_description)
    VALUES ('delete', old.rowid, old.canonical_title, old.canonical_description);
END;

CREATE TRIGGER trg_ledger_items_fts_au AFTER UPDATE ON ledger_items BEGIN
    INSERT INTO ledger_items_fts(ledger_items_fts, rowid, canonical_title, canonical_description)
    VALUES ('delete', old.rowid, old.canonical_title, old.canonical_description);
    INSERT INTO ledger_items_fts(rowid, canonical_title, canonical_description)
    VALUES (new.rowid, new.canonical_title, new.canonical_description);
END;

CREATE VIRTUAL TABLE observations_fts USING fts5(
    subject,
    statement,
    content='observations',
    content_rowid='rowid'
);

CREATE TRIGGER trg_observations_fts_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observations_fts(rowid, subject, statement)
    VALUES (new.rowid, new.subject, new.statement);
END;

CREATE TRIGGER trg_observations_fts_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, subject, statement)
    VALUES ('delete', old.rowid, old.subject, old.statement);
END;
