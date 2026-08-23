"""A fake ``LLMProvider`` for tests — never touches the network.

Every extraction/provider test in this repository uses this fixture (or
constructs its own trivial function) instead of a real
``project_context.llm.openai_provider.OpenAIProvider`` (Section 15:
"Tests must use a fake/mock provider and never call the network").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from project_context.llm.provider import ModelConfig, StructuredResult, estimate_cost_usd


@dataclass
class RecordedCall:
    task: str
    system: str
    input_text: str
    response_model: type[BaseModel]
    config: ModelConfig


@dataclass
class FakeLLMProvider:
    """Returns queued results/exceptions in order, one per call.

    Pass either ``ExtractionBatch``/``BaseModel`` instances (wrapped into
    a ``StructuredResult`` automatically, with tiny made-up telemetry
    values) or already-built ``StructuredResult``/``Exception`` instances
    for full control. Raises ``AssertionError`` if called more times than
    queued — a test that expects N provider calls should queue exactly N
    responses.
    """

    responses: list[BaseModel | StructuredResult | Exception] = field(default_factory=list)
    calls: list[RecordedCall] = field(default_factory=list)
    default_input_tokens: int = 100
    default_output_tokens: int = 50
    default_latency_ms: int = 10

    def generate_structured(
        self,
        *,
        task: str,
        system: str,
        input_text: str,
        response_model: type[BaseModel],
        config: ModelConfig,
    ) -> StructuredResult:
        self.calls.append(
            RecordedCall(
                task=task,
                system=system,
                input_text=input_text,
                response_model=response_model,
                config=config,
            )
        )
        if not self.responses:
            raise AssertionError(
                f"FakeLLMProvider.generate_structured called with no queued response "
                f"(call #{len(self.calls)})"
            )
        next_item: Any = self.responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        if isinstance(next_item, StructuredResult):
            return next_item
        return StructuredResult(
            parsed=next_item,
            provider="fake",
            model=config.model,
            request_id=None,
            input_tokens=self.default_input_tokens,
            output_tokens=self.default_output_tokens,
            latency_ms=self.default_latency_ms,
            estimated_cost_usd=estimate_cost_usd(
                config.model, self.default_input_tokens, self.default_output_tokens
            ),
        )
