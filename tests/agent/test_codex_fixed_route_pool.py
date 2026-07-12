"""Runtime enforcement tests for the global fixed Codex route policy."""

from __future__ import annotations

import time
from pathlib import Path

import pytest


def _entry(credential_id: str, *, priority: int, status: str | None = None):
    from agent.credential_pool import PooledCredential

    return PooledCredential(
        provider="openai-codex",
        id=credential_id,
        label=credential_id,
        auth_type="oauth",
        priority=priority,
        source="manual",
        access_token=f"{credential_id}-token",
        last_status=status,
        last_status_at=time.time() if status else None,
        last_error_code=429 if status else None,
    )


def _fixed(monkeypatch, credential_id: str | None = "company-id"):
    from agent import credential_pool

    policy = {"mode": "fixed"}
    if credential_id is not None:
        policy["credential_id"] = credential_id
    monkeypatch.setattr(credential_pool, "_load_codex_route_policy", lambda: policy)
    return credential_pool


def _pool_state(pool):
    return {
        "entries": [entry.to_dict() for entry in pool.entries()],
        "current_id": pool._current_id,
        "active_leases": dict(pool._active_leases),
    }


def test_fixed_route_selects_exact_credential_regardless_of_priority(monkeypatch):
    credential_pool = _fixed(monkeypatch)
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [
            _entry("personal-id", priority=0),
            _entry("company-id", priority=10),
        ],
    )

    selected = pool.select()

    assert selected is not None
    assert selected.id == "company-id"
    assert pool.peek().id == "company-id"


def test_fixed_route_duplicate_exact_id_fails_closed(monkeypatch):
    credential_pool = _fixed(monkeypatch)
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [
            _entry("company-id", priority=0),
            _entry("company-id", priority=10),
            _entry("personal-id", priority=20),
        ],
    )

    assert pool.select() is None
    assert pool.peek() is None
    assert pool.has_available() is False
    assert pool.acquire_lease() is None


def test_fixed_route_refreshes_only_the_exact_target(monkeypatch):
    credential_pool = _fixed(monkeypatch)
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [
            _entry("personal-id", priority=0),
            _entry("company-id", priority=10),
        ],
    )
    refreshed_ids = []
    pool._entry_needs_refresh = lambda _entry: True
    pool._refresh_entry = lambda entry, *, force: (
        refreshed_ids.append(entry.id) or entry
    )

    selected = pool.select()

    assert selected is not None
    assert selected.id == "company-id"
    assert refreshed_ids == ["company-id"]


def test_duplicate_fixed_route_has_no_refresh_side_effect(monkeypatch):
    credential_pool = _fixed(monkeypatch)
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [
            _entry("company-id", priority=0),
            _entry("company-id", priority=10),
            _entry("personal-id", priority=20),
        ],
    )
    refreshed_ids = []
    pool._entry_needs_refresh = lambda _entry: True
    pool._refresh_entry = lambda entry, *, force: (
        refreshed_ids.append(entry.id) or entry
    )

    assert pool.select() is None
    assert refreshed_ids == []


def test_invalid_route_policy_has_no_refresh_side_effect(monkeypatch):
    from agent import credential_pool

    monkeypatch.setattr(
        credential_pool,
        "_load_codex_route_policy",
        lambda: {"mode": "invalid"},
    )
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [
            _entry("personal-id", priority=0),
            _entry("company-id", priority=10),
        ],
    )
    refreshed_ids = []
    pool._entry_needs_refresh = lambda _entry: True
    pool._refresh_entry = lambda entry, *, force: (
        refreshed_ids.append(entry.id) or entry
    )

    assert pool.select() is None
    assert refreshed_ids == []


def test_fixed_route_fails_closed_when_exact_credential_is_exhausted(monkeypatch):
    credential_pool = _fixed(monkeypatch)
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [
            _entry("company-id", priority=0, status="exhausted"),
            _entry("personal-id", priority=10),
        ],
    )

    assert pool.select() is None
    assert pool.peek() is None
    assert pool.has_available() is False
    assert pool.acquire_lease() is None
    assert pool.acquire_lease("personal-id") is None


def test_fixed_route_rotation_never_returns_another_account(monkeypatch):
    credential_pool = _fixed(monkeypatch)
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [
            _entry("company-id", priority=0),
            _entry("personal-id", priority=10),
        ],
    )
    pool._persist = lambda **_kwargs: None
    assert pool.select().id == "company-id"

    rotated = pool.mark_exhausted_and_rotate(status_code=429)

    assert rotated is None
    assert pool.current() is None
    assert (
        next(item for item in pool.entries() if item.id == "personal-id").last_status
        is None
    )


@pytest.mark.parametrize(
    ("fixed_id", "opposite_id"),
    [
        ("company-id", "personal-id"),
        ("personal-id", "company-id"),
    ],
)
def test_fixed_route_rejects_opposite_api_key_hint_without_mutation(
    monkeypatch,
    fixed_id,
    opposite_id,
):
    credential_pool = _fixed(monkeypatch, fixed_id)
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [
            _entry("personal-id", priority=0),
            _entry("company-id", priority=10),
        ],
    )
    before = _pool_state(pool)

    rotated = pool.mark_exhausted_and_rotate(
        status_code=429,
        api_key_hint=f"{opposite_id}-token",
    )

    assert rotated is None
    assert _pool_state(pool) == before


def test_duplicate_fixed_route_rejects_hint_without_mutation(monkeypatch):
    credential_pool = _fixed(monkeypatch)
    first = _entry("company-id", priority=0)
    first.access_token = "company-token-a"
    second = _entry("company-id", priority=10)
    second.access_token = "company-token-b"
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [first, second, _entry("personal-id", priority=20)],
    )
    before = _pool_state(pool)

    rotated = pool.mark_exhausted_and_rotate(
        status_code=429,
        api_key_hint="company-token-a",
    )

    assert rotated is None
    assert _pool_state(pool) == before


def test_invalid_route_rejects_api_key_hint_without_mutation(monkeypatch):
    from agent import credential_pool

    monkeypatch.setattr(
        credential_pool,
        "_load_codex_route_policy",
        lambda: {"mode": "invalid"},
    )
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [
            _entry("personal-id", priority=0),
            _entry("company-id", priority=10),
        ],
    )
    before = _pool_state(pool)

    rotated = pool.mark_exhausted_and_rotate(
        status_code=429,
        api_key_hint="personal-id-token",
    )

    assert rotated is None
    assert _pool_state(pool) == before


def test_auto_to_fixed_transition_rejects_stale_no_hint_failure(monkeypatch):
    from agent import credential_pool

    policy = {"mode": "auto"}
    monkeypatch.setattr(
        credential_pool,
        "_load_codex_route_policy",
        lambda: dict(policy),
    )
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [
            _entry("personal-id", priority=0),
            _entry("company-id", priority=10),
        ],
    )
    assert pool.select().id == "personal-id"
    before = _pool_state(pool)

    policy.clear()
    policy.update({"mode": "fixed", "credential_id": "company-id"})
    rotated = pool.mark_exhausted_and_rotate(status_code=429)

    assert rotated is None
    assert _pool_state(pool) == before
    assert pool._current_id == "personal-id"
    assert pool.current() is None


def test_malformed_fixed_route_without_id_fails_closed(monkeypatch):
    credential_pool = _fixed(monkeypatch, None)
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [_entry("personal-id", priority=0)],
    )

    assert pool.select() is None
    assert pool.has_available() is False


def test_corrupt_route_policy_file_fails_closed(monkeypatch, tmp_path):
    from agent import credential_pool
    from hermes_cli import auth as auth_mod

    policy_path = tmp_path / "codex_route_policy.json"
    policy_path.write_text('{"mode":', encoding="utf-8")
    monkeypatch.setattr(auth_mod, "CODEX_ROUTE_POLICY_PATH", policy_path)
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [_entry("personal-id", priority=0)],
    )

    assert pool.select() is None
    assert pool.has_available() is False


def test_unknown_route_policy_mode_fails_closed(monkeypatch, tmp_path):
    from agent import credential_pool
    from hermes_cli import auth as auth_mod

    policy_path = tmp_path / "codex_route_policy.json"
    policy_path.write_text('{"mode":"surprise"}', encoding="utf-8")
    monkeypatch.setattr(auth_mod, "CODEX_ROUTE_POLICY_PATH", policy_path)
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [_entry("personal-id", priority=0)],
    )

    assert pool.select() is None
    assert pool.has_available() is False


def test_missing_route_policy_file_keeps_auto_mode(monkeypatch, tmp_path):
    from agent import credential_pool
    from hermes_cli import auth as auth_mod

    monkeypatch.setattr(
        auth_mod,
        "CODEX_ROUTE_POLICY_PATH",
        tmp_path / "missing-policy.json",
    )
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [_entry("personal-id", priority=0)],
    )

    assert pool.select().id == "personal-id"


def test_pytest_seatbelt_uses_import_time_default_path(monkeypatch, tmp_path):
    from agent import credential_pool
    from hermes_cli import auth as auth_mod

    default_path = auth_mod.CODEX_ROUTE_POLICY_PATH
    original_read_text = Path.read_text

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def fake_read_text(path, *args, **kwargs):
        if path == default_path:
            return '{"mode":"fixed","credential_id":"other-id"}'
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [_entry("personal-id", priority=0)],
    )

    assert pool.select().id == "personal-id"


def test_pool_reuses_canonical_runtime_route_policy_loader(monkeypatch):
    from agent import credential_pool

    monkeypatch.setattr(
        credential_pool.auth_mod,
        "_load_codex_runtime_route_policy",
        lambda: {"mode": "fixed", "credential_id": "company-id"},
    )
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [
            _entry("personal-id", priority=0),
            _entry("company-id", priority=10),
        ],
    )

    assert pool.select().id == "company-id"


def test_auto_policy_keeps_normal_pool_rotation(monkeypatch):
    from agent import credential_pool

    monkeypatch.setattr(
        credential_pool,
        "_load_codex_route_policy",
        lambda: {"mode": "auto"},
    )
    pool = credential_pool.CredentialPool(
        "openai-codex",
        [
            _entry("personal-id", priority=0),
            _entry("company-id", priority=10),
        ],
    )

    assert pool.select().id == "personal-id"


def test_fixed_codex_policy_does_not_constrain_other_providers(monkeypatch):
    credential_pool = _fixed(monkeypatch)
    other = _entry("personal-id", priority=0)
    other.provider = "openrouter"
    pool = credential_pool.CredentialPool("openrouter", [other])

    assert pool.select().id == "personal-id"
