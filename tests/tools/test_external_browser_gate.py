"""An external browser owner must fail closed when its plugin is unavailable."""

import json

import pytest

from tools import browser_tool_install as install
from tools import browser_tool_session as session
from tools import browser_use_cli as cli


@pytest.fixture
def external(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {"browser": {"backend": "external", "cloud_provider": "browser-use"}},
    )


def forbidden_legacy(*args, **kwargs):
    raise AssertionError("external owner fell back to a legacy browser")


def test_external_owner_hides_stock_tools_before_cdp_or_cloud_resolution(external, monkeypatch):
    monkeypatch.setattr(install._cdp, "_get_cdp_override_raw", forbidden_legacy)
    monkeypatch.setattr(install._cloud, "_get_cloud_provider", forbidden_legacy)
    assert install.check_browser_requirements() is False


def test_external_owner_refuses_direct_stock_command_without_starting_a_browser(external, monkeypatch):
    monkeypatch.setattr(session, "_browser_command_preflight", forbidden_legacy)
    result = session._run_browser_command("synthetic-task", "open", ["https://example.com"])
    assert result["success"] is False
    assert result["error_code"] == "external_browser_required"


def test_external_owner_cannot_execute_python_through_legacy_cli(external, monkeypatch):
    monkeypatch.setattr(cli, "_find_cli", forbidden_legacy)
    result = json.loads(cli.browser_exec('print("must not execute")', task_id="synthetic-task"))
    assert result["error_code"] == "external_browser_required"
    assert "external" in result["error"].lower()


def test_external_backend_never_advertises_browser_use_exec(external, monkeypatch):
    monkeypatch.setattr(cli, "_find_cli", forbidden_legacy)
    assert cli.is_browser_use_cli_mode() is False
