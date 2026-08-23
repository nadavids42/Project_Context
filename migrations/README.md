# Migrations

Numbered SQL migrations, applied in order by
[`project_context.db.migrations`](../src/project_context/db/migrations.py).

- Filename pattern: `NNNN_description.sql` (four-digit, zero-padded,
  lowercase snake_case description).
- Each file is applied inside one transaction and recorded in the
  `schema_migrations` table; a migration that fails is rolled back and
  left unrecorded.
- Migrations are never edited after being committed — a schema change is
  always a new, higher-numbered migration.

| File | Adds |
|---|---|
| `0001_evidence_foundation.sql` | `projects`, `sources`, `source_artifacts`, `source_contents`, `source_chunks`, `sync_runs`, `sync_items` |
| `0002_project_audit.sql` | `audit_entries` (minimal project lifecycle audit trail) |
| `0003_evidence_manual_fields.sql` | `source_artifacts.source_type`, `source_contents.original_filename` |
| `0004_source_chunks_fts.sql` | `source_chunks_fts` (FTS5) and sync triggers |
| `0005_ledger_and_review.sql` | `people`, `person_aliases`, `project_people`, `observations`, `ledger_items`, `ledger_versions`, `evidence_links`, `proposed_mutations`, `reviews`, `corrections` |
| `0006_ledger_fts.sql` | `ledger_items_fts`, `observations_fts` (FTS5) and sync triggers |
| `0007_ledger_supersession_links.sql` | `ledger_items.superseded_by_item_id`, `ledger_items.supersedes_item_id` |
