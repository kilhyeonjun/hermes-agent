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
