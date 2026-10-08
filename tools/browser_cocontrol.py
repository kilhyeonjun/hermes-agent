"""Opt-in task-bound local co-control transport; never fall back once configured."""
from __future__ import annotations

import json
import hashlib
import hmac
import secrets
import time
import os
from pathlib import Path
import socket
import stat


def cocontrol_enabled():
    from hermes_cli.config import load_config_readonly
    browser = load_config_readonly().get("browser", {})
    config = browser.get("cocontrol") if isinstance(browser, dict) else None
    return isinstance(config, dict) and config.get("enabled") is True


def route_cocontrol(action, args, *, task_id=None, session_id=None):
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly().get("browser", {}).get("cocontrol")
    if config is None or isinstance(config, dict) and config.get("enabled") is not True:
        return None
    try:
        if not isinstance(config, dict):
            raise ValueError("invalid_config")
        from gateway.session_context import get_session_env, get_bound_session_env, session_context_engaged
        bound = get_bound_session_env("HERMES_SESSION_ID", "")
        context = (bound if session_context_engaged()
                   else get_session_env("HERMES_SESSION_ID", ""))
        if session_context_engaged() and (not context or session_id is not None and session_id != context):
            raise ValueError("wrong_context")
        actual_session = context or session_id
        if isinstance(config.get("bindings"), dict):
            if not bound or session_id is not None and session_id != bound:
                raise ValueError("unbound_or_wrong_context")
            actual_session = bound
            config = config["bindings"].get(actual_session)
            if not isinstance(config, dict):
                raise ValueError("unbound_context")
            actual_task = config.get("task_id")
            if task_id is not None and task_id != actual_task:
                raise ValueError("wrong_task")
            portal_session = config.get("session_id")
        else:
            actual_task = task_id or actual_session
            portal_session = actual_session
            if actual_session != config.get("session_id") or actual_task != config.get("task_id"):
                raise ValueError("wrong_task")
        if not actual_session or not actual_task or not portal_session:
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
        body = {"task_id": actual_task, "session_id": portal_session, "action": action, "args": args}
        if "runtime" in config:
            marker = Path(config["runtime"]) / "browser-harness-session.json"
            fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError("unsafe_marker")
                raw = stream.read(16385)
                if len(raw) > 16384:raise ValueError("oversize_marker")
                record = json.loads(raw)
            if (record.get("active") is not True or record.get("task_id") != actual_task or
                record.get("session_id") != portal_session or record.get("agent_socket") != str(path)):
                raise ValueError("wrong_binding")
            body.update(owner=record["owner"], claim_nonce=record["claim_nonce"],
                        request_id=secrets.token_hex(16), expires_at=time.time()+10)
            canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            body["signature"] = hmac.new(bytes.fromhex(record["agent_key"]), canonical, hashlib.sha256).hexdigest()
        request = json.dumps(body, ensure_ascii=True).encode() + b"\n"
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
