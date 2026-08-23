"""A duck-typed stand-in for ``openai.OpenAI`` — no network, no real SDK
HTTP client. ``project_context.llm.openai_provider.OpenAIProvider`` only
ever touches ``client.responses.parse(**kwargs)`` and, on the object it
returns, ``.output_parsed``, ``.usage.input_tokens``/``.output_tokens``,
``.model``, ``.id``, ``.incomplete_details``, and ``.output`` — all
duck-typed via ``types.SimpleNamespace`` here, never a real
``openai.types.responses.ParsedResponse``."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


def make_usage(input_tokens: int, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)


def make_response(
    *,
    parsed: Any,
    model: str = "gpt-5.6-terra",
    response_id: str = "resp_fake",
    input_tokens: int = 100,
    output_tokens: int = 50,
    incomplete_details: Any = None,
    output: list[Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        output_parsed=parsed,
        usage=make_usage(input_tokens, output_tokens),
        model=model,
        id=response_id,
        incomplete_details=incomplete_details,
        output=output or [],
    )


def make_refusal_response(
    *,
    refusal_text: str = "I can't help with that.",
    model: str = "gpt-5.6-terra",
    response_id: str = "resp_refusal",
) -> SimpleNamespace:
    refusal_content = SimpleNamespace(type="refusal", refusal=refusal_text)
    message = SimpleNamespace(type="message", content=[refusal_content])
    return SimpleNamespace(
        output_parsed=None,
        usage=make_usage(10, 5),
        model=model,
        id=response_id,
        incomplete_details=None,
        output=[message],
    )


@dataclass
class FakeResponsesResource:
    queue: list[Any] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.queue:
            raise AssertionError("FakeResponsesResource.parse called with an empty queue")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class FakeOpenAIClient:
    responses: FakeResponsesResource = field(default_factory=FakeResponsesResource)
