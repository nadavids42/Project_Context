-- 0007_ledger_supersession_links.sql
--
-- Item-level bidirectional supersession links (Prompt 8; Section 10.10:
-- "Transition prior item to superseded... Set superseded_by and
-- reciprocal relation").
--
-- Section 9's `ledger_versions.superseded_by_version_id` already links
-- one version to the version that replaced it, but only within the
-- *same* item's own append-only chain (see migrations/0005's
-- `services.ledger.append_ledger_version`, which closes an item's own
-- prior version when a new version of that same item is appended).
-- Supersession is different: it replaces one whole ITEM with a
-- different, newly created item (kind is immutable per item — see
-- project_context.domain.ledger — so "this risk became a blocker" or
-- "this decision was replaced by a new decision" cannot be one item's
-- version history; it is two items, linked to each other).
--
-- These two nullable, symmetric columns are that link, at the
-- granularity the rest of `ledger_items` already operates at (current
-- projection), so the review/ledger-history UI can navigate either
-- direction without joining through `ledger_versions`:
--   * `superseded_by_item_id` on the OLD item points at the NEW item
--     that replaced it (set together with status='superseded').
--   * `supersedes_item_id` on the NEW item points back at the OLD item
--     it replaced.
--
-- The review transaction (project_context.services.review) additionally
-- sets the OLD item's final `ledger_versions` row's own
-- `superseded_by_version_id` to the NEW item's first version id — legal
-- today with no schema change, since that column's FK is not
-- constrained to the same `ledger_item_id`. Together, both a version-
-- level and an item-level trail exist; this migration adds only the
-- item-level half that has no existing column to reuse.

ALTER TABLE ledger_items ADD COLUMN superseded_by_item_id TEXT REFERENCES ledger_items (id);
ALTER TABLE ledger_items ADD COLUMN supersedes_item_id TEXT REFERENCES ledger_items (id);

CREATE INDEX idx_ledger_items_superseded_by ON ledger_items (superseded_by_item_id);
CREATE INDEX idx_ledger_items_supersedes ON ledger_items (supersedes_item_id);
