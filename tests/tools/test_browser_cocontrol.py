"""The opt-in local lane is authoritative for every registered browser action."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading

from tools.browser_extension_router import routed_browser_handler


def configure(path):
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps({"browser": {"cocontrol": {
        "enabled": True, "socket": str(path), "task_id": "task", "session_id": "session",
    }}}))


def no_legacy():
    raise AssertionError("configured co-control lane fell back to a different browser")


def test_registered_lane_uses_private_socket_and_same_task():
    with tempfile.TemporaryDirectory(prefix="hc-", dir="/tmp") as directory:
        parent = Path(directory).resolve()
        path = parent / "a.sock"
        configure(path)
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        path.chmod(0o600)
        server.listen()
        seen = []
        def receive():
            connection, _ = server.accept()
            with connection:
                request = json.loads(connection.makefile("rb").readline())
                seen.append(request)
                connection.sendall(b'{"ok":true}\n')
        thread = threading.Thread(target=receive, daemon=True)
        thread.start()
        try:
            result = routed_browser_handler("browser_click", {"ref": "fixture"}, fallback=no_legacy,
                                            task_id="task", session_id="session")
            assert json.loads(result)["ok"] is True
            thread.join(2)
            assert seen == [{"task_id": "task", "session_id": "session", "action": "browser_click",
                             "args": {"ref": "fixture"}}]
        finally:
            server.close()


def test_authoritative_lane_rejects_wrong_identity_missing_socket_and_privileged_actions():
    with tempfile.TemporaryDirectory(prefix="hc-", dir="/tmp") as directory:
        path = Path(directory).resolve() / "missing.sock"
        configure(path)
        for action in ("browser_snapshot", "browser_exec", "browser_dialog", "browser_cdp",
                       "browser_console", "browser_vision", "browser_get_images"):
            result = routed_browser_handler(action, {}, fallback=no_legacy, task_id="task", session_id="session")
            assert "error" in json.loads(result)
        result = routed_browser_handler("browser_click", {}, fallback=no_legacy,
                                        task_id="other-task", session_id="session")
        assert "error" in json.loads(result)


def test_opt_in_advertises_regular_browser_tools_without_other_backend(monkeypatch):
    configure(Path("/private/tmp/owned-cocontrol/agent.sock"))
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "config.yaml"
    config = json.loads(path.read_text())
    config["browser"]["backend"] = "browser-use"
    path.write_text(json.dumps(config))
    from tools.browser_use_cli import is_browser_use_cli_mode
    from tools import browser_tool_install
    def missing(**kwargs):
        raise FileNotFoundError("synthetic missing legacy CLI")
    monkeypatch.setattr(browser_tool_install, "_find_agent_browser", missing)
    assert not is_browser_use_cli_mode()
    assert browser_tool_install.check_browser_requirements()
