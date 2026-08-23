"""Tests for `project_context.connectors.http.request_with_retry`:
bounded exponential backoff, `Retry-After` handling, typed errors, and
no header/body logging (Section 8; Section 16; Prompt 10)."""

from __future__ import annotations

import logging

import pytest

from project_context.connectors.errors import (
    ConnectorAuthError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
    ConnectorRateLimitError,
    ConnectorServerError,
)
from project_context.connectors.http import HttpResponse, request_with_retry


class FakeTransport:
    """Returns queued `HttpResponse`s in order; records every call's
    method/url/params/headers so tests can assert on request shape
    without touching the network."""

    def __init__(self, responses: list[HttpResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, *, params=None, headers=None, timeout=30.0):
        self.calls.append(
            {"method": method, "url": url, "params": params, "headers": headers}
        )
        if not self.responses:
            raise AssertionError("FakeTransport called with no queued response")
        return self.responses.pop(0)


def _resp(status_code, *, headers=None, content=b"{}"):
    return HttpResponse(status_code=status_code, headers=headers or {}, content=content)


def _no_sleep(_seconds):
    pass


def test_successful_first_attempt_returns_response_without_sleeping(monkeypatch):
    transport = FakeTransport([_resp(200)])
    slept = []
    response = request_with_retry(
        transport, "GET", "https://www.googleapis.com/drive/v3/files", sleep=slept.append
    )
    assert response.status_code == 200
    assert slept == []
    assert len(transport.calls) == 1


def test_401_raises_auth_error_without_retrying():
    transport = FakeTransport([_resp(401)])
    with pytest.raises(ConnectorAuthError):
        request_with_retry(transport, "GET", "https://example.com/x", sleep=_no_sleep)
    assert len(transport.calls) == 1


def test_403_raises_permission_error_without_retrying():
    transport = FakeTransport([_resp(403)])
    with pytest.raises(ConnectorPermissionError):
        request_with_retry(transport, "GET", "https://example.com/x", sleep=_no_sleep)
    assert len(transport.calls) == 1


def test_404_raises_not_found_error_without_retrying():
    transport = FakeTransport([_resp(404)])
    with pytest.raises(ConnectorNotFoundError):
        request_with_retry(transport, "GET", "https://example.com/x", sleep=_no_sleep)
    assert len(transport.calls) == 1


def test_429_with_retry_after_sleeps_the_exact_header_value_then_succeeds():
    transport = FakeTransport([_resp(429, headers={"Retry-After": "3"}), _resp(200)])
    slept = []
    response = request_with_retry(
        transport, "GET", "https://example.com/x", sleep=slept.append
    )
    assert response.status_code == 200
    assert slept == [3.0]


def test_429_without_retry_after_uses_bounded_exponential_backoff():
    transport = FakeTransport([_resp(429), _resp(429), _resp(200)])
    slept = []
    response = request_with_retry(
        transport, "GET", "https://example.com/x", sleep=slept.append, rand=lambda: 1.0
    )
    assert response.status_code == 200
    assert slept == [0.5, 1.0]  # base 0.5s, doubling, full jitter fixed at 1.0


def test_429_exhausting_attempts_raises_rate_limit_error_with_retry_after():
    transport = FakeTransport([_resp(429, headers={"Retry-After": "7"})] * 3)
    with pytest.raises(ConnectorRateLimitError) as exc_info:
        request_with_retry(
            transport, "GET", "https://example.com/x", sleep=_no_sleep, max_attempts=3
        )
    assert exc_info.value.retry_after_seconds == 7.0
    assert len(transport.calls) == 3


def test_5xx_is_retried_then_succeeds():
    transport = FakeTransport([_resp(503), _resp(500), _resp(200)])
    slept = []
    response = request_with_retry(
        transport, "GET", "https://example.com/x", sleep=slept.append, rand=lambda: 0.0
    )
    assert response.status_code == 200
    assert len(slept) == 2


def test_5xx_exhausting_attempts_raises_server_error():
    transport = FakeTransport([_resp(500)] * 2)
    with pytest.raises(ConnectorServerError):
        request_with_retry(
            transport, "GET", "https://example.com/x", sleep=_no_sleep, max_attempts=2
        )


def test_408_is_treated_as_transient_and_retried():
    transport = FakeTransport([_resp(408), _resp(200)])
    response = request_with_retry(
        transport, "GET", "https://example.com/x", sleep=_no_sleep
    )
    assert response.status_code == 200


def test_retry_never_logs_the_authorization_header_value(caplog):
    secret_token = "FAKE-TEST-ACCESS-TOKEN-do-not-log-this-value"
    transport = FakeTransport([_resp(200)])
    with caplog.at_level(logging.DEBUG):
        request_with_retry(
            transport, "GET", "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": f"Bearer {secret_token}"}, sleep=_no_sleep,
        )
    for record in caplog.records:
        assert secret_token not in record.getMessage()
        assert secret_token not in str(record.__dict__)
