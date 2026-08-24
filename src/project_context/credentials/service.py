"""Credential service: the one locked entry point through which a
`sources` row's credential material is connected, refreshed, read,
masked, or disconnected (Section 16: "Refresh through one locked
credential service to prevent concurrent token rotation races").

This module never touches a `sources` row's `boundary_json` or a
connector's own HTTP calls — it only manages the credential_ref/secret
relationship and the source's health status as credential state
changes. `project_context.connectors.google_oauth` supplies the actual
Google-specific `refresh_fn`; this service is provider-agnostic.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable

from project_context.credentials.store import CredentialStore
from project_context.db import sources_repository
from project_context.domain.sources import Source, SourceHealthStatus

#: A fixed-width, non-reversible mask — never a prefix/suffix of the
#: real secret (Section 16: "Mask secrets in UI; never echo them after
#: save"). Constant regardless of the underlying secret's length, so
#: the mask itself never leaks length information either.
MASKED_SECRET_DISPLAY = "••••••••"
NOT_CONNECTED_DISPLAY = "Not connected"


class TokenRefreshError(RuntimeError):
    """Raised by a caller-supplied `refresh_fn` when a provider rejects
    a refresh outright (revoked/invalid token) — distinct from a
    transient network failure, which should propagate normally so the
    caller can retry rather than being treated as reauth-required."""


class CredentialService:
    """Connect/refresh/mask/disconnect for one credential store,
    serialized behind a single process-wide lock (Section 16)."""

    def __init__(self, store: CredentialStore) -> None:
        self._store = store
        self._lock = threading.Lock()

    def connect(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        source_id: str,
        *,
        secret: str,
        external_account_id: str | None = None,
    ) -> Source:
        """Store `secret` and point the source at it. `external_account_id`
        is *not* a secret (Section 9: a plain `sources` column) — an
        email address or account label safe to display and log."""
        with self._lock:
            credential_ref = self._store.set_secret(secret)
            sources_repository.update_credential_ref(
                conn, project_id, source_id, credential_ref=credential_ref
            )
            if external_account_id is not None:
                sources_repository.update_external_account_id(
                    conn, project_id, source_id, external_account_id=external_account_id
                )
            return sources_repository.update_health(
                conn, project_id, source_id, health_status=SourceHealthStatus.READY
            )

    def refresh(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        source_id: str,
        *,
        refresh_fn: Callable[[str], str],
    ) -> Source:
        """Refresh the stored secret through `refresh_fn(current_secret)
        -> new_secret`, locked so two concurrent syncs of the same
        source cannot race a token rotation. A `TokenRefreshError` from
        `refresh_fn` moves the source to `reauth_required` without
        raising further — any other exception propagates so a
        transient failure is retried, not mistaken for revocation."""
        with self._lock:
            source = sources_repository.get_source(conn, project_id, source_id)
            if source is None or source.credential_ref is None:
                return sources_repository.update_health(
                    conn,
                    project_id,
                    source_id,
                    health_status=SourceHealthStatus.REAUTH_REQUIRED,
                    last_error_code="auth",
                )
            current_secret = self._store.get_secret(source.credential_ref)
            if current_secret is None:
                return sources_repository.update_health(
                    conn,
                    project_id,
                    source_id,
                    health_status=SourceHealthStatus.REAUTH_REQUIRED,
                    last_error_code="auth",
                )
            try:
                new_secret = refresh_fn(current_secret)
            except TokenRefreshError:
                return sources_repository.update_health(
                    conn,
                    project_id,
                    source_id,
                    health_status=SourceHealthStatus.REAUTH_REQUIRED,
                    last_error_code="auth",
                )
            self._store.update_secret(source.credential_ref, new_secret)
            return sources_repository.update_health(
                conn, project_id, source_id, health_status=SourceHealthStatus.HEALTHY
            )

    def get_secret(self, conn: sqlite3.Connection, project_id: str, source_id: str) -> str | None:
        source = sources_repository.get_source(conn, project_id, source_id)
        if source is None or source.credential_ref is None:
            return None
        return self._store.get_secret(source.credential_ref)

    def disconnect(self, conn: sqlite3.Connection, project_id: str, source_id: str) -> Source:
        """Delete local credential material and disable the source
        (Section 16: "Disconnect deletes local credential material and
        marks the source disabled"). The source row itself is retained
        — an audit-safe record that this connector was once configured
        — but carries no credential reference afterward."""
        with self._lock:
            source = sources_repository.get_source(conn, project_id, source_id)
            if source is not None and source.credential_ref is not None:
                self._store.delete_secret(source.credential_ref)
            sources_repository.update_credential_ref(
                conn, project_id, source_id, credential_ref=None
            )
            sources_repository.set_enabled(conn, project_id, source_id, enabled=False)
            return sources_repository.update_health(
                conn, project_id, source_id, health_status=SourceHealthStatus.DISABLED
            )

    def mark_reauth_required(
        self, conn: sqlite3.Connection, project_id: str, source_id: str, *, error_code: str = "auth"
    ) -> Source:
        """Move one source to `reauth_required` without touching any
        other source (Section 8: "Token expiry should move connector to
        reauth_required without disabling other sources")."""
        return sources_repository.update_health(
            conn,
            project_id,
            source_id,
            health_status=SourceHealthStatus.REAUTH_REQUIRED,
            last_error_code=error_code,
        )


def mask_secret(secret: str | None) -> str:
    """A constant, non-reversible display value — never a substring of
    the real secret, and never varies with the secret's length."""
    return MASKED_SECRET_DISPLAY if secret else NOT_CONNECTED_DISPLAY
