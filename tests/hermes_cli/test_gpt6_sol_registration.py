"""Behavior contracts for GPT-6 Sol registration."""
from decimal import Decimal

from agent.model_metadata import DEFAULT_CONTEXT_LENGTHS
from agent.reasoning_effort import CODEX_GPT56_EFFORTS, codex_supported_efforts
from agent.usage_pricing import CanonicalUsage, estimate_usage_cost, get_pricing_entry
from hermes_cli.models_catalog_static import _PROVIDER_MODELS


def test_gpt6_sol_capabilities_are_registered():
    assert DEFAULT_CONTEXT_LENGTHS["gpt-6-sol"] == 1_050_000
    assert codex_supported_efforts("gpt-6-sol") is CODEX_GPT56_EFFORTS
    assert "gpt-6-sol" in _PROVIDER_MODELS["openai-api"]


def test_gpt6_sol_official_pricing_and_long_context_tier():
    entry = get_pricing_entry("gpt-6-sol", provider="openai-api")
    assert entry is not None
    assert get_pricing_entry(
        "gpt-6-sol", provider="cliproxy", base_url="http://127.0.0.1:8317/v1"
    ) is entry
    assert entry.input_cost_per_million == Decimal("2.00")
    assert entry.output_cost_per_million == Decimal("10.00")
    assert entry.cache_read_cost_per_million == Decimal("0.20")
    assert entry.cache_write_cost_per_million == Decimal("2.50")

    result = estimate_usage_cost(
        "gpt-6-sol",
        CanonicalUsage(
            input_tokens=300_000,
            output_tokens=10_000,
            cache_read_tokens=20_000,
            cache_write_tokens=5_000,
        ),
        provider="openai-api",
    )
    assert result.amount_usd == Decimal("1.383")
