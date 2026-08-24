This directory is intentionally empty of test files.

The end-to-end "service/repository tests against a real test SQLite
database and mocked providers" Section 15 describes are real and
extensive — they just live alongside the unit tests they build on,
under `tests/unit/` (every `test_*_service.py` file there runs its
full flow against a real, migrated, temporary SQLite database — see
e.g. `test_review_service.py`'s module docstring: "end to end against
real SQLite"), plus `tests/golden_projects/test_manual_vertical_slice.py`
for the complete project -> ingest -> extract -> reconcile -> review ->
brief walkthrough Section 15's test pyramid calls "End-to-end." A
separate `tests/integration/` split was considered and rejected: this
codebase's unit/service tests already use a real (temporary) database
rather than mocks for anything below the LLM/connector boundary, so a
second directory drawing an "integration" line around the same tests
would only fragment discovery, not add coverage.
