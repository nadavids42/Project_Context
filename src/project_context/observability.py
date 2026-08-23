"""Redacted, structured logging.

Per the product plan (Sections 8 and 16), logs are structured JSON and must
never contain secrets or source text. This module provides:

- :class:`RedactionFilter`, which scrubs the *value* of any structured log
  field whose *name* looks credential-like (contains ``token``, ``secret``,
  ``authorization``, ``api_key``, or ``credential``, case-insensitively) —
  including nested dict/list values passed via ``extra=``.
- :class:`JSONFormatter`, a minimal structured JSON log formatter.
- :func:`configure_logging` / :func:`get_logger`, thin setup helpers.

This module has no knowledge of "source text" as a concept — the guarantee
that source bodies, evidence quotes, and model payloads are never logged is
a discipline enforced by callers (never pass them to a logger), not
something this filter can detect from field names alone.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

#: Substrings (case-insensitive) that mark a structured field name as
#: credential-like and therefore subject to redaction.
SENSITIVE_KEY_MARKERS = ("token", "secret", "authorization", "api_key", "credential")

REDACTED_VALUE = "[REDACTED]"

# Attribute names every stdlib LogRecord carries, computed dynamically so it
# stays correct across Python versions rather than hardcoding a list.
_STANDARD_LOG_RECORD_ATTRS = frozenset(
    logging.LogRecord(
        name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
    ).__dict__
) | {"message", "asctime"}


def is_sensitive_key(key: Any) -> bool:
    """Return True if a structured field name looks credential-like."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def redact(value: Any) -> Any:
    """Recursively replace values of sensitive-named keys within `value`."""
    if isinstance(value, dict):
        return {
            key: REDACTED_VALUE if is_sensitive_key(key) else redact(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


class RedactionFilter(logging.Filter):
    """Scrubs credential-like structured fields before a record is emitted.

    Applies to fields attached via ``logger.info(msg, extra={...})`` and to
    dict-style ``%``-formatting args. It does not, and cannot, detect a
    secret embedded directly in a free-text log message — callers must not
    put secrets or source text in the message itself.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__):
            if key in _STANDARD_LOG_RECORD_ATTRS:
                continue
            value = record.__dict__[key]
            record.__dict__[key] = REDACTED_VALUE if is_sensitive_key(key) else redact(value)

        if isinstance(record.args, dict):
            record.args = redact(record.args)

        return True


class JSONFormatter(logging.Formatter):
    """Renders a LogRecord as a single-line structured JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_ATTRS or key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


def configure_logging(level: str = "INFO", *, stream: TextIO | None = None) -> None:
    """Configure the root logger with redacted structured JSON output.

    Idempotent: replaces any handlers previously installed by this
    function, so it is safe to call again (e.g. on Streamlit re-run).
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(RedactionFilter())

    root.handlers = [handler]


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Configure logging first via `configure_logging`."""
    return logging.getLogger(name)
