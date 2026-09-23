"""Behavior contracts for GPT-6 Sol registration."""
from decimal import Decimal

from agent.model_metadata import DEFAULT_CONTEXT_LENGTHS
from agent.reasoning_effort import CODEX_GPT56_EFFORTS, codex_supported_efforts
from agent import usage_pricing
from agent.usage_pricing import CanonicalUsage, estimate_usage_cost, get_pricing_entry
from hermes_cli import runtime_provider
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


def test_gpt6_luna_capabilities_and_official_pricing():
    assert DEFAULT_CONTEXT_LENGTHS["gpt-6-luna"] == 1_050_000
    assert codex_supported_efforts("gpt-6-luna") is CODEX_GPT56_EFFORTS
    assert "gpt-6-luna" in _PROVIDER_MODELS["openai-api"]
    entry = get_pricing_entry("gpt-6-luna", provider="openai-api")
    assert entry is not None
    assert get_pricing_entry("gpt-6-luna", provider="cliproxy") is entry
    assert get_pricing_entry("gpt-6-luna", provider="cliproxy-luna") is entry
    assert (entry.input_cost_per_million, entry.cache_read_cost_per_million,
            entry.cache_write_cost_per_million, entry.output_cost_per_million) == (
                Decimal("0.10"), Decimal("0.01"), Decimal("0.125"), Decimal("0.50"))
    result = estimate_usage_cost(
        "gpt-6-luna",
        CanonicalUsage(input_tokens=300_000, output_tokens=10_000,
                       cache_read_tokens=20_000, cache_write_tokens=5_000),
        provider="cliproxy",
    )
    assert result.amount_usd == Decimal("0.06915")


def test_gpt6_luna_named_custom_runtime_billing_identity(monkeypatch):
    loopback = "http://127.0.0.1:8317/v1"
    monkeypatch.setattr(runtime_provider._config_mod, "load_config", lambda: {"model": {}, "providers": {}})
    monkeypatch.setattr(
        runtime_provider, "_get_named_custom_provider",
        lambda name: {"name": name, "base_url": loopback} if name in {"cliproxy-luna", "other-proxy"} else None,
    )
    monkeypatch.setattr(runtime_provider, "_try_resolve_from_custom_pool", lambda *args, **kwargs: None)
    monkeypatch.setattr(usage_pricing, "fetch_endpoint_model_metadata", lambda *args, **kwargs: {})
    runtime = runtime_provider.resolve_runtime_provider(requested="cliproxy-luna", target_model="gpt-6-luna")
    assert runtime["provider"] == "custom"
    assert runtime["base_url"] == loopback
    assert runtime["requested_provider"] == "cliproxy-luna"

    usage = CanonicalUsage(input_tokens=300_000, output_tokens=10_000,
                           cache_read_tokens=20_000, cache_write_tokens=5_000)
    result = estimate_usage_cost("gpt-6-luna", usage, provider=runtime["provider"],
                                 base_url=runtime["base_url"],
                                 requested_provider=runtime["requested_provider"])
    assert result.status == "estimated"
    assert result.amount_usd == Decimal("0.06915")

    for requested_provider, base_url in (("other-proxy", loopback),
                                         ("cliproxy-luna", "https://other.example/v1")):
        unknown = estimate_usage_cost("gpt-6-luna", usage, provider="custom",
                                      base_url=base_url, requested_provider=requested_provider)
        assert unknown.status == "unknown"
        assert unknown.amount_usd is None
