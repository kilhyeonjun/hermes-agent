import plistlib

import pytest

from hermes_cli import gateway


def _patch_plists(monkeypatch, tmp_path, installed, expected, *, binary=False):
    path = tmp_path / "gateway.plist"
    path.write_bytes(
        plistlib.dumps(installed, fmt=plistlib.FMT_BINARY if binary else plistlib.FMT_XML)
    )
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: path)
    monkeypatch.setattr(
        gateway,
        "generate_launchd_plist",
        lambda: plistlib.dumps(expected).decode("utf-8"),
    )


def test_semantic_plist_comparison_ignores_format_order_and_path(tmp_path, monkeypatch):
    installed = {
        "Label": "ai.hermes.gateway",
        "EnvironmentVariables": {"PATH": "/installed", "HERMES_HOME": "/home"},
        "RunAtLoad": True,
    }
    expected = {
        "RunAtLoad": True,
        "EnvironmentVariables": {"HERMES_HOME": "/home", "PATH": "/generated"},
        "Label": "ai.hermes.gateway",
    }
    _patch_plists(monkeypatch, tmp_path, installed, expected, binary=True)
    assert gateway.launchd_plist_is_current() is True


@pytest.mark.parametrize("bad_path", [42, True])
def test_semantic_plist_comparison_rejects_non_string_path(
    bad_path, tmp_path, monkeypatch
):
    _patch_plists(
        monkeypatch,
        tmp_path,
        {"EnvironmentVariables": {"PATH": bad_path}},
        {"EnvironmentVariables": {"PATH": "/generated"}},
    )
    assert gateway.launchd_plist_is_current() is False


def test_semantic_plist_comparison_preserves_value_types(tmp_path, monkeypatch):
    _patch_plists(
        monkeypatch,
        tmp_path,
        {"RunAtLoad": 1, "EnvironmentVariables": {"PATH": "/installed"}},
        {"RunAtLoad": True, "EnvironmentVariables": {"PATH": "/generated"}},
    )
    assert gateway.launchd_plist_is_current() is False


def test_semantic_plist_comparison_rejects_malformed_bytes(tmp_path, monkeypatch):
    path = tmp_path / "gateway.plist"
    path.write_bytes(b"\xff\xfe-not-a-plist")
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: path)
    monkeypatch.setattr(
        gateway,
        "generate_launchd_plist",
        lambda: plistlib.dumps({"Label": "ai.hermes.gateway"}).decode("utf-8"),
    )
    assert gateway.launchd_plist_is_current() is False
