"""Tests for cross-profile auth fallback.

When ``HERMES_HOME`` points to a named profile, ``read_credential_pool()``
and ``get_provider_auth_state()`` fall back to the global-root
``auth.json`` per-provider when the profile has no entries for that
provider.  Writes still target the profile only.

See the #18594 follow-up report: profile workers couldn't see providers
authenticated only at the global root.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _make_auth_store(pool: dict | None = None, providers: dict | None = None) -> dict:
    store: dict = {"version": 1}
    if pool is not None:
        store["credential_pool"] = pool
    if providers is not None:
        store["providers"] = providers
    return store


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Set up a global root + an active profile under Path.home()/.hermes/profiles/coder.

    * Path.home() -> tmp_path
    * Global root -> tmp_path/.hermes            (has its own auth.json fixture)
    * Profile     -> tmp_path/.hermes/profiles/coder   (active, HERMES_HOME points here)

    This mirrors the real "named profile mounted under the default root"
    layout that profile users actually have on disk.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    global_root = tmp_path / ".hermes"
    global_root.mkdir()
    profile_dir = global_root / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    return {"global": global_root, "profile": profile_dir}


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2))


def _invoke_jwt(*, seconds: int) -> str:
    def _part(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    claims = {
        "sub": "profile-fallback-test",
        "scope": "inference:invoke",
        "exp": int(time.time() + seconds),
    }
    return f"{_part({'alg': 'none', 'typ': 'JWT'})}.{_part(claims)}.sig"


def _iso_from_now(seconds: int) -> str:
    return datetime.fromtimestamp(
        time.time() + seconds,
        tz=timezone.utc,
    ).isoformat()


def _nous_state(auth_mod, *, access_token: str, refresh_token: str) -> dict:
    return {
        "portal_base_url": auth_mod.DEFAULT_NOUS_PORTAL_URL,
        "inference_base_url": auth_mod.DEFAULT_NOUS_INFERENCE_URL,
        "client_id": auth_mod.DEFAULT_NOUS_CLIENT_ID,
        "token_type": "Bearer",
        "scope": auth_mod.DEFAULT_NOUS_SCOPE,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": 0,
        "expires_at": _iso_from_now(-60),
    }


# ---------------------------------------------------------------------------
# read_credential_pool — provider-slice reads
# ---------------------------------------------------------------------------


def test_profile_with_zero_entries_falls_back_to_global(profile_env):
    """Empty profile pool inherits the global-root entries for that provider."""
    from hermes_cli.auth import read_credential_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "glob-1",
            "label": "global-key",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-or-global",
        }],
    }))
    # Profile auth.json: exists but has no openrouter entries.
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={}))

    entries = read_credential_pool("openrouter")
    assert len(entries) == 1
    assert entries[0]["id"] == "glob-1"
    assert entries[0]["access_token"] == "sk-or-global"


@pytest.mark.parametrize("provider", ["openai-codex", "xai-oauth", "nous"])
def test_profile_singleton_claims_pool_source_without_mutating_root(
    profile_env,
    provider,
):
    """A profile-owned singleton must never be seeded into a root pool."""
    from agent.credential_pool import load_pool

    def _state(prefix: str) -> dict:
        if provider in {"openai-codex", "xai-oauth"}:
            state = {
                "tokens": {
                    "access_token": f"{prefix}-access",
                    "refresh_token": f"{prefix}-refresh",
                    "token_type": "Bearer",
                }
            }
            if provider == "xai-oauth":
                state["discovery"] = {
                    "token_endpoint": "https://auth.x.ai/oauth2/token"
                }
            return state
        return {
            "access_token": f"{prefix}-access",
            "refresh_token": f"{prefix}-refresh",
            "agent_key": f"{prefix}-agent",
            "agent_key_expires_at": _iso_from_now(3600),
            "scope": "inference:invoke",
        }

    root_path = profile_env["global"] / "auth.json"
    profile_path = profile_env["profile"] / "auth.json"
    root_entry = {
        "id": "root-device",
        "label": "root-device",
        "auth_type": "oauth",
        "priority": 0,
        "source": "device_code",
        "access_token": "root-access",
        "refresh_token": "root-refresh",
    }
    if provider == "nous":
        root_entry.update(
            {
                "agent_key": "root-agent",
                "agent_key_expires_at": _iso_from_now(3600),
            }
        )
    _write(
        root_path,
        _make_auth_store(
            providers={provider: _state("root")},
            pool={provider: [root_entry]},
        ),
    )
    _write(
        profile_path,
        _make_auth_store(providers={provider: _state("profile")}, pool={}),
    )
    root_before = root_path.read_bytes()

    pool = load_pool(provider)

    assert pool._source_auth_path == profile_path
    assert [entry.access_token for entry in pool.entries()] == ["profile-access"]
    assert root_path.read_bytes() == root_before
    profile = json.loads(profile_path.read_text())
    assert profile["credential_pool"][provider][0]["access_token"] == "profile-access"


@pytest.mark.parametrize("provider", ["openai-codex", "xai-oauth", "nous"])
def test_quarantined_profile_singleton_blocks_live_root_pool(
    profile_env,
    provider,
):
    """A dead profile-owned grant must not resurrect the root credential."""
    from agent.credential_pool import load_pool

    root_path = profile_env["global"] / "auth.json"
    profile_path = profile_env["profile"] / "auth.json"
    _write(
        root_path,
        _make_auth_store(
            pool={
                provider: [
                    {
                        "id": "root-live",
                        "label": "root-live",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "device_code",
                        "access_token": "root-access",
                        "refresh_token": "root-refresh",
                    }
                ]
            }
        ),
    )
    _write(
        profile_path,
        _make_auth_store(
            providers={
                provider: {
                    "last_auth_error": {
                        "code": "invalid_grant",
                        "relogin_required": True,
                    }
                }
            },
            pool={},
        ),
    )
    root_before = root_path.read_bytes()

    pool = load_pool(provider)

    assert pool._source_auth_path == profile_path
    assert pool.entries() == []
    assert root_path.read_bytes() == root_before


@pytest.mark.parametrize("provider", ["xai-oauth", "nous"])
def test_stale_root_pool_rejects_later_profile_shadow(
    profile_env,
    monkeypatch,
    provider,
):
    """A loaded root pool may not silently adopt a newly-created profile grant."""
    from agent.credential_pool import load_pool
    from hermes_cli import auth as auth_mod

    def _state(prefix: str) -> dict:
        if provider == "xai-oauth":
            return {
                "tokens": {
                    "access_token": f"{prefix}-access",
                    "refresh_token": f"{prefix}-refresh",
                    "token_type": "Bearer",
                },
                "discovery": {
                    "token_endpoint": "https://auth.x.ai/oauth2/token"
                },
            }
        return {
            "access_token": f"{prefix}-access",
            "refresh_token": f"{prefix}-refresh",
            "agent_key": f"{prefix}-agent",
            "agent_key_expires_at": _iso_from_now(3600),
            "scope": "inference:invoke",
        }

    def _entry(prefix: str) -> dict:
        row = {
            "id": "shared-id",
            "label": f"{prefix}-device",
            "auth_type": "oauth",
            "priority": 0,
            "source": "device_code",
            "access_token": f"{prefix}-access",
            "refresh_token": f"{prefix}-refresh",
        }
        if provider == "nous":
            row.update(
                {
                    "agent_key": f"{prefix}-agent",
                    "agent_key_expires_at": _iso_from_now(3600),
                }
            )
        return row

    root_path = profile_env["global"] / "auth.json"
    profile_path = profile_env["profile"] / "auth.json"
    _write(
        root_path,
        _make_auth_store(
            providers={provider: _state("root")},
            pool={provider: [_entry("root")]},
        ),
    )
    _write(profile_path, _make_auth_store(providers={}, pool={}))
    pool = load_pool(provider)
    entry = pool.entries()[0]
    assert pool._source_auth_path == root_path

    _write(
        profile_path,
        _make_auth_store(
            providers={provider: _state("profile")},
            pool={provider: [_entry("profile")]},
        ),
    )
    root_before = root_path.read_bytes()
    if provider == "nous":
        monkeypatch.setattr(
            auth_mod,
            "resolve_nous_runtime_credentials",
            lambda **_kwargs: {"api_key": "profile-agent"},
        )

    with pytest.raises(auth_mod.AuthStoreConflictError, match="source changed"):
        pool._refresh_entry(entry, force=True)

    assert root_path.read_bytes() == root_before


def test_stable_id_removal_mutates_global_pool_source_without_profile_shadow(
    profile_env,
):
    """A profile deleting an inherited row must target the owning root store."""
    global_path = profile_env["global"] / "auth.json"
    profile_path = profile_env["profile"] / "auth.json"
    _write(
        global_path,
        _make_auth_store(
            pool={
                "openrouter": [
                    {
                        "id": "global-row",
                        "label": "global-row",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-or-global",
                    }
                ]
            }
        ),
    )
    _write(profile_path, _make_auth_store(pool={}))
    profile_before = profile_path.read_bytes()

    from agent.credential_sources import remove_credential_target

    outcome = remove_credential_target("openrouter", "global-row")

    assert outcome.remaining_count == 0
    root = json.loads(global_path.read_text())
    assert root["credential_pool"]["openrouter"] == []
    assert profile_path.read_bytes() == profile_before


def test_profile_with_entries_fully_shadows_global(profile_env):
    """Once the profile has any entries for a provider, global is ignored."""
    from hermes_cli.auth import read_credential_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "glob-1",
            "label": "global-key",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-or-global",
        }],
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "prof-1",
            "label": "profile-key",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-or-profile",
        }],
    }))

    entries = read_credential_pool("openrouter")
    assert len(entries) == 1
    assert entries[0]["id"] == "prof-1"
    assert entries[0]["access_token"] == "sk-or-profile"


def test_per_provider_shadowing_is_independent(profile_env):
    """Profile can override one provider while inheriting another from global."""
    from hermes_cli.auth import read_credential_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "glob-or",
            "label": "global-or",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-or-global",
        }],
        "anthropic": [{
            "id": "glob-ant",
            "label": "global-ant",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-ant-global",
        }],
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        # Profile has openrouter only — anthropic should still fall back.
        "openrouter": [{
            "id": "prof-or",
            "label": "profile-or",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-or-profile",
        }],
    }))

    or_entries = read_credential_pool("openrouter")
    ant_entries = read_credential_pool("anthropic")
    assert [e["id"] for e in or_entries] == ["prof-or"]
    assert [e["id"] for e in ant_entries] == ["glob-ant"]


def test_missing_global_auth_file_is_safe(profile_env):
    """Profile processes that never had a global auth.json still work."""
    from hermes_cli.auth import read_credential_pool

    # No global auth.json written at all.
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "prof-1",
            "label": "profile",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-profile",
        }],
    }))

    assert read_credential_pool("openrouter")[0]["id"] == "prof-1"
    assert read_credential_pool("anthropic") == []


def test_malformed_global_auth_file_does_not_break_profile_read(profile_env):
    (profile_env["global"] / "auth.json").write_text("{not valid json")
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "prof-1",
            "label": "profile",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-profile",
        }],
    }))

    from hermes_cli.auth import read_credential_pool

    # Profile reads still work; malformed global is silently ignored.
    assert read_credential_pool("openrouter")[0]["id"] == "prof-1"
    # And no fallback for anthropic since global is unreadable.
    assert read_credential_pool("anthropic") == []


# ---------------------------------------------------------------------------
# read_credential_pool — whole-pool reads (provider_id=None)
# ---------------------------------------------------------------------------


def test_whole_pool_merges_global_providers_when_missing_locally(profile_env):
    from hermes_cli.auth import read_credential_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "glob-or",
            "label": "global-or",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-or-global",
        }],
        "anthropic": [{
            "id": "glob-ant",
            "label": "global-ant",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-ant-global",
        }],
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "prof-or",
            "label": "profile-or",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-or-profile",
        }],
    }))

    pool = read_credential_pool(None)
    # Profile wins for openrouter, global fills in anthropic.
    assert [e["id"] for e in pool["openrouter"]] == ["prof-or"]
    assert [e["id"] for e in pool["anthropic"]] == ["glob-ant"]


# ---------------------------------------------------------------------------
# get_provider_auth_state — singleton fallback
# ---------------------------------------------------------------------------


def test_provider_auth_state_falls_back_to_global_when_profile_has_none(profile_env):
    from hermes_cli.auth import get_provider_auth_state

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "nous": {"access_token": "nous-global", "refresh_token": "rt-global"},
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))

    state = get_provider_auth_state("nous")
    assert state is not None
    assert state["access_token"] == "nous-global"


def test_provider_auth_state_profile_wins_when_present(profile_env):
    from hermes_cli.auth import get_provider_auth_state

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "nous": {"access_token": "nous-global"},
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={
        "nous": {"access_token": "nous-profile"},
    }))

    state = get_provider_auth_state("nous")
    assert state is not None
    assert state["access_token"] == "nous-profile"


def test_provider_auth_state_returns_none_when_neither_has_it(profile_env):
    from hermes_cli.auth import get_provider_auth_state

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={}))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))

    assert get_provider_auth_state("nous") is None


# ---------------------------------------------------------------------------
# _load_provider_state — internal global fallback (issue #18594 follow-up)
#
# Several runtime helpers (notably ``resolve_nous_runtime_credentials`` and
# ``resolve_nous_access_token``) call ``_load_provider_state`` directly with
# a profile-loaded auth store rather than going through
# ``get_provider_auth_state``. Without the fallback wired into
# ``_load_provider_state`` itself, those helpers raise ``"Hermes is not
# logged into Nous Portal"`` even though the user has a valid global Nous
# login. These tests pin the per-provider shadowing into the helper.
# ---------------------------------------------------------------------------


def test_load_provider_state_falls_back_to_global(profile_env):
    """When the loaded profile store has no provider entry, fall back to global."""
    from hermes_cli.auth import _load_auth_store, _load_provider_state

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "nous": {"access_token": "global-nous-token", "refresh_token": "rt"},
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))

    auth_store = _load_auth_store()
    state = _load_provider_state(auth_store, "nous")
    assert state is not None
    assert state["access_token"] == "global-nous-token"


def test_resolve_nous_access_token_refreshes_global_source_without_profile_write(
    profile_env, monkeypatch
):
    """A profile fallback refresh must lock and update only its root source."""
    from hermes_cli import auth as auth_mod

    _write(
        profile_env["global"] / "auth.json",
        {
            **_make_auth_store(
            providers={
                "nous": _nous_state(
                    auth_mod,
                    access_token="expired-access",
                    refresh_token="root-refresh-old",
                )
            }
            ),
            "active_provider": "openai-codex",
        },
    )
    profile_path = profile_env["profile"] / "auth.json"
    _write(profile_path, _make_auth_store(providers={"marker": {"keep": True}}))
    profile_before = profile_path.read_bytes()
    monkeypatch.setattr(
        auth_mod,
        "_merge_shared_nous_oauth_state",
        lambda _state: False,
    )
    monkeypatch.setattr(auth_mod, "_write_shared_nous_state", lambda _state: None)
    monkeypatch.setattr(
        auth_mod,
        "_refresh_access_token",
        lambda **_kwargs: {
            "access_token": "root-access-new",
            "refresh_token": "root-refresh-new",
            "expires_in": 3600,
        },
    )

    token = auth_mod.resolve_nous_access_token()

    assert token == "root-access-new"
    root = json.loads((profile_env["global"] / "auth.json").read_text())
    assert root["providers"]["nous"]["refresh_token"] == "root-refresh-new"
    assert root["active_provider"] == "openai-codex"
    assert profile_path.read_bytes() == profile_before


def test_resolve_nous_runtime_refreshes_global_source_without_profile_write(
    profile_env, monkeypatch
):
    """Runtime JWT refresh follows the same single-source lock discipline."""
    from hermes_cli import auth as auth_mod

    _write(
        profile_env["global"] / "auth.json",
        {
            **_make_auth_store(
            providers={
                "nous": _nous_state(
                    auth_mod,
                    access_token=_invoke_jwt(seconds=-60),
                    refresh_token="root-runtime-refresh-old",
                )
            }
            ),
            "active_provider": "openai-codex",
        },
    )
    profile_path = profile_env["profile"] / "auth.json"
    _write(profile_path, _make_auth_store(providers={"marker": {"keep": True}}))
    profile_before = profile_path.read_bytes()
    fresh_jwt = _invoke_jwt(seconds=3600)
    monkeypatch.setattr(
        auth_mod,
        "_merge_shared_nous_oauth_state",
        lambda _state: False,
    )
    monkeypatch.setattr(auth_mod, "_write_shared_nous_state", lambda _state: None)
    monkeypatch.setattr(auth_mod, "_sync_nous_pool_from_auth_store", lambda: None)
    monkeypatch.setattr(
        auth_mod,
        "_refresh_access_token",
        lambda **_kwargs: {
            "access_token": fresh_jwt,
            "refresh_token": "root-runtime-refresh-new",
            "expires_in": 3600,
            "scope": auth_mod.DEFAULT_NOUS_SCOPE,
        },
    )

    credentials = auth_mod.resolve_nous_runtime_credentials(force_refresh=True)

    assert credentials["api_key"] == fresh_jwt
    root = json.loads((profile_env["global"] / "auth.json").read_text())
    assert (
        root["providers"]["nous"]["refresh_token"]
        == "root-runtime-refresh-new"
    )
    assert root["active_provider"] == "openai-codex"
    assert profile_path.read_bytes() == profile_before


def test_nous_pool_wrapper_persists_once_to_root_source(
    profile_env,
    monkeypatch,
):
    """Pool wrapper adopts resolver output without provider write-through."""
    from agent.credential_pool import load_pool
    from hermes_cli import auth as auth_mod

    root_path = profile_env["global"] / "auth.json"
    profile_path = profile_env["profile"] / "auth.json"
    old_state = _nous_state(
        auth_mod,
        access_token="old-access",
        refresh_token="old-refresh",
    )
    old_state.update(
        {
            "agent_key": "old-agent",
            "agent_key_expires_at": _iso_from_now(3600),
            "inference_base_url": auth_mod.DEFAULT_NOUS_INFERENCE_URL,
        }
    )
    _write(
        root_path,
        {
            **_make_auth_store(providers={"nous": old_state}),
            "credential_pool": {
                "nous": [
                    {
                        "id": "root-nous",
                        "label": "root-nous",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "device_code",
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                        "agent_key": "old-agent",
                        "agent_key_expires_at": _iso_from_now(3600),
                    }
                ]
            },
        },
    )
    _write(profile_path, _make_auth_store(providers={}, pool={}))
    profile_before = profile_path.read_bytes()
    pool = load_pool("nous")
    entry = pool.entries()[0]

    def _resolver(*, force_refresh, sync_pool, **_kwargs):
        assert force_refresh is True
        assert sync_pool is False
        with auth_mod._auth_store_lock(root_path):
            store = auth_mod._load_auth_store(root_path, strict=True)
            state = dict(store["providers"]["nous"])
            state.update(
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "agent_key": "new-agent",
                    "agent_key_expires_at": _iso_from_now(7200),
                }
            )
            auth_mod._store_provider_state(
                store,
                "nous",
                state,
                set_active=False,
            )
            auth_mod._save_auth_store(store, target_path=root_path)
        return {"api_key": "new-agent"}

    monkeypatch.setattr(auth_mod, "resolve_nous_runtime_credentials", _resolver)
    persist_calls = []
    original_persist = pool._persist

    def _persist_once(*args, **kwargs):
        persist_calls.append(True)
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(pool, "_persist", _persist_once)
    sync_back_calls = []
    monkeypatch.setattr(
        pool,
        "_sync_device_code_entry_to_auth_store",
        lambda *_args, **_kwargs: sync_back_calls.append(True),
    )

    refreshed = pool._refresh_entry(entry, force=True)

    assert refreshed is not None
    assert refreshed.refresh_token == "new-refresh"
    assert refreshed.agent_key == "new-agent"
    assert len(persist_calls) == 1
    assert sync_back_calls == []
    root = json.loads(root_path.read_text())
    assert root["providers"]["nous"]["refresh_token"] == "new-refresh"
    assert root["credential_pool"]["nous"][0]["refresh_token"] == "new-refresh"
    assert profile_path.read_bytes() == profile_before


def test_terminal_nous_fallback_persists_root_pool_quarantine(
    profile_env, monkeypatch, caplog
):
    """Provider quarantine and pool removal must commit in one root snapshot."""
    from hermes_cli import auth as auth_mod

    root_state = _nous_state(
        auth_mod,
        access_token="expired-access",
        refresh_token="revoked-refresh",
    )
    _write(
        profile_env["global"] / "auth.json",
        {
            **_make_auth_store(providers={"nous": root_state}),
            "active_provider": "openai-codex",
            "credential_pool": {
                "nous": [
                    {
                        "id": "device-code-id",
                        "source": auth_mod.NOUS_DEVICE_CODE_SOURCE,
                        "access_token": "expired-access",
                        "refresh_token": "revoked-refresh",
                    },
                    {
                        "id": "manual-id",
                        "source": "manual",
                        "access_token": "manual-access",
                    },
                ]
            },
        },
    )
    profile_path = profile_env["profile"] / "auth.json"
    _write(profile_path, _make_auth_store(providers={"marker": {"keep": True}}))
    profile_before = profile_path.read_bytes()
    monkeypatch.setenv(
        "HERMES_SHARED_AUTH_DIR",
        str(profile_env["global"] / "shared-test"),
    )
    monkeypatch.setattr(
        auth_mod,
        "_merge_shared_nous_oauth_state",
        lambda _state: False,
    )

    def terminal_refresh(**_kwargs):
        raise auth_mod.AuthError(
            "Refresh session has been revoked",
            provider="nous",
            code="invalid_grant",
            relogin_required=True,
        )

    monkeypatch.setattr(auth_mod, "_refresh_access_token", terminal_refresh)
    caplog.set_level("WARNING")

    with pytest.raises(auth_mod.AuthError, match="revoked"):
        auth_mod.resolve_nous_access_token()

    root = json.loads((profile_env["global"] / "auth.json").read_text())
    assert not root["providers"]["nous"].get("access_token")
    assert not root["providers"]["nous"].get("refresh_token")
    assert [row["id"] for row in root["credential_pool"]["nous"]] == [
        "manual-id"
    ]
    assert root["active_provider"] == "openai-codex"
    assert any(
        "Nous OAuth state quarantined" in record.message
        and str(profile_env["global"] / "auth.json") in record.message
        for record in caplog.records
    )
    assert profile_path.read_bytes() == profile_before


def test_nous_pool_terminal_refresh_uses_resolver_quarantine_without_profile_shadow(
    profile_env,
    monkeypatch,
):
    """Wrapper must not repeat terminal writes after source resolver cleanup."""
    from agent.credential_pool import load_pool
    from hermes_cli import auth as auth_mod

    root_path = profile_env["global"] / "auth.json"
    profile_path = profile_env["profile"] / "auth.json"
    root_state = _nous_state(
        auth_mod,
        access_token=_invoke_jwt(seconds=-60),
        refresh_token="revoked-refresh",
    )
    root_state.update(
        {
            "agent_key": "expired-agent",
            "agent_key_expires_at": _iso_from_now(-60),
        }
    )
    _write(
        root_path,
        {
            **_make_auth_store(providers={"nous": root_state}),
            "credential_pool": {
                "nous": [
                    {
                        "id": "device-code-id",
                        "label": "device",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "device_code",
                        "access_token": root_state["access_token"],
                        "refresh_token": "revoked-refresh",
                        "agent_key": "expired-agent",
                    },
                    {
                        "id": "manual-id",
                        "label": "manual",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "manual-access",
                    },
                ]
            },
        },
    )
    _write(profile_path, _make_auth_store(providers={}, pool={}))
    profile_before = profile_path.read_bytes()
    monkeypatch.setenv(
        "HERMES_SHARED_AUTH_DIR",
        str(profile_env["global"] / "shared-test"),
    )
    monkeypatch.setattr(
        auth_mod,
        "_merge_shared_nous_oauth_state",
        lambda _state: False,
    )

    def _terminal(**_kwargs):
        raise auth_mod.AuthError(
            "Refresh session has been revoked",
            provider="nous",
            code="invalid_grant",
            relogin_required=True,
        )

    monkeypatch.setattr(auth_mod, "_refresh_access_token", _terminal)
    pool = load_pool("nous")
    entry = next(row for row in pool.entries() if row.source == "device_code")

    assert pool._refresh_entry(entry, force=True) is None

    root = json.loads(root_path.read_text())
    assert not root["providers"]["nous"].get("refresh_token")
    assert [row["id"] for row in root["credential_pool"]["nous"]] == [
        "manual-id"
    ]
    assert [row.id for row in pool.entries()] == ["manual-id"]
    assert profile_path.read_bytes() == profile_before


def test_load_provider_state_profile_wins_over_global(profile_env):
    from hermes_cli.auth import _load_auth_store, _load_provider_state

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "nous": {"access_token": "global-token"},
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={
        "nous": {"access_token": "profile-token"},
    }))

    auth_store = _load_auth_store()
    state = _load_provider_state(auth_store, "nous")
    assert state is not None
    assert state["access_token"] == "profile-token"


def test_load_provider_state_returns_none_when_neither_has_it(profile_env):
    from hermes_cli.auth import _load_auth_store, _load_provider_state

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={}))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))

    auth_store = _load_auth_store()
    assert _load_provider_state(auth_store, "nous") is None


def test_load_provider_state_classic_mode_no_fallback(tmp_path, monkeypatch):
    """In classic mode there is no global to fall back to; behavior is unchanged."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    hermes_home = tmp_path / "classic"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    _write(hermes_home / "auth.json", _make_auth_store(providers={
        "nous": {"access_token": "classic-token"},
    }))

    from hermes_cli.auth import _load_auth_store, _load_provider_state

    auth_store = _load_auth_store()
    state = _load_provider_state(auth_store, "nous")
    assert state is not None
    assert state["access_token"] == "classic-token"
    # Absent providers still return None.
    assert _load_provider_state(auth_store, "anthropic") is None


def test_load_provider_state_malformed_global_does_not_break_profile(profile_env):
    """A corrupt global auth.json must not break profile reads."""
    (profile_env["global"] / "auth.json").write_text("{not valid json")
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={
        "nous": {"access_token": "profile-token"},
    }))

    from hermes_cli.auth import _load_auth_store, _load_provider_state

    auth_store = _load_auth_store()
    state = _load_provider_state(auth_store, "nous")
    assert state is not None
    assert state["access_token"] == "profile-token"


# ---------------------------------------------------------------------------
# Classic mode — no fallback path should ever trigger
# ---------------------------------------------------------------------------


def test_classic_mode_does_not_double_read_same_file(tmp_path, monkeypatch):
    """In classic mode (HERMES_HOME == global root), no fallback path runs.

    This guards against the merge accidentally duplicating entries when the
    profile and global resolve to the same directory.
    """
    # Put Path.home() under a subdir so the seat belt in _auth_file_path()
    # sees tmp_path/home/.hermes as the "real home" — which is NOT equal
    # to the HERMES_HOME we set (tmp_path/classic), so the guard passes.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    hermes_home = tmp_path / "classic"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    _write(hermes_home / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "only",
            "label": "classic",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-classic",
        }],
    }))

    from hermes_cli.auth import read_credential_pool, _global_auth_file_path

    # Classic mode: HERMES_HOME is set to a custom path that is NOT under
    # ~/.hermes/profiles/ — get_default_hermes_root() returns HERMES_HOME
    # itself, so the profile root and global root are the same directory,
    # and the helper correctly returns None (no fallback).
    assert _global_auth_file_path() is None
    # And the read should return exactly one entry (not two).
    entries = read_credential_pool("openrouter")
    assert len(entries) == 1
    assert entries[0]["id"] == "only"


# ---------------------------------------------------------------------------
# Writes stay scoped to the profile
# ---------------------------------------------------------------------------


def test_write_credential_pool_targets_profile_not_global(profile_env):
    from hermes_cli.auth import read_credential_pool, write_credential_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "glob-1",
            "label": "global",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-global",
        }],
    }))

    write_credential_pool("openrouter", [{
        "id": "prof-new",
        "label": "profile-new",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "sk-profile-new",
    }])

    # Global auth.json unchanged.
    global_data = json.loads((profile_env["global"] / "auth.json").read_text())
    assert global_data["credential_pool"]["openrouter"][0]["id"] == "glob-1"

    # Profile auth.json holds the new entry.
    profile_data = json.loads((profile_env["profile"] / "auth.json").read_text())
    assert profile_data["credential_pool"]["openrouter"][0]["id"] == "prof-new"

    # Subsequent read returns profile (shadows global).
    assert [e["id"] for e in read_credential_pool("openrouter")] == ["prof-new"]
