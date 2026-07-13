import os

import pytest

from hermes_cli.codex_route_lock import atomic_write_bytes, route_lock


def test_atomic_write_is_mode_0600_and_leaves_no_temp(tmp_path):
    target = tmp_path / "auth.json"

    atomic_write_bytes(target, b'{"secret": "redacted"}\n')

    assert target.read_bytes() == b'{"secret": "redacted"}\n'
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))


def test_route_lock_contention_fails_within_bound(tmp_path):
    lock_path = tmp_path / "codex_route.lock"

    with route_lock(path=lock_path, timeout=0.1):
        with pytest.raises(TimeoutError, match="route lock busy"):
            with route_lock(path=lock_path, timeout=0.1):
                pass

    assert os.stat(lock_path).st_mode & 0o777 == 0o600


def test_route_helpers_refuse_caller_live_home_in_test_mode(tmp_path, monkeypatch):
    real_home = tmp_path / "real-home"
    state_dir = real_home / ".hermes" / "state"
    state_dir.mkdir(parents=True)
    route_state = state_dir / "codex_route_policy.json"
    route_state.write_bytes(b'{"mode": "auto"}\n')
    before = route_state.read_bytes()

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HERMES_TESTING", "1")
    monkeypatch.setenv("HERMES_TEST_REAL_HOME", str(real_home))

    with pytest.raises(RuntimeError, match="live routing path"):
        atomic_write_bytes(route_state, b'{"mode": "fixed"}\n')
    with pytest.raises(RuntimeError, match="live routing path"):
        with route_lock(path=state_dir / "codex_route.lock"):
            pass

    assert route_state.read_bytes() == before
    assert not (state_dir / "codex_route.lock").exists()
    assert not list(state_dir.glob(".*.tmp"))


def test_route_guard_blocks_lexical_live_symlink_to_external_target(
    tmp_path, monkeypatch
):
    real_home = tmp_path / "real-home"
    state_dir = real_home / ".hermes" / "state"
    state_dir.mkdir(parents=True)
    external = tmp_path / "external-route.json"
    external.write_bytes(b'{"mode": "auto"}\n')
    before = external.read_bytes()
    live_link = state_dir / "codex_route_policy.json"
    live_link.symlink_to(external)

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HERMES_TESTING", "1")
    monkeypatch.setenv("HERMES_TEST_REAL_HOME", str(real_home))

    with pytest.raises(RuntimeError, match="live routing path"):
        atomic_write_bytes(live_link, b'{"mode": "fixed"}\n')

    assert external.read_bytes() == before
