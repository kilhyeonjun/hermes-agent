"""GPT-6 Astra's API constraints; no assumptions about proxy or Codex context windows.

Source: https://developers.openai.com/api/docs/models/gpt-6-astra
Only the published alias is recognized; unverified snapshots/variants are not aliases.
"""
from typing import Any


def is_astra_model(model: Any) -> bool:
    return isinstance(model, str) and model.strip().lower().rsplit("/", 1)[-1] == "gpt-6-astra"


def _normalize_astra_effort(effort: Any) -> str:
    from agent.reasoning_effort import CODEX_ASTRA_EFFORTS, EFFORT_LADDER, clamp_effort

    if not isinstance(effort, str) or effort.strip().lower() not in EFFORT_LADDER:
        raise ValueError(
            "GPT-6 Astra reasoning effort must be a recognized string level "
            "(low, medium, high, xhigh, max; internal levels are clamped)."
        )
    return clamp_effort(effort.strip().lower(), CODEX_ASTRA_EFFORTS)


def finalize_astra_kwargs(kwargs: dict, *, responses: bool) -> dict:
    """Enforce wire constraints after overrides without mutating caller-owned nested data."""
    extra = kwargs.get("extra_body")
    effective_model = extra.get("model", kwargs.get("model")) if isinstance(extra, dict) else kwargs.get("model")
    if not is_astra_model(effective_model):
        return kwargs
    bodies = [kwargs]
    if isinstance(kwargs.get("extra_body"), dict):
        kwargs["extra_body"] = dict(kwargs["extra_body"])
        bodies.append(kwargs["extra_body"])
    if not responses:
        history_has_tools = any(
            isinstance(message, dict) and (
                message.get("role") in {"tool", "function"} or message.get("tool_calls") or message.get("function_call")
            ) for body in bodies for message in body.get("messages", [])
        )
        if history_has_tools or any(body.get("tools") or body.get("functions") for body in bodies):
            raise ValueError(
                "GPT-6 Astra tool calling requires the Responses API. Configure the selected "
                "provider's api_mode as responses only after verifying endpoint support, "
                "or select a model supporting Chat Completions tools."
            )
    for body in bodies:
        for field in ("temperature", "top_p", "top_logprobs", "logprobs"):
            body.pop(field, None)
        if isinstance(body.get("include"), list):
            body["include"] = [item for item in body["include"] if item != "message.output_text.logprobs"]
        if isinstance(body.get("reasoning"), dict) and "effort" in body["reasoning"]:
            body["reasoning"] = {
                **body["reasoning"], "effort": _normalize_astra_effort(body["reasoning"]["effort"]),
            }
        if "reasoning_effort" in body:
            body["reasoning_effort"] = _normalize_astra_effort(body["reasoning_effort"])
    return kwargs
