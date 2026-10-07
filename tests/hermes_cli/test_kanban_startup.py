"""Startup contracts for the lightweight Kanban CLI path."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


def _run_kanban_cli(argv, home, *, force_full_parser=False):
    _prepare_profile(argv, home)

    script = """
import json
import sys

force_full_parser, serialized_argv = sys.argv[1:3]
sys.argv = ['hermes', *json.loads(serialized_argv)]
from hermes_cli import _kanban_startup

if json.loads(force_full_parser):
    _kanban_startup.parse_kanban_args = lambda argv: None

from hermes_cli import main

main.main()
"""
    env = os.environ | {"HOME": str(home), "HERMES_HOME": str(home / ".hermes")}
    return subprocess.run(
        [sys.executable, "-c", script, str(force_full_parser).lower(), json.dumps(argv)],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
    )


def _prepare_profile(argv, home):
    if argv[:2] == ["-p", "work"]:
        profile_home = home / ".hermes" / "profiles" / "work"
        profile_home.mkdir(parents=True)
        (profile_home / "config.yaml").write_text("{}\n")


def _visible_result(result, home):
    return (
        result.returncode,
        result.stdout.replace(str(home), "<HOME>"),
        result.stderr.replace(str(home), "<HOME>"),
    )


@pytest.mark.parametrize(
    "argv",
    [["kanban", "init"], ["-p", "work", "kanban", "init"]],
)
def test_kanban_init_starts_without_unrelated_parser_builder(argv, tmp_path):
    """Kanban must reach its real database command without loading doctor CLI."""
    script = """
import builtins
import json
import os
import sys

_real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == 'hermes_cli.subcommands.doctor':
        raise AssertionError('unrelated doctor parser was imported')
    return _real_import(name, *args, **kwargs)

sys.argv = ['hermes', *json.loads(sys.argv[1])]
builtins.__import__ = guarded_import
from hermes_cli import main
main.main()
"""
    temp_home = tmp_path / "home"
    _prepare_profile(argv, temp_home)
    env = os.environ | {
        "HOME": str(temp_home),
        "HERMES_HOME": str(temp_home / ".hermes"),
    }

    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(argv)],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert (temp_home / ".hermes" / "kanban.db").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["kanban", "ls", "--json"],
        ["kanban", "--help"],
        ["kanban", "missing-action"],
        ["-p", "work", "kanban", "init"],
    ],
)
def test_kanban_cli_matches_full_parser_output_and_exit(argv, tmp_path):
    """Leading Kanban parsing preserves the full parser's visible contract."""
    fast_home = tmp_path / "fast"
    full_home = tmp_path / "full"
    fast = _run_kanban_cli(argv, fast_home)
    full = _run_kanban_cli(argv, full_home, force_full_parser=True)

    assert _visible_result(fast, fast_home) == _visible_result(full, full_home)
    if argv[:2] == ["-p", "work"]:
        assert fast.returncode == full.returncode == 0
        assert (fast_home / ".hermes" / "kanban.db").exists()
