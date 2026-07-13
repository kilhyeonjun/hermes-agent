"""Unit tests for scripts/docker_rebootstrap_nous_session.py.

The boot-time re-seed is the load-bearing "does not clobber a healthy session"
guard: it must overwrite the on-disk Nous provider entry ONLY when that entry is
provably terminal (quarantine marker + no usable tokens), and no-op in every
other case. These are pure-stdlib tmp_path tests (no container build).
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

# Import the stdlib-only boot helper by path (it lives under scripts/, not an
# installed package) — mirrors the repo's other scripts/-helper tests.
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "docker_rebootstrap_nous_session.py"
_spec = importlib.util.spec_from_file_location("docker_rebootstrap_nous_session", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _terminal_nous_state():
    """On-disk shape after a terminal quarantine: tokens cleared, marker set."""
    return {
        "portal_base_url": "https://portal.example.com",
        "client_id": "hermes-cli-vps",
        "last_auth_error": {
            "provider": "nous",
            "code": "invalid_grant",
            "relogin_required": True,
        },
    }


def _healthy_nous_state():
    return {
        "portal_base_url": "https://portal.example.com",
        "client_id": "hermes-cli-vps",
        "access_token": "live-at",
        "refresh_token": "live-rt",
    }


def _write_auth(tmp_path: Path, providers: dict) -> str:
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"version": 1, "providers": providers}))
    return str(p)


_FRESH_SEED = json.dumps({
    "version": 1,
    "providers": {
        "nous": {
            "portal_base_url": "https://portal.example.com",
            "client_id": "hermes-cli-vps",
            "access_token": "FRESH-at",
            "refresh_token": "FRESH-rt",
        }
    },
})


def test_reseeds_terminal_entry(tmp_path):
    """Terminal on-disk entry + valid seed → providers.nous replaced."""
    auth = _write_auth(tmp_path, {"nous": _terminal_nous_state()})
    result = mod.reseed_if_terminal(auth, _FRESH_SEED)
    assert result == "reseeded"
    store = json.loads(Path(auth).read_text())
    assert store["providers"]["nous"]["refresh_token"] == "FRESH-rt"
    assert "last_auth_error" not in store["providers"]["nous"]


def test_does_not_clobber_healthy_entry(tmp_path):
    """LOAD-BEARING: a healthy (live-token) entry must never be overwritten."""
    auth = _write_auth(tmp_path, {"nous": _healthy_nous_state()})
    result = mod.reseed_if_terminal(auth, _FRESH_SEED)
    assert result == "not_terminal"
    store = json.loads(Path(auth).read_text())
    # Untouched — still the live tokens, not the seed.
    assert store["providers"]["nous"]["refresh_token"] == "live-rt"


def test_marker_but_live_token_is_not_terminal(tmp_path):
    """Stale marker + a live token present → NOT terminal (don't clobber)."""
    state = _terminal_nous_state()
    state["refresh_token"] = "somehow-live"
    auth = _write_auth(tmp_path, {"nous": state})
    assert mod.reseed_if_terminal(auth, _FRESH_SEED) == "not_terminal"


def test_preserves_other_providers(tmp_path):
    """Re-seed swaps ONLY providers.nous; other providers survive intact."""
    auth = _write_auth(tmp_path, {
        "nous": _terminal_nous_state(),
        "openai-codex": {"tokens": {"access_token": "codex-at"}},
    })
    assert mod.reseed_if_terminal(auth, _FRESH_SEED) == "reseeded"
    store = json.loads(Path(auth).read_text())
    assert store["providers"]["openai-codex"]["tokens"]["access_token"] == "codex-at"
    assert store["providers"]["nous"]["refresh_token"] == "FRESH-rt"


def test_reseed_uses_canonical_revision_and_preserves_unrelated_state(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "_auth_revision": 7,
                "active_provider": "openai-codex",
                "providers": {
                    "nous": _terminal_nous_state(),
                    "openai-codex": {"opaque": "preserve"},
                },
                "credential_pool": {
                    "openai-codex": [{"id": "keep", "opaque": "pool"}]
                },
                "unrelated": {"sequence": 42},
            }
        )
    )

    assert mod.reseed_if_terminal(str(path), _FRESH_SEED) == "reseeded"

    store = json.loads(path.read_text())
    assert store["_auth_revision"] == 8
    assert store["active_provider"] == "openai-codex"
    assert store["providers"]["openai-codex"] == {"opaque": "preserve"}
    assert store["credential_pool"]["openai-codex"][0]["opaque"] == "pool"
    assert store["unrelated"] == {"sequence": 42}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_protocol_unavailable_never_falls_back_to_raw_replace(
    tmp_path,
    monkeypatch,
):
    auth = _write_auth(tmp_path, {"nous": _terminal_nous_state()})
    before = Path(auth).read_bytes()
    monkeypatch.setattr(mod, "_load_auth_protocol", lambda: None)

    assert mod.reseed_if_terminal(auth, _FRESH_SEED) == "protocol_unavailable"
    assert Path(auth).read_bytes() == before


def test_concurrent_reseed_has_exactly_one_writer(tmp_path):
    auth = _write_auth(tmp_path, {"nous": _terminal_nous_state()})

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: mod.reseed_if_terminal(auth, _FRESH_SEED),
                range(2),
            )
        )

    assert sorted(results) == ["not_terminal", "reseeded"]
    store = json.loads(Path(auth).read_text())
    assert store["_auth_revision"] == 1
    assert store["providers"]["nous"]["refresh_token"] == "FRESH-rt"


def test_first_bootstrap_uses_canonical_writer(tmp_path):
    auth = tmp_path / "auth.json"

    assert mod.bootstrap_if_absent(str(auth), _FRESH_SEED) == "bootstrapped"

    store = json.loads(auth.read_text())
    assert store["_auth_revision"] == 1
    assert store["providers"]["nous"]["refresh_token"] == "FRESH-rt"
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600


def test_first_bootstrap_never_clobbers_existing_store(tmp_path):
    auth = Path(_write_auth(tmp_path, {"nous": _healthy_nous_state()}))
    before = auth.read_bytes()

    assert mod.bootstrap_if_absent(str(auth), _FRESH_SEED) == "already_exists"
    assert auth.read_bytes() == before


def test_first_bootstrap_rejects_malformed_seed_without_creating_file(tmp_path):
    auth = tmp_path / "auth.json"

    assert mod.bootstrap_if_absent(str(auth), "}{bad") == "bad_seed"
    assert not auth.exists()


def test_concurrent_first_bootstrap_has_exactly_one_writer(tmp_path):
    auth = tmp_path / "auth.json"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: mod.bootstrap_if_absent(str(auth), _FRESH_SEED),
                range(2),
            )
        )

    assert sorted(results) == ["already_exists", "bootstrapped"]
    assert json.loads(auth.read_text())["_auth_revision"] == 1


def test_first_bootstrap_rejects_symlink_target(tmp_path):
    real = tmp_path / "real-auth.json"
    real.write_text('{"sentinel": true}\n', encoding="utf-8")
    alias = tmp_path / "auth.json"
    alias.symlink_to(real)
    before = real.read_bytes()

    assert mod.bootstrap_if_absent(str(alias), _FRESH_SEED) == "unsafe_path"
    assert real.read_bytes() == before


def test_first_bootstrap_fails_closed_without_protocol(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    monkeypatch.setattr(mod, "_load_auth_protocol", lambda: None)

    assert mod.bootstrap_if_absent(str(auth), _FRESH_SEED) == "protocol_unavailable"
    assert not auth.exists()


def test_first_bootstrap_requests_non_superseding_create_only_transaction(
    tmp_path,
    monkeypatch,
):
    auth = tmp_path / "auth.json"
    calls = []

    class Conflict(RuntimeError):
        pass

    class Corrupt(RuntimeError):
        pass

    def _replace(candidate, **kwargs):
        calls.append((candidate, kwargs))
        auth.write_text('{"version": 1, "providers": {}}', encoding="utf-8")

    protocol = SimpleNamespace(
        AuthStoreConflictError=Conflict,
        AuthStoreCorruptError=Corrupt,
        replace_auth_store_from_snapshot=_replace,
    )
    monkeypatch.setattr(
        mod,
        "_load_bootstrap_auth_protocol",
        lambda: protocol,
        raising=False,
    )

    assert mod.bootstrap_if_absent(str(auth), _FRESH_SEED) == "bootstrapped"
    assert len(calls) == 1
    assert calls[0][1]["require_absent"] is True
    assert calls[0][1]["supersede_ambiguous"] is False


def test_rebootstrap_rejects_seed_without_both_usable_tokens(tmp_path):
    auth = _write_auth(tmp_path, {"nous": _terminal_nous_state()})
    for nous in (
        {"access_token": "only-access"},
        {"refresh_token": "only-refresh"},
        {"access_token": "", "refresh_token": "refresh"},
    ):
        seed = json.dumps({"version": 1, "providers": {"nous": nous}})
        assert mod.reseed_if_terminal(auth, seed) == "bad_seed"


def test_rebootstrap_rejects_terminal_marked_seed(tmp_path):
    auth = _write_auth(tmp_path, {"nous": _terminal_nous_state()})
    terminal_seed = _healthy_nous_state()
    terminal_seed["last_auth_error"] = {
        "code": "invalid_grant",
        "relogin_required": True,
    }

    assert mod.reseed_if_terminal(
        auth,
        json.dumps({"version": 1, "providers": {"nous": terminal_seed}}),
    ) == "bad_seed"


def test_bootstrap_cli_returns_failure_for_protocol_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "bootstrap_if_absent", lambda *_args: "protocol_error")
    monkeypatch.setenv(mod.BOOTSTRAP_ENV, _FRESH_SEED)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(_SCRIPT), "--bootstrap", str(tmp_path / "auth.json")],
    )

    assert mod.main() == 1


def test_first_bootstrap_serializes_two_independent_processes(tmp_path):
    auth = tmp_path / "auth.json"
    env = os.environ.copy()
    env[mod.BOOTSTRAP_ENV] = _FRESH_SEED
    repo_root = str(_SCRIPT.parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (repo_root, env.get("PYTHONPATH", "")) if item
    )
    command = [sys.executable, str(_SCRIPT), "--bootstrap", str(auth)]
    processes = [
        subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    outcomes = [process.communicate(timeout=15) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], outcomes
    assert sum("created auth.json" in stdout for stdout, _stderr in outcomes) == 1
    assert json.loads(auth.read_text(encoding="utf-8"))["_auth_revision"] == 1


def test_stage2_consumes_auth_seed_environment_before_supervision():
    stage2 = (_SCRIPT.parents[1] / "docker" / "stage2-hook.sh").read_text(
        encoding="utf-8"
    )

    assert "auth_bootstrap_seed=${HERMES_AUTH_JSON_BOOTSTRAP:-}" in stage2
    assert "auth_rebootstrap_seed=${HERMES_AUTH_JSON_REBOOTSTRAP:-}" in stage2
    assert "unset HERMES_AUTH_JSON_BOOTSTRAP HERMES_AUTH_JSON_REBOOTSTRAP" in stage2
    assert 'rm -f "$S6_CONTAINER_ENV_DIR/HERMES_AUTH_JSON_BOOTSTRAP"' in stage2
    assert '"$S6_CONTAINER_ENV_DIR/HERMES_AUTH_JSON_REBOOTSTRAP"' in stage2
    assert (
        'as_hermes env HERMES_AUTH_JSON_BOOTSTRAP="$auth_bootstrap_seed"'
        in stage2
    )
    assert (
        'as_hermes env HERMES_AUTH_JSON_REBOOTSTRAP="$auth_rebootstrap_seed"'
        in stage2
    )
    assert "unset auth_bootstrap_seed auth_rebootstrap_seed" in stage2


def test_no_seed_is_noop(tmp_path):
    auth = _write_auth(tmp_path, {"nous": _terminal_nous_state()})
    assert mod.reseed_if_terminal(auth, "") == "no_seed"


def test_bad_seed_is_noop(tmp_path):
    auth = _write_auth(tmp_path, {"nous": _terminal_nous_state()})
    assert mod.reseed_if_terminal(auth, "}{not json") == "bad_seed"
    # Original terminal entry left untouched.
    store = json.loads(Path(auth).read_text())
    assert store["providers"]["nous"]["last_auth_error"]["relogin_required"] is True


def test_seed_without_nous_entry_is_noop(tmp_path):
    auth = _write_auth(tmp_path, {"nous": _terminal_nous_state()})
    seed = json.dumps({"version": 1, "providers": {"openai-codex": {}}})
    assert mod.reseed_if_terminal(auth, seed) == "bad_seed"


def test_absent_auth_file_defers_to_bootstrap(tmp_path):
    """No auth.json → blank volume; the normal *_BOOTSTRAP path handles it."""
    auth = str(tmp_path / "auth.json")
    assert mod.reseed_if_terminal(auth, _FRESH_SEED) == "no_auth_file"


def test_unreadable_auth_file_is_left_alone(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text("}{ corrupt")
    assert mod.reseed_if_terminal(str(p), _FRESH_SEED) == "auth_unreadable"
    # Not overwritten.
    assert p.read_text() == "}{ corrupt"


def test_terminal_entry_missing_marker_is_not_terminal(tmp_path):
    """No last_auth_error at all (e.g. a merely-expired but not-quarantined
    entry) → not terminal, no re-seed."""
    auth = _write_auth(tmp_path, {"nous": {"client_id": "hermes-cli-vps"}})
    assert mod.reseed_if_terminal(auth, _FRESH_SEED) == "not_terminal"
