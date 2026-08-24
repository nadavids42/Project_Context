-- 0010_calendar_match_reason.sql
--
-- Two additive, nullable columns on `source_artifacts` for the
-- Calendar connector's deterministic assignment rules (Section 11.4;
-- Prompt 12: "Store the exact match reason and score/rule outcome").
--
-- Drive/Gmail assignment is self-evident from their boundary alone (a
-- folder ID or a label/query either matches or the item was never
-- discovered at all) so neither needed this. Calendar assignment is a
-- deterministic *rule evaluation* over one shared calendar — which of
-- four priority tiers matched, and why — so, unlike Drive/Gmail, that
-- explanation is worth keeping as its own queryable field rather than
-- only inside the stored evidence text:
--
--   * match_rule — which priority tier matched: 'event_id' (an
--     explicitly included event ID), 'project_name_term' (title/
--     description contains a configured project term), 'domain_participant'
--     (attendee/organizer email domain or explicit participant match),
--     or 'include_rule' (a configured include term/regex). NULL for
--     every non-Calendar artifact.
--   * match_reason — the human-readable explanation for that tier's
--     match (e.g. which term/domain/ID matched). NULL alongside
--     match_rule.
--
-- Both are nullable so every existing Drive/Gmail artifact row (and
-- any future connector that has no equivalent concept) is unaffected.

ALTER TABLE source_artifacts ADD COLUMN match_rule TEXT
    CHECK (match_rule IS NULL OR match_rule IN (
        'event_id', 'project_name_term', 'domain_participant', 'include_rule'
    ));

ALTER TABLE source_artifacts ADD COLUMN match_reason TEXT;
