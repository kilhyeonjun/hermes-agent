"""Startup diagnostics preserve operations and identify real startup boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest


class _Events(logging.Handler):
    def __init__(self, fail=False):
        super().__init__()
        self.events = []
        self.fail = fail
        self.core_lock_held = []
        self.homes = []

    def emit(self, record):
        self.events.append(json.loads(record.getMessage().removeprefix("STARTUP_TIMING ")))
        self.homes.append(getattr(record, "hermes_home", None))
        if "tools.mcp_tool" in sys.modules:
            self.core_lock_held.append(sys.modules["tools.mcp_tool"]._lock.locked())
        if self.fail:
            raise RuntimeError("private-sink-failure")


def _capture(monkeypatch, fail=False):
    handler = _Events(fail)
    logger = logging.getLogger("gateway.startup_timing")
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.INFO)
    return handler


@pytest.mark.parametrize("outcome", ["ok", "error", "cancelled"])
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("sink_failure", [False, True])
def test_timing_preserves_outcome_and_private_monotonic_identity(
    monkeypatch, outcome, deferred, sink_failure
):
    # Break caught: logging masks an exception, uses wall time, loses early events,
    # leaks error text, or races concurrent spans into the same identity.
    import hermes_startup_timing as timing

    ticks = itertools.chain(
        [1_000_000_000, 1_005_000_000, 1_017_000_000],
        itertools.count(2_000_000_000, 1_000_000),
    )
    monkeypatch.setattr(timing, "perf_counter_ns", lambda: next(ticks))
    handler = _capture(monkeypatch, sink_failure)
    trace = timing.StartupTiming()
    if not deferred:
        trace.flush()
    failure = (
        asyncio.CancelledError("private-token-sentinel")
        if outcome == "cancelled" else RuntimeError("private-token-sentinel")
    )
    completed = []
    try:
        with trace.span("mcp.connection", profile_slot=1, server_slot=3):
            if outcome != "ok":
                raise failure
            completed.append("original-result")
    except BaseException as exc:
        assert outcome != "ok" and exc is failure
    if deferred:
        assert handler.events == []
    trace.flush()
    trace.flush()  # Duplicate flush cannot replay prior events.
    begin, end = handler.events
    assert begin["phase"] == "begin" and end["phase"] == "end"
    assert begin["elapsed_ms"] == 5.0
    assert end["elapsed_ms"] == 17.0 and end["duration_ms"] == 12.0
    assert end["status"] == outcome
    assert begin["trace_id"] == end["trace_id"]
    assert begin["span_id"] == end["span_id"]
    assert begin["pid"] == end["pid"] == os.getpid()
    assert (end["profile_slot"], end["server_slot"]) == (1, 3)
    assert completed == (["original-result"] if outcome == "ok" else [])

    def worker(slot):
        with trace.span("mcp.connection", server_slot=slot):
            return slot

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(worker, range(12))) == list(range(12))
    starts = [e for e in handler.events if e["phase"] == "begin"]
    ends = [e for e in handler.events if e["phase"] == "end"]
    assert len({e["span_id"] for e in starts}) == len(starts)
    assert {e["span_id"] for e in starts} == {e["span_id"] for e in ends}
    assert all(e["duration_ms"] >= 0 for e in ends)
    assert "private-token-sentinel" not in json.dumps(handler.events)
    assert "private-sink-failure" not in json.dumps(handler.events)
    paired = trace.span("gateway.config")
    paired.__enter__(); paired.__exit__(None, None, None)
    count = len(handler.events)
    paired.__exit__(None, None, None)
    assert len(handler.events) == count, "duplicate span end"
    allowed = {"trace_id", "span_id", "pid", "stage", "phase", "status",
               "elapsed_ms", "duration_ms", "profile_slot", "server_slot", "lock_outcome"}
    assert all(set(e) <= allowed for e in handler.events)
    for stage, slot in [("private-name", 0), ("mcp.connection", "private-name")]:
        with pytest.raises(ValueError):
            trace.span(stage, server_slot=slot)
    buffered = timing.StartupTiming()
    before = len(handler.events)
    for _ in range(90):
        with buffered.span("gateway.config"):
            pass
    assert len(handler.events) == before
    buffered.flush()
    assert len(handler.events) - before == 128, "pre-logging buffer is not bounded"


def _cli_wiring(tmp_path):
    script = r'''
import json, logging, sys
sys.argv = ["hermes", "gateway", "run"]
import hermes_cli.main as main
import hermes_cli.gateway as gateway
events = []
class Capture(logging.Handler):
    def emit(self, record):
        events.append(json.loads(record.getMessage().removeprefix("STARTUP_TIMING ")))
logger = logging.getLogger("gateway.startup_timing")
logger.handlers = [Capture()]; logger.setLevel(logging.INFO); logger.propagate = False
main._prepare_agent_startup = lambda args: None
gateway._maybe_redirect_run_to_s6_supervision = lambda args: False
seen = []
original_run = gateway.run_gateway
def consume(*args, **kwargs):
    trace = kwargs.get("startup_timing")
    assert trace is not None, "native dispatcher lost the startup trace"
    seen.append(trace)
    return original_run(*args, **kwargs)
gateway.run_gateway = consume
for name in ("_guard_official_docker_root_gateway", "_guard_named_profile_under_multiplexer",
             "_guard_supervised_gateway_conflict", "_guard_existing_gateway_process_conflict",
             "_apply_startup_watchdog_config"):
    setattr(gateway, name, lambda **kwargs: None)
gateway.supports_systemd_services = lambda: False
def exit_after_teardown(code):
    raise SystemExit(code)
gateway.os._exit = exit_after_teardown
import gateway.run as runtime
from tools import tirith_security
tirith_security.ensure_installed = lambda **kwargs: None
runtime._start_gateway_claim_pid_file = lambda: False
runtime._run_planned_stop_watcher = lambda *args: None
try:
    main.main()
except SystemExit as exc:
    assert exc.code == 1
else:
    raise AssertionError("native gateway did not preserve the startup-abort exit")
assert len(seen) == 1
ends = [e for e in events if e["phase"] == "end"]
assert {e["stage"] for e in ends} >= {"cli.imports_after_watchdog", "cli.agent_startup",
                                    "gateway.module_import", "gateway.constructor", "gateway.registries"}
assert len({e["trace_id"] for e in events}) == 1
before = len(events)
def second_call(*args, **kwargs):
    assert "startup_timing" not in kwargs, "one-shot CLI trace was reused"
gateway.run_gateway = second_call
main.main()
assert len(events) == before
print("TIMING_CHECK:" + json.dumps({"events": events}))
'''
    env = dict(os.environ, HERMES_HOME=str(tmp_path), HOME=str(tmp_path))
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            text=True, capture_output=True, timeout=40)
    assert result.returncode == 0, (result.stderr[-4000:], result.stdout[-4000:])
    assert "TIMING_CHECK:" in result.stdout


def _constructor_wiring(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from hermes_startup_timing import StartupTiming
    from tools import tirith_security

    monkeypatch.setattr(tirith_security, "ensure_installed", lambda **kw: None)
    handler = _capture(monkeypatch)
    trace = StartupTiming(); trace.flush()
    runner = GatewayRunner(GatewayConfig(), startup_timing=trace)
    assert runner.config is not None
    ends = [e for e in handler.events if e["phase"] == "end"]
    assert {e["stage"] for e in ends} >= {
        "gateway.config", "gateway.runtime_settings", "gateway.session_store",
        "gateway.lifecycle_state", "gateway.runtime_caches", "gateway.startup_checks",
        "gateway.session_db", "gateway.registries",
    }
    assert all(e["status"] == "ok" and e["duration_ms"] >= 0 for e in ends)


def _local_peer(tmp_path):
    server = tmp_path / "local_server.py"
    # Independent legacy JSON-RPC peer: exercise the installed client SDK and
    # real stdio transport without depending on optional/versioned server APIs.
    server.write_text('''import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "timing-fixture", "version": "1"}}
    elif method == "tools/list":
        time.sleep(0.05)
        result = {"tools": [{"name": "echo", "description": "Local echo",
                   "inputSchema": {"type": "object", "properties": {}}}]}
    elif method in ("resources/list", "prompts/list"):
        result = {"resources" if method == "resources/list" else "prompts": []}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''')
    return server


def _mcp_wiring(tmp_path, monkeypatch):
    from hermes_startup_timing import StartupTiming
    from tools import mcp_tool, mcp_tool_discovery as discovery, mcp_tool_loop as loop
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    import yaml

    server = _local_peer(tmp_path)
    configured = {name: {"command": sys.executable, "args": [str(server)],
                         "connect_timeout": 15}
                  for name in ["private-server-a", "private-server-b"]}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": configured}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mcp_tool, "_MCP_DISCOVERY_LOCK_PATH", None)
    handler = _capture(monkeypatch)
    trace = StartupTiming(); trace.flush()
    ready = tmp_path / "lock-ready"
    lock_script = '''import sys
from pathlib import Path
from tools.mcp_tool_loop import _try_acquire_mcp_discovery_lock
cookie = _try_acquire_mcp_discovery_lock()
assert cookie is not None and hasattr(cookie, "release")
Path(sys.argv[1]).write_text("ready")
sys.stdin.readline()
cookie.release()
'''
    holder = subprocess.Popen([sys.executable, "-c", lock_script, str(ready)],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
    contested = threading.Event()
    original_try = loop._try_acquire_mcp_discovery_lock

    def observe_real_lock():
        cookie = original_try()
        if cookie is None:
            contested.set()
        return cookie

    monkeypatch.setattr(loop, "_try_acquire_mcp_discovery_lock", observe_real_lock)
    release_lock = threading.Lock()
    released = threading.Event()

    def release_holder(wait=True):
        if wait and not contested.wait(10):
            return
        with release_lock:
            if not released.is_set() and holder.poll() is None:
                holder.stdin.write(b"release\n"); holder.stdin.flush()
                released.set()

    releaser = None
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "real lock holder did not become ready"
        releaser = threading.Thread(target=release_holder); releaser.start()
        names = discovery.discover_mcp_tools(startup_timing=trace, profile_slot=2)
        assert contested.is_set()
        assert holder.wait(timeout=10) == 0
        assert len(names) >= 2
        assert {e["name"] for e in discovery.get_mcp_status() if e["connected"]} == set(configured)
        ends = [e for e in handler.events if e["phase"] == "end"]
        assert {e["lock_outcome"] for e in ends if e["stage"] == "mcp.lock_wait"} == {"acquired"}
        connections = [e for e in ends if e["stage"] == "mcp.connection"]
        assert len(connections) == 2 and len({e["server_slot"] for e in connections}) == 2
        assert all(e["profile_slot"] == 2 and e["status"] == "ok" for e in connections)
        starts = [e for e in handler.events if e["stage"] == "mcp.connection" and e["phase"] == "begin"]
        assert max(e["elapsed_ms"] for e in starts) < min(e["elapsed_ms"] for e in connections)
        assert len([e for e in ends if e["stage"] == "mcp.registration"]) == 2
        assert not any(handler.core_lock_held)
        assert "private-server" not in json.dumps(handler.events)
        before = len(handler.events)
        assert discovery.discover_mcp_tools() == names
        assert len(handler.events) == before, "trace leaked into ordinary discovery"
        cookie = original_try(); assert hasattr(cookie, "release"); cookie.release()
    finally:
        if holder.poll() is None:
            release_holder(wait=False)
            try:
                holder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                holder.kill(); holder.wait(timeout=5)
        if releaser is not None:
            releaser.join(timeout=12)
        shutdown_mcp_servers()


def _mcp_profiles(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import _discover_gateway_mcp_tools
    from hermes_constants import get_hermes_home
    from hermes_startup_timing import StartupTiming
    from tools import mcp_tool, mcp_tool_discovery as discovery
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    import yaml

    server = _local_peer(tmp_path)
    homes = [("private-default", tmp_path / "default"), ("private-worker", tmp_path / "worker")]
    for slot, (_, home) in enumerate(homes):
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {
            f"private-profile-server-{slot}": {"command": sys.executable, "args": [str(server)]}}}))
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda *args, **kw: homes)
    monkeypatch.setattr(mcp_tool, "_MCP_DISCOVERY_LOCK_PATH", None)
    seen = []
    original = discovery._connect_server

    async def observe_transport(name, config):
        seen.append((get_hermes_home(), threading.current_thread().name))
        return await original(name, config)

    monkeypatch.setattr(discovery, "_connect_server", observe_transport)
    handler = _capture(monkeypatch)
    trace = StartupTiming(); trace.flush()
    original_home = get_hermes_home()
    try:
        asyncio.run(_discover_gateway_mcp_tools(GatewayConfig(multiplex_profiles=True), startup_timing=trace))
        assert [h for h, _ in seen] == [h for _, h in homes]
        assert all(t != threading.current_thread().name for _, t in seen)
        assert get_hermes_home() == original_home, "profile override escaped its scope"
        ends = [e for e in handler.events if e["stage"] == "mcp.connection" and e["phase"] == "end"]
        assert [(e["profile_slot"], e["server_slot"], e["status"]) for e in ends] == [(0, 0, "ok"), (1, 0, "ok")]
        assert len({e["trace_id"] for e in handler.events}) == 1
        assert all(Path(home) == homes[event["profile_slot"]][1]
                   for event, home in zip(handler.events, handler.homes))
        assert "private-" not in json.dumps(handler.events)
    finally:
        shutdown_mcp_servers()


def _lock_outcomes(tmp_path, monkeypatch):
    from hermes_startup_timing import StartupTiming
    from tools import mcp_tool, mcp_tool_discovery as discovery
    import yaml

    # Disabled peer keeps the real discovery/SDK/lock/return path, with no spawn.
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {"unused": {"enabled": False}}}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    handler = _capture(monkeypatch)
    trace = StartupTiming(); trace.flush()
    lock_path = tmp_path / "not-a-lock-file"
    lock_path.mkdir()
    monkeypatch.setattr(mcp_tool, "_MCP_DISCOVERY_LOCK_PATH", str(lock_path))
    discovery.discover_mcp_tools(startup_timing=trace)
    assert [e["lock_outcome"] for e in handler.events if e["stage"] == "mcp.lock_wait" and e["phase"] == "end"] == ["unavailable"]
    monkeypatch.setattr(mcp_tool, "_MCP_DISCOVERY_LOCK_PATH", None)
    from tools.mcp_tool_loop import _try_acquire_mcp_discovery_lock
    cookie = _try_acquire_mcp_discovery_lock()
    assert hasattr(cookie, "release")
    # Exercise bounded exhaustion without waiting the production 120s policy.
    monkeypatch.setattr(mcp_tool, "_MCP_DISCOVERY_LOCK_MAX_RETRIES", 0)
    try:
        discovery.discover_mcp_tools(startup_timing=trace)
        assert [e["lock_outcome"] for e in handler.events if e["stage"] == "mcp.lock_wait" and e["phase"] == "end"] == ["unavailable", "exhausted"]
    finally:
        cookie.release()
    cookie = _try_acquire_mcp_discovery_lock()
    assert hasattr(cookie, "release"); cookie.release()


def _connection_outcomes(tmp_path, monkeypatch):
    from hermes_startup_timing import StartupTiming
    from tools import mcp_tool, mcp_tool_discovery as discovery, mcp_tool_loop as loop
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers

    handler = _capture(monkeypatch)
    trace = StartupTiming(); trace.flush()
    assert mcp_tool._ensure_mcp_sdk()
    loop._ensure_mcp_loop()
    marker = tmp_path / "owned-peer-pid"
    peer = tmp_path / "unresponsive_peer.py"
    peer.write_text('''import os, sys
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
for line in sys.stdin:
    pass
''')

    async def exercise():
        with pytest.raises(Exception):
            await discovery._discover_and_register_server(
                "private-invalid-url", {"url": "not-a-url"},
                startup_timing=trace, server_slot=0)
        task = asyncio.create_task(discovery._discover_and_register_server(
            "private-cancelled-peer", {"command": sys.executable,
             "args": [str(peer), str(marker)], "connect_timeout": 20},
            startup_timing=trace, server_slot=1))
        try:
            async with asyncio.timeout(10):
                while not marker.exists():
                    await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    try:
        loop._run_on_mcp_loop(exercise, timeout=25)
        ends = [e for e in handler.events if e["stage"] == "mcp.connection" and e["phase"] == "end"]
        assert [(e["server_slot"], e["status"]) for e in ends] == [(0, "error"), (1, "cancelled")]
        assert not any(e["stage"] == "mcp.registration" for e in handler.events)
        assert mcp_tool._connect_server_claim.get() is None
        assert "private-" not in json.dumps(handler.events)
        assert not any(handler.core_lock_held)
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)  # owned peer was reaped by original cancellation
    finally:
        shutdown_mcp_servers()


def _unrelated_cli(tmp_path):
    script = '''import json, sys
sys.argv = ["hermes", *json.loads(sys.argv[1])]
import hermes_cli.main as main
if sys.argv[1:] == ["mcp", "serve"]:
    import hermes_cli.mcp_config as mcp_config
    mcp_config.mcp_command = lambda args: None  # final server boundary only
try:
    main.main()
except SystemExit as exc:
    assert exc.code in (None, 0)
assert "hermes_startup_timing" not in sys.modules
assert "gateway.run" not in sys.modules
'''
    for argv in (["--help"], ["kanban", "--help"], ["kanban", "ls", "--json"], ["mcp", "serve"]):
        result = subprocess.run([sys.executable, "-c", script, json.dumps(argv)],
                                env=dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(tmp_path)),
                                text=True, capture_output=True, timeout=40)
        assert result.returncode == 0, result.stderr[-4000:]
        assert "STARTUP_TIMING" not in result.stdout + result.stderr


@pytest.mark.parametrize("path", ["cli", "constructor", "mcp", "profiles", "locks", "outcomes", "lazy"])
def test_real_startup_paths_keep_explicit_trace_and_original_scope(tmp_path, monkeypatch, path):
    # Break caught: native argv/dispatcher drops the trace, ctor phases aren't
    # observed, or executor/real lock/parallel stdio discovery changes behavior.
    runners = {"cli": lambda: _cli_wiring(tmp_path),
               "constructor": lambda: _constructor_wiring(tmp_path, monkeypatch),
               "mcp": lambda: _mcp_wiring(tmp_path, monkeypatch),
               "profiles": lambda: _mcp_profiles(tmp_path, monkeypatch),
               "locks": lambda: _lock_outcomes(tmp_path, monkeypatch),
               "outcomes": lambda: _connection_outcomes(tmp_path, monkeypatch),
               "lazy": lambda: _unrelated_cli(tmp_path)}
    runners[path]()
