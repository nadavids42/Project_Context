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
