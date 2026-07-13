"""Regression tests for strict Hermes/native Codex credential isolation.

Hermes owns its OAuth refresh chain. Native Codex CLI credentials are never
imported as an automatic recovery path because copying a single-use refresh
generation into two writers creates duplicate consumption and stale rollback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth import AuthError, resolve_codex_runtime_credentials


def _write_hermes_store(path: Path, *, access: str | None, refresh: str) -> bytes:
    tokens = {"refresh_token": refresh}
    if access is not None:
        tokens["access_token"] = access
    payload = {
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {
            "openai-codex": {
                "tokens": tokens,
                "last_refresh": "2026-06-01T00:00:00Z",
                "auth_mode": "chatgpt",
                "grant_id": "e" * 32,
            }
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path.read_bytes()


def _write_native_store(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "native-access",
                    "refresh_token": "native-refresh",
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path.read_bytes()


def _configure(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    hermes_auth = tmp_path / "hermes" / "auth.json"
    native_auth = tmp_path / "codex" / "auth.json"
    monkeypatch.setenv("HERMES_HOME", str(hermes_auth.parent))
    monkeypatch.setenv("CODEX_HOME", str(native_auth.parent))
    monkeypatch.setenv(
        "HERMES_CODEX_REFRESH_STATE_DIR",
        str(tmp_path / "codex-refresh-state"),
    )
    return hermes_auth, native_auth


def test_missing_hermes_access_token_never_imports_native_codex(
    tmp_path, monkeypatch
):
    hermes_auth, native_auth = _configure(tmp_path, monkeypatch)
    hermes_before = _write_hermes_store(
        hermes_auth,
        access=None,
        refresh="hermes-refresh",
    )
    native_before = _write_native_store(native_auth)

    with pytest.raises(AuthError) as exc:
        resolve_codex_runtime_credentials()

    assert exc.value.code == "codex_auth_missing_access_token"
    assert hermes_auth.read_bytes() == hermes_before
    assert native_auth.read_bytes() == native_before


def test_terminal_hermes_refresh_never_adopts_native_generation(
    tmp_path, monkeypatch
):
    hermes_auth, native_auth = _configure(tmp_path, monkeypatch)
    _write_hermes_store(
        hermes_auth,
        access="hermes-access",
        refresh="hermes-refresh",
    )
    native_before = _write_native_store(native_auth)

    def _rejected(*_args, **_kwargs):
        raise AuthError(
            "refresh token rejected",
            provider="openai-codex",
            code="invalid_grant",
            relogin_required=True,
        )

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rejected)

    with pytest.raises(AuthError) as exc:
        resolve_codex_runtime_credentials(
            force_refresh=True,
            refresh_if_expiring=False,
        )

    assert exc.value.code == "invalid_grant"
    saved = json.loads(hermes_auth.read_text(encoding="utf-8"))
    assert saved["providers"]["openai-codex"]["tokens"] == {}
    assert saved["providers"]["openai-codex"]["last_auth_error"]["code"] == "invalid_grant"
    assert native_auth.read_bytes() == native_before


def test_successful_hermes_rotation_leaves_native_store_byte_identical(
    tmp_path, monkeypatch
):
    hermes_auth, native_auth = _configure(tmp_path, monkeypatch)
    _write_hermes_store(
        hermes_auth,
        access="hermes-access",
        refresh="hermes-refresh",
    )
    native_before = _write_native_store(native_auth)
    monkeypatch.setattr(
        auth,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: {
            "access_token": "hermes-access-new",
            "refresh_token": "hermes-refresh-new",
            "last_refresh": "2026-07-13T13:00:00Z",
        },
    )

    resolved = resolve_codex_runtime_credentials(
        force_refresh=True,
        refresh_if_expiring=False,
    )

    assert resolved["api_key"] == "hermes-access-new"
    saved = json.loads(hermes_auth.read_text(encoding="utf-8"))
    assert saved["providers"]["openai-codex"]["tokens"]["refresh_token"] == "hermes-refresh-new"
    assert native_auth.read_bytes() == native_before


def test_rate_limit_preserves_both_credential_stores(
    tmp_path, monkeypatch
):
    hermes_auth, native_auth = _configure(tmp_path, monkeypatch)
    hermes_before = _write_hermes_store(
        hermes_auth,
        access="hermes-access",
        refresh="hermes-refresh",
    )
    native_before = _write_native_store(native_auth)

    def _limited(*_args, **_kwargs):
        raise AuthError(
            "quota exhausted",
            provider="openai-codex",
            code=auth.CODEX_RATE_LIMITED_CODE,
            relogin_required=False,
        )

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _limited)

    with pytest.raises(AuthError) as exc:
        resolve_codex_runtime_credentials(
            force_refresh=True,
            refresh_if_expiring=False,
        )

    assert exc.value.code == auth.CODEX_RATE_LIMITED_CODE
    assert hermes_auth.read_bytes() == hermes_before
    assert native_auth.read_bytes() == native_before
