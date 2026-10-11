"""A fresh Kanban database command must not require the agent's compression engine."""

import os
import sqlite3
import subprocess
import sys

import pytest


@pytest.mark.parametrize("command", ["init", "list"])
def test_kanban_database_commands_do_not_require_context_compression(command, tmp_path):
    # Importing compressor/auxiliary code here regresses CLI isolation even when
    # those modules happen to be installed. Exercise the real parser and database.
    script = """
import builtins
import sys

original_import = builtins.__import__
blocked = {'agent.context_compressor', 'agent.auxiliary_client', 'agent.credential_pool'}

def import_without_compression(name, *args, **kwargs):
    if name in blocked:
        raise AssertionError('Kanban required unrelated agent module: ' + name)
    return original_import(name, *args, **kwargs)

builtins.__import__ = import_without_compression
from hermes_cli._kanban_startup import parse_kanban_args
from hermes_cli.kanban import kanban_command

argv = ['kanban', sys.argv[1]]
if sys.argv[1] == 'list':
    argv.append('--json')
sys.exit(kanban_command(parse_kanban_args(argv)))
"""
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    env = os.environ | {"HOME": str(home), "HERMES_HOME": str(hermes_home)}
    result = subprocess.run(
        [sys.executable, "-c", script, command],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    if command == "list":
        assert result.stdout.strip() == "[]"
    path = hermes_home / "kanban.db"
    assert path.is_file()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
