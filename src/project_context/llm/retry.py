"""Centralized retry classification and bounded exponential backoff.

Deliberately provider-agnostic — this module has no knowledge of the
OpenAI SDK or its exception types. An adapter (e.g.
``project_context.llm.openai_provider``) is responsible for mapping its
own transport exceptions onto ``project_context.llm.provider``'s small
exception hierarchy *before* calling ``call_with_retry``; this module
only ever reacts to that hierarchy. That split is what makes retry
classification testable with plain synthetic exceptions and no network
access (Section 15: "Tests must use a fake/mock provider and never call
the network").

Bounded exponential backoff with jitter (Section 12.6, item 1): delay
doubles each attempt, capped at ``max_delay_s``, plus a small random
jitter so concurrent callers do not retry in lockstep. A provider-supplied
``retry_after_s`` (e.g. a 429's ``Retry-After`` header) overrides the
computed delay, still capped at ``max_delay_s``.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from project_context.llm.provider import RetryableProviderError

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_S = 0.5
DEFAULT_MAX_DELAY_S = 8.0
DEFAULT_JITTER_S = 0.25


@dataclass(frozen=True)
class RetryPolicy:
    """Bounds for the retry loop. ``max_attempts`` counts the initial
    attempt, so ``max_attempts=3`` means up to two retries."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay_s: float = DEFAULT_BASE_DELAY_S
    max_delay_s: float = DEFAULT_MAX_DELAY_S
    jitter_s: float = DEFAULT_JITTER_S


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    retry_after_s: float | None = None


def classify_exception(exc: Exception) -> RetryDecision:
    """Default classifier: retryable iff it's a ``RetryableProviderError``
    (timeout, rate limit, server error — see ``project_context.llm.provider``).
    Everything else (client errors, refusals, schema failures, unknown
    exceptions) is treated as non-retryable."""
    if isinstance(exc, RetryableProviderError):
        return RetryDecision(retryable=True, retry_after_s=exc.retry_after_s)
    return RetryDecision(retryable=False)


def compute_delay(
    attempt: int,
    policy: RetryPolicy,
    *,
    rng: random.Random,
    retry_after_s: float | None = None,
) -> float:
    """Delay before the *next* attempt, given this was attempt number
    ``attempt`` (1-indexed) that just failed."""
    if retry_after_s is not None:
        return max(0.0, min(retry_after_s, policy.max_delay_s))
    exponential = policy.base_delay_s * (2 ** (attempt - 1))
    capped = min(exponential, policy.max_delay_s)
    jitter = rng.uniform(0.0, policy.jitter_s) if policy.jitter_s > 0 else 0.0
    return capped + jitter


_DEFAULT_RETRY_POLICY = RetryPolicy()


def call_with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy = _DEFAULT_RETRY_POLICY,
    classify: Callable[[Exception], RetryDecision] = classify_exception,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """Call ``fn()``, retrying transient failures per ``policy``.

    ``sleep`` and ``rng`` are injectable so tests can assert on retry
    counts and computed delays without a real network call or a real
    wall-clock sleep (Section 15: "Retry classification without sleeping
    for real in tests").
    """
    rng = rng if rng is not None else random.Random()
    attempt = 1
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - reraised below when not retryable
            decision = classify(exc)
            if not decision.retryable or attempt >= policy.max_attempts:
                raise
            delay = compute_delay(attempt, policy, rng=rng, retry_after_s=decision.retry_after_s)
            sleep(delay)
            attempt += 1
