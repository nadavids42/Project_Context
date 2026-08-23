"""Local credential storage: OS keyring first, an explicit encrypted-file
fallback second, never plaintext (Section 16; FR-032; Prompt 10).

Nothing in this package writes to SQLite — `project_context.db.
sources_repository` stores only the opaque `credential_ref` strings this
package hands back. See `project_context.credentials.store` for the
storage primitive and `project_context.credentials.service` for the
connect/refresh/mask/disconnect API the rest of the application uses.
"""
