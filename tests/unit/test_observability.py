"""Tests for redacted structured logging."""

from __future__ import annotations

import io
import json
import logging

import pytest

from project_context.observability import (
    REDACTED_VALUE,
    JSONFormatter,
    RedactionFilter,
    is_sensitive_key,
    redact,
)


@pytest.fixture
def captured_log():
    """A logger wired to a JSON formatter + redaction filter, writing to a buffer."""
    stream = io.StringIO()
    logger = logging.getLogger("project_context.tests.redaction")
    logger.handlers.clear()
    logger.filters.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)

    yield logger, stream

    logger.handlers.clear()
    logger.filters.clear()


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "API_KEY",
        "openai_api_key",
        "auth_token",
        "access_token",
        "authorization",
        "Authorization",
        "client_secret",
        "credential_ref",
        "google_credential",
    ],
)
def test_is_sensitive_key_matches_expected_field_names(key):
    assert is_sensitive_key(key) is True


@pytest.mark.parametrize("key", ["project_id_hash", "artifact_count", "status", "email"])
def test_is_sensitive_key_ignores_ordinary_field_names(key):
    assert is_sensitive_key(key) is False


def test_redaction_scrubs_sensitive_field_values_from_captured_logs(captured_log):
    logger, stream = captured_log
    secret_values = {
        "api_key": "sk-live-super-secret-value",
        "auth_token": "ey-jwt-secret-value",
        "authorization": "Bearer secret-value",
        "client_secret": "another-secret-value",
        "credential_ref": "cred-secret-value",
    }

    logger.info("connector_configured", extra=secret_values)

    output = stream.getvalue()
    record = json.loads(output)

    for value in secret_values.values():
        assert value not in output, f"secret-like value leaked into logs: {value!r}"
    for key in secret_values:
        assert record[key] == REDACTED_VALUE


def test_redaction_preserves_non_sensitive_fields(captured_log):
    logger, stream = captured_log

    logger.info("sync_completed", extra={"project_id_hash": "abc123", "artifact_count": 5})

    record = json.loads(stream.getvalue())
    assert record["project_id_hash"] == "abc123"
    assert record["artifact_count"] == 5
    assert record["message"] == "sync_completed"


def test_redaction_handles_nested_dicts_and_lists(captured_log):
    logger, stream = captured_log

    logger.info(
        "outbound_request",
        extra={
            "headers": {
                "Authorization": "Bearer nested-secret",
                "Content-Type": "application/json",
            },
            "accounts": [{"email": "user@example.com", "api_key": "list-nested-secret"}],
        },
    )

    output = stream.getvalue()
    record = json.loads(output)

    assert "nested-secret" not in output
    assert "list-nested-secret" not in output
    assert record["headers"]["Authorization"] == REDACTED_VALUE
    assert record["headers"]["Content-Type"] == "application/json"
    assert record["accounts"][0]["api_key"] == REDACTED_VALUE
    assert record["accounts"][0]["email"] == "user@example.com"


def test_redact_helper_is_pure_and_recursive():
    original = {"token": "shh", "nested": {"secret": "also-shh", "safe": "ok"}}

    result = redact(original)

    assert result == {"token": REDACTED_VALUE, "nested": {"secret": REDACTED_VALUE, "safe": "ok"}}
    # Original input must not be mutated.
    assert original == {"token": "shh", "nested": {"secret": "also-shh", "safe": "ok"}}
