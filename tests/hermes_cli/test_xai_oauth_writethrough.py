"""Regression tests for xAI OAuth refresh write-through to the global root.

Companion to ``test_xai_oauth_profile_auth.py``. That file covers the READ
fallback (profile -> credential pool -> global root). These cover the WRITE
side: when a profile that has no own ``providers.xai-oauth`` block refreshes
the (rotating) grant it resolved from the root fallback, the rotated tokens
must be written back to the global root too. Otherwise root keeps a revoked
refresh token and every other profile reading root's stale grant dies with
``invalid_grant`` once its access token expires (issue #43589).

The tests drive the real ``_save_xai_oauth_tokens`` against real on-disk auth
stores (profile + root under ``tmp_path``) rather than mocking the save
boundary, so they exercise the actual atomic write path.
"""

import json

import pytest

from hermes_cli import auth


def _write_store(path, store):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store), encoding="utf-8")


def _read_store(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def profile_and_root(tmp_path, monkeypatch):
    """Wire a profile auth store + a distinct global-root auth store on disk.

    Returns (profile_path, root_path). The pytest seat belt in
    ``_write_through_xai_oauth_to_global_root`` only refuses the *real* user's
    ``$HOME/.hermes/auth.json``; a tmp_path root is allowed, so we point HOME
    away from the tmp root to keep the guard from tripping on these fixtures.
    """
    profile_path = tmp_path / "profiles" / "work" / "auth.json"
    root_path = tmp_path / "root" / "auth.json"

    monkeypatch.setattr(auth, "_auth_file_path", lambda: profile_path)
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: root_path)
    # Keep the pytest write seat belt from matching our tmp root.
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-root"))
    return profile_path, root_path


def test_fresh_save_creates_profile_state_without_touching_root(profile_and_root):
    """Fresh device login belongs only to the active profile."""
    profile_path, root_path = profile_and_root
    # Profile has NO own xai-oauth block (reads root via fallback).
    _write_store(profile_path, {"version": 1, "providers": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                "xai-oauth": {
                    "tokens": {
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    }
                }
            },
        },
    )

    rotated = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "token_type": "Bearer",
    }
    auth._save_xai_oauth_tokens(rotated)

    # Profile got the rotated chain (existing behavior).
    profile = _read_store(profile_path)
    assert profile["providers"]["xai-oauth"]["tokens"]["refresh_token"] == "new-refresh"

    # Runtime fallback rotation uses the exact-source coordinator; a fresh
    # login must never overwrite the independent root grant.
    root = _read_store(root_path)
    assert root["providers"]["xai-oauth"]["tokens"]["access_token"] == "old-access"
    assert root["providers"]["xai-oauth"]["tokens"]["refresh_token"] == "old-refresh"


def test_refresh_does_not_touch_root_when_profile_has_own_state(profile_and_root):
    """A profile that genuinely shadows root must NOT clobber the root grant."""
    profile_path, root_path = profile_and_root
    # Profile has its OWN xai-oauth block: it shadows root legitimately.
    _write_store(
        profile_path,
        {
            "version": 1,
            "providers": {
                "xai-oauth": {
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
                "xai-oauth": {
                    "tokens": {
                        "access_token": "root-untouched",
                        "refresh_token": "root-untouched-refresh",
                    }
                }
            },
        },
    )

    auth._save_xai_oauth_tokens(
        {"access_token": "profile-new", "refresh_token": "profile-new-refresh"}
    )

    profile = _read_store(profile_path)
    assert profile["providers"]["xai-oauth"]["tokens"]["refresh_token"] == "profile-new-refresh"

    # Root is a separate grant chain — must be left exactly as-is.
    root = _read_store(root_path)
    assert root["providers"]["xai-oauth"]["tokens"]["access_token"] == "root-untouched"
    assert root["providers"]["xai-oauth"]["tokens"]["refresh_token"] == "root-untouched-refresh"


def test_write_through_is_noop_in_classic_mode(tmp_path, monkeypatch):
    """Classic mode (profile == root) already saves to root; no double write."""
    profile_path = tmp_path / "auth.json"
    monkeypatch.setattr(auth, "_auth_file_path", lambda: profile_path)
    # Classic mode: _global_auth_file_path returns None.
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: None)
    _write_store(profile_path, {"version": 1, "providers": {}})

    # Should not raise and should persist to the single store.
    auth._save_xai_oauth_tokens(
        {"access_token": "a", "refresh_token": "r"}
    )
    store = _read_store(profile_path)
    assert store["providers"]["xai-oauth"]["tokens"]["refresh_token"] == "r"


def test_write_through_failure_does_not_break_profile_save(profile_and_root, monkeypatch):
    """A failed root write-through must not break the profile's own save."""
    profile_path, root_path = profile_and_root
    _write_store(profile_path, {"version": 1, "providers": {}})
    _write_store(root_path, {"version": 1, "providers": {}})

    # Make the root write blow up; the profile save must still succeed.
    real_save = auth._save_auth_store

    def _exploding_save(store, target_path=None):
        if target_path is not None and target_path == root_path:
            raise OSError("simulated root write failure")
        return real_save(store, target_path)

    monkeypatch.setattr(auth, "_save_auth_store", _exploding_save)

    auth._save_xai_oauth_tokens({"access_token": "a", "refresh_token": "r"})

    profile = _read_store(profile_path)
    assert profile["providers"]["xai-oauth"]["tokens"]["refresh_token"] == "r"


def test_xai_write_through_refuses_stale_same_provider_state(profile_and_root):
    """A profile refresh must never roll a newer root grant backward."""
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
        {"version": 1, "providers": {"xai-oauth": newer_state}},
    )

    auth._write_through_xai_oauth_to_global_root(
        {
            "tokens": {
                "access_token": "stale-writer-access",
                "refresh_token": "stale-writer-refresh",
            }
        },
        expected_token_pair_fingerprint=(
            auth.provider_oauth_token_pair_fingerprint("xai-oauth", old_state)
        ),
    )

    root = _read_store(root_path)
    assert root["providers"]["xai-oauth"] == newer_state


def test_runtime_refresh_updates_root_source_without_creating_profile_shadow(
    profile_and_root,
    monkeypatch,
):
    """A profile refresh must mutate the root grant it actually resolved."""
    profile_path, root_path = profile_and_root
    _write_store(profile_path, {"version": 1, "providers": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                "xai-oauth": {
                    "tokens": {
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    },
                    "discovery": {
                        "token_endpoint": "https://auth.x.ai/oauth/token",
                    },
                    "auth_mode": "oauth_device_code",
                }
            },
        },
    )

    calls = []

    def _refresh(access_token, refresh_token, **kwargs):
        calls.append((access_token, refresh_token, kwargs["token_endpoint"]))
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "Bearer",
            "last_refresh": "2026-07-13T12:34:56Z",
        }

    monkeypatch.setattr(auth, "refresh_xai_oauth_pure", _refresh)

    resolved = auth.resolve_xai_oauth_runtime_credentials(
        force_refresh=True,
        refresh_if_expiring=False,
    )

    assert calls == [
        ("old-access", "old-refresh", "https://auth.x.ai/oauth/token")
    ]
    assert resolved["api_key"] == "new-access"
    assert (
        _read_store(root_path)["providers"]["xai-oauth"]["tokens"]
        ["refresh_token"]
        == "new-refresh"
    )
    assert _read_store(profile_path).get("providers") == {}


def test_terminal_runtime_refresh_quarantines_root_source_without_profile_shadow(
    profile_and_root,
    monkeypatch,
):
    """A terminal fallback refresh must quarantine the owning root store."""
    profile_path, root_path = profile_and_root
    _write_store(profile_path, {"version": 1, "providers": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {
                "xai-oauth": {
                    "tokens": {
                        "access_token": "dead-access",
                        "refresh_token": "dead-refresh",
                    },
                    "discovery": {
                        "token_endpoint": "https://auth.x.ai/oauth/token",
                    },
                }
            },
            "credential_pool": {
                "xai-oauth": [
                    {
                        "id": "device-alias",
                        "source": "device_code",
                        "access_token": "dead-access",
                        "refresh_token": "dead-refresh",
                    },
                    {
                        "id": "manual-keep",
                        "source": "manual",
                        "access_token": "manual-access",
                        "refresh_token": "manual-refresh",
                    },
                ]
            },
        },
    )

    def _terminal(*_args, **_kwargs):
        raise auth.AuthError(
            "refresh token rejected",
            provider="xai-oauth",
            code="xai_refresh_failed",
            relogin_required=True,
        )

    monkeypatch.setattr(auth, "refresh_xai_oauth_pure", _terminal)

    with pytest.raises(auth.AuthError, match="refresh token rejected"):
        auth.resolve_xai_oauth_runtime_credentials(
            force_refresh=True,
            refresh_if_expiring=False,
        )

    root_state = _read_store(root_path)["providers"]["xai-oauth"]
    assert "access_token" not in root_state["tokens"]
    assert "refresh_token" not in root_state["tokens"]
    assert root_state["last_auth_error"]["relogin_required"] is True
    root_rows = _read_store(root_path)["credential_pool"]["xai-oauth"]
    assert [row["id"] for row in root_rows] == ["manual-keep"]
    assert _read_store(profile_path).get("providers") == {}


def test_runtime_refresh_updates_root_pool_source_in_place(
    profile_and_root,
    monkeypatch,
):
    """Pool-only fallback rotation must update its exact owning row."""
    profile_path, root_path = profile_and_root
    _write_store(profile_path, {"version": 1, "providers": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "xai-oauth": [
                    {
                        "id": "pool-owner",
                        "source": "device_code",
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    },
                    {
                        "id": "manual-keep",
                        "source": "manual",
                        "access_token": "manual-access",
                        "refresh_token": "manual-refresh",
                    },
                ]
            },
        },
    )
    monkeypatch.setattr(
        auth,
        "_xai_oauth_discovery",
        lambda _timeout: {"token_endpoint": "https://auth.x.ai/oauth/token"},
    )
    monkeypatch.setattr(
        auth,
        "refresh_xai_oauth_pure",
        lambda *_args, **_kwargs: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "Bearer",
            "last_refresh": "2026-07-13T12:34:56Z",
        },
    )

    resolved = auth.resolve_xai_oauth_runtime_credentials(
        force_refresh=True,
        refresh_if_expiring=False,
    )

    root = _read_store(root_path)
    rows = {row["id"]: row for row in root["credential_pool"]["xai-oauth"]}
    assert resolved["api_key"] == "new-access"
    assert rows["pool-owner"]["access_token"] == "new-access"
    assert rows["pool-owner"]["refresh_token"] == "new-refresh"
    assert rows["manual-keep"]["refresh_token"] == "manual-refresh"
    assert root.get("providers") == {}
    assert _read_store(profile_path).get("providers") == {}


def test_terminal_runtime_refresh_removes_only_root_pool_source(
    profile_and_root,
    monkeypatch,
):
    """Pool-only terminal death removes its row while preserving manual rows."""
    profile_path, root_path = profile_and_root
    _write_store(profile_path, {"version": 1, "providers": {}})
    _write_store(
        root_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "xai-oauth": [
                    {
                        "id": "pool-owner",
                        "source": "device_code",
                        "access_token": "dead-access",
                        "refresh_token": "dead-refresh",
                    },
                    {
                        "id": "manual-keep",
                        "source": "manual",
                        "access_token": "manual-access",
                        "refresh_token": "manual-refresh",
                    },
                ]
            },
        },
    )
    monkeypatch.setattr(
        auth,
        "_xai_oauth_discovery",
        lambda _timeout: {"token_endpoint": "https://auth.x.ai/oauth/token"},
    )

    def _terminal(*_args, **_kwargs):
        raise auth.AuthError(
            "refresh token rejected",
            provider="xai-oauth",
            code="xai_refresh_failed",
            relogin_required=True,
        )

    monkeypatch.setattr(auth, "refresh_xai_oauth_pure", _terminal)

    with pytest.raises(auth.AuthError, match="refresh token rejected"):
        auth.resolve_xai_oauth_runtime_credentials(
            force_refresh=True,
            refresh_if_expiring=False,
        )

    root = _read_store(root_path)
    assert [
        row["id"] for row in root["credential_pool"]["xai-oauth"]
    ] == ["manual-keep"]
    assert root.get("providers") == {}
    assert _read_store(profile_path).get("providers") == {}
