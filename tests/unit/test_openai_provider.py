"""The OpenAI adapter (Section 12.5, 12.6): exception classification, the
happy path, transient-error retry, refusal handling, and the one-shot
schema-repair flow. Every test injects a fake ``openai``-shaped client
(tests/fixtures/fake_openai_client.py) and a no-op ``sleep`` — no network
call and no real wall-clock sleep (Section 15)."""

from __future__ import annotations

import httpx2
import openai
import pytest
from pydantic import ValidationError

from fixtures.fake_openai_client import (
    FakeOpenAIClient,
    FakeResponsesResource,
    make_refusal_response,
    make_response,
)
from project_context.llm.openai_provider import OpenAIProvider, map_transport_exception
from project_context.llm.provider import (
    LLMClientError,
    LLMConnectionError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMSchemaFailureError,
    LLMServerError,
    LLMTimeoutError,
    ModelConfig,
)
from project_context.llm.retry import RetryPolicy
from project_context.llm.schemas import ExtractionBatch

_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


def _status_error(cls, status_code, *, headers=None):
    response = httpx2.Response(status_code, request=_REQUEST, headers=headers or {})
    return cls(f"status {status_code}", response=response, body=None)


# --- transport exception classification -------------------------------


def test_maps_timeout():
    mapped = map_transport_exception(openai.APITimeoutError(request=_REQUEST))
    assert isinstance(mapped, LLMTimeoutError)


def test_maps_connection_error_that_is_not_a_timeout():
    mapped = map_transport_exception(openai.APIConnectionError(request=_REQUEST))
    assert isinstance(mapped, LLMConnectionError)


def test_maps_rate_limit_with_retry_after_header():
    exc = _status_error(openai.RateLimitError, 429, headers={"retry-after": "7"})
    mapped = map_transport_exception(exc)
    assert isinstance(mapped, LLMRateLimitError)
    assert mapped.retry_after_s == 7.0


def test_maps_rate_limit_without_retry_after_header():
    exc = _status_error(openai.RateLimitError, 429)
    mapped = map_transport_exception(exc)
    assert isinstance(mapped, LLMRateLimitError)
    assert mapped.retry_after_s is None


def test_maps_5xx_to_server_error():
    exc = _status_error(openai.InternalServerError, 503)
    assert isinstance(map_transport_exception(exc), LLMServerError)


def test_maps_4xx_to_client_error():
    exc = _status_error(openai.BadRequestError, 400)
    assert isinstance(map_transport_exception(exc), LLMClientError)


def test_maps_auth_error_to_client_error():
    exc = _status_error(openai.AuthenticationError, 401)
    assert isinstance(map_transport_exception(exc), LLMClientError)


def test_unrecognized_exception_passes_through_unchanged():
    original = ValueError("not an openai error")
    assert map_transport_exception(original) is original


# --- generate_structured: happy path ------------------------------------


def _batch(**overrides):
    payload = {
        "observations": [],
        "source_contains_no_material_updates": True,
    }
    payload.update(overrides)
    return ExtractionBatch.model_validate(payload)


def _provider(client=None, **kwargs):
    client = client or FakeOpenAIClient()
    return OpenAIProvider(
        api_key="sk-test-not-real",
        client=client,
        sleep=lambda s: None,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_s=0.0, max_delay_s=0.0, jitter_s=0.0),
        **kwargs,
    ), client


def test_happy_path_returns_structured_result_with_telemetry():
    batch = _batch()
    client = FakeOpenAIClient(
        responses=FakeResponsesResource(
            queue=[
                make_response(
                    parsed=batch, model="gpt-5.6-terra", input_tokens=200, output_tokens=40
                )
            ]
        )
    )
    provider, client = _provider(client=client)

    result = provider.generate_structured(
        task="extract_observations",
        system="system prompt",
        input_text="user input",
        response_model=ExtractionBatch,
        config=ModelConfig(),
    )

    assert result.parsed is batch
    assert result.provider == "openai"
    assert result.model == "gpt-5.6-terra"
    assert result.request_id == "resp_fake"
    assert result.input_tokens == 200
    assert result.output_tokens == 40
    assert result.estimated_cost_usd > 0
    assert result.latency_ms >= 0

    # Section 12.10: every call must be stateless (`store: false`), and
    # Section 12.2: pinned low reasoning effort.
    sent_kwargs = client.responses.calls[0]
    assert sent_kwargs["store"] is False
    assert sent_kwargs["reasoning"] == {"effort": "low"}
    assert sent_kwargs["text_format"] is ExtractionBatch


def test_generate_structured_retries_transient_error_then_succeeds():
    batch = _batch()
    client = FakeOpenAIClient(
        responses=FakeResponsesResource(
            queue=[
                openai.APITimeoutError(request=_REQUEST),
                make_response(parsed=batch),
            ]
        )
    )
    provider, client = _provider(client=client)

    result = provider.generate_structured(
        task="extract_observations",
        system="s",
        input_text="i",
        response_model=ExtractionBatch,
        config=ModelConfig(),
    )
    assert result.parsed is batch
    assert len(client.responses.calls) == 2


def test_generate_structured_raises_after_exhausting_retries():
    client = FakeOpenAIClient(
        responses=FakeResponsesResource(
            queue=[
                _status_error(openai.InternalServerError, 500),
                _status_error(openai.InternalServerError, 500),
                _status_error(openai.InternalServerError, 500),
            ]
        )
    )
    provider, client = _provider(client=client)

    with pytest.raises(LLMServerError):
        provider.generate_structured(
            task="extract_observations",
            system="s",
            input_text="i",
            response_model=ExtractionBatch,
            config=ModelConfig(),
        )
    assert len(client.responses.calls) == 3


def test_generate_structured_does_not_retry_client_errors():
    client = FakeOpenAIClient(
        responses=FakeResponsesResource(queue=[_status_error(openai.BadRequestError, 400)])
    )
    provider, client = _provider(client=client)

    with pytest.raises(LLMClientError):
        provider.generate_structured(
            task="extract_observations",
            system="s",
            input_text="i",
            response_model=ExtractionBatch,
            config=ModelConfig(),
        )
    assert len(client.responses.calls) == 1


def test_generate_structured_raises_refusal_without_retry():
    client = FakeOpenAIClient(
        responses=FakeResponsesResource(
            queue=[make_refusal_response(refusal_text="policy declined")]
        )
    )
    provider, client = _provider(client=client)

    with pytest.raises(LLMRefusalError, match="policy declined"):
        provider.generate_structured(
            task="extract_observations",
            system="s",
            input_text="i",
            response_model=ExtractionBatch,
            config=ModelConfig(),
        )
    assert len(client.responses.calls) == 1


# --- schema repair (Section 12.6, item 3) --------------------------------


def _validation_error() -> ValidationError:
    try:
        ExtractionBatch.model_validate(
            {"observations": [], "source_contains_no_material_updates": False}
        )
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def test_schema_repair_succeeds_on_the_one_allowed_attempt():
    good_batch = _batch()
    client = FakeOpenAIClient(
        responses=FakeResponsesResource(
            queue=[
                _validation_error(),
                make_response(parsed=good_batch),
            ]
        )
    )
    provider, client = _provider(client=client)

    result = provider.generate_structured(
        task="extract_observations",
        system="s",
        input_text="i",
        response_model=ExtractionBatch,
        config=ModelConfig(),
    )
    assert result.parsed is good_batch
    assert len(client.responses.calls) == 2
    # The repair call's instructions include the validation error detail,
    # not a resend of extra project data.
    repair_instructions = client.responses.calls[1]["instructions"]
    assert "Repair" in repair_instructions


def test_schema_repair_failing_twice_raises_schema_failure_error():
    client = FakeOpenAIClient(
        responses=FakeResponsesResource(queue=[_validation_error(), _validation_error()])
    )
    provider, client = _provider(client=client)

    with pytest.raises(LLMSchemaFailureError) as exc_info:
        provider.generate_structured(
            task="extract_observations",
            system="s",
            input_text="i",
            response_model=ExtractionBatch,
            config=ModelConfig(),
        )
    assert exc_info.value.validation_errors
    assert len(client.responses.calls) == 2


def test_schema_repair_does_not_double_count_transient_retries():
    """A network hiccup during the repair call should still retry (up to
    the policy), without that consuming a second schema-repair attempt."""
    good_batch = _batch()
    client = FakeOpenAIClient(
        responses=FakeResponsesResource(
            queue=[
                _validation_error(),
                openai.APITimeoutError(request=_REQUEST),
                make_response(parsed=good_batch),
            ]
        )
    )
    provider, client = _provider(client=client)

    result = provider.generate_structured(
        task="extract_observations",
        system="s",
        input_text="i",
        response_model=ExtractionBatch,
        config=ModelConfig(),
    )
    assert result.parsed is good_batch
    assert len(client.responses.calls) == 3


def test_generate_structured_is_a_valid_llm_provider_error_subclass():
    # LLMSchemaFailureError, LLMRefusalError, etc. must all be catchable
    # via the common LLMProviderError base (services/extraction.py relies
    # on this for a single except clause covering unrecoverable errors).
    client = FakeOpenAIClient(
        responses=FakeResponsesResource(queue=[_validation_error(), _validation_error()])
    )
    provider, client = _provider(client=client)
    with pytest.raises(LLMProviderError):
        provider.generate_structured(
            task="extract_observations",
            system="s",
            input_text="i",
            response_model=ExtractionBatch,
            config=ModelConfig(),
        )
