"""Discovery must deliver Kanban hooks without preparing unconfigured credentials."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


def _private_cli_env(tmp_path):
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    env = os.environ | {"HOME": str(home), "HERMES_HOME": str(hermes_home)}
    for key in list(env):
        if key.startswith("HERMES_DASHBOARD_"):
            env.pop(key)
    return hermes_home, env


@pytest.mark.parametrize("basic_settings", [{}, {"username": "admin"}])
def test_cold_kanban_hook_does_not_hash_unconfigured_basic_credentials(tmp_path, basic_settings):
    home, env = _private_cli_env(tmp_path)
    plugin = home / "plugins" / "record-block"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(json.dumps({"name": "record-block"}))
    (plugin / "__init__.py").write_text("""
import json
import sqlite3
from hermes_constants import get_hermes_home

def register(ctx):
    def blocked(task_id, **kwargs):
        home = get_hermes_home()
        with sqlite3.connect(home / 'kanban.db') as conn:
            state = conn.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
        with (home / 'observed.jsonl').open('a') as out:
            out.write(json.dumps({'task_id': task_id, 'status': state}) + '\\n')
    ctx.register_hook('kanban_task_blocked', blocked)
""")
    (home / "config.yaml").write_text(json.dumps({
        "plugins": {"enabled": ["record-block"]},
        "dashboard": {"basic_auth": basic_settings},
    }))
    create = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "create", "private fixture", "--json"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert create.returncode == 0, create.stderr
    task_id = json.loads(create.stdout)["id"]
    script = """
import hashlib
import sys

real_scrypt = hashlib.scrypt
calls = []
def observed_scrypt(*args, **kwargs):
    calls.append(1)
    return real_scrypt(*args, **kwargs)
hashlib.scrypt = observed_scrypt

from hermes_cli.main import main
sys.argv = ['hermes', 'kanban', 'block', sys.argv[1], 'private hook fixture']
assert main() in (None, 0)
assert not calls, 'Unconfigured BasicAuth performed a credential KDF during Kanban discovery'
"""
    result = subprocess.run(
        [sys.executable, "-c", script, task_id],
        env=env, capture_output=True, text=True, timeout=30,
    )
    observed = [json.loads(line) for line in (home / "observed.jsonl").read_text().splitlines()]
    assert observed == [{"task_id": task_id, "status": "blocked"}]
    assert result.returncode == 0, result.stderr


def test_discovered_basic_provider_verifies_each_bad_login_with_real_scrypt(tmp_path):
    home, env = _private_cli_env(tmp_path)
    (home / "config.yaml").write_text(json.dumps({"dashboard": {"basic_auth": {
        "username": "admin", "password": "correct-fixture-password", "secret": "test-signing-secret-for-fixture-32",
    }}}))
    script = """
import hashlib
from hermes_cli.plugins import discover_plugins
from hermes_cli.dashboard_auth import get_provider, InvalidCredentialsError

discover_plugins()
provider = get_provider('basic')
assert provider is not None
real_scrypt = hashlib.scrypt
calls = []
def observed_scrypt(*args, **kwargs):
    calls.append(tuple(kwargs[k] for k in ('n', 'r', 'p', 'dklen')))
    return real_scrypt(*args, **kwargs)
hashlib.scrypt = observed_scrypt

parameters = None
for username, password in [('admin', 'wrong'), ('ghost', 'correct-fixture-password'),
                           ('ghost', 'dummy-password-for-constant-time-verify')]:
    calls.clear()
    try:
        provider.complete_password_login(username=username, password=password)
    except InvalidCredentialsError:
        pass
    else:
        raise AssertionError('Invalid credentials were accepted')
    assert len(calls) == 1, 'Each rejected login must still perform one real KDF'
    parameters = parameters or calls[0]
    assert calls[0] == parameters
calls.clear()
session = provider.complete_password_login(username='admin', password='correct-fixture-password')
assert len(calls) == 1 and calls[0] == parameters
assert session.user_id == 'admin'
assert provider.verify_session(access_token=session.access_token) is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
