from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import gateway.run as gateway_run
import hermes_cli.gateway as cli_gateway


class _ExitCalled(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _raise_exit(code: int) -> None:
    raise _ExitCalled(code)


def test_main_force_exits_zero_after_clean_shutdown(monkeypatch):
    async def fake_start_gateway(config=None):
        return True

    stdout = SimpleNamespace(flush=Mock())
    stderr = SimpleNamespace(flush=Mock())

    monkeypatch.setattr(gateway_run, "start_gateway", fake_start_gateway)
    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "argv", ["gateway.run"])
    monkeypatch.setattr(gateway_run.sys, "stdout", stdout)
    monkeypatch.setattr(gateway_run.sys, "stderr", stderr)

    with pytest.raises(_ExitCalled) as exc_info:
        gateway_run.main()

    assert exc_info.value.code == 0
    stdout.flush.assert_called_once_with()
    stderr.flush.assert_called_once_with()


def test_main_force_exits_one_after_failed_shutdown(monkeypatch):
    async def fake_start_gateway(config=None):
        return False

    stdout = SimpleNamespace(flush=Mock())
    stderr = SimpleNamespace(flush=Mock())

    monkeypatch.setattr(gateway_run, "start_gateway", fake_start_gateway)
    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "argv", ["gateway.run"])
    monkeypatch.setattr(gateway_run.sys, "stdout", stdout)
    monkeypatch.setattr(gateway_run.sys, "stderr", stderr)

    with pytest.raises(_ExitCalled) as exc_info:
        gateway_run.main()

    assert exc_info.value.code == 1
    stdout.flush.assert_called_once_with()
    stderr.flush.assert_called_once_with()


def test_main_terminates_via_os_exit_not_systemexit(monkeypatch):
    """The terminating call must be os._exit, NOT sys.exit — SystemExit is
    exactly what triggers the Py_FinalizeEx non-daemon-thread join hang this
    fixes (#53107). If main() ever regresses to sys.exit(), SystemExit would
    propagate instead of our os._exit sentinel and this test would fail.

    Test contributed by @AgenticSpark (PR #53122, duplicate of #53121)."""
    async def fake_start_gateway(config=None):
        return False

    stdout = SimpleNamespace(flush=Mock())
    stderr = SimpleNamespace(flush=Mock())

    monkeypatch.setattr(gateway_run, "start_gateway", fake_start_gateway)
    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "argv", ["gateway.run"])
    monkeypatch.setattr(gateway_run.sys, "stdout", stdout)
    monkeypatch.setattr(gateway_run.sys, "stderr", stderr)

    # Our os._exit sentinel must be what terminates main() — not SystemExit.
    with pytest.raises(_ExitCalled):
        gateway_run.main()


def test_main_routes_systemexit_through_os_exit(monkeypatch):
    """start_gateway raises SystemExit on the clean-fatal-config (#51228),
    planned-restart, and service-restart paths. main() must catch it and route
    the carried code through os._exit too, so those paths are equally wedge-proof
    (#53107) — a SystemExit propagating to interpreter finalization would join a
    stuck non-daemon worker and hang. Verifies the explicit code (e.g. 78) is
    preserved through the os._exit backstop."""
    async def fake_start_gateway(config=None):
        raise SystemExit(78)

    stdout = SimpleNamespace(flush=Mock())
    stderr = SimpleNamespace(flush=Mock())

    monkeypatch.setattr(gateway_run, "start_gateway", fake_start_gateway)
    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "argv", ["gateway.run"])
    monkeypatch.setattr(gateway_run.sys, "stdout", stdout)
    monkeypatch.setattr(gateway_run.sys, "stderr", stderr)

    with pytest.raises(_ExitCalled) as exc_info:
        gateway_run.main()

    # The SystemExit(78) must be converted to os._exit(78), not propagated.
    assert exc_info.value.code == 78
    stdout.flush.assert_called_once_with()
    stderr.flush.assert_called_once_with()


def test_main_systemexit_none_code_maps_to_zero(monkeypatch):
    """SystemExit() with no code (or None) is a clean exit → os._exit(0)."""
    async def fake_start_gateway(config=None):
        raise SystemExit()

    monkeypatch.setattr(gateway_run, "start_gateway", fake_start_gateway)
    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "argv", ["gateway.run"])
    monkeypatch.setattr(gateway_run.sys, "stdout", SimpleNamespace(flush=Mock()))
    monkeypatch.setattr(gateway_run.sys, "stderr", SimpleNamespace(flush=Mock()))

    with pytest.raises(_ExitCalled) as exc_info:
        gateway_run.main()

    assert exc_info.value.code == 0


def test_main_systemexit_str_code_maps_to_one(monkeypatch):
    """SystemExit with a str code (CPython prints it to stderr then exits 1).
    We can't print during os._exit, but the code must still map to 1 — matching
    CPython's handle_system_exit semantics for a non-int, non-None code."""
    async def fake_start_gateway(config=None):
        raise SystemExit("fatal: something went wrong")

    monkeypatch.setattr(gateway_run, "start_gateway", fake_start_gateway)
    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "argv", ["gateway.run"])
    monkeypatch.setattr(gateway_run.sys, "stdout", SimpleNamespace(flush=Mock()))
    monkeypatch.setattr(gateway_run.sys, "stderr", SimpleNamespace(flush=Mock()))

    with pytest.raises(_ExitCalled) as exc_info:
        gateway_run.main()

    assert exc_info.value.code == 1


@pytest.fixture
def cli_run_gateway_harness(monkeypatch):
    """Make ``hermes_cli.gateway.run_gateway`` safe + hermetic to call directly.

    ``run_gateway`` is the entry point launchd/systemd actually exec
    (``hermes_cli.main gateway run``). Its preamble runs four process-table
    guards — ``_guard_existing_gateway_process_conflict`` will TERMINATE a
    running gateway when ``replace=True`` — plus a respawn-storm breaker that
    can ``time.sleep``. All are neutralised here so the test exercises only the
    exit path, and can never touch a real gateway on the developer's machine.

    PID-file / runtime-lock releases inside the backstop are mocked for the same
    reason. ``os._exit`` is replaced with a raising sentinel so the real
    ``_exit_after_graceful_shutdown`` composition (flush → release → drain →
    exit) is exercised rather than mocked away.
    """
    from gateway import status as gateway_status

    for guard in (
        "_guard_official_docker_root_gateway",
        "_guard_named_profile_under_multiplexer",
        "_guard_supervised_gateway_conflict",
        "_guard_existing_gateway_process_conflict",
    ):
        monkeypatch.setattr(cli_gateway, guard, lambda *a, **k: None)

    # Respawn-storm breaker: never let a test sleep on a backoff.
    monkeypatch.setattr(
        gateway_status, "record_start_and_check_storm", lambda *a, **k: None
    )
    # Never mutate the real PID file / runtime lock.
    monkeypatch.setattr(gateway_status, "remove_pid_file", Mock())
    monkeypatch.setattr(gateway_status, "release_gateway_runtime_lock", Mock())

    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "stdout", SimpleNamespace(flush=Mock()))
    monkeypatch.setattr(gateway_run.sys, "stderr", SimpleNamespace(flush=Mock()))

    def _set_start_gateway(fn):
        monkeypatch.setattr(gateway_run, "start_gateway", fn)

    return _set_start_gateway


def test_cli_run_gateway_routes_systemexit_75_through_os_exit(
    cli_run_gateway_harness,
):
    """Regression: the launchd/systemd entry point must hard-exit on SystemExit.

    ``gateway/run.py::main()`` already routed every exit path through the
    ``os._exit`` backstop, but ``hermes_cli.gateway.run_gateway`` — the entry
    point actually exec'd by the service — re-raised instead. A propagating
    SystemExit reaches ``Py_FinalizeEx`` → ``wait_for_thread_shutdown``, which
    joins every non-daemon thread; one wedged ThreadPoolExecutor worker (e.g. a
    streaming LLM call to an endpoint with no listener) then strands the
    process. Observed in the wild: SystemExit(75) raised and logged at T+0 with
    the PID still alive 190s later, only dying to an external SIGTERM — so
    launchd never saw an exit and never respawned.

    Code 75 is the service-restart signal, making it the exact code whose loss
    breaks restart-on-exit supervision.
    """
    async def fake_start_gateway(replace=False, verbosity=0):
        raise SystemExit(75)

    cli_run_gateway_harness(fake_start_gateway)

    with pytest.raises(_ExitCalled) as exc_info:
        cli_gateway.run_gateway(replace=False)

    assert exc_info.value.code == 75


def test_cli_run_gateway_hard_exits_zero_on_clean_shutdown(
    cli_run_gateway_harness,
):
    """A clean ``start_gateway`` return must also hard-exit, not fall off the
    end of the function into interpreter finalization (same join hang)."""
    async def fake_start_gateway(replace=False, verbosity=0):
        return True

    cli_run_gateway_harness(fake_start_gateway)

    with pytest.raises(_ExitCalled) as exc_info:
        cli_gateway.run_gateway(replace=False)

    assert exc_info.value.code == 0


def test_cli_run_gateway_hard_exits_one_on_failed_startup(
    cli_run_gateway_harness,
):
    """Failed startup keeps its exit-1 contract (systemd Restart=on-failure)
    while still terminating via the wedge-proof backstop."""
    async def fake_start_gateway(replace=False, verbosity=0):
        return False

    cli_run_gateway_harness(fake_start_gateway)

    with pytest.raises(_ExitCalled) as exc_info:
        cli_gateway.run_gateway(replace=False)

    assert exc_info.value.code == 1


def test_cli_run_gateway_propagates_unexpected_exception(
    cli_run_gateway_harness,
):
    """Unexpected crashes must NOT be converted to a hard exit.

    On the SystemExit / clean-return paths teardown has completed, so hard-exit
    is safe. An arbitrary exception means teardown state is unknown and the
    traceback is the only diagnostic — it must propagate to
    stderr/gateway.error.log rather than being swallowed by ``os._exit``.
    """
    class _Boom(RuntimeError):
        pass

    async def fake_start_gateway(replace=False, verbosity=0):
        raise _Boom("upstream exploded")

    cli_run_gateway_harness(fake_start_gateway)

    with pytest.raises(_Boom):
        cli_gateway.run_gateway(replace=False)


def test_exit_backstop_releases_pid_file_and_runtime_lock(monkeypatch):
    """os._exit bypasses atexit, and the early SystemExit exit paths never run
    _stop_impl — so the force-exit backstop itself must release the PID file and
    runtime lock, or those early paths (#51228 fatal-config) would leak them.
    Both releases are idempotent, so this is safe on every exit path."""
    from gateway import status as gateway_status

    remove_pid = Mock()
    release_lock = Mock()
    monkeypatch.setattr(gateway_status, "remove_pid_file", remove_pid)
    monkeypatch.setattr(gateway_status, "release_gateway_runtime_lock", release_lock)
    monkeypatch.setattr(gateway_run.os, "_exit", _raise_exit)
    monkeypatch.setattr(gateway_run.sys, "stdout", SimpleNamespace(flush=Mock()))
    monkeypatch.setattr(gateway_run.sys, "stderr", SimpleNamespace(flush=Mock()))

    with pytest.raises(_ExitCalled) as exc_info:
        gateway_run._exit_after_graceful_shutdown(78)

    assert exc_info.value.code == 78
    remove_pid.assert_called_once_with()
    release_lock.assert_called_once_with()
