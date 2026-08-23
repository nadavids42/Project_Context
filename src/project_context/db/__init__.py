"""SQLite connection, migrations, and health check.

Implemented so far: `connection` (pragmas, row mapping, explicit
transactions), `migrations` (numbered SQL migration runner), and `health`
(status for the Streamlit panel). Repositories and FTS5 access are not
yet implemented — see docs/Project_Context_Product_Plan_v1.md Section 8.
"""
