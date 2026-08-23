-- 0009_sync_run_counts.sql
--
-- Two additive counters on `sync_runs` (Section 9's table already
-- listed "counts JSON or explicit counters" as an acceptable design;
-- migrations/0001 chose explicit counters but only covered
-- discovered/unchanged/parsed/failed/proposed/needs_assignment — five
-- pipeline stages, not six-plus a distinct "downloaded" and
-- "extracted"). Prompt 10's required Sync Project UI counts are
-- exactly "discovered, unchanged, downloaded, parsed, extracted,
-- failed, unassigned" — seven distinct numbers. `downloaded_count` and
-- `extracted_count` were the two missing from the original schema;
-- everything else (`discovered_count`, `unchanged_count`,
-- `parsed_count`, `failed_count`, `needs_assignment_count`) already
-- existed and is reused unchanged. `proposed_count` also already
-- existed and is retained (it is not one of the seven UI counts, but
-- is separately useful audit information — "how many new reconciliation
-- proposals did this sync produce").

ALTER TABLE sync_runs ADD COLUMN downloaded_count INTEGER NOT NULL DEFAULT 0
    CHECK (downloaded_count >= 0);
ALTER TABLE sync_runs ADD COLUMN extracted_count INTEGER NOT NULL DEFAULT 0
    CHECK (extracted_count >= 0);
