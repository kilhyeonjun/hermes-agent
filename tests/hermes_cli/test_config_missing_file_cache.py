"""Absent configuration files still need cached, fresh, profile-scoped reads."""

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def private_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import hermes_cli.config as config
    from hermes_cli import managed_scope

    config._LOAD_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()
    monkeypatch.setitem(config.DEFAULT_CONFIG, "cache_probe", {
        "value": "default", "token": "${env:HERMES_CONFIG_CACHE_PROBE_TOKEN}",
    })
    monkeypatch.delenv("HERMES_CONFIG_CACHE_PROBE_TOKEN", raising=False)
    yield config, home, managed
    config._LOAD_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()


def _write(path, body):
    path.write_text(body, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_missing_file_cache_is_reused_but_mutable_and_file_updates_stay_isolated(private_config):
    config, home, _managed = private_config
    first = config.load_config_readonly()
    assert config.load_config_readonly() is first
    assert not (home / "config.yaml").exists()

    mutable = config.load_config()
    mutable["cache_probe"]["value"] = "caller mutation"
    assert config.load_config_readonly()["cache_probe"]["value"] == "default"

    path = home / "config.yaml"
    _write(path, json.dumps({"cache_probe": {"value": "user value"}}))
    assert config.load_config_readonly()["cache_probe"]["value"] == "user value"
    _write(path, json.dumps({"cache_probe": {"value": "updated user value"}}))
    assert config.load_config_readonly()["cache_probe"]["value"] == "updated user value"
    _write(path, "cache_probe: [")
    last_good = config.load_config_readonly()
    assert last_good["cache_probe"]["value"] == "updated user value"
    assert config.load_config_readonly() is last_good
    path.unlink()
    defaults = config.load_config_readonly()
    assert defaults["cache_probe"]["value"] == "default"
    assert config.load_config_readonly() is defaults


def test_missing_file_cache_tracks_env_defaults_profiles_and_managed_policy(private_config, monkeypatch):
    config, home, managed = private_config
    name = "HERMES_CONFIG_CACHE_PROBE_TOKEN"
    first = config.load_config_readonly()
    assert config.load_config_readonly() is first
    assert first["cache_probe"]["token"] == "${env:" + name + "}"
    monkeypatch.setenv(name, "process value")
    assert config.load_config_readonly()["cache_probe"]["token"] == "process value"
    monkeypatch.setitem(config.DEFAULT_CONFIG["cache_probe"], "value", "new default")
    refreshed = config.load_config_readonly()
    assert refreshed["cache_probe"]["value"] == "new default"
    assert config.load_config_readonly() is refreshed

    from agent import secret_scope

    was_multiplex = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope({name: "scoped value"})
    try:
        assert config.load_config_readonly()["cache_probe"]["token"] == "scoped value"
    finally:
        secret_scope.reset_secret_scope(token)
    token = secret_scope.set_secret_scope({})
    try:
        # A profile miss must never pick up the process-level value.
        assert config.load_config_readonly()["cache_probe"]["token"] == "${env:" + name + "}"
    finally:
        secret_scope.reset_secret_scope(token)
        secret_scope.set_multiplex_active(was_multiplex)
    assert config.load_config_readonly()["cache_probe"]["token"] == "process value"

    other = home.parent / "other-profile"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    other_config = config.load_config_readonly()
    assert config.load_config_readonly() is other_config
    _write(other / "config.yaml", json.dumps({"cache_probe": {"value": "other profile"}}))
    assert config.load_config_readonly()["cache_probe"]["value"] == "other profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert config.load_config_readonly()["cache_probe"]["value"] == "new default"

    path = managed / "config.yaml"
    _write(path, json.dumps({"cache_probe": {"value": "managed value"}}))
    assert config.load_config_readonly()["cache_probe"]["value"] == "managed value"
    _write(path, json.dumps({"cache_probe": {"value": "managed update"}}))
    assert config.load_config_readonly()["cache_probe"]["value"] == "managed update"
    path.unlink()
    assert config.load_config_readonly()["cache_probe"]["value"] == "new default"
    monkeypatch.delenv(name)
    final = config.load_config_readonly()
    assert final["cache_probe"]["token"] == "${env:" + name + "}"
    assert config.load_config_readonly() is final
