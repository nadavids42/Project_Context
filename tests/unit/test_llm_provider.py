"""ModelConfig defaults and token/cost telemetry calculation (Section
12.2, 12.5, 12.7)."""

from __future__ import annotations

from project_context.config import DEFAULT_OPENAI_MODEL
from project_context.llm.provider import ModelConfig, estimate_cost_usd


def test_model_config_defaults_match_the_pinned_prototype_configuration():
    config = ModelConfig()
    assert config.model == DEFAULT_OPENAI_MODEL == "gpt-5.6-terra"
    assert config.reasoning_effort == "low"
    assert config.store is False


def test_estimate_cost_usd_matches_pinned_pricing():
    # Section 12.2: $2/M input, $12/M output for gpt-5.6-terra.
    cost = estimate_cost_usd(DEFAULT_OPENAI_MODEL, 1_000_000, 1_000_000)
    assert cost == 2.0 + 12.0


def test_estimate_cost_usd_scales_linearly_with_tokens():
    cost = estimate_cost_usd(DEFAULT_OPENAI_MODEL, 500_000, 250_000)
    assert cost == 1.0 + 3.0


def test_estimate_cost_usd_zero_tokens_is_zero():
    assert estimate_cost_usd(DEFAULT_OPENAI_MODEL, 0, 0) == 0.0


def test_estimate_cost_usd_falls_back_to_default_pricing_for_unknown_model():
    known = estimate_cost_usd(DEFAULT_OPENAI_MODEL, 1000, 1000)
    unknown = estimate_cost_usd("some-future-model", 1000, 1000)
    assert unknown == known
