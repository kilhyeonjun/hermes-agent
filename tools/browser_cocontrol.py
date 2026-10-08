"""Opt-in task-bound local co-control transport; never fall back once configured."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat


def route_cocontrol(action, args, *, task_id=None, session_id=None):
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly().get("browser", {}).get("cocontrol")
    if config is None or isinstance(config, dict) and config.get("enabled") is not True:
        return None
    try:
        if not isinstance(config, dict):
            raise ValueError("invalid_config")
        from gateway.session_context import get_session_env
        actual_session = session_id or get_session_env("HERMES_SESSION_ID", "")
        actual_task = task_id or actual_session
        if (not actual_session or not actual_task or actual_session != config.get("session_id") or
                actual_task != config.get("task_id")):
            raise ValueError("wrong_task")
        allowed = {"browser_navigate", "browser_snapshot", "browser_click", "browser_type", "browser_scroll",
                   "browser_back", "browser_press", "browser_tabs", "browser_tab_activate"}
        if action not in allowed:
            raise ValueError("unsupported_action")
        path = Path(config["socket"])
        parent, entry = path.parent.lstat(), path.lstat()
        if (not path.is_absolute() or path.parent.resolve() != path.parent or
                not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) != 0o700 or
                parent.st_uid != os.getuid() or not stat.S_ISSOCK(entry.st_mode) or
                stat.S_IMODE(entry.st_mode) != 0o600 or entry.st_uid != os.getuid()):
            raise ValueError("unsafe_socket")
        request = json.dumps({"task_id": actual_task, "session_id": actual_session,
                              "action": action, "args": args}, ensure_ascii=True).encode() + b"\n"
        if len(request) > 16384:
            raise ValueError("request_too_large")
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(12)
            connection.connect(str(path))
            connection.sendall(request)
            with connection.makefile("rb") as stream:
                raw = stream.readline(65537)
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise ValueError("invalid_response")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("invalid_response")
            return json.dumps(result, ensure_ascii=False)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return json.dumps({"success": False, "error": "Co-control browser unavailable, paused, or unauthorized."})
