"""A thin, injectable HTTP transport plus a bounded-retry wrapper shared
by every connector (Section 8: "Retry network 408/429/5xx with bounded
exponential backoff and jitter; obey Retry-After"; Section 16: redact
headers/query strings from logs).

`HttpTransport` is a Protocol so tests substitute a fake instead of
mocking `requests` internals — the same "protocol + fake" pattern
already used for `project_context.llm.provider.LLMProvider`.
`request_with_retry` never logs a header, query string, or response
body; it logs only method, status code, attempt number, and the
request's host.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from project_context.connectors.errors import (
    ConnectorAuthError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
    ConnectorRateLimitError,
    ConnectorServerError,
)
from project_context.observability import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY_SECONDS = 0.5
DEFAULT_MAX_DELAY_SECONDS = 20.0
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Status codes the retry wrapper treats as transient (Section 8: "Retry
#: network 408/429/5xx"). 429 and 5xx get their own explicit branches
#: below (429 honors Retry-After specifically); 408 falls through the
#: generic transient path.
_TRANSIENT_STATUS_CODES = frozenset({408})


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes

    def json(self) -> Any:
        import json

        return json.loads(self.content.decode("utf-8"))


class HttpTransport(Protocol):
    """One HTTP call, no retry logic — `request_with_retry` supplies
    that. A fake test transport only needs to implement this."""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse: ...


class RequestsHttpTransport:
    """The real transport, backed by `requests`. Never constructed by
    any test — see `project_context.connectors.http.HttpTransport`."""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        import requests

        response = requests.request(method, url, params=params, headers=headers, timeout=timeout)
        return HttpResponse(
            status_code=response.status_code, headers=response.headers, content=response.content
        )


def _parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` is either an integer/float seconds count or an
    HTTP-date; only the seconds form is handled — an HTTP-date falls
    back to the ordinary backoff schedule rather than failing the
    request over an unparsed header."""
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _backoff_delay(attempt: int, *, rand: Callable[[], float]) -> float:
    """Exponential backoff with full jitter, capped at
    `DEFAULT_MAX_DELAY_SECONDS` — `attempt` is 1-indexed."""
    exponential = DEFAULT_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
    capped = min(exponential, DEFAULT_MAX_DELAY_SECONDS)
    return capped * rand()


def _host_of(url: str) -> str:
    return urlsplit(url).netloc


def request_with_retry(
    transport: HttpTransport,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> HttpResponse:
    """One logical call, retried transparently on 408/429/5xx up to
    `max_attempts` total attempts. Raises a typed `ConnectorError` for
    401/403/404 immediately (never retried) and for 429/5xx once
    attempts are exhausted. Never logs `headers`, `params`, or response
    content — only method, host, status, and attempt number.
    """
    host = _host_of(url)
    attempt = 0
    while True:
        attempt += 1
        response = transport.request(method, url, params=params, headers=headers, timeout=timeout)
        logger.info(
            "connector_http_request",
            extra={
                "method": method,
                "host": host,
                "status_code": response.status_code,
                "attempt": attempt,
            },
        )

        if response.status_code == 401:
            raise ConnectorAuthError("authentication failed (401)")
        if response.status_code == 403:
            raise ConnectorPermissionError("permission denied (403)")
        if response.status_code == 404:
            raise ConnectorNotFoundError("resource not found (404)")

        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            if attempt >= max_attempts:
                raise ConnectorRateLimitError(
                    "rate limited (429): attempts exhausted", retry_after_seconds=retry_after
                )
            sleep(retry_after if retry_after is not None else _backoff_delay(attempt, rand=rand))
            continue

        if response.status_code in _TRANSIENT_STATUS_CODES or 500 <= response.status_code < 600:
            if attempt >= max_attempts:
                raise ConnectorServerError(
                    f"transient server error ({response.status_code}): attempts exhausted"
                )
            sleep(_backoff_delay(attempt, rand=rand))
            continue

        return response
