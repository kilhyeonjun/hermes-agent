"""Astra's documented API contract, exercised without inference calls."""
import copy
import json

import pytest

from agent.transports import get_transport


@pytest.mark.parametrize("model,astra", [
    ("gpt-6-astra", True), ("openai/gpt-6-astra", True),
    ("gpt-6-astra-pro", False), ("gpt-6-astra-2026-09-01", False),
    ("not-gpt-6-astra", False), ("gpt-5.6-sol", False),
])
def test_responses_final_payload_obeys_model_contract(model, astra):
    from agent.model_metadata import _endpoint_scoped_context_length
    from agent.reasoning_effort import codex_supported_efforts

    overrides = {
        "temperature": 0.7, "top_p": 0.9, "top_logprobs": 3, "logprobs": True,
        "reasoning": {"effort": "ultra", "summary": "auto"},
        "include": ["reasoning.encrypted_content", "message.output_text.logprobs"],
        "extra_body": {"temperature": 0.4, "top_p": 0.8, "top_logprobs": 2,
                       "logprobs": True, "metadata": {"fixture": "retained"},
                       "include": ["message.output_text.logprobs", "reasoning.encrypted_content"],
                       "reasoning": {"effort": "ultra"}},
    }
    original = copy.deepcopy(overrides)
    wire = get_transport("codex_responses").build_kwargs(
        model=model, messages=[{"role": "user", "content": "fixture"}],
        reasoning_config={"effort": "max"}, request_overrides=overrides,
    )
    assert overrides == original
    if astra:
        for body in (wire, wire["extra_body"]):
            assert not {"temperature", "top_p", "top_logprobs", "logprobs"} & body.keys()
            assert body["reasoning"]["effort"] == "max"
            assert body["include"] == ["reasoning.encrypted_content"]
        preflight = get_transport("codex_responses").preflight_kwargs({**wire, **original})
        assert "temperature" not in preflight
        assert "temperature" not in preflight["extra_body"]
        assert preflight["reasoning"]["effort"] == "max"
        assert preflight["include"] == ["reasoning.encrypted_content"]
        assert "none" not in codex_supported_efforts(model)
        assert _endpoint_scoped_context_length(model, "https://api.openai.com/v1") == 1_050_000
    else:
        assert wire["temperature"] == original["temperature"]
        assert wire["extra_body"] == original["extra_body"]
        assert _endpoint_scoped_context_length(model, "https://api.openai.com/v1") is None
    assert _endpoint_scoped_context_length(model, "https://chatgpt.com/backend-api/codex") is None
    assert _endpoint_scoped_context_length(model, "http://localhost:8317/v1") is None


@pytest.mark.parametrize("mode", ["openai", "responses"])
def test_named_profile_route_is_explicit_and_tool_replay_is_preserved(tmp_path, monkeypatch, mode):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = {
        "model": {"default": "gpt-6-astra", "provider": "fixture-proxy"},
        "providers": {"fixture-proxy": {"base_url": "http://127.0.0.1:1/v1",
                         "api_key": "fixture-not-a-secret", "api_mode": mode}},
    }
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    rt = resolve_runtime_provider(requested="fixture-proxy", target_model="gpt-6-astra")
    transport = get_transport(rt["api_mode"])
    tools = [{"type": "function", "function": {"name": "lookup", "description": "Fixture",
             "parameters": {"type": "object", "properties": {}}}}]
    messages = [
        {"role": "user", "content": "lookup"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_fixture", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_fixture", "content": "fixture-result"},
    ]
    if mode == "openai":
        assert rt["api_mode"] == "chat_completions"
        with pytest.raises(ValueError, match="Responses"):
            transport.build_kwargs(model="gpt-6-astra", messages=messages, tools=tools)
        # Overrides cannot smuggle tools past the check. Text-only Chat remains usable.
        for overrides in ({"tools": tools}, {"extra_body": {"tools": tools}},
                          {"extra_body": {"messages": messages}}):
            with pytest.raises(ValueError, match="Responses"):
                transport.build_kwargs(model="gpt-6-astra", messages=messages[:1], request_overrides=overrides)
        with pytest.raises(ValueError, match="Responses"):
            transport.build_kwargs(model="gpt-5.6-sol", messages=messages[:1], tools=tools,
                                   request_overrides={"extra_body": {"model": "gpt-6-astra"}})
        wire = transport.build_kwargs(model="gpt-6-astra", messages=messages[:1],
                                      request_overrides={"temperature": 0.7, "top_p": 0.8, "logprobs": True})
        assert not {"temperature", "top_p", "logprobs"} & wire.keys()
    else:
        assert rt["api_mode"] == "codex_responses"
        wire = transport.build_kwargs(model="gpt-6-astra", messages=messages, tools=tools,
                                      reasoning_config={"effort": "ultra"}, base_url=rt["base_url"])
        assert wire["reasoning"]["effort"] == "max"
        calls = [item for item in wire["input"] if item.get("type") == "function_call"]
        outputs = [item for item in wire["input"] if item.get("type") == "function_call_output"]
        assert calls[0]["call_id"] == outputs[0]["call_id"]
        assert outputs[0]["output"] == "fixture-result"
    assert json.loads((tmp_path / "config.yaml").read_text()) == config


@pytest.mark.parametrize("api_mode", ["codex_responses", "chat_completions"])
@pytest.mark.parametrize("nested,field", [(False, "reasoning"), (False, "reasoning_effort"),
                                          (True, "reasoning"), (True, "reasoning_effort")])
@pytest.mark.parametrize("effort,expected", [("MAX", "max"), (" high ", "high"), (" ULTRA ", "max"),
                                            ("minimal", "low"), ("none", "low"),
                                            ("banana", None), ("", None), (None, None),
                                            (3, None), (False, None), ({}, None), ([], None)])
def test_astra_effort_is_canonical_or_rejected_before_wire(api_mode, nested, field, effort, expected):
    value = {"effort": effort, "summary": "auto"} if field == "reasoning" else effort
    body = {field: value}
    overrides = {"extra_body": body} if nested else body
    original = copy.deepcopy(overrides)
    transport = get_transport(api_mode)
    args = {"messages": [{"role": "user", "content": "fixture"}], "request_overrides": overrides}
    if expected is None:
        with pytest.raises(ValueError, match="Astra reasoning effort"):
            transport.build_kwargs(model="gpt-6-astra", **args)
    else:
        wire = transport.build_kwargs(model="gpt-6-astra", **args)
        actual_body = wire["extra_body"] if nested else wire
        actual = actual_body[field]["effort"] if field == "reasoning" else actual_body[field]
        assert actual == expected
    # Generic relay models deliberately support bespoke effort vocabularies.
    other = transport.build_kwargs(model="fixture-other-model", **args)
    other_body = other["extra_body"] if nested else other
    assert other_body[field] == value
    assert overrides == original
