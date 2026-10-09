"""A stale process env cannot attest the native browser's session identity."""

from gateway import session_context as context


def test_browser_identity_requires_a_bound_native_context(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-process-session")
    context.reset_session_vars()
    read = getattr(context, "get_bound_session_env", None)
    assert callable(read), "a native-only context reader is required"
    assert read("HERMES_SESSION_ID", "") == ""
    with context.scoped_current_session_id("actual-session"):
        assert read("HERMES_SESSION_ID", "") == "actual-session"
    assert read("HERMES_SESSION_ID", "") == ""


def test_each_gateway_turn_binds_resolved_session_even_when_agent_is_cached(monkeypatch):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionContext, SessionSource
    runner = object.__new__(GatewayRunner)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="synthetic-chat")
    resolved = SessionContext(source=source, connected_platforms=[], home_channels={})
    resolved.session_id = "resolved-native-session"
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-process-session")
    for _ in range(2):
        tokens = runner._set_session_env(resolved)
        try:
            # Cached turns do not construct an AIAgent to publish the ID again.
            assert context.get_bound_session_env("HERMES_SESSION_ID", "") == resolved.session_id
        finally:
            runner._clear_session_env(tokens)
