This directory is intentionally empty of test files — a known,
tracked gap, not a stale placeholder.

Extraction prompts exist (`prompts/extraction_v1.md`) and are covered
piecemeal: `tests/unit/test_llm_prompts.py` (system-prompt framing,
injection-boundary construction), `tests/unit/test_extraction_service.py`
(span/owner/date validation, prompt-injection-in-evidence-text handling,
empty-material-source, provider-refusal/error handling — using
`FakeLLMProvider`, one case per test rather than a table). What Section
15 specifically asks for and this codebase does not yet have is a
single **frozen, versioned corpus of 20-30 compact cases** — one
source-chunk input and one expected structured-output pair per case,
covering: positive explicit item; negative discussion-without-decision;
future intent vs. completed action; tentative vs. final date; assistant/
source instruction injection; two people with similar names; stale
document vs. newer meeting; source with no material project state —
run against stored expected/mock responses in CI, with a separate,
explicitly paid, live-model command reporting metric deltas without
failing on exact wording (Section 15: "CI can run against stored
expected/mock responses... does not fail solely on exact wording").

Building that frozen corpus is real design work (choosing and writing
20-30 representative cases, deciding what "matches" means well enough
not to be wording-brittle) — deliberately left as a named, tracked P1
item for a future iteration rather than done partially or hastily here.
See `docs/RELEASE_NOTES.md`.
