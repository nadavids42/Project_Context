"""Retry classification and bounded backoff (Section 12.6, item 1;
Section 15: "Retry classification without sleeping for real in tests").
No network, no real ``time.sleep`` — ``sleep`` is a plain recording
callable and ``rng`` is a seeded ``random.Random`` for deterministic
jitter."""

from __future__ import annotations

import random

import pytest

from project_context.llm.provider import (
    LLMClientError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMServerError,
    LLMTimeoutError,
)
from project_context.llm.retry import (
    RetryPolicy,
    call_with_retry,
    classify_exception,
    compute_delay,
)


def test_classify_timeout_is_retryable():
    decision = classify_exception(LLMTimeoutError("timed out"))
    assert decision.retryable is True
    assert decision.retry_after_s is None


def test_classify_rate_limit_is_retryable_and_carries_retry_after():
    decision = classify_exception(LLMRateLimitError("slow down", retry_after_s=3.5))
    assert decision.retryable is True
    assert decision.retry_after_s == 3.5


def test_classify_server_error_is_retryable():
    assert classify_exception(LLMServerError("boom")).retryable is True


def test_classify_client_error_is_not_retryable():
    assert classify_exception(LLMClientError("bad request")).retryable is False


def test_classify_refusal_is_not_retryable():
    assert classify_exception(LLMRefusalError("no")).retryable is False


def test_classify_unknown_exception_is_not_retryable():
    assert classify_exception(ValueError("whatever")).retryable is False


def test_compute_delay_is_bounded_by_max_delay():
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=2.0, jitter_s=0.0)
    rng = random.Random(0)
    # attempt 5 would be 1 * 2**4 = 16s uncapped; must clamp to max_delay_s.
    assert compute_delay(5, policy, rng=rng) == 2.0


def test_compute_delay_grows_exponentially_before_the_cap():
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=100.0, jitter_s=0.0)
    rng = random.Random(0)
    assert compute_delay(1, policy, rng=rng) == 1.0
    assert compute_delay(2, policy, rng=rng) == 2.0
    assert compute_delay(3, policy, rng=rng) == 4.0


def test_compute_delay_prefers_retry_after_over_backoff():
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=100.0, jitter_s=0.0)
    rng = random.Random(0)
    assert compute_delay(3, policy, rng=rng, retry_after_s=0.75) == 0.75


def test_compute_delay_still_caps_retry_after():
    policy = RetryPolicy(max_delay_s=2.0)
    rng = random.Random(0)
    assert compute_delay(1, policy, rng=rng, retry_after_s=30.0) == 2.0


def test_compute_delay_adds_jitter_within_bounds():
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=100.0, jitter_s=0.25)
    rng = random.Random(42)
    delay = compute_delay(1, policy, rng=rng)
    assert 1.0 <= delay <= 1.25


def test_call_with_retry_succeeds_without_retry_on_first_try():
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    result = call_with_retry(fn, sleep=lambda s: (_ for _ in ()).throw(AssertionError("slept")))
    assert result == "ok"
    assert len(calls) == 1


def test_call_with_retry_retries_transient_errors_then_succeeds():
    attempts = []
    sleeps = []

    def fn():
        attempts.append(1)
        if len(attempts) < 3:
            raise LLMServerError("server hiccup")
        return "recovered"

    result = call_with_retry(
        fn,
        policy=RetryPolicy(max_attempts=3, base_delay_s=0.01, max_delay_s=0.02, jitter_s=0.0),
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    assert result == "recovered"
    assert len(attempts) == 3
    assert len(sleeps) == 2  # slept between attempt 1->2 and 2->3, never for real


def test_call_with_retry_raises_after_exhausting_attempts():
    attempts = []
    sleeps = []

    def fn():
        attempts.append(1)
        raise LLMTimeoutError("still timing out")

    with pytest.raises(LLMTimeoutError):
        call_with_retry(
            fn,
            policy=RetryPolicy(max_attempts=3, base_delay_s=0.01, max_delay_s=0.02, jitter_s=0.0),
            sleep=sleeps.append,
            rng=random.Random(0),
        )
    assert len(attempts) == 3
    assert len(sleeps) == 2


def test_call_with_retry_does_not_retry_non_retryable_errors():
    attempts = []

    def fn():
        attempts.append(1)
        raise LLMClientError("bad request")

    with pytest.raises(LLMClientError):
        call_with_retry(
            fn,
            sleep=lambda s: (_ for _ in ()).throw(AssertionError("must not sleep/retry")),
        )
    assert len(attempts) == 1


def test_call_with_retry_obeys_retry_after_over_backoff():
    delays = []

    def fn():
        if len(delays) == 0:
            raise LLMRateLimitError("slow down", retry_after_s=0.05)
        return "ok"

    call_with_retry(
        fn,
        policy=RetryPolicy(max_attempts=2, base_delay_s=5.0, max_delay_s=10.0, jitter_s=0.0),
        sleep=delays.append,
        rng=random.Random(0),
    )
    assert delays == [0.05]
