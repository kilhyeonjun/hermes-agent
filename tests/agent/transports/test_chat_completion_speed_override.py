from copy import deepcopy

from agent.transports import get_transport
import agent.transports.chat_completions  # noqa: F401


def test_chat_completions_strips_native_anthropic_speed_override():
    transport = get_transport("chat_completions")
    assert transport is not None
    overrides = {
        "speed": "fast",
        "service_tier": "priority",
        "extra_body": {"speed": 1.25},
    }
    original = deepcopy(overrides)

    kwargs = transport.build_kwargs(
        model="claude-opus-4.6",
        messages=[{"role": "user", "content": "Hi"}],
        request_overrides=overrides,
    )

    assert "speed" not in kwargs
    assert kwargs["service_tier"] == "priority"
    assert kwargs["extra_body"]["speed"] == 1.25
    assert overrides == original
