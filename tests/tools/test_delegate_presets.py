"""Trusted delegation presets stay opaque to model-facing results."""

import json
from unittest.mock import MagicMock, patch


def test_preset_schema_exposes_only_names_not_operator_descriptions():
    from tools.delegate_tool import _build_preset_param_schema

    schema = _build_preset_param_schema({"review": {"description": "use secret-model"}})
    assert schema["enum"] == ["review"]
    assert "secret-model" not in schema["description"]


def test_preset_merges_route_and_rejects_invalid_effort():
    from tools.delegate_tool import _resolve_delegation_preset_config

    cfg = {"model": "base", "presets": {"review": {"model": "trusted", "reasoning_effort": "high"}}}
    assert _resolve_delegation_preset_config(cfg, "review")["model"] == "trusted"


def test_preset_is_absent_from_schema_without_operator_presets():
    from tools.delegate_tool import _build_dynamic_schema_overrides

    with patch("tools.delegate_tool._load_config", return_value={}):
        properties = _build_dynamic_schema_overrides()["parameters"]["properties"]
    assert "preset" not in properties


def test_preset_resolution_error_hides_routing_metadata():
    from tools.delegate_tool import delegate_task

    parent = MagicMock(model="parent-secret", _delegate_depth=0)
    with patch("tools.delegate_tool._load_config", return_value={"presets": {"review": {"model": "secret-model", "provider": "secret-provider", "reasoning_effort": "invalid"}}}):
        result = json.loads(delegate_task(goal="Review this file thoroughly", preset="review", parent_agent=parent))
    assert "error" in result
    assert "preset" in result["error"].lower()
    assert "secret-model" not in json.dumps(result)
    assert "secret-provider" not in json.dumps(result)


def test_preset_credential_error_hides_routing_metadata():
    from tools.delegate_tool import delegate_task

    parent = MagicMock(model="parent-secret", _delegate_depth=0)
    with (
        patch("tools.delegate_tool._load_config", return_value={"presets": {"review": {"model": "secret-model", "provider": "secret-provider"}}}),
        patch("tools.delegate_tool._resolve_delegation_credentials", side_effect=ValueError("Cannot resolve delegation provider 'secret-provider' for model 'secret-model'")),
    ):
        result = json.loads(delegate_task(goal="Review this file thoroughly", preset="review", parent_agent=parent))
    assert "error" in result
    assert "preset" in result["error"].lower()
    assert "secret-model" not in json.dumps(result)
    assert "secret-provider" not in json.dumps(result)


def test_preset_result_hides_resolved_model():
    from tools.delegate_tool import delegate_task

    parent = MagicMock(model="parent", _delegate_depth=0, _memory_manager=None)
    parent.valid_tool_names = []
    with (
        patch("tools.delegate_tool._load_config", return_value={"presets": {"review": {"model": "secret-model"}}}),
        patch("tools.delegate_tool._resolve_delegation_credentials", return_value={"model": "secret-model", "provider": None, "base_url": None, "api_key": None, "api_mode": None, "request_overrides": {}, "max_output_tokens": None, "command": None, "args": []}),
        patch("tools.delegate_tool._build_child_preserving_parent_tools", return_value=MagicMock()),
        patch("tools.delegate_tool._run_single_child", return_value={"task_index": 0, "status": "completed", "summary": "done", "model": "secret-model", "api_calls": 1, "duration_seconds": 0}),
    ):
        result = json.loads(delegate_task(goal="Review this file thoroughly", preset="review", parent_agent=parent))
    assert "secret-model" not in json.dumps(result)
