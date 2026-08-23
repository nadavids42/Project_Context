"""Typed connector errors (Section 8: "Classify errors: auth, permission,
rate_limit, not_found, parse, schema, provider, database, assignment").

Every connector — today only Drive — raises one of these instead of a
raw HTTP/library exception, so `project_context.services.sync` can
record a safe `error_class`/`safe_error_message` per `sync_items` row
without ever inspecting a raw response body, header, or traceback
(Section 16: "Exception reporting must redact headers, query strings,
message bodies, evidence quotes, and provider payloads").
"""

from __future__ import annotations

from project_context.domain.sync import SyncErrorClass


class ConnectorError(RuntimeError):
    """Base class for every connector error. `error_class` is always one
    of the `sync_items.error_class` enum values; `safe_message` is
    already redacted and fit to store/log/display as-is."""

    error_class: SyncErrorClass = SyncErrorClass.PROVIDER

    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message


class ConnectorAuthError(ConnectorError):
    """401 — the credential is missing, expired, or revoked."""

    error_class = SyncErrorClass.AUTH


class ConnectorPermissionError(ConnectorError):
    """403 — the credential is valid but lacks access to this resource."""

    error_class = SyncErrorClass.PERMISSION


class ConnectorNotFoundError(ConnectorError):
    """404, or a resource otherwise confirmed gone/inaccessible."""

    error_class = SyncErrorClass.NOT_FOUND


class ConnectorRateLimitError(ConnectorError):
    """429. Carries `retry_after_seconds` when the provider supplied a
    `Retry-After` header, so the retry wrapper can honor it exactly
    (Section 11.2: "obey Retry-After")."""

    error_class = SyncErrorClass.RATE_LIMIT

    def __init__(self, safe_message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(safe_message)
        self.retry_after_seconds = retry_after_seconds


class ConnectorServerError(ConnectorError):
    """5xx — transient, retryable."""

    error_class = SyncErrorClass.PROVIDER


class ConnectorConfigError(ConnectorError):
    """The connector cannot even attempt a call: missing/invalid
    boundary or configuration (FR-002: "Sync refuses enabled connectors
    without a boundary"). Raised before any HTTP call and, in normal
    orchestration, before any `sync_run`/`sync_items` row is even
    created — `SyncErrorClass.SCHEMA` is used only if a caller chooses
    to record one anyway. Also raised for a provider-rejected malformed
    query (Gmail's 400 response to an invalid `q` — Section 11.3:
    "Malformed query shown to user")."""

    error_class = SyncErrorClass.SCHEMA


class ConnectorParseError(ConnectorError):
    """One artifact's content could not be decoded (Section 11.3:
    "multipart decode failures isolated" — e.g. a Gmail message whose
    MIME parts don't decode as declared). Raised per-item, from
    `Connector.fetch()`, so the sync loop records exactly one failed
    `sync_items` row rather than aborting the run. `safe_message` never
    contains the undecodable bytes/text itself (Section 16)."""

    error_class = SyncErrorClass.PARSE
