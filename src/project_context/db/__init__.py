"""SQLite connection, migrations, health check, and repositories.

Implemented so far: `connection` (pragmas, row mapping, explicit
transactions), `migrations` (numbered SQL migration runner), `health`
(status for the Streamlit panel), `projects_repository`, and
`audit_repository`. FTS5 access and the remaining evidence/ledger
repositories are not yet implemented — see
docs/Project_Context_Product_Plan_v1.md Section 8.
"""
