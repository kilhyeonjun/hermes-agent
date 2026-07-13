"""Regression tests for the Codex time-to-first-byte (TTFB) watchdog.

The chatgpt.com/backend-api/codex endpoint has an intermittent failure mode
where it accepts the connection but never emits a single stream event. The
watchdog in ``interruptible_api_call`` kills such a connection at a short TTFB
cutoff (instead of waiting out the much longer wall-clock stale timeout) so the
retry loop can reconnect promptly. Once any stream event arrives, the TTFB
watchdog is satisfied and a separate idle watchdog handles streams that stop
emitting SSE events.

The "bytes flowing" signal is ``agent._codex_stream_last_event_ts``, set on
*any* event by ``codex_runtime.run_codex_stream`` — so reasoning-only or
tool-call-only turns (which emit no output-text deltas) are not mistaken for a
stall.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

# Stub optional heavy imports so run_agent imports cleanly in isolation.
sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())


def _make_codex_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    from run_agent import AIAgent

    agent = AIAgent(
        model="gpt-5.5",
        provider="openai-codex",
        api_key="sk-dummy",
        base_url="https://chatgpt.com/backend-api/codex",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
    )
    # The watchdog is gated on the codex_responses api_mode; assert/force it so
    # the test is robust to detection-logic changes elsewhere.
    agent.api_mode = "codex_responses"
    monkeypatch.setattr(agent, "_emit_status", lambda *a, **k: None)
    # Keep the wall-clock stale timeout high so any early kill is unambiguously
    # the TTFB path, not the stale-call path.
    monkeypatch.setattr(
        agent, "_compute_non_stream_stale_timeout", lambda *a, **k: 60.0
    )
    return agent


@pytest.mark.parametrize(
    ("est_tokens", "expected_floor"),
    [
        (9_999, 0.0),
        (10_000, 0.0),
        (10_001, 180.0),
        (50_000, 180.0),
        (50_001, 240.0),
        (100_000, 240.0),
        (100_001, 300.0),
    ],
)
def test_large_ttfb_timeout_floor_is_context_aware(est_tokens, expected_floor):
    """Large prefill gets staged headroom, but never an unbounded deadline."""
    from agent.chat_completion_helpers import openai_codex_ttfb_timeout_floor

    assert openai_codex_ttfb_timeout_floor(est_tokens) == expected_floor


def test_ttfb_kills_when_no_stream_event(tmp_path, monkeypatch):
    """Backend accepts the connection but emits no event -> killed at the TTFB
    cutoff, well before the 60s wall-clock stale timeout, with a retryable
    TimeoutError and a ``codex_ttfb_kill`` close reason."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "1")

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    stop = {"flag": False}

    def fake_hang(api_kwargs, client=None, on_first_delta=None, cancel_event=None):
        # Never set _codex_stream_last_event_ts: simulate zero events arriving.
        deadline = time.time() + 30
        while time.time() < deadline and not stop["flag"] and not agent._interrupt_requested:
            time.sleep(0.02)
        raise RuntimeError("connection closed")

    monkeypatch.setattr(agent, "_run_codex_stream", fake_hang)

    t0 = time.time()
    try:
        with pytest.raises(TimeoutError) as excinfo:
            h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "hi"})
        elapsed = time.time() - t0
        assert "TTFB" in str(excinfo.value)
        assert "codex_ttfb_kill" in closes
        assert agent._consecutive_stale_streams == 1
        # ~1s cutoff + 2s join grace; must be far under the 60s stale timeout.
        assert elapsed < 15, f"TTFB watchdog took {elapsed:.1f}s"
    finally:
        stop["flag"] = True


def test_ttfb_abort_stops_codex_runtime_before_internal_retry(tmp_path, monkeypatch):
    """The watchdog owns retries after a no-byte timeout.

    Aborting the first socket makes the Responses iterator raise ReadTimeout.
    ``run_codex_stream`` must not consume that forced error and open its own
    second stream after ``interruptible_api_call`` has already timed out.
    """
    import httpx

    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "1")

    abort_seen = threading.Event()
    retry_started = threading.Event()
    release_retry = threading.Event()
    owner_closed = threading.Event()
    calls = {"create": 0}
    worker_threads = []

    real_thread = threading.Thread

    def _capture_worker(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        worker_threads.append(worker)
        return worker

    monkeypatch.setattr(h.threading, "Thread", _capture_worker)

    class _BlockingStream:
        def __init__(self, *, first: bool):
            self.first = first
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.first:
                assert abort_seen.wait(10), "watchdog never aborted first stream"
            else:
                assert release_retry.wait(10), "orphan retry was not released"
            raise httpx.ReadTimeout("forced stream abort")

        def close(self):
            self.closed = True

    class _Responses:
        def create(self, **kwargs):
            calls["create"] += 1
            if calls["create"] == 1:
                return _BlockingStream(first=True)
            retry_started.set()
            return _BlockingStream(first=False)

    dummy_client = SimpleNamespace(responses=_Responses())
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent,
        "_abort_request_openai_client",
        lambda c, reason=None: abort_seen.set(),
    )
    monkeypatch.setattr(
        agent,
        "_close_request_openai_client",
        lambda c, reason=None: owner_closed.set(),
    )

    try:
        with pytest.raises(TimeoutError):
            h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "hi"})

        assert calls["create"] == 1
        assert retry_started.is_set() is False
        assert owner_closed.wait(1), "worker did not close its request-local client"
        assert len(worker_threads) == 1
        assert worker_threads[0].is_alive() is False
    finally:
        # Old behavior leaves the internal retry worker alive after the caller
        # returns. Always release it so a RED test cannot leak a daemon worker.
        release_retry.set()


def test_watchdog_managed_codex_runtime_leaves_retry_to_outer_loop(
    tmp_path, monkeypatch
):
    """A runtime carrying the watchdog's request-local Event gets one stream.

    The conversation loop already owns provider retries. Removing the nested
    Codex retry also removes the check-then-create window in which a second
    socket could be opened immediately after the watchdog's one-shot abort.
    """
    import httpx

    agent = _make_codex_agent(tmp_path, monkeypatch)
    calls = {"create": 0}

    class _FailingStream:
        def __iter__(self):
            return self

        def __next__(self):
            raise httpx.ReadTimeout("forced transport failure")

        def close(self):
            pass

    class _Responses:
        def create(self, **kwargs):
            calls["create"] += 1
            return _FailingStream()

    client = SimpleNamespace(responses=_Responses())

    with pytest.raises(httpx.ReadTimeout):
        agent._run_codex_stream(
            {"model": "gpt-5.5", "input": "hi"},
            client=client,
            cancel_event=threading.Event(),
        )

    assert calls["create"] == 1


def test_direct_codex_runtime_retains_single_inner_retry(tmp_path, monkeypatch):
    """Non-watchdog callers keep the historical one transport retry."""
    import httpx

    agent = _make_codex_agent(tmp_path, monkeypatch)
    calls = {"create": 0}
    sentinel = SimpleNamespace(output=[], status="completed")

    class _FailingStream:
        def __iter__(self):
            return self

        def __next__(self):
            raise httpx.ReadTimeout("forced first transport failure")

        def close(self):
            pass

    class _Responses:
        def create(self, **kwargs):
            calls["create"] += 1
            return _FailingStream() if calls["create"] == 1 else sentinel

    client = SimpleNamespace(responses=_Responses())

    response = agent._run_codex_stream(
        {"model": "gpt-5.5", "input": "hi"}, client=client
    )

    assert response is sentinel
    assert calls["create"] == 2


def test_cancel_after_retry_check_cannot_start_second_create(tmp_path, monkeypatch):
    """Deterministically cover cancel between retry check and ``create``.

    Attempt one fails before TTFB. On the old nested-retry path, attempt two
    passes its cancel check and pauses while resolving ``client.responses``.
    The main watchdog then cancels/aborts the request; releasing the property
    lookup must not allow a second ``create()`` or leave the worker alive.
    """
    import httpx

    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "1")

    retry_check_passed = threading.Event()
    abort_complete = threading.Event()
    retry_create_started = threading.Event()
    release_retry_create = threading.Event()
    owner_closed = threading.Event()
    counts = {"responses_access": 0, "create": 0}
    worker_threads = []

    class _FailingStream:
        def __iter__(self):
            return self

        def __next__(self):
            raise httpx.ReadTimeout("forced transport failure")

        def close(self):
            pass

    class _Responses:
        def create(self, **kwargs):
            counts["create"] += 1
            if counts["create"] == 1:
                return _FailingStream()
            retry_create_started.set()
            assert release_retry_create.wait(10), "orphan retry was not released"
            return _FailingStream()

    responses = _Responses()

    class _Client:
        @property
        def responses(self):
            counts["responses_access"] += 1
            if counts["responses_access"] == 2:
                # The loop's Event check already ran. Hold this property lookup
                # until the main watchdog has set Event and completed abort.
                retry_check_passed.set()
                assert abort_complete.wait(10), "watchdog abort never completed"
            return responses

    client = _Client()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: client)

    def _abort(c, reason=None):
        assert retry_check_passed.is_set()
        abort_complete.set()

    monkeypatch.setattr(agent, "_abort_request_openai_client", _abort)
    monkeypatch.setattr(
        agent,
        "_close_request_openai_client",
        lambda c, reason=None: owner_closed.set(),
    )

    real_thread = threading.Thread

    def _capture_worker(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        worker_threads.append(worker)
        return worker

    monkeypatch.setattr(h.threading, "Thread", _capture_worker)

    try:
        with pytest.raises(Exception):
            h.interruptible_api_call(
                agent, {"model": "gpt-5.5", "input": "hi"}
            )

        assert counts["create"] == 1
        assert retry_create_started.is_set() is False
        assert owner_closed.wait(1), "worker did not close its request-local client"
        assert len(worker_threads) == 1
        assert worker_threads[0].is_alive() is False
    finally:
        abort_complete.set()
        release_retry_create.set()
        for worker in worker_threads:
            worker.join(timeout=2)


def test_ttfb_default_tolerates_slow_first_event(tmp_path, monkeypatch):
    """With no env var set, the no-byte TTFB default is generous (120s), so a
    request whose first stream event is merely slow (~2s of backend admission /
    prefill) is NOT killed. This is the subscription-backed Codex case the tight
    12s default used to abort mid-prefill."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    # Default behavior: no explicit TTFB override.
    monkeypatch.delenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("HERMES_CODEX_TTFB_MAX_SECONDS", raising=False)

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    sentinel = SimpleNamespace(ok=True)

    def fake_slow_first_event(
        api_kwargs, client=None, on_first_delta=None, cancel_event=None
    ):
        # Backend is alive but slow to admit: first event lands after ~2s,
        # well under the 120s default cutoff. Mark the first byte so the
        # no-byte detector sees activity, then return the response.
        time.sleep(2.0)
        agent._codex_stream_last_event_ts = time.time()
        return sentinel

    monkeypatch.setattr(agent, "_run_codex_stream", fake_slow_first_event)

    resp = h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "hi"})
    assert resp is sentinel
    assert "codex_ttfb_kill" not in closes


def test_ttfb_includes_silent_hang_hint_for_gpt_5_5(tmp_path, monkeypatch):
    """The no-first-byte watchdog should surface the same actionable hint as the
    stale-call timeout path when the model matches the silent-hang heuristic."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "1")

    closes: list = []
    statuses: list[str] = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(agent, "_buffer_status", lambda msg: statuses.append(msg))
    monkeypatch.setattr(agent, "_emit_status", lambda msg: statuses.append(msg))
    monkeypatch.setattr(
        agent, "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    stop = {"flag": False}

    def fake_hang(api_kwargs, client=None, on_first_delta=None, cancel_event=None):
        deadline = time.time() + 30
        while time.time() < deadline and not stop["flag"] and not agent._interrupt_requested:
            time.sleep(0.02)
        raise RuntimeError("connection closed")

    monkeypatch.setattr(agent, "_run_codex_stream", fake_hang)

    try:
        with pytest.raises(TimeoutError) as excinfo:
            h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "hi"})
        message = str(excinfo.value)
        assert "gpt-5.4" in message
        assert "gpt-5.3-codex" in message
        assert "gpt-5.4-codex" in message
        assert "codex_ttfb_kill" in closes
        assert statuses, "expected a user-facing watchdog status"
        assert any("gpt-5.4" in s and "gpt-5.3-codex" in s for s in statuses)
    finally:
        stop["flag"] = True


def test_ttfb_high_env_is_capped_for_openai_codex(tmp_path, monkeypatch):
    """A stale local env value like 90s must not make openai-codex wait 90s
    before reconnecting when the backend emits no SSE frames."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("HERMES_CODEX_TTFB_MAX_SECONDS", "1")

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    stop = {"flag": False}

    def fake_hang(api_kwargs, client=None, on_first_delta=None, cancel_event=None):
        deadline = time.time() + 30
        while time.time() < deadline and not stop["flag"] and not agent._interrupt_requested:
            time.sleep(0.02)
        raise RuntimeError("connection closed")

    monkeypatch.setattr(agent, "_run_codex_stream", fake_hang)

    t0 = time.time()
    try:
        with pytest.raises(TimeoutError) as excinfo:
            h.interruptible_api_call(agent, {"model": "gpt-5.4", "input": "hi"})
        elapsed = time.time() - t0
        assert "TTFB threshold: 1s" in str(excinfo.value)
        assert "codex_ttfb_kill" in closes
        assert elapsed < 15, f"TTFB watchdog ignored cap and took {elapsed:.1f}s"
    finally:
        stop["flag"] = True


def test_ttfb_does_not_kill_when_events_flow(tmp_path, monkeypatch):
    """Once a stream event has arrived, a generation that runs past the TTFB
    cutoff is NOT killed by the watchdog — it completes normally."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "1")

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    sentinel = SimpleNamespace(ok=True)

    def fake_stream(api_kwargs, client=None, on_first_delta=None, cancel_event=None):
        # Bytes flowing: mark stream activity right away, then keep generating
        # past the 1s TTFB cutoff before returning a real response.
        agent._codex_stream_last_event_ts = time.time()
        if on_first_delta:
            on_first_delta()
        time.sleep(2.0)
        return sentinel

    monkeypatch.setattr(agent, "_run_codex_stream", fake_stream)

    resp = h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "hi"})
    assert resp is sentinel
    assert "codex_ttfb_kill" not in closes


def test_event_idle_kills_after_first_event_then_silence(tmp_path, monkeypatch):
    """If Codex emits an opening SSE event and then goes silent, kill it via
    the stream-idle watchdog instead of waiting for the long non-stream stale
    timeout."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS", "1")

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent,
        "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent,
        "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    stop = {"flag": False}

    def fake_stream(api_kwargs, client=None, on_first_delta=None, cancel_event=None):
        agent._codex_stream_last_event_ts = time.time()
        deadline = time.time() + 30
        while time.time() < deadline and not stop["flag"] and not agent._interrupt_requested:
            time.sleep(0.02)
        raise RuntimeError("connection closed")

    monkeypatch.setattr(agent, "_run_codex_stream", fake_stream)

    try:
        with pytest.raises(TimeoutError) as excinfo:
            h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "hi"})
        assert "after first byte" in str(excinfo.value)
        assert "codex_stream_idle_kill" in closes
        assert "codex_ttfb_kill" not in closes
        assert agent._consecutive_stale_streams == 1
    finally:
        stop["flag"] = True


def test_ttfb_disabled_via_env_zero(tmp_path, monkeypatch):
    """Setting HERMES_CODEX_TTFB_TIMEOUT_SECONDS=0 disables the TTFB watchdog;
    a no-event stall then falls through to the (here, 60s) stale timeout, so a
    short hang is NOT killed by TTFB."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "0")

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    sentinel = SimpleNamespace(ok=True)

    def fake_stream(api_kwargs, client=None, on_first_delta=None, cancel_event=None):
        # No event marker, but only briefly — well under the 60s stale timeout.
        time.sleep(2.0)
        return sentinel

    monkeypatch.setattr(agent, "_run_codex_stream", fake_stream)

    resp = h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "hi"})
    assert resp is sentinel
    assert "codex_ttfb_kill" not in closes


def test_large_codex_request_waits_instead_of_ttfb_reconnect(tmp_path, monkeypatch):
    """Large Codex inputs can legitimately take longer than the small-request
    first-byte cutoff before the first SSE frame. Preserve the full input and
    wait instead of killing/retrying at TTFB."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "1")

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client", lambda c, reason=None: closes.append(reason)
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client", lambda c, reason=None: closes.append(reason)
    )

    sentinel = SimpleNamespace(ok=True)

    def fake_stream(api_kwargs, client=None, on_first_delta=None, cancel_event=None):
        # No event marker for 2s: this would trip the 1s TTFB watchdog on a
        # small request, but should be allowed for a large request.
        time.sleep(2.0)
        return sentinel

    monkeypatch.setattr(agent, "_run_codex_stream", fake_stream)

    large_input = "x" * 44_000  # ~11k estimated tokens, above the 10k gate.
    resp = h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": large_input})
    assert resp is sentinel
    assert "codex_ttfb_kill" not in closes


def test_large_codex_request_has_bounded_ttfb(tmp_path, monkeypatch):
    """A large request with zero SSE events keeps its prefill floor but must
    still obey an operator's finite hard cap instead of disabling TTFB."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("HERMES_CODEX_TTFB_MAX_SECONDS", "1")
    monkeypatch.setattr(
        agent, "_compute_non_stream_stale_timeout", lambda *a, **k: 3.0
    )

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client", lambda c, reason=None: closes.append(reason)
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client", lambda c, reason=None: closes.append(reason)
    )

    stop = {"flag": False}

    def fake_hang(api_kwargs, client=None, on_first_delta=None, cancel_event=None):
        deadline = time.time() + 30
        while time.time() < deadline and not stop["flag"] and not agent._interrupt_requested:
            time.sleep(0.02)
        raise RuntimeError("connection closed")

    monkeypatch.setattr(agent, "_run_codex_stream", fake_hang)

    large_input = "x" * 44_000  # ~11k estimated tokens.
    try:
        with pytest.raises(TimeoutError) as excinfo:
            h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": large_input})
        assert "TTFB threshold: 1s" in str(excinfo.value)
        assert "codex_ttfb_kill" in closes
        assert "stale_call_kill" not in closes
    finally:
        stop["flag"] = True


def test_large_codex_request_explicit_disable_threshold_is_preserved(
    tmp_path, monkeypatch
):
    """An explicitly configured positive disable threshold remains a supported
    escape hatch for operators with unusually slow large-context backends."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("HERMES_CODEX_TTFB_MAX_SECONDS", "1")
    monkeypatch.setenv("HERMES_CODEX_TTFB_DISABLE_ABOVE_TOKENS", "10_000")

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client", lambda c, reason=None: closes.append(reason)
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client", lambda c, reason=None: closes.append(reason)
    )

    sentinel = SimpleNamespace(ok=True)

    def fake_slow_prefill(
        api_kwargs, client=None, on_first_delta=None, cancel_event=None
    ):
        time.sleep(2.0)
        return sentinel

    monkeypatch.setattr(agent, "_run_codex_stream", fake_slow_prefill)

    large_input = "x" * 44_000
    resp = h.interruptible_api_call(
        agent, {"model": "gpt-5.5", "input": large_input}
    )
    assert resp is sentinel
    assert "codex_ttfb_kill" not in closes


def test_large_codex_request_strict_ttfb_env_still_reconnects(tmp_path, monkeypatch):
    """Operators can force the old early-reconnect behavior for large inputs
    with HERMES_CODEX_TTFB_STRICT=1."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("HERMES_CODEX_TTFB_STRICT", "1")

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client", lambda c, reason=None: closes.append(reason)
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client", lambda c, reason=None: closes.append(reason)
    )

    stop = {"flag": False}

    def fake_hang(api_kwargs, client=None, on_first_delta=None, cancel_event=None):
        deadline = time.time() + 30
        while time.time() < deadline and not stop["flag"] and not agent._interrupt_requested:
            time.sleep(0.02)
        raise RuntimeError("connection closed")

    monkeypatch.setattr(agent, "_run_codex_stream", fake_hang)

    large_input = "x" * 44_000
    try:
        with pytest.raises(TimeoutError) as excinfo:
            h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": large_input})
        assert "TTFB threshold: 1s" in str(excinfo.value)
        assert "codex_ttfb_kill" in closes
    finally:
        stop["flag"] = True
