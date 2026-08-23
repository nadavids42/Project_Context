"""The provider-neutral LLM boundary (Section 12.5).

``LLMProvider`` is the one structured-generation contract every adapter
implements. Only one adapter exists in this repository
(``project_context.llm.openai_provider.OpenAIProvider``) — per Section
12.5: "Implement only OpenAIProvider in MVP. No model picker, automatic
fallback, or provider-specific fields outside the adapter." This module
must stay free of any OpenAI-specific import so a future Anthropic (or
other) adapter is a matter of implementing the same protocol, not
reshaping this module.

Exceptions are grouped by how ``project_context.llm.retry`` and callers
should react to them:

- ``RetryableProviderError`` (and its subclasses) are transient — the
  centralized retry policy in ``project_context.llm.retry`` retries them
  with bounded exponential backoff and jitter (Section 12.6, item 1).
- ``LLMClientError`` is a non-retryable request problem (bad request,
  auth, permission, not-found) — retrying it unchanged would just fail
  again.
- ``LLMRefusalError`` means the model declined; Section 12.6 item 2 is
  explicit that a refusal is recorded with a safe reason and *not*
  repeatedly reformulated.
- ``LLMSchemaFailureError`` means structured output failed Pydantic
  validation even after the adapter's one allowed repair attempt (Section
  12.6 item 3).
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from project_context.config import DEFAULT_OPENAI_MODEL

DEFAULT_REASONING_EFFORT = "low"

#: Pinned per Section 12.2 ("$2 per million input tokens and $12 per
#: million output tokens at the time of planning"). Keyed by model so a
#: future model change is one table entry, not a scattered constant.
PRICING_USD_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    DEFAULT_OPENAI_MODEL: (2.0, 12.0),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost from a pinned per-model price table.

    Falls back to the pinned default model's pricing for an unrecognized
    model string rather than raising — cost estimation is diagnostic
    telemetry (Section 12.7), not a request-blocking control, and an
    approximate-but-present figure is more useful than none.
    """
    input_price, output_price = PRICING_USD_PER_MILLION_TOKENS.get(
        model, PRICING_USD_PER_MILLION_TOKENS[DEFAULT_OPENAI_MODEL]
    )
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price


class ModelConfig(BaseModel):
    """Per-call model configuration (Section 12.5).

    A single pinned model/configuration, not a router: callers set
    ``model`` from ``AppConfig.openai_model`` (itself pinned to
    ``gpt-5.6-terra`` by default — Section 12.2), never from end-user
    choice in MVP.
    """

    model_config = ConfigDict(frozen=True)

    model: str = DEFAULT_OPENAI_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    #: Section 12.10 / 16: stateless Responses calls only.
    store: bool = False
    max_output_tokens: int | None = None


class StructuredResult(BaseModel):
    """One provider call's structured output plus telemetry (Section 12.5)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    parsed: BaseModel
    provider: str
    model: str
    request_id: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    estimated_cost_usd: float


class LLMProvider(Protocol):
    """The one structured-generation contract every adapter implements."""

    def generate_structured(
        self,
        *,
        task: str,
        system: str,
        input_text: str,
        response_model: type[BaseModel],
        config: ModelConfig,
    ) -> StructuredResult: ...


class LLMProviderError(Exception):
    """Base class for every error this boundary raises."""


class RetryableProviderError(LLMProviderError):
    """A transient provider failure eligible for centralized retry.

    ``retry_after_s``, when the provider supplied one (e.g. a rate-limit
    response's ``Retry-After`` header), takes priority over the computed
    backoff delay (Section 8: "obey Retry-After").
    """

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class LLMTimeoutError(RetryableProviderError):
    """Network timeout."""


class LLMConnectionError(RetryableProviderError):
    """A non-timeout connection failure (DNS, TLS, reset, ...)."""


class LLMRateLimitError(RetryableProviderError):
    """HTTP 429."""


class LLMServerError(RetryableProviderError):
    """HTTP 5xx."""


class LLMClientError(LLMProviderError):
    """A non-retryable 4xx (bad request, auth, permission, not found)."""


class LLMRefusalError(LLMProviderError):
    """The model declined to respond. Not retried (Section 12.6, item 2)."""


class LLMSchemaFailureError(LLMProviderError):
    """Structured output failed validation after one repair attempt.

    ``validation_errors`` holds the safe, structured Pydantic error list
    (``ValidationError.errors(include_url=False)``) — field paths and
    short messages, never the raw model response or source text.
    """

    def __init__(self, message: str, *, validation_errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.validation_errors = validation_errors or []
