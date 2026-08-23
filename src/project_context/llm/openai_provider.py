"""The one OpenAI adapter (Section 12.5).

Uses the installed OpenAI Python SDK's Responses API structured-output
mechanism: ``client.responses.parse(..., text_format=<pydantic model>)``,
which builds a strict JSON Schema from the given Pydantic model and
validates the response against it (verified directly against the
installed SDK — see the "SDK verification" note at the bottom of this
docstring). Every call is stateless: ``store`` comes from ``ModelConfig``
and defaults to ``False`` (Section 12.10).

Two layers of failure handling, kept deliberately separate:

1. **Transport retry** (Section 12.6, item 1): timeouts, 429s, and 5xx
   responses are mapped onto ``project_context.llm.provider``'s
   provider-agnostic exceptions and handed to
   ``project_context.llm.retry.call_with_retry`` for bounded exponential
   backoff with jitter. This happens *inside* each individual API call
   below, so a transient network blip never consumes the schema-repair
   budget.
2. **Schema repair** (Section 12.6, item 3): ``client.responses.parse``
   runs the returned JSON through the given Pydantic model itself
   (including cross-field ``model_validator``s like
   ``ExtractionBatch.empty_flag_consistent``, which strict JSON Schema
   cannot express), so a structurally-schema-valid-but-business-rule-
   invalid response surfaces as a ``pydantic.ValidationError`` raised by
   the call itself. That is treated as a schema failure distinct from a
   transport error: at most one repair call is issued, containing only
   the validation errors (not the original chunk again, which is already
   implicit in the unchanged ``input_text``) — never a repeated
   reformulation loop.

SDK verification: this module was written and tested against the
installed ``openai`` package (see ``pyproject.toml``), inspecting
``openai/resources/responses/responses.py`` directly. That version's
``Responses.parse()`` accepts ``text_format=<BaseModel subclass>``,
builds the ``strict: true`` JSON Schema conversion via its own
``openai.lib._parsing`` machinery, and exposes ``ParsedResponse.output_parsed``
plus ``Response.usage`` (``input_tokens``/``output_tokens``) and
``Response.id`` — exactly what ``StructuredResult`` needs. No custom JSON
parser was written; this adapter only calls the SDK's own documented
entry point.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import openai
import pydantic
from pydantic import BaseModel

from project_context.llm.provider import (
    LLMClientError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMSchemaFailureError,
    LLMServerError,
    LLMTimeoutError,
    ModelConfig,
    StructuredResult,
    estimate_cost_usd,
)
from project_context.llm.retry import RetryPolicy, call_with_retry
from project_context.observability import get_logger

logger = get_logger(__name__)

_REFUSAL_CONTENT_TYPE = "refusal"


def _retry_after_seconds(response: Any) -> float | None:
    if response is None:
        return None
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def map_transport_exception(exc: Exception) -> Exception:
    """Map an ``openai`` SDK transport exception onto
    ``project_context.llm.provider``'s provider-agnostic hierarchy.

    Exposed as a module-level function (rather than nested) so retry
    classification can be exercised in tests with plain, network-free,
    directly-constructed ``openai`` exception instances (Section 15:
    "Retry classification without sleeping for real in tests").
    ``pydantic.ValidationError`` is not handled here — that is a schema
    failure, handled separately by the caller.
    """
    if isinstance(exc, openai.APITimeoutError):
        return LLMTimeoutError("request timed out")
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitError(
            "rate limited",
            retry_after_s=_retry_after_seconds(getattr(exc, "response", None)),
        )
    if isinstance(exc, openai.APIConnectionError):
        return LLMConnectionError("connection error")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return LLMServerError(f"provider server error ({exc.status_code})")
        return LLMClientError(f"provider client error ({exc.status_code})")
    return exc


def _extract_refusal_text(response: Any) -> str | None:
    for output in getattr(response, "output", None) or []:
        if getattr(output, "type", None) != "message":
            continue
        for item in getattr(output, "content", None) or []:
            if getattr(item, "type", None) == _REFUSAL_CONTENT_TYPE:
                return getattr(item, "refusal", None) or "model refused to respond"
    return None


class OpenAIProvider:
    """The one ``LLMProvider`` implementation in this repository.

    Stateless across calls (no conversation/session id is ever sent —
    every call sets ``store`` from ``ModelConfig``, default ``False``).
    The SDK's own retry loop is disabled (``max_retries=0``) because
    retry is centralized in ``project_context.llm.retry`` — letting both
    retry independently would double backoff delays unpredictably.
    """

    def __init__(
        self,
        *,
        api_key: str,
        retry_policy: RetryPolicy | None = None,
        client: openai.OpenAI | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._retry_policy = retry_policy or RetryPolicy()
        self._client = client or openai.OpenAI(api_key=api_key, max_retries=0)
        # Injectable so tests exercise real retry-loop behavior (attempt
        # counts, backoff-vs-Retry-After choice) without a real wall-clock
        # sleep (Section 15: "Retry classification without sleeping for
        # real in tests"); production use keeps the real `time.sleep`.
        self._sleep = sleep
        self._rng = rng or random.Random()

    def generate_structured(
        self,
        *,
        task: str,
        system: str,
        input_text: str,
        response_model: type[BaseModel],
        config: ModelConfig,
    ) -> StructuredResult:
        start = time.monotonic()
        try:
            response = call_with_retry(
                lambda: self._call_once(system, input_text, response_model, config),
                policy=self._retry_policy,
                sleep=self._sleep,
                rng=self._rng,
            )
        except pydantic.ValidationError as first_error:
            response = self._repair(task, system, input_text, response_model, config, first_error)
        latency_ms = int((time.monotonic() - start) * 1000)

        if response.output_parsed is None:
            incomplete = getattr(response, "incomplete_details", None)
            if incomplete is not None:
                reason = getattr(incomplete, "reason", None) or "incomplete"
                message = f"model response incomplete: {reason}"
            else:
                message = _extract_refusal_text(response) or (
                    "model returned no parsable structured output"
                )
            logger.info(
                "llm_no_structured_output",
                extra={"task": task, "model": config.model, "latency_ms": latency_ms},
            )
            raise LLMRefusalError(message)

        usage = response.usage
        input_tokens = usage.input_tokens if usage else 0
        output_tokens = usage.output_tokens if usage else 0
        result = StructuredResult(
            parsed=response.output_parsed,
            provider="openai",
            model=response.model or config.model,
            request_id=getattr(response, "id", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            estimated_cost_usd=estimate_cost_usd(config.model, input_tokens, output_tokens),
        )
        logger.info(
            "llm_call_completed",
            extra={
                "task": task,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "latency_ms": result.latency_ms,
                "estimated_cost_usd": result.estimated_cost_usd,
            },
        )
        return result

    def _call_once(
        self,
        system: str,
        input_text: str,
        response_model: type[BaseModel],
        config: ModelConfig,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": config.model,
            "instructions": system,
            "input": input_text,
            "text_format": response_model,
            "reasoning": {"effort": config.reasoning_effort},
            "store": config.store,
        }
        if config.max_output_tokens is not None:
            kwargs["max_output_tokens"] = config.max_output_tokens
        try:
            return self._client.responses.parse(**kwargs)
        except pydantic.ValidationError:
            raise
        except Exception as exc:
            raise map_transport_exception(exc) from exc

    def _repair(
        self,
        task: str,
        system: str,
        input_text: str,
        response_model: type[BaseModel],
        config: ModelConfig,
        first_error: pydantic.ValidationError,
    ) -> Any:
        """One structured repair attempt for schema failure (Section 12.6,
        item 3). Sends only the validation errors, not additional project
        data — the schema, instructions, and the original single chunk are
        already present in `system`/`input_text`, unchanged."""
        error_summary = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in first_error.errors(include_url=False)
        )
        repair_system = (
            f"{system}\n\n"
            "## Repair\n\n"
            "Your previous structured response for this exact input failed "
            f"schema validation with these errors:\n{error_summary}\n\n"
            "Return a corrected response that satisfies the schema and every "
            "rule above."
        )
        logger.info("llm_schema_repair_attempted", extra={"task": task, "model": config.model})
        try:
            return call_with_retry(
                lambda: self._call_once(repair_system, input_text, response_model, config),
                policy=self._retry_policy,
                sleep=self._sleep,
                rng=self._rng,
            )
        except pydantic.ValidationError as second_error:
            logger.info("llm_schema_repair_failed", extra={"task": task, "model": config.model})
            raise LLMSchemaFailureError(
                "structured output failed validation after one repair attempt",
                validation_errors=second_error.errors(include_url=False),
            ) from second_error
