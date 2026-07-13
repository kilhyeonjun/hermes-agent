from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest


def _codex_store(
    access_token: str,
    refresh_token: str,
    *,
    include_alias: bool = False,
    manual_pair: tuple[str, str] | None = None,
    grant_id: str | None = None,
) -> dict:
    store: dict = {
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {
            "openai-codex": {
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                },
                "last_refresh": "2026-07-01T00:00:00Z",
                "auth_mode": "chatgpt",
                **({"grant_id": grant_id} if grant_id else {}),
            }
        },
    }
    rows: list[dict] = []
    if include_alias:
        rows.append(
            {
                "id": "singleton-alias",
                "source": "device_code",
                "auth_type": "oauth",
                "access_token": access_token,
                "refresh_token": refresh_token,
                "last_status": "dead",
                "last_error_code": 401,
                **({"grant_id": grant_id} if grant_id else {}),
            }
        )
    if manual_pair is not None:
        rows.append(
            {
                "id": "independent-manual",
                "source": "manual:device_code",
                "auth_type": "oauth",
                "access_token": manual_pair[0],
                "refresh_token": manual_pair[1],
            }
        )
    if rows:
        store["credential_pool"] = {"openai-codex": rows}
    return store


def _codex_grants(store: dict) -> set[str]:
    grants: set[str] = set()
    provider = store.get("providers", {}).get("openai-codex", {})
    if isinstance(provider, dict) and provider.get("grant_id"):
        grants.add(str(provider["grant_id"]))
    for row in store.get("credential_pool", {}).get("openai-codex", []):
        if isinstance(row, dict) and row.get("grant_id"):
            grants.add(str(row["grant_id"]))
    return grants


def _write_store(path: Path, payload: dict) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path.read_bytes()


def _configure_root(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    import hermes_cli.auth as auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    state_dir = tmp_path / "codex-refresh-state"
    monkeypatch.setenv("HERMES_CODEX_REFRESH_STATE_DIR", str(state_dir))
    monkeypatch.setattr(auth_mod, "AUTH_LOCK_TIMEOUT_SECONDS", 2.0)
    return root, state_dir


def _wal_paths(state_dir: Path) -> list[Path]:
    return sorted(state_dir.glob("grant-*.wal.json"))


def _single_wal_path(state_dir: Path) -> Path:
    paths = _wal_paths(state_dir)
    assert len(paths) == 1
    return paths[0]


def _wal_grant_id(path: Path) -> str:
    return path.name.removeprefix("grant-").removesuffix(".wal.json")


def _refresh(auth_mod, auth_path: Path) -> dict:
    return auth_mod.refresh_codex_oauth_coordinated(
        expected_access_token="access-old",
        expected_refresh_token="refresh-old",
        source_auth_path=auth_path,
        timeout_seconds=0.1,
    )


def _leave_rotated_wal(auth_mod, monkeypatch, auth_path: Path) -> dict:
    original_finish = auth_mod._finish_codex_wal_commit
    calls = {"post": 0}

    def _pure(*_args, **_kwargs):
        calls["post"] += 1
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-07-13T13:03:00Z",
        }

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _pure)
    monkeypatch.setattr(
        auth_mod,
        "_finish_codex_wal_commit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated crash after durable ROTATED WAL")
        ),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        _refresh(auth_mod, auth_path)
    monkeypatch.setattr(auth_mod, "_finish_codex_wal_commit", original_finish)
    return calls


def test_structural_codex_mutation_retains_inventory_with_grant_locks(
    monkeypatch,
):
    import hermes_cli.auth as auth_mod

    grant_id = "f" * 32
    events: list[str] = []

    class Scope:
        def handoff_to_grants(self, *_args, **_kwargs):
            pytest.fail("structural mutation must not release inventory")

        def retain_inventory_and_lock_grants(
            self, grant_ids, *, timeout_seconds
        ):
            assert tuple(grant_ids) == (grant_id,)
            assert timeout_seconds > 0
            events.append("inventory+grant")

    @contextmanager
    def fake_refresh_lock(*, timeout_seconds):
        assert timeout_seconds > 0
        events.append("inventory")
        yield Scope()
        events.append("released")

    def fake_prepare(*, reason, supersede_ambiguous, grant_ids):
        assert reason == "profile_rename"
        assert supersede_ambiguous is True
        assert grant_ids == (grant_id,)
        assert events == ["inventory", "inventory+grant"]
        events.append("prepared")

    monkeypatch.setattr(auth_mod, "_codex_refresh_lock", fake_refresh_lock)
    monkeypatch.setattr(
        auth_mod, "_prepare_codex_auth_store_mutation_locked", fake_prepare
    )

    with auth_mod._codex_auth_store_mutation(
        reason="profile_rename",
        supersede_ambiguous=True,
        grant_ids=(grant_id,),
    ):
        events.append("mutated")

    assert events == [
        "inventory",
        "inventory+grant",
        "prepared",
        "mutated",
        "released",
    ]


def test_refresh_rejects_grant_identity_change_while_waiting(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, _state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    old_grant = "1" * 32
    new_grant = "2" * 32
    _write_store(
        auth_path,
        _codex_store(
            "access-old",
            "refresh-old",
            grant_id=old_grant,
        ),
    )
    real_handoff = auth_mod._CodexInventoryLockScope.handoff_to_grants

    def change_identity_after_handoff(self, grant_ids, *, timeout_seconds):
        real_handoff(self, grant_ids, timeout_seconds=timeout_seconds)
        store = json.loads(auth_path.read_text(encoding="utf-8"))
        store["providers"]["openai-codex"]["grant_id"] = new_grant
        auth_path.write_text(json.dumps(store, indent=2), encoding="utf-8")

    monkeypatch.setattr(
        auth_mod._CodexInventoryLockScope,
        "handoff_to_grants",
        change_identity_after_handoff,
    )
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: pytest.fail(
            "transport must not run after grant identity changes"
        ),
    )

    with pytest.raises(
        auth_mod.AuthStoreConflictError,
        match="grant identity changed while waiting",
    ):
        _refresh(auth_mod, auth_path)


def test_codex_refresh_success_commits_generation_and_cleans_secure_wal(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(
        auth_path,
        _codex_store(
            "access-old",
            "refresh-old",
            include_alias=True,
            manual_pair=("manual-access", "manual-refresh"),
            grant_id="1" * 32,
        ),
    )
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-07-13T13:00:00Z",
        },
    )

    outcome = _refresh(auth_mod, auth_path)

    assert outcome["adopted"] is False
    saved = json.loads(auth_path.read_text(encoding="utf-8"))
    assert saved["providers"]["openai-codex"]["tokens"] == {
        "access_token": "access-new",
        "refresh_token": "refresh-new",
    }
    rows = {
        row["id"]: row
        for row in saved["credential_pool"]["openai-codex"]
    }
    assert rows["singleton-alias"]["access_token"] == "access-new"
    assert rows["singleton-alias"]["last_status"] is None
    assert rows["independent-manual"]["access_token"] == "manual-access"
    assert saved["_auth_revision"] == 1
    assert not _wal_paths(state_dir)
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((state_dir / "refresh.lock").stat().st_mode) == 0o600
    assert stat.S_IMODE((state_dir / "refresh.hmac.key").stat().st_mode) == 0o600


def test_codex_rotated_wal_recovers_without_second_refresh_post(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(auth_path, _codex_store("access-old", "refresh-old"))
    calls = {"post": 0, "finish": 0}

    def _pure(*_args, **_kwargs):
        calls["post"] += 1
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-07-13T13:01:00Z",
        }

    original_finish = auth_mod._finish_codex_wal_commit

    def _crash_once(*args, **kwargs):
        calls["finish"] += 1
        if calls["finish"] == 1:
            raise RuntimeError("simulated crash after durable ROTATED WAL")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _pure)
    monkeypatch.setattr(auth_mod, "_finish_codex_wal_commit", _crash_once)

    with pytest.raises(RuntimeError, match="simulated crash"):
        _refresh(auth_mod, auth_path)
    assert json.loads(_single_wal_path(state_dir).read_text())["state"] == "rotated"
    assert json.loads(auth_path.read_text())["providers"]["openai-codex"]["tokens"]["access_token"] == "access-old"

    outcome = _refresh(auth_mod, auth_path)

    assert outcome["adopted"] is True
    assert calls["post"] == 1
    assert not _wal_paths(state_dir)
    saved = json.loads(auth_path.read_text())
    assert saved["providers"]["openai-codex"]["tokens"]["access_token"] == "access-new"


def test_codex_prepared_wal_is_ambiguous_and_never_replays_old_token(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    before = _write_store(
        auth_path,
        _codex_store("access-old", "refresh-old", grant_id="2" * 32),
    )
    calls = {"post": 0}

    def _ambiguous(*_args, **_kwargs):
        calls["post"] += 1
        raise auth_mod.AuthError(
            "transport outcome unknown",
            provider="openai-codex",
            code="codex_refresh_transport_error",
            relogin_required=False,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _ambiguous)

    with pytest.raises(auth_mod.AuthError) as first:
        _refresh(auth_mod, auth_path)
    assert first.value.code == "codex_refresh_transport_error"
    assert json.loads(_single_wal_path(state_dir).read_text())["state"] == "prepared"

    with pytest.raises(auth_mod.AuthError) as second:
        _refresh(auth_mod, auth_path)
    assert second.value.code == "codex_refresh_ambiguous"
    assert calls["post"] == 1
    assert auth_path.read_bytes() == before


def test_ambiguous_grant_wal_does_not_block_independent_codex_account(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    grant_a = "8" * 32
    grant_b = "9" * 32
    _write_store(
        auth_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "account-a",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-a",
                        "refresh_token": "refresh-a",
                        "grant_id": grant_a,
                    },
                    {
                        "id": "account-b",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-b",
                        "refresh_token": "refresh-b",
                        "grant_id": grant_b,
                    },
                ]
            },
        },
    )
    calls = {"a": 0, "b": 0}

    def _ambiguous_a(*_args, **_kwargs):
        calls["a"] += 1
        raise auth_mod.AuthError(
            "transport outcome unknown",
            provider="openai-codex",
            code="codex_refresh_transport_error",
            relogin_required=False,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _ambiguous_a)
    with pytest.raises(auth_mod.AuthError):
        auth_mod.refresh_codex_oauth_coordinated(
            expected_access_token="access-a",
            expected_refresh_token="refresh-a",
            credential_id="account-a",
            source_auth_path=auth_path,
            timeout_seconds=0.1,
        )

    def _rotate_b(*_args, **_kwargs):
        calls["b"] += 1
        return {
            "access_token": "access-b-new",
            "refresh_token": "refresh-b-new",
            "last_refresh": "2026-07-13T14:03:00Z",
        }

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _rotate_b)
    outcome = auth_mod.refresh_codex_oauth_coordinated(
        expected_access_token="access-b",
        expected_refresh_token="refresh-b",
        credential_id="account-b",
        source_auth_path=auth_path,
        timeout_seconds=0.1,
    )

    assert outcome["grant_id"] == grant_b
    assert calls == {"a": 1, "b": 1}
    assert [path.name for path in _wal_paths(state_dir)] == [
        f"grant-{grant_a}.wal.json"
    ]
    rows = {
        row["id"]: row
        for row in json.loads(auth_path.read_text())["credential_pool"][
            "openai-codex"
        ]
    }
    assert rows["account-a"]["access_token"] == "access-a"
    assert rows["account-b"]["access_token"] == "access-b-new"


def test_corrupt_grant_wal_isolated_from_independent_codex_account(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    grant_a = "6" * 32
    grant_b = "7" * 32
    _write_store(
        auth_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "account-a",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-a",
                        "refresh_token": "refresh-a",
                        "grant_id": grant_a,
                    },
                    {
                        "id": "account-b",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-b",
                        "refresh_token": "refresh-b",
                        "grant_id": grant_b,
                    },
                ]
            },
        },
    )
    auth_mod._codex_refresh_hmac_key()
    corrupt_wal = state_dir / f"grant-{grant_a}.wal.json"
    corrupt_wal.write_text("{malformed", encoding="utf-8")
    corrupt_wal.chmod(0o600)
    calls = {"post": 0}

    def _rotate_b(*_args, **_kwargs):
        calls["post"] += 1
        return {
            "access_token": "access-b-new",
            "refresh_token": "refresh-b-new",
            "last_refresh": "2026-07-13T14:03:30Z",
        }

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _rotate_b)
    outcome = auth_mod.refresh_codex_oauth_coordinated(
        expected_access_token="access-b",
        expected_refresh_token="refresh-b",
        credential_id="account-b",
        source_auth_path=auth_path,
        timeout_seconds=0.1,
    )

    assert outcome["grant_id"] == grant_b
    assert calls["post"] == 1
    assert corrupt_wal.read_text(encoding="utf-8") == "{malformed"

    with pytest.raises(auth_mod.AuthStoreCorruptError):
        auth_mod.refresh_codex_oauth_coordinated(
            expected_access_token="access-a",
            expected_refresh_token="refresh-a",
            credential_id="account-a",
            source_auth_path=auth_path,
            timeout_seconds=0.1,
        )
    assert calls["post"] == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_codex_refresh_is_single_flight_across_os_processes(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, _state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    grant_id = "d" * 32
    _write_store(
        auth_path,
        _codex_store(
            "access-old",
            "refresh-old",
            include_alias=True,
            grant_id=grant_id,
        ),
    )
    counter = tmp_path / "refresh-post-count"

    def _slow_refresh(*_args, **_kwargs):
        fd = os.open(counter, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, b"x")
            os.fsync(fd)
        finally:
            os.close(fd)
        time.sleep(0.2)
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-07-13T14:05:00Z",
        }

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _slow_refresh)
    children: list[int] = []
    for _index in range(2):
        pid = os.fork()
        if pid == 0:
            try:
                outcome = _refresh(auth_mod, auth_path)
                if outcome.get("grant_id") != grant_id:
                    os._exit(2)
                os._exit(0)
            except BaseException as exc:
                (tmp_path / f"single-flight-error-{os.getpid()}").write_text(
                    f"{type(exc).__name__}:{getattr(exc, 'code', '')}",
                    encoding="utf-8",
                )
                os._exit(1)
        children.append(pid)

    statuses = [os.waitpid(pid, 0)[1] for pid in children]

    errors = sorted(tmp_path.glob("single-flight-error-*"))
    assert all(
        os.waitstatus_to_exitcode(status) == 0 for status in statuses
    ), [path.read_text(encoding="utf-8") for path in errors]
    assert counter.read_bytes() == b"x"
    saved = json.loads(auth_path.read_text())
    assert saved["providers"]["openai-codex"]["tokens"] == {
        "access_token": "access-new",
        "refresh_token": "refresh-new",
    }
    assert not _wal_paths(_state_dir)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows locking")
def test_native_windows_codex_refresh_is_single_flight_across_processes(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    grant_id = "e" * 32
    _write_store(
        auth_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "account-a",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-old",
                        "refresh_token": "refresh-old",
                        "grant_id": grant_id,
                    }
                ]
            },
        },
    )
    counter = tmp_path / "windows-refresh-post-count"
    start_gate = tmp_path / "windows-refresh-start"
    child_script = tmp_path / "windows-refresh-child.py"
    child_script.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "root, state, auth_path, counter, gate = map(Path, sys.argv[1:6])\n"
        "os.environ['HERMES_HOME'] = str(root)\n"
        "os.environ['HERMES_CODEX_REFRESH_STATE_DIR'] = str(state)\n"
        "import hermes_cli.auth as auth_mod\n"
        "auth_mod.AUTH_LOCK_TIMEOUT_SECONDS = 4.0\n"
        "def rotate(*_args, **_kwargs):\n"
        "    with counter.open('ab', buffering=0) as handle:\n"
        "        handle.write(b'x')\n"
        "        os.fsync(handle.fileno())\n"
        "    time.sleep(0.4)\n"
        "    return {'access_token': 'access-new', 'refresh_token': 'refresh-new', "
        "'last_refresh': '2026-07-13T14:05:15Z'}\n"
        "auth_mod.refresh_codex_oauth_pure = rotate\n"
        "deadline = time.monotonic() + 5.0\n"
        "while not gate.exists():\n"
        "    if time.monotonic() >= deadline: raise RuntimeError('start gate timeout')\n"
        "    time.sleep(0.01)\n"
        "result = auth_mod.refresh_codex_oauth_coordinated(\n"
        "    expected_access_token='access-old',\n"
        "    expected_refresh_token='refresh-old',\n"
        "    credential_id='account-a',\n"
        "    source_auth_path=auth_path,\n"
        "    timeout_seconds=1.0,\n"
        ")\n"
        "if result.get('grant_id') != '" + grant_id + "': raise SystemExit(2)\n",
        encoding="utf-8",
    )
    repo_root = Path(auth_mod.__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    argv = [
        sys.executable,
        str(child_script),
        str(root),
        str(state_dir),
        str(auth_path),
        str(counter),
        str(start_gate),
    ]
    children = [
        subprocess.Popen(
            argv,
            cwd=repo_root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        for _index in range(2)
    ]
    start_gate.write_text("start", encoding="utf-8")
    results = [child.communicate(timeout=15) for child in children]

    assert [child.returncode for child in children] == [0, 0], results
    assert counter.read_bytes() == b"x"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_independent_codex_grants_enter_refresh_transport_concurrently(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, _state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    grant_a = "4" * 32
    grant_b = "5" * 32
    _write_store(
        auth_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "account-a",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-a",
                        "refresh_token": "refresh-a",
                        "grant_id": grant_a,
                    },
                    {
                        "id": "account-b",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-b",
                        "refresh_token": "refresh-b",
                        "grant_id": grant_b,
                    },
                ]
            },
        },
    )
    barrier_dir = tmp_path / "transport-barrier"
    barrier_dir.mkdir()

    def _parallel_refresh(access_token, *_args, **_kwargs):
        suffix = access_token.rsplit("-", 1)[-1]
        marker = barrier_dir / suffix
        marker.write_text("entered", encoding="utf-8")
        deadline = time.monotonic() + 2.0
        while len(list(barrier_dir.iterdir())) < 2:
            if time.monotonic() >= deadline:
                raise RuntimeError("independent grant transport was serialized")
            time.sleep(0.01)
        return {
            "access_token": f"access-{suffix}-new",
            "refresh_token": f"refresh-{suffix}-new",
            "last_refresh": "2026-07-13T14:05:30Z",
        }

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _parallel_refresh)
    children: list[int] = []
    for suffix in ("a", "b"):
        pid = os.fork()
        if pid == 0:
            try:
                outcome = auth_mod.refresh_codex_oauth_coordinated(
                    expected_access_token=f"access-{suffix}",
                    expected_refresh_token=f"refresh-{suffix}",
                    credential_id=f"account-{suffix}",
                    source_auth_path=auth_path,
                    timeout_seconds=0.1,
                )
                if outcome.get("grant_id") not in {grant_a, grant_b}:
                    os._exit(2)
                os._exit(0)
            except BaseException:
                os._exit(1)
        children.append(pid)

    statuses = [os.waitpid(pid, 0)[1] for pid in children]

    assert all(os.waitstatus_to_exitcode(status) == 0 for status in statuses)
    assert {path.name for path in barrier_dir.iterdir()} == {"a", "b"}
    rows = {
        row["id"]: row
        for row in json.loads(auth_path.read_text())["credential_pool"][
            "openai-codex"
        ]
    }
    assert rows["account-a"]["refresh_token"] == "refresh-a-new"
    assert rows["account-b"]["refresh_token"] == "refresh-b-new"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_same_grant_waiter_does_not_convoy_later_independent_grant(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, _state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    grant_a = "6" * 32
    grant_b = "7" * 32
    _write_store(
        auth_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "account-a",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-a",
                        "refresh_token": "refresh-a",
                        "grant_id": grant_a,
                    },
                    {
                        "id": "account-b",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-b",
                        "refresh_token": "refresh-b",
                        "grant_id": grant_b,
                    },
                ]
            },
        },
    )
    a_entered = tmp_path / "grant-a-transport-entered"
    a_release = tmp_path / "grant-a-transport-release"
    a_waiter = tmp_path / "grant-a-waiter-entered"
    b_entered = tmp_path / "grant-b-transport-entered"
    real_grant_lock = auth_mod._codex_grant_lock

    def _tracked_grant_lock(grant_id, *, timeout_seconds):
        if grant_id == grant_a and os.environ.get("HERMES_TEST_ROLE") == "a2":
            a_waiter.write_text("waiting", encoding="utf-8")
        return real_grant_lock(grant_id, timeout_seconds=timeout_seconds)

    def _controlled_refresh(access_token, *_args, **_kwargs):
        if access_token == "access-a":
            a_entered.write_text("entered", encoding="utf-8")
            deadline = time.monotonic() + 5.0
            while not a_release.exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("timed out waiting to release grant A")
                time.sleep(0.01)
            return {
                "access_token": "access-a-new",
                "refresh_token": "refresh-a-new",
                "last_refresh": "2026-07-13T14:06:00Z",
            }
        b_entered.write_text("entered", encoding="utf-8")
        return {
            "access_token": "access-b-new",
            "refresh_token": "refresh-b-new",
            "last_refresh": "2026-07-13T14:06:01Z",
        }

    monkeypatch.setattr(auth_mod, "_codex_grant_lock", _tracked_grant_lock)
    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _controlled_refresh)

    def _fork_refresh(role: str, account: str, suffix: str) -> int:
        pid = os.fork()
        if pid == 0:
            try:
                os.environ["HERMES_TEST_ROLE"] = role
                auth_mod.refresh_codex_oauth_coordinated(
                    expected_access_token=f"access-{suffix}",
                    expected_refresh_token=f"refresh-{suffix}",
                    credential_id=account,
                    source_auth_path=auth_path,
                    timeout_seconds=1.0,
                )
                os._exit(0)
            except BaseException as exc:
                (tmp_path / f"convoy-error-{role}").write_text(
                    f"{type(exc).__name__}:{getattr(exc, 'code', '')}",
                    encoding="utf-8",
                )
                os._exit(1)
        return pid

    first_a = _fork_refresh("a1", "account-a", "a")
    deadline = time.monotonic() + 2.0
    while not a_entered.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert a_entered.exists()

    second_a = _fork_refresh("a2", "account-a", "a")
    deadline = time.monotonic() + 2.0
    while not a_waiter.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert a_waiter.exists()

    independent_b = _fork_refresh("b", "account-b", "b")
    deadline = time.monotonic() + 0.75
    while not b_entered.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    b_entered_before_a_release = b_entered.exists()

    a_release.write_text("release", encoding="utf-8")
    statuses = [
        os.waitpid(pid, 0)[1] for pid in (first_a, second_a, independent_b)
    ]
    errors = sorted(tmp_path.glob("convoy-error-*"))

    assert b_entered_before_a_release, "grant A waiter convoyed independent grant B"
    assert all(
        os.waitstatus_to_exitcode(status) == 0 for status in statuses
    ), [path.read_text(encoding="utf-8") for path in errors]


def test_fresh_singleton_login_does_not_supersede_unrelated_ambiguous_grant(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    grant_a = "a" * 32
    grant_b = "b" * 32
    store = _codex_store(
        "singleton-access",
        "singleton-refresh",
        grant_id=grant_b,
    )
    store["credential_pool"] = {
        "openai-codex": [
            {
                "id": "account-a",
                "source": "manual:device_code",
                "auth_type": "oauth",
                "access_token": "access-a",
                "refresh_token": "refresh-a",
                "grant_id": grant_a,
            }
        ]
    }
    _write_store(auth_path, store)

    def _ambiguous(*_args, **_kwargs):
        raise auth_mod.AuthError(
            "transport outcome unknown",
            provider="openai-codex",
            code="codex_refresh_transport_error",
            relogin_required=False,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _ambiguous)
    with pytest.raises(auth_mod.AuthError):
        auth_mod.refresh_codex_oauth_coordinated(
            expected_access_token="access-a",
            expected_refresh_token="refresh-a",
            credential_id="account-a",
            source_auth_path=auth_path,
            timeout_seconds=0.1,
        )

    auth_mod._save_codex_tokens(
        {
            "access_token": "singleton-fresh-access",
            "refresh_token": "singleton-fresh-refresh",
        },
        last_refresh="2026-07-13T14:04:00Z",
    )

    assert [path.name for path in _wal_paths(state_dir)] == [
        f"grant-{grant_a}.wal.json"
    ]
    saved = json.loads(auth_path.read_text())
    assert saved["providers"]["openai-codex"]["tokens"]["access_token"] == (
        "singleton-fresh-access"
    )
    rows = saved["credential_pool"]["openai-codex"]
    assert rows == [
        {
            "id": "account-a",
            "source": "manual:device_code",
            "auth_type": "oauth",
            "access_token": "access-a",
            "refresh_token": "refresh-a",
            "grant_id": grant_a,
        }
    ]


def test_codex_429_preserves_generation_and_clears_prepared_wal(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    before = _write_store(
        auth_path,
        _codex_store("access-old", "refresh-old", grant_id="3" * 32),
    )

    def _limited(*_args, **_kwargs):
        raise auth_mod.AuthError(
            "limited",
            provider="openai-codex",
            code=auth_mod.CODEX_RATE_LIMITED_CODE,
            relogin_required=False,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _limited)

    with pytest.raises(auth_mod.AuthError) as exc:
        _refresh(auth_mod, auth_path)

    assert exc.value.code == auth_mod.CODEX_RATE_LIMITED_CODE
    assert auth_path.read_bytes() == before
    assert not _wal_paths(state_dir)


def test_codex_429_retryable_wal_recovers_after_cleanup_crash(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    grant_id = "f" * 32
    before = _write_store(
        auth_path,
        _codex_store("access-old", "refresh-old", grant_id=grant_id),
    )
    calls = {"post": 0}

    def _limited(*_args, **_kwargs):
        calls["post"] += 1
        raise auth_mod.AuthError(
            "limited",
            provider="openai-codex",
            code=auth_mod.CODEX_RATE_LIMITED_CODE,
            relogin_required=False,
        )

    original_clear = auth_mod._clear_codex_refresh_wal
    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _limited)
    monkeypatch.setattr(
        auth_mod,
        "_clear_codex_refresh_wal",
        lambda _grant_id: (_ for _ in ()).throw(
            RuntimeError("simulated crash before retryable WAL cleanup")
        ),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        _refresh(auth_mod, auth_path)

    wal_path = _single_wal_path(state_dir)
    assert json.loads(wal_path.read_text())["state"] == "retryable"
    assert auth_path.read_bytes() == before

    monkeypatch.setattr(auth_mod, "_clear_codex_refresh_wal", original_clear)
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-07-13T14:06:00Z",
        },
    )

    outcome = _refresh(auth_mod, auth_path)

    assert outcome["adopted"] is False
    assert calls["post"] == 1
    assert not _wal_paths(state_dir)


def test_codex_terminal_refresh_quarantines_exact_root_without_profile_shadow(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "default"
    profile.mkdir(parents=True)
    root_auth = root / "auth.json"
    profile_auth = profile / "auth.json"
    _write_store(
        root_auth,
        _codex_store(
            "access-old",
            "refresh-old",
            include_alias=True,
            manual_pair=("manual-access", "manual-refresh"),
        ),
    )
    profile_before = _write_store(profile_auth, {"version": 1, "providers": {}})
    monkeypatch.setenv("HERMES_HOME", str(profile))
    state_dir = tmp_path / "codex-refresh-state"
    monkeypatch.setenv("HERMES_CODEX_REFRESH_STATE_DIR", str(state_dir))

    def _rejected(*_args, **_kwargs):
        raise auth_mod.AuthError(
            "rejected",
            provider="openai-codex",
            code="invalid_grant",
            relogin_required=True,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _rejected)

    with pytest.raises(auth_mod.AuthError) as exc:
        _refresh(auth_mod, root_auth)

    assert exc.value.code == "invalid_grant"
    assert profile_auth.read_bytes() == profile_before
    saved = json.loads(root_auth.read_text())
    assert saved["providers"]["openai-codex"]["tokens"] == {}
    rows = saved["credential_pool"]["openai-codex"]
    assert [row["id"] for row in rows] == ["independent-manual"]
    assert rows[0]["access_token"] == "manual-access"
    assert not _wal_paths(state_dir)


def test_codex_duplicate_generation_across_profiles_rotates_once_and_updates_all(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, _state_dir = _configure_root(tmp_path, monkeypatch)
    root_auth = root / "auth.json"
    profile_auth = root / "profiles" / "coder" / "auth.json"
    _write_store(root_auth, _codex_store("access-old", "refresh-old"))
    _write_store(
        profile_auth,
        _codex_store(
            "access-old",
            "refresh-old",
            include_alias=True,
            manual_pair=("profile-manual-access", "profile-manual-refresh"),
        ),
    )
    calls = {"post": 0}

    def _pure(*_args, **_kwargs):
        calls["post"] += 1
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-07-13T13:02:00Z",
        }

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _pure)

    _refresh(auth_mod, root_auth)

    assert calls["post"] == 1
    for path in (root_auth, profile_auth):
        saved = json.loads(path.read_text())
        assert saved["providers"]["openai-codex"]["tokens"]["access_token"] == "access-new"
    profile_saved = json.loads(profile_auth.read_text())
    rows = {
        row["id"]: row
        for row in profile_saved["credential_pool"]["openai-codex"]
    }
    assert rows["singleton-alias"]["access_token"] == "access-new"
    assert rows["independent-manual"]["access_token"] == "profile-manual-access"


def test_codex_legacy_partial_aliases_share_stable_grant_and_rotate_together(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, _state_dir = _configure_root(tmp_path, monkeypatch)
    root_auth = root / "auth.json"
    profile_auth = root / "profiles" / "coder" / "auth.json"
    _write_store(root_auth, _codex_store("access-old", "refresh-shared"))
    _write_store(
        profile_auth,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "stale-alias",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-stale",
                        "refresh_token": "refresh-shared",
                    },
                    {
                        "id": "independent",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-independent",
                        "refresh_token": "refresh-independent",
                    },
                ]
            },
        },
    )
    calls = {"post": 0}

    def _pure(*_args, **_kwargs):
        calls["post"] += 1
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-07-13T14:00:00Z",
        }

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _pure)

    outcome = auth_mod.refresh_codex_oauth_coordinated(
        expected_access_token="access-old",
        expected_refresh_token="refresh-shared",
        source_auth_path=root_auth,
        timeout_seconds=0.1,
    )

    assert calls["post"] == 1
    assert outcome["adopted"] is False
    assert len(str(outcome["grant_id"])) == 32
    root_saved = json.loads(root_auth.read_text())
    profile_saved = json.loads(profile_auth.read_text())
    assert _codex_grants(root_saved) == {outcome["grant_id"]}
    rows = {
        row["id"]: row
        for row in profile_saved["credential_pool"]["openai-codex"]
    }
    assert rows["stale-alias"]["access_token"] == "access-new"
    assert rows["stale-alias"]["refresh_token"] == "refresh-new"
    assert rows["stale-alias"]["grant_id"] == outcome["grant_id"]
    assert rows["independent"]["access_token"] == "access-independent"
    assert "grant_id" not in rows["independent"]


def test_codex_existing_stable_grant_survives_rotation(tmp_path, monkeypatch):
    import hermes_cli.auth as auth_mod

    root, _state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    grant_id = "a" * 32
    _write_store(
        auth_path,
        _codex_store(
            "access-old",
            "refresh-old",
            include_alias=True,
            grant_id=grant_id,
        ),
    )
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-07-13T14:01:00Z",
        },
    )

    outcome = _refresh(auth_mod, auth_path)

    assert outcome["grant_id"] == grant_id
    assert _codex_grants(json.loads(auth_path.read_text())) == {grant_id}


def test_codex_forged_grant_id_on_distinct_refresh_token_fails_before_post(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, _state_dir = _configure_root(tmp_path, monkeypatch)
    root_auth = root / "auth.json"
    profile_auth = root / "profiles" / "coder" / "auth.json"
    grant_id = "b" * 32
    root_before = _write_store(
        root_auth,
        _codex_store("access-old", "refresh-old", grant_id=grant_id),
    )
    profile_before = _write_store(
        profile_auth,
        _codex_store("other-access", "other-refresh", grant_id=grant_id),
    )
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: pytest.fail("refresh POST must not run"),
    )

    with pytest.raises(auth_mod.AuthStoreConflictError, match="grant"):
        _refresh(auth_mod, root_auth)

    assert root_auth.read_bytes() == root_before
    assert profile_auth.read_bytes() == profile_before


def test_fresh_codex_login_assigns_new_grant_to_singleton_aliases(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, _state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    old_grant = "c" * 32
    _write_store(
        auth_path,
        _codex_store(
            "access-old",
            "refresh-old",
            include_alias=True,
            grant_id=old_grant,
        ),
    )

    auth_mod._save_codex_tokens(
        {
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
        },
        last_refresh="2026-07-13T14:02:00Z",
    )

    saved = json.loads(auth_path.read_text())
    grants = _codex_grants(saved)
    assert len(grants) == 1
    assert old_grant not in grants
    assert len(next(iter(grants))) == 32


def test_codex_refresh_rejects_hardlinked_host_secret_before_post(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    before = _write_store(
        auth_path,
        _codex_store("access-old", "refresh-old", grant_id="4" * 32),
    )
    state_dir.mkdir(mode=0o700)
    external = tmp_path / "external-key"
    external.write_bytes(b"x" * 32)
    (state_dir / "refresh.hmac.key").hardlink_to(external)
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: pytest.fail("refresh POST must not run"),
    )

    with pytest.raises(auth_mod.AuthStoreCorruptError, match="hard links"):
        _refresh(auth_mod, auth_path)

    assert auth_path.read_bytes() == before


def test_codex_refresh_fails_closed_when_private_state_permissions_cannot_be_fixed(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    before = _write_store(auth_path, _codex_store("access-old", "refresh-old"))
    state_dir.mkdir(mode=0o777)
    state_dir.chmod(0o777)
    original_chmod = Path.chmod

    def _ignore_state_dir_chmod(path, mode, *args, **kwargs):
        if path == state_dir:
            return None
        return original_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", _ignore_state_dir_chmod)
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: pytest.fail("refresh POST must not run"),
    )

    with pytest.raises(auth_mod.AuthStoreCorruptError, match="permissions"):
        _refresh(auth_mod, auth_path)

    assert auth_path.read_bytes() == before


def test_codex_prepared_wal_with_missing_hmac_key_never_replays(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    before = _write_store(
        auth_path,
        _codex_store("access-old", "refresh-old", grant_id="6" * 32),
    )
    calls = {"post": 0}

    def _ambiguous(*_args, **_kwargs):
        calls["post"] += 1
        raise auth_mod.AuthError(
            "transport outcome unknown",
            provider="openai-codex",
            code="codex_refresh_transport_error",
            relogin_required=False,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _ambiguous)
    with pytest.raises(auth_mod.AuthError):
        _refresh(auth_mod, auth_path)
    (state_dir / "refresh.hmac.key").unlink()

    with pytest.raises(auth_mod.AuthStoreCorruptError, match="HMAC key"):
        _refresh(auth_mod, auth_path)

    assert calls["post"] == 1
    assert auth_path.read_bytes() == before
    assert _wal_paths(state_dir)
    assert not (state_dir / "refresh.hmac.key").exists()


def test_codex_wal_full_mac_rejects_raw_target_tampering(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(auth_path, _codex_store("access-old", "refresh-old"))
    calls = _leave_rotated_wal(auth_mod, monkeypatch, auth_path)
    wal_path = _single_wal_path(state_dir)
    payload = json.loads(wal_path.read_text(encoding="utf-8"))
    payload["targets"] = [str(tmp_path / "forged" / "auth.json")]
    wal_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: pytest.fail("refresh POST must not run"),
    )

    with pytest.raises(auth_mod.AuthStoreCorruptError, match="MAC"):
        _refresh(auth_mod, auth_path)

    assert calls["post"] == 1
    assert json.loads(auth_path.read_text())["providers"]["openai-codex"]["tokens"]["access_token"] == "access-old"


def test_codex_wal_writer_rejects_target_outside_auth_inventory(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(auth_path, _codex_store("access-old", "refresh-old"))
    outside = tmp_path / "forged" / "auth.json"
    outside_before = _write_store(
        outside,
        _codex_store("access-old", "refresh-old"),
    )
    _leave_rotated_wal(auth_mod, monkeypatch, auth_path)
    wal_path = _single_wal_path(state_dir)
    wal_before = wal_path.read_bytes()
    payload = auth_mod._read_codex_refresh_wal(_wal_grant_id(wal_path))
    payload["targets"].append(str(outside))

    with pytest.raises(
        auth_mod.AuthStoreCorruptError,
        match="outside the auth inventory",
    ):
        auth_mod._write_codex_refresh_wal(payload)

    assert wal_path.read_bytes() == wal_before
    assert outside.read_bytes() == outside_before
    assert json.loads(auth_path.read_text())["providers"]["openai-codex"]["tokens"]["access_token"] == "access-old"


def test_codex_applied_target_marker_is_revalidated_before_wal_cleanup(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(auth_path, _codex_store("access-old", "refresh-old"))
    calls = _leave_rotated_wal(auth_mod, monkeypatch, auth_path)
    wal_path = _single_wal_path(state_dir)
    payload = auth_mod._read_codex_refresh_wal(_wal_grant_id(wal_path))
    payload["state"] = "committing"
    payload["applied_targets"] = [str(auth_path)]
    auth_mod._write_codex_refresh_wal(payload)

    outcome = _refresh(auth_mod, auth_path)

    assert outcome["adopted"] is True
    assert calls["post"] == 1
    assert not _wal_paths(state_dir)
    assert json.loads(auth_path.read_text())["providers"]["openai-codex"]["tokens"]["access_token"] == "access-new"


def test_codex_committed_wal_verifies_every_target_before_cleanup(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(auth_path, _codex_store("access-old", "refresh-old"))
    calls = _leave_rotated_wal(auth_mod, monkeypatch, auth_path)
    wal_path = _single_wal_path(state_dir)
    payload = auth_mod._read_codex_refresh_wal(_wal_grant_id(wal_path))
    payload["state"] = "committed"
    payload["applied_targets"] = [str(auth_path)]
    auth_mod._write_codex_refresh_wal(payload)

    with pytest.raises(auth_mod.AuthStoreConflictError, match="committed"):
        _refresh(auth_mod, auth_path)

    assert calls["post"] == 1
    assert _wal_paths(state_dir)
    assert json.loads(auth_path.read_text())["providers"]["openai-codex"]["tokens"]["access_token"] == "access-old"


def test_codex_rotated_wal_rejects_inconsistent_new_pair_hmac(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(auth_path, _codex_store("access-old", "refresh-old"))
    _leave_rotated_wal(auth_mod, monkeypatch, auth_path)
    wal_path = _single_wal_path(state_dir)
    wal_before = wal_path.read_bytes()
    payload = auth_mod._read_codex_refresh_wal(_wal_grant_id(wal_path))
    payload["new_pair_hmac"] = "0" * 64

    with pytest.raises(auth_mod.AuthStoreCorruptError, match="new pair HMAC"):
        auth_mod._write_codex_refresh_wal(payload)

    assert wal_path.read_bytes() == wal_before
    assert json.loads(auth_path.read_text())["providers"]["openai-codex"]["tokens"]["access_token"] == "access-old"


def test_explicit_fresh_login_supersedes_prepared_wal_without_replay(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(
        auth_path,
        _codex_store(
            "access-old",
            "refresh-old",
            include_alias=True,
            grant_id="5" * 32,
        ),
    )
    calls = {"post": 0}

    def _ambiguous(*_args, **_kwargs):
        calls["post"] += 1
        raise auth_mod.AuthError(
            "transport outcome unknown",
            provider="openai-codex",
            code="codex_refresh_transport_error",
            relogin_required=False,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _ambiguous)
    with pytest.raises(auth_mod.AuthError):
        _refresh(auth_mod, auth_path)

    auth_mod._save_codex_tokens(
        {
            "access_token": "fresh-login-access",
            "refresh_token": "fresh-login-refresh",
        },
        last_refresh="2026-07-13T13:04:00Z",
    )

    assert calls["post"] == 1
    assert not _wal_paths(state_dir)
    saved = json.loads(auth_path.read_text())
    assert saved["providers"]["openai-codex"]["tokens"]["access_token"] == "fresh-login-access"
    assert saved.get("credential_pool", {}).get("openai-codex", []) == []


def test_explicit_codex_logout_supersedes_prepared_wal_without_replay(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(
        auth_path,
        _codex_store("access-old", "refresh-old", include_alias=True),
    )
    calls = {"post": 0}

    def _ambiguous(*_args, **_kwargs):
        calls["post"] += 1
        raise auth_mod.AuthError(
            "transport outcome unknown",
            provider="openai-codex",
            code="codex_refresh_transport_error",
            relogin_required=False,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _ambiguous)
    with pytest.raises(auth_mod.AuthError):
        _refresh(auth_mod, auth_path)

    assert auth_mod.clear_provider_auth("openai-codex") is True

    assert calls["post"] == 1
    assert not _wal_paths(state_dir)
    saved = json.loads(auth_path.read_text())
    assert "openai-codex" not in saved.get("providers", {})
    assert "openai-codex" not in saved.get("credential_pool", {})


def test_create_only_snapshot_ignores_unrelated_ambiguous_codex_wal(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    root_auth = root / "auth.json"
    root_before = _write_store(
        root_auth,
        _codex_store(
            "access-old",
            "refresh-old",
            include_alias=True,
            grant_id="7" * 32,
        ),
    )
    calls = {"post": 0}

    def _ambiguous(*_args, **_kwargs):
        calls["post"] += 1
        raise auth_mod.AuthError(
            "transport outcome unknown",
            provider="openai-codex",
            code="codex_refresh_transport_error",
            relogin_required=False,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _ambiguous)
    with pytest.raises(auth_mod.AuthError):
        _refresh(auth_mod, root_auth)
    wal_path = _single_wal_path(state_dir)
    wal_before = wal_path.read_bytes()
    bootstrap_target = root / "profiles" / "new" / "auth.json"

    saved_path = auth_mod.replace_auth_store_from_snapshot(
        {
            "version": 1,
            "providers": {
                "nous": {
                    "access_token": "bootstrap-access",
                    "refresh_token": "bootstrap-refresh",
                }
            },
        },
        target_path=bootstrap_target,
        actor="docker_bootstrap",
        reason="first boot auth bootstrap",
        require_absent=True,
        supersede_ambiguous=False,
    )

    assert saved_path == bootstrap_target
    assert calls["post"] == 1
    assert root_auth.read_bytes() == root_before
    assert wal_path.read_bytes() == wal_before
    assert json.loads(bootstrap_target.read_text())["providers"]["nous"][
        "access_token"
    ] == "bootstrap-access"


def test_named_logout_ignores_unrelated_root_codex_wal(tmp_path, monkeypatch):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    root_auth = root / "auth.json"
    _write_store(
        root_auth,
        _codex_store(
            "access-old",
            "refresh-old",
            include_alias=True,
            grant_id="c" * 32,
        ),
    )
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            auth_mod.AuthError(
                "transport outcome unknown",
                provider="openai-codex",
                code="codex_refresh_transport_error",
                relogin_required=False,
            )
        ),
    )
    with pytest.raises(auth_mod.AuthError):
        _refresh(auth_mod, root_auth)
    wal_path = _single_wal_path(state_dir)
    root_before = root_auth.read_bytes()
    wal_before = wal_path.read_bytes()

    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)
    _write_store(profile / "auth.json", {"version": 1, "providers": {}})
    monkeypatch.setenv("HERMES_HOME", str(profile))

    assert auth_mod.clear_provider_auth("openai-codex") is False
    assert root_auth.read_bytes() == root_before
    assert wal_path.read_bytes() == wal_before


def test_remove_account_b_ignores_account_a_ambiguous_wal(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod
    from agent.credential_sources import remove_credential_target

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    root_auth = root / "auth.json"
    grant_a = "d" * 32
    grant_b = "e" * 32
    _write_store(
        root_auth,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "account-a",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-a",
                        "refresh_token": "refresh-a",
                        "grant_id": grant_a,
                    },
                    {
                        "id": "account-b",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "access_token": "access-b",
                        "refresh_token": "refresh-b",
                        "grant_id": grant_b,
                    },
                ]
            },
        },
    )
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            auth_mod.AuthError(
                "transport outcome unknown",
                provider="openai-codex",
                code="codex_refresh_transport_error",
                relogin_required=False,
            )
        ),
    )
    with pytest.raises(auth_mod.AuthError):
        auth_mod.refresh_codex_oauth_coordinated(
            expected_access_token="access-a",
            expected_refresh_token="refresh-a",
            credential_id="account-a",
            source_auth_path=root_auth,
            timeout_seconds=0.1,
        )
    wal_path = _single_wal_path(state_dir)
    wal_before = wal_path.read_bytes()

    outcome = remove_credential_target("openai-codex", "account-b")

    assert outcome.removed.id == "account-b"
    saved = json.loads(root_auth.read_text())
    assert [
        row["id"] for row in saved["credential_pool"]["openai-codex"]
    ] == ["account-a"]
    assert wal_path.read_bytes() == wal_before


def test_named_profile_fresh_login_does_not_supersede_root_grant_wal(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    root_auth = root / "auth.json"
    grant_id = "8" * 32
    _write_store(
        root_auth,
        _codex_store(
            "access-old",
            "refresh-old",
            include_alias=True,
            grant_id=grant_id,
        ),
    )
    calls = {"post": 0}

    def _ambiguous(*_args, **_kwargs):
        calls["post"] += 1
        raise auth_mod.AuthError(
            "transport outcome unknown",
            provider="openai-codex",
            code="codex_refresh_transport_error",
            relogin_required=False,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _ambiguous)
    with pytest.raises(auth_mod.AuthError):
        _refresh(auth_mod, root_auth)
    wal_path = _single_wal_path(state_dir)
    root_before = root_auth.read_bytes()
    wal_before = wal_path.read_bytes()

    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)
    profile_auth = profile / "auth.json"
    _write_store(profile_auth, {"version": 1, "providers": {}})
    monkeypatch.setenv("HERMES_HOME", str(profile))

    auth_mod._save_codex_tokens(
        {
            "access_token": "profile-fresh-access",
            "refresh_token": "profile-fresh-refresh",
        },
        last_refresh="2026-07-13T15:05:00Z",
    )

    assert calls["post"] == 1
    assert root_auth.read_bytes() == root_before
    assert wal_path.read_bytes() == wal_before
    saved = json.loads(profile_auth.read_text())
    assert (
        saved["providers"]["openai-codex"]["tokens"]["access_token"]
        == "profile-fresh-access"
    )


def test_first_codex_refresh_fsyncs_new_state_directory_parent_before_post(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(
        auth_path,
        _codex_store(
            "access-old",
            "refresh-old",
            grant_id="9" * 32,
        ),
    )
    real_fsync_directory = auth_mod._fsync_directory
    synced: list[Path] = []

    def _record_fsync(path):
        synced.append(Path(path))
        real_fsync_directory(Path(path))

    def _pure(*_args, **_kwargs):
        assert state_dir.parent in synced
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-07-13T15:06:00Z",
        }

    monkeypatch.setattr(auth_mod, "_fsync_directory", _record_fsync)
    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _pure)

    _refresh(auth_mod, auth_path)

    assert state_dir.parent in synced


def test_native_windows_refresh_uses_platform_durability_without_posix_modes(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(
        auth_path,
        _codex_store(
            "access-old",
            "refresh-old",
            grant_id="b" * 32,
        ),
    )
    monkeypatch.setattr(auth_mod, "_is_native_windows", lambda: True)
    assert auth_mod._codex_private_mode_matches(
        stat.S_IFREG | 0o666,
        stat.S_IRUSR | stat.S_IWUSR,
    )

    real_open = os.open

    def _windows_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise AssertionError("native Windows must not use dir_fd")
        if Path(path).is_dir():
            raise PermissionError("the Windows CRT cannot open directories")
        return real_open(path, flags, mode)

    durable_moves: list[tuple[Path, Path]] = []

    def _windows_replace(source, destination):
        durable_moves.append((Path(source), Path(destination)))
        os.replace(source, destination)

    class _WindowsOsProxy:
        def __getattr__(self, name):
            return getattr(os, name)

        open = staticmethod(_windows_open)

    monkeypatch.setattr(auth_mod, "os", _WindowsOsProxy())
    if os.name != "nt":
        monkeypatch.setattr(
            auth_mod,
            "_windows_durable_replace",
            _windows_replace,
        )
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-07-13T15:06:30Z",
        },
    )

    outcome = _refresh(auth_mod, auth_path)

    assert outcome["adopted"] is False
    saved = json.loads(auth_path.read_text(encoding="utf-8"))
    assert (
        saved["providers"]["openai-codex"]["tokens"]["refresh_token"]
        == "refresh-new"
    )
    if os.name != "nt":
        assert durable_moves
    assert not _wal_paths(state_dir)


def test_terminal_reject_committing_wal_recovers_after_target_crash(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod
    from agent.credential_pool import load_pool

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    root_auth = root / "auth.json"
    profile_auth = root / "profiles" / "mirror" / "auth.json"
    grant_id = "a" * 32
    payload = _codex_store(
        "access-old",
        "refresh-old",
        include_alias=True,
        grant_id=grant_id,
    )
    _write_store(root_auth, payload)
    _write_store(profile_auth, payload)
    monkeypatch.setattr(
        "agent.credential_pool._load_codex_route_policy",
        lambda: {"mode": "auto"},
    )
    pool = load_pool("openai-codex")
    assert len(pool.entries()) == 1
    pool._current_id = pool.entries()[0].id
    posts = {"count": 0}

    def _terminal(*_args, **_kwargs):
        posts["count"] += 1
        raise auth_mod.AuthError(
            "revoked",
            provider="openai-codex",
            code="invalid_grant",
            relogin_required=True,
        )

    real_write = auth_mod._write_codex_refresh_wal
    crashed = {"done": False}

    def _crash_after_first_committing(wal):
        real_write(wal)
        if (
            wal.get("state") == "committing"
            and len(wal.get("applied_targets", [])) == 1
            and not crashed["done"]
        ):
            crashed["done"] = True
            raise RuntimeError("simulated crash after first terminal target")

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _terminal)
    monkeypatch.setattr(
        auth_mod, "_write_codex_refresh_wal", _crash_after_first_committing
    )

    with pytest.raises(RuntimeError, match="first terminal target"):
        pool.try_refresh_current()

    assert crashed["done"] is True
    assert _wal_paths(state_dir)
    monkeypatch.setattr(auth_mod, "_write_codex_refresh_wal", real_write)
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: pytest.fail("terminal recovery must not POST"),
    )

    assert pool.try_refresh_current() is None

    assert posts["count"] == 1
    assert not _wal_paths(state_dir)
    assert pool.entries() == []
    assert pool.select() is None
    for auth_path in (root_auth, profile_auth):
        saved = json.loads(auth_path.read_text())
        assert saved["providers"]["openai-codex"]["tokens"] == {}
        assert saved.get("credential_pool", {}).get("openai-codex", []) == []


def test_profile_retire_recovers_terminal_wal_after_primary_grant_removal(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    root_auth = root / "auth.json"
    profile_dir = root / "profiles" / "oldname"
    profile_auth = profile_dir / "auth.json"
    renamed_dir = root / "profiles" / "newname"
    grant_id = "c" * 32
    payload = _codex_store(
        "access-old",
        "refresh-old",
        include_alias=True,
        grant_id=grant_id,
    )
    _write_store(root_auth, payload)
    _write_store(profile_auth, payload)

    def _terminal(*_args, **_kwargs):
        raise auth_mod.AuthError(
            "revoked",
            provider="openai-codex",
            code="invalid_grant",
            relogin_required=True,
        )

    real_apply = auth_mod._apply_codex_generation_to_path
    crashed = {"done": False}

    def _crash_after_primary_apply(auth_path, *args, **kwargs):
        result = real_apply(auth_path, *args, **kwargs)
        if (
            Path(auth_path) == profile_auth
            and kwargs.get("new_tokens") is None
            and not crashed["done"]
        ):
            crashed["done"] = True
            raise RuntimeError(
                "simulated crash after terminal primary removal"
            )
        return result

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _terminal)
    monkeypatch.setattr(
        auth_mod,
        "_apply_codex_generation_to_path",
        _crash_after_primary_apply,
    )

    with pytest.raises(RuntimeError, match="terminal primary removal"):
        auth_mod.refresh_codex_oauth_coordinated(
            expected_access_token="access-old",
            expected_refresh_token="refresh-old",
            source_auth_path=profile_auth,
            timeout_seconds=0.1,
        )

    assert crashed["done"] is True
    assert _wal_paths(state_dir)
    assert not _codex_grants(json.loads(profile_auth.read_text()))
    assert _codex_grants(json.loads(root_auth.read_text())) == {grant_id}

    monkeypatch.setattr(
        auth_mod,
        "_apply_codex_generation_to_path",
        real_apply,
    )
    with auth_mod._retire_codex_auth_store(
        profile_auth,
        reason="profile_rename",
    ):
        profile_dir.rename(renamed_dir)

    assert renamed_dir.is_dir()
    assert not _wal_paths(state_dir)
    assert not _codex_grants(json.loads(root_auth.read_text()))


def test_load_pool_retries_if_codex_singleton_rotates_before_seed_write(
    tmp_path, monkeypatch
):
    import agent.credential_pool as pool_mod
    import hermes_cli.auth as auth_mod

    root, _state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(auth_path, _codex_store("access-old", "refresh-old"))
    monkeypatch.setattr(
        pool_mod,
        "_load_codex_route_policy",
        lambda: {"mode": "auto"},
    )
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-07-13T16:00:00Z",
        },
    )
    real_write = pool_mod.write_credential_pool
    raced = {"done": False}

    def _rotate_before_seed_write(*args, **kwargs):
        if not raced["done"]:
            raced["done"] = True
            _refresh(auth_mod, auth_path)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(
        pool_mod,
        "write_credential_pool",
        _rotate_before_seed_write,
    )

    pool = pool_mod.load_pool("openai-codex")

    assert raced["done"] is True
    assert len(pool.entries()) == 1
    saved = json.loads(auth_path.read_text())
    singleton = saved["providers"]["openai-codex"]
    row = saved["credential_pool"]["openai-codex"][0]
    assert singleton["tokens"]["access_token"] == "access-new"
    assert singleton["tokens"]["refresh_token"] == "refresh-new"
    assert row["access_token"] == "access-new"
    assert row["refresh_token"] == "refresh-new"
    assert row["grant_id"] == singleton["grant_id"]


def test_snapshot_restore_cannot_resurrect_prepared_codex_generation(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    original = _codex_store(
        "access-old",
        "refresh-old",
        include_alias=True,
        grant_id="b" * 32,
    )
    _write_store(auth_path, original)
    calls = {"post": 0}

    def _ambiguous(*_args, **_kwargs):
        calls["post"] += 1
        raise auth_mod.AuthError(
            "transport outcome unknown",
            provider="openai-codex",
            code="codex_refresh_transport_error",
            relogin_required=False,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _ambiguous)
    with pytest.raises(auth_mod.AuthError):
        _refresh(auth_mod, auth_path)
    wal_path = _single_wal_path(state_dir)
    wal_before = wal_path.read_bytes()
    live_before = auth_path.read_bytes()

    with pytest.raises(
        auth_mod.AuthStoreConflictError,
        match="ambiguous Codex refresh generation",
    ):
        auth_mod.replace_auth_store_from_snapshot(
            original,
            target_path=auth_path,
            actor="test.snapshot_restore",
            reason="regression",
            allow_same_host_codex_oauth=True,
        )

    assert calls["post"] == 1
    assert auth_path.read_bytes() == live_before
    assert wal_path.read_bytes() == wal_before


def test_snapshot_restore_cannot_roll_back_rotated_codex_wal(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    original = _codex_store(
        "access-old",
        "refresh-old",
        include_alias=True,
        grant_id="c" * 32,
    )
    _write_store(auth_path, original)
    _leave_rotated_wal(auth_mod, monkeypatch, auth_path)
    wal_path = _single_wal_path(state_dir)
    wal_before = wal_path.read_bytes()
    live_before = auth_path.read_bytes()

    with pytest.raises(
        auth_mod.AuthStoreConflictError,
        match="ambiguous Codex refresh generation",
    ):
        auth_mod.replace_auth_store_from_snapshot(
            original,
            target_path=auth_path,
            actor="test.snapshot_restore",
            reason="rotated rollback regression",
            allow_same_host_codex_oauth=True,
        )

    assert auth_path.read_bytes() == live_before
    assert wal_path.read_bytes() == wal_before


def test_snapshot_restore_scans_wal_when_legacy_target_is_missing(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    _write_store(auth_path, _codex_store("access-old", "refresh-old"))
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            auth_mod.AuthError(
                "transport outcome unknown",
                provider="openai-codex",
                code="codex_refresh_transport_error",
                relogin_required=False,
            )
        ),
    )
    with pytest.raises(auth_mod.AuthError):
        _refresh(auth_mod, auth_path)
    wal_path = _single_wal_path(state_dir)
    wal_before = wal_path.read_bytes()
    auth_path.unlink()
    legacy = _codex_store("access-old", "refresh-old")

    with pytest.raises(
        auth_mod.AuthStoreConflictError,
        match="ambiguous Codex refresh generation",
    ):
        auth_mod.replace_auth_store_from_snapshot(
            legacy,
            target_path=auth_path,
            actor="test.snapshot_restore",
            reason="missing legacy target regression",
            allow_same_host_codex_oauth=True,
        )

    assert not auth_path.exists()
    assert wal_path.read_bytes() == wal_before


def test_snapshot_restore_rejects_same_grant_token_rollback_without_wal(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    grant_id = "d" * 32
    current = _codex_store(
        "access-new",
        "refresh-new",
        include_alias=True,
        grant_id=grant_id,
    )
    before = _write_store(auth_path, current)
    stale = _codex_store(
        "access-old",
        "refresh-old",
        include_alias=True,
        grant_id=grant_id,
    )

    with pytest.raises(
        auth_mod.AuthStoreConflictError,
        match="older Codex generation",
    ):
        auth_mod.replace_auth_store_from_snapshot(
            stale,
            target_path=auth_path,
            actor="test.snapshot_restore",
            reason="same grant rollback regression",
            allow_same_host_codex_oauth=True,
        )

    assert auth_path.read_bytes() == before
    assert not _wal_paths(state_dir)


def test_fresh_host_snapshot_restore_excludes_codex_oauth_by_default(
    tmp_path, monkeypatch
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    candidate = _codex_store(
        "access-old",
        "refresh-old",
        include_alias=True,
        grant_id="e" * 32,
    )
    candidate["providers"]["nous"] = {
        "access_token": "nous-access",
        "refresh_token": "nous-refresh",
    }

    auth_mod.replace_auth_store_from_snapshot(
        candidate,
        target_path=auth_path,
        actor="test.fresh_host_restore",
        reason="fresh host regression",
        require_absent=True,
        supersede_ambiguous=False,
    )

    saved = json.loads(auth_path.read_text())
    assert "openai-codex" not in saved["providers"]
    assert "openai-codex" not in saved.get("credential_pool", {})
    assert saved["providers"]["nous"]["refresh_token"] == "nous-refresh"
    assert not _wal_paths(state_dir)


@pytest.mark.parametrize("symlink_profiles_root", [False, True])
def test_codex_inventory_rejects_symlinked_profile_paths_before_mutation(
    tmp_path, monkeypatch, symlink_profiles_root
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    root_before = _write_store(
        auth_path,
        _codex_store("access-old", "refresh-old"),
    )
    outside_profile = tmp_path / "outside-profile"
    outside_auth = outside_profile / "auth.json"
    outside_before = _write_store(
        outside_auth,
        _codex_store("access-old", "refresh-old"),
    )
    try:
        if symlink_profiles_root:
            outside_profiles = tmp_path / "outside-profiles"
            outside_profiles.mkdir()
            outside_profile.rename(outside_profiles / "mirror")
            outside_profile = outside_profiles / "mirror"
            outside_auth = outside_profile / "auth.json"
            (root / "profiles").symlink_to(
                outside_profiles,
                target_is_directory=True,
            )
        else:
            (root / "profiles").mkdir()
            (root / "profiles" / "mirror").symlink_to(
                outside_profile,
                target_is_directory=True,
            )
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: pytest.fail("refresh POST must not run"),
    )

    with pytest.raises(auth_mod.AuthStoreConflictError, match="symlink"):
        _refresh(auth_mod, auth_path)

    assert auth_path.read_bytes() == root_before
    assert outside_auth.read_bytes() == outside_before
    assert not _wal_paths(state_dir)


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_codex_refresh_rejects_linked_primary_auth_before_mutation(
    tmp_path, monkeypatch, link_kind
):
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    outside = tmp_path / "outside-auth.json"
    outside_before = _write_store(
        outside,
        _codex_store("access-old", "refresh-old"),
    )
    try:
        if link_kind == "symlink":
            auth_path.symlink_to(outside)
        else:
            auth_path.hardlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"{link_kind} unavailable: {exc}")
    original_inode = auth_path.lstat().st_ino
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: pytest.fail("refresh POST must not run"),
    )

    expected = "symlink" if link_kind == "symlink" else "hard links"
    with pytest.raises(auth_mod.AuthStoreConflictError, match=expected):
        _refresh(auth_mod, auth_path)

    assert outside.read_bytes() == outside_before
    assert auth_path.lstat().st_ino == original_inode
    assert (auth_path.is_symlink()) is (link_kind == "symlink")
    assert not _wal_paths(state_dir)


def test_dead_ttl_prune_cannot_remove_codex_row_during_rotated_wal(
    tmp_path, monkeypatch
):
    import agent.credential_pool as pool_mod
    import hermes_cli.auth as auth_mod

    root, state_dir = _configure_root(tmp_path, monkeypatch)
    auth_path = root / "auth.json"
    grant_id = "1" * 32
    _write_store(
        auth_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "manual-dead",
                        "label": "manual-dead",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "priority": 0,
                        "access_token": "access-old",
                        "refresh_token": "refresh-old",
                        "grant_id": grant_id,
                        "last_status": "dead",
                        "last_status_at": time.time()
                        - pool_mod.DEAD_MANUAL_PRUNE_TTL_SECONDS
                        - 60,
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(
        pool_mod,
        "_load_codex_route_policy",
        lambda: {"mode": "auto"},
    )
    pool = pool_mod.load_pool("openai-codex")
    monkeypatch.setattr(
        auth_mod,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "last_refresh": "2026-07-13T17:30:00Z",
        },
    )
    real_finish = auth_mod._finish_codex_wal_commit
    monkeypatch.setattr(
        auth_mod,
        "_finish_codex_wal_commit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated crash before rotated commit")
        ),
    )
    with pytest.raises(RuntimeError, match="before rotated commit"):
        auth_mod.refresh_codex_oauth_coordinated(
            expected_access_token="access-old",
            expected_refresh_token="refresh-old",
            credential_id="manual-dead",
            source_auth_path=auth_path,
            timeout_seconds=0.1,
        )
    assert _wal_paths(state_dir)
    monkeypatch.setattr(
        auth_mod,
        "_finish_codex_wal_commit",
        real_finish,
    )

    with pytest.raises(auth_mod.AuthStoreConflictError):
        pool.select()

    saved = json.loads(auth_path.read_text())
    row = saved["credential_pool"]["openai-codex"][0]
    assert row["id"] == "manual-dead"
    assert row["access_token"] == "access-new"
    assert row["refresh_token"] == "refresh-new"
    assert not _wal_paths(state_dir)
