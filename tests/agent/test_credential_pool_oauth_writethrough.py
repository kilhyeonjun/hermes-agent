"""Regression tests for credential-pool OAuth refresh write-through to root.

Companion to ``tests/hermes_cli/test_xai_oauth_writethrough.py``. That file
covers the *non-pool* xAI refresh path (``_save_xai_oauth_tokens``). These
cover the **credential-pool** refresh path
(``CredentialPool._sync_device_code_entry_to_auth_store``): when a profile
that has no own ``providers.<id>`` block refreshes — via the pool — a rotating
OAuth grant it resolved from the global-root fallback, the rotated chain must
be written back to the global root too. Otherwise root keeps a revoked refresh
token and every other profile reading root's stale grant dies with
``refresh_token_reused`` / ``invalid_grant`` once its access token expires
(issue #48415, the Codex/xAI analog of #43589).

The tests drive the real ``_sync_device_code_entry_to_auth_store`` against
real on-disk auth stores (profile + root under ``tmp_path``) rather than
mocking the save boundary, so they exercise the actual atomic write path.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent import credential_pool as CP
from agent.credential_pool import (
    AUTH_TYPE_OAUTH,
    CredentialPool,
    PooledCredential,
)
from hermes_cli import auth as A


def _write_store(path, store):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store), encoding="utf-8")


def _read_store(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _entry(
    provider: str,
    *,
    id: str,
    access_token: str,
    refresh_token: str,
    grant_id: str | None = None,
):
    return PooledCredential(
        provider=provider,
        id=id,
        label="cred",
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source="device_code",
        access_token=access_token,
        refresh_token=refresh_token,
        grant_id=grant_id,
    )


@pytest.fixture
def profile_and_root(tmp_path, monkeypatch):
    """Wire a profile auth store + a distinct global-root auth store on disk.

    The pytest seat belt in ``_write_through_provider_state_to_global_root``
    only refuses the *real* user's ``$HOME/.hermes/auth.json``; a tmp_path
    root is allowed, so point HOME away from the tmp root to keep the guard
    from tripping on these fixtures.
    """
    profile_path = tmp_path / "profiles" / "work" / "auth.json"
    root_path = tmp_path / "root" / "auth.json"

    monkeypatch.setattr(A, "_auth_file_path", lambda: profile_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: root_path)
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-root"))
    return profile_path, root_path


@pytest.mark.parametrize(
    "provider",
    ["openai-codex", "xai-oauth"],
)
def test_pool_refresh_updates_exact_root_source_without_profile_shadow(
    profile_and_root, provider
):
    """A root-owned pool updates root directly and never creates a shadow."""
    profile_path, root_path = profile_and_root
    # Profile has NO own provider block (reads root via fallback).
    _write_store(profile_path, {"version": 1, "providers": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                provider: {
                    "tokens": {
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    }
                }
            },
        },
    )

    profile_before = profile_path.read_bytes()
    pool = CredentialPool(provider, [], source_auth_path=root_path)
    pool._sync_device_code_entry_to_auth_store(
        _entry(provider, id="e1", access_token="new-access", refresh_token="new-refresh")
    )

    assert profile_path.read_bytes() == profile_before
    root = _read_store(root_path)
    assert root["providers"][provider]["tokens"]["access_token"] == "new-access"
    assert root["providers"][provider]["tokens"]["refresh_token"] == "new-refresh"


@pytest.mark.parametrize(
    "provider",
    ["openai-codex", "xai-oauth"],
)
def test_pool_refresh_does_not_touch_root_when_profile_shadows(
    profile_and_root, provider
):
    """A profile that genuinely shadows root must NOT clobber the root grant."""
    profile_path, root_path = profile_and_root
    # Profile has its OWN provider block: it shadows root legitimately.
    _write_store(
        profile_path,
        {
            "version": 1,
            "providers": {
                provider: {
                    "tokens": {
                        "access_token": "profile-old",
                        "refresh_token": "profile-old-refresh",
                    }
                }
            },
        },
    )
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                provider: {
                    "tokens": {
                        "access_token": "root-untouched",
                        "refresh_token": "root-untouched-refresh",
                    }
                }
            },
        },
    )

    pool = CredentialPool(provider, [], source_auth_path=profile_path)
    pool._sync_device_code_entry_to_auth_store(
        _entry(
            provider,
            id="e2",
            access_token="profile-new",
            refresh_token="profile-new-refresh",
        )
    )

    profile = _read_store(profile_path)
    assert (
        profile["providers"][provider]["tokens"]["refresh_token"]
        == "profile-new-refresh"
    )

    # Root keeps its own grant — write-through must not run when the profile
    # owns the block.
    root = _read_store(root_path)
    assert (
        root["providers"][provider]["tokens"]["refresh_token"]
        == "root-untouched-refresh"
    )


def test_exact_source_sync_refuses_stale_same_provider_state(profile_and_root):
    """Exact-source CAS must not let an old chain replace a newer root one."""
    _profile_path, root_path = profile_and_root
    old_state = {
        "tokens": {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
        }
    }
    newer_state = {
        "tokens": {
            "access_token": "peer-access",
            "refresh_token": "peer-refresh",
        }
    }
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {"openai-codex": newer_state},
        },
    )

    pool = CredentialPool(
        "openai-codex",
        [],
        source_auth_path=root_path,
    )
    with pytest.raises(A.AuthStoreConflictError, match="changed before"):
        pool._sync_device_code_entry_to_auth_store(
            _entry(
                "openai-codex",
                id="stale",
                access_token="stale-writer-access",
                refresh_token="stale-writer-refresh",
            ),
            expected_token_pair_fingerprint=(
                A.provider_oauth_token_pair_fingerprint(
                    "openai-codex",
                    old_state,
                )
            ),
        )

    root = _read_store(root_path)
    assert root["providers"]["openai-codex"] == newer_state


def test_codex_pool_refresh_posts_under_grant_not_inventory_lock(monkeypatch, tmp_path):
    """Network runs under one grant lock, outside inventory and auth locks.

    Codex refresh tokens are single-use. If two Hermes processes both read the
    same on-disk token and both POST it, the loser gets ``refresh_token_reused``.
    The grant-scoped flock keeps that generation single-flight, while releasing
    the short host inventory lock before network I/O lets independent grants
    refresh concurrently.

    Source auth locks are only for short revalidation/commit sections; holding
    one across network I/O would block unrelated auth mutations.
    """
    provider = "openai-codex"
    profile_path = tmp_path / "auth.json"
    monkeypatch.setattr(A, "_auth_file_path", lambda: profile_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-root"))

    lock_held: dict = {
        "auth_during_post": None,
        "refresh_during_post": None,
        "grant_during_post": None,
    }
    real_lock = A._auth_store_lock
    real_refresh_lock = A._codex_refresh_lock

    depth = {"auth": 0, "refresh": 0, "grant": 0}

    import contextlib

    @contextlib.contextmanager
    def tracking_lock(*args, **kwargs):
        try:
            with real_lock(*args, **kwargs):
                depth["auth"] += 1
                yield
        finally:
            depth["auth"] -= 1

    @contextlib.contextmanager
    def tracking_refresh_lock(*args, **kwargs):
        with real_refresh_lock(*args, **kwargs) as scope:
            depth["refresh"] += 1
            handed_off = {"value": False}
            real_handoff = scope.handoff_to_grants

            def _tracking_handoff(*handoff_args, **handoff_kwargs):
                real_handoff(*handoff_args, **handoff_kwargs)
                depth["refresh"] -= 1
                depth["grant"] += 1
                handed_off["value"] = True

            scope.handoff_to_grants = _tracking_handoff
            try:
                yield scope
            finally:
                if handed_off["value"]:
                    depth["grant"] -= 1
                else:
                    depth["refresh"] -= 1

    monkeypatch.setattr(A, "_auth_store_lock", tracking_lock)
    monkeypatch.setattr(A, "_codex_refresh_lock", tracking_refresh_lock)
    # credential_pool imported _auth_store_lock by name; patch that binding too.
    monkeypatch.setattr(CP, "_auth_store_lock", tracking_lock)

    def fake_refresh(access_token, refresh_token, **kwargs):
        lock_held["auth_during_post"] = depth["auth"] > 0
        lock_held["refresh_during_post"] = depth["refresh"] > 0
        lock_held["grant_during_post"] = depth["grant"] > 0
        return {
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "last_refresh": "2020-01-02T00:00:00Z",
        }

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", fake_refresh)

    entry = _entry(
        provider,
        id="codex-1",
        access_token="stale-access",
        refresh_token="stale-refresh",
    )
    _write_store(
        profile_path,
        {
            "version": 1,
            "providers": {
                provider: {
                    "tokens": {
                        "access_token": "stale-access",
                        "refresh_token": "stale-refresh",
                    }
                }
            },
            "credential_pool": {provider: [entry.to_dict()]},
        },
    )
    pool = CredentialPool(
        provider,
        [entry],
        source_auth_path=profile_path,
    )

    refreshed = pool._refresh_entry(entry, force=True)

    assert refreshed is not None
    assert refreshed.access_token == "rotated-access"
    assert refreshed.refresh_token == "rotated-refresh"
    assert lock_held["refresh_during_post"] is False
    assert lock_held["grant_during_post"] is True
    assert lock_held["auth_during_post"] is False


def test_two_codex_pool_force_waiters_issue_one_refresh_post(
    monkeypatch,
    tmp_path,
):
    """Host-wide waiters adopt one rotated Codex generation."""
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(A, "_auth_file_path", lambda: auth_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: None)
    entry = _entry(
        "openai-codex",
        id="shared-codex",
        access_token="old-access",
        refresh_token="old-refresh",
        grant_id="a" * 32,
    )
    _write_store(
        auth_path,
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    }
                }
            },
            "credential_pool": {"openai-codex": [entry.to_dict()]},
        },
    )
    pools = [
        CredentialPool(
            "openai-codex",
            [entry],
            source_auth_path=auth_path,
        )
        for _ in range(2)
    ]
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def _refresh(access_token, refresh_token, **_kwargs):
        calls.append((access_token, refresh_token))
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "last_refresh": "2026-07-13T12:34:56Z",
        }

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", _refresh)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(pools[0]._refresh_entry, entry, force=True)
        assert entered.wait(2)
        second = executor.submit(pools[1]._refresh_entry, entry, force=True)
        time.sleep(0.05)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert calls == [("old-access", "old-refresh")]
    assert {result.refresh_token for result in results if result} == {
        "new-refresh"
    }
    assert {result.grant_id for result in results if result} == {"a" * 32}
    saved = json.loads(auth_path.read_text())
    assert saved["providers"]["openai-codex"]["grant_id"] == "a" * 32
    assert saved["credential_pool"]["openai-codex"][0]["grant_id"] == "a" * 32


@pytest.mark.parametrize(
    ("provider", "refresh_name"),
    [
        ("openai-codex", "refresh_codex_oauth_pure"),
        ("xai-oauth", "refresh_xai_oauth_pure"),
    ],
)
def test_pool_refresh_actual_caller_cannot_roll_back_peer_root_rotation(
    profile_and_root,
    monkeypatch,
    provider,
    refresh_name,
):
    """POST-start pair, not a later root read, must guard write-through."""
    profile_path, root_path = profile_and_root
    _write_store(profile_path, {"version": 1, "providers": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                provider: {
                    "tokens": {
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    }
                }
            },
            "credential_pool": {
                provider: [
                    {
                        "id": f"{provider}-actual-caller",
                        "label": "cred",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "device_code",
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    }
                ]
            },
        },
    )

    peer_state = {
        "tokens": {
            "access_token": "peer-access",
            "refresh_token": "peer-refresh",
        }
    }

    def _refresh(access_token, refresh_token, **_kwargs):
        assert (access_token, refresh_token) == ("old-access", "old-refresh")
        _write_store(
            root_path,
            {
                "version": 1,
                "providers": {provider: peer_state},
            },
        )
        return {
            "access_token": "stale-writer-access",
            "refresh_token": "stale-writer-refresh",
            "last_refresh": "2026-07-13T12:34:56Z",
        }

    monkeypatch.setattr(A, refresh_name, _refresh)
    entry = _entry(
        provider,
        id=f"{provider}-actual-caller",
        access_token="old-access",
        refresh_token="old-refresh",
    )
    pool = CredentialPool(
        provider,
        [entry],
        source_auth_path=root_path,
    )

    with pytest.raises(A.AuthStoreConflictError):
        pool._refresh_entry(entry, force=True)
    stored = _read_store(root_path)
    assert stored["providers"][provider] == peer_state
    assert not stored.get("credential_pool", {}).get(provider)


def test_two_xai_pool_force_waiters_issue_one_refresh_post(
    monkeypatch,
    tmp_path,
):
    """Waiters adopt the rotated generation instead of replaying it."""
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(A, "_auth_file_path", lambda: auth_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-root"))
    _write_store(
        auth_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "xai-oauth": [
                    {
                        "id": "shared-xai",
                        "label": "shared",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    }
                ]
            },
        },
    )
    entry = _entry(
        "xai-oauth",
        id="shared-xai",
        access_token="old-access",
        refresh_token="old-refresh",
    )
    entry = replace(entry, source="manual")
    pools = [CredentialPool("xai-oauth", [entry]) for _ in range(2)]
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def _refresh(access_token, refresh_token, **_kwargs):
        calls.append((access_token, refresh_token))
        entered.set()
        assert release.wait(2)
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "Bearer",
            "last_refresh": "2026-07-13T12:34:56Z",
        }

    monkeypatch.setattr(A, "refresh_xai_oauth_pure", _refresh)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(pools[0]._refresh_entry, entry, force=True)
        assert entered.wait(2)
        second = executor.submit(pools[1]._refresh_entry, entry, force=True)
        time.sleep(0.05)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert calls == [("old-access", "old-refresh")]
    assert all(result is not None for result in results), results
    assert {result.refresh_token for result in results} == {"new-refresh"}
    stored = _read_store(auth_path)["credential_pool"]["xai-oauth"][0]
    assert stored["refresh_token"] == "new-refresh"


def test_xai_waiter_budget_covers_discovery_plus_refresh(monkeypatch, tmp_path):
    """A waiter survives two timeout-sized phases held by the winner."""
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(A, "_auth_file_path", lambda: auth_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: None)
    monkeypatch.setattr(A, "AUTH_LOCK_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(A, "XAI_REFRESH_LOCK_GRACE_SECONDS", 0.01)
    _write_store(
        auth_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "xai-oauth": [
                    {
                        "id": "shared-xai",
                        "label": "shared",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    }
                ]
            },
        },
    )
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def _refresh(*_args, **_kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(1)
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "Bearer",
            "last_refresh": "2026-07-13T12:34:56Z",
        }

    monkeypatch.setattr(A, "refresh_xai_oauth_pure", _refresh)

    def _coordinated():
        return A.refresh_xai_oauth_coordinated(
            expected_access_token="old-access",
            expected_refresh_token="old-refresh",
            credential_id="shared-xai",
            source_auth_path=auth_path,
            timeout_seconds=0.05,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_coordinated)
        assert entered.wait(1)
        second = executor.submit(_coordinated)
        # 80ms is longer than timeout+grace (60ms) but shorter than the
        # discovery+POST budget (110ms).
        time.sleep(0.08)
        release.set()
        results = [first.result(timeout=1), second.result(timeout=1)]

    assert calls == [True]
    assert {result["tokens"]["refresh_token"] for result in results} == {
        "new-refresh"
    }


def _write_xai_alias_store(auth_path):
    _write_store(
        auth_path,
        {
            "version": 1,
            "providers": {
                "xai-oauth": {
                    "tokens": {
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                        "token_type": "Bearer",
                    },
                    "discovery": {
                        "token_endpoint": "https://auth.x.ai/oauth2/token"
                    },
                }
            },
            "credential_pool": {
                "xai-oauth": [
                    {
                        "id": "manual-owner",
                        "label": "manual-owner",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:xai_pkce",
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    },
                    {
                        "id": "device-alias",
                        "label": "device-alias",
                        "auth_type": "oauth",
                        "priority": 1,
                        "source": "device_code",
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                        "last_status": "exhausted",
                        "last_error_code": "rate_limit_exceeded",
                        "last_error_reset_at": time.time() + 3600,
                    },
                    {
                        "id": "other-manual",
                        "label": "other-manual",
                        "auth_type": "oauth",
                        "priority": 2,
                        "source": "manual",
                        "access_token": "other-access",
                        "refresh_token": "other-refresh",
                    },
                ]
            },
        },
    )


def test_xai_pool_refresh_reloads_all_matching_aliases(monkeypatch, tmp_path):
    """Disk aliases and the live pool must share one refreshed generation."""
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(A, "_auth_file_path", lambda: auth_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: None)
    _write_xai_alias_store(auth_path)

    def _refresh(*_args, **_kwargs):
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "Bearer",
            "last_refresh": "2026-07-13T12:34:56Z",
        }

    monkeypatch.setattr(A, "refresh_xai_oauth_pure", _refresh)
    pool = CP.load_pool("xai-oauth")
    owner = next(row for row in pool.entries() if row.id == "manual-owner")

    refreshed = pool._refresh_entry(owner, force=True)

    assert refreshed is not None
    rows = {row.id: row for row in pool.entries()}
    for row_id in ("manual-owner", "device-alias"):
        assert rows[row_id].access_token == "new-access"
        assert rows[row_id].refresh_token == "new-refresh"
        assert rows[row_id].last_status is None
        assert rows[row_id].last_error_reset_at is None
    assert rows["other-manual"].refresh_token == "other-refresh"
    # A later persistence must use disk-current base fingerprints rather than
    # conflict with aliases the coordinator already committed.
    pool._persist()


def test_xai_pool_terminal_refresh_reloads_all_removed_aliases(
    monkeypatch,
    tmp_path,
):
    """Terminal quarantine removes every matching alias in disk and memory."""
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(A, "_auth_file_path", lambda: auth_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: None)
    _write_xai_alias_store(auth_path)

    def _terminal(*_args, **_kwargs):
        raise A.AuthError(
            "revoked",
            provider="xai-oauth",
            code="xai_refresh_failed",
            relogin_required=True,
        )

    monkeypatch.setattr(A, "refresh_xai_oauth_pure", _terminal)
    pool = CP.load_pool("xai-oauth")
    owner = next(row for row in pool.entries() if row.id == "manual-owner")

    assert pool._refresh_entry(owner, force=True) is None
    assert [row.id for row in pool.entries()] == ["other-manual"]
    stored = _read_store(auth_path)["credential_pool"]["xai-oauth"]
    assert [row["id"] for row in stored] == ["other-manual"]
    pool._persist()
