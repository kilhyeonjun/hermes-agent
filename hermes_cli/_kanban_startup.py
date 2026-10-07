"""Lightweight parser path for unambiguous Kanban CLI invocations."""

from __future__ import annotations

import contextlib
import io


def parse_kanban_args(argv):
    """Return parsed args for a leading ``kanban`` command, else ``None``.

    Invalid Kanban arguments use the full parser so its canonical root error
    output stays unchanged. Help exits directly after argparse prints it.
    """
    if not argv or argv[0] != "kanban":
        return None

    from hermes_cli._parser import build_top_level_parser
    from hermes_cli.kanban_parser import build_parser

    parser, subparsers, _chat_parser = build_top_level_parser()
    build_parser(subparsers)
    subparsers.required = True

    with contextlib.redirect_stderr(io.StringIO()):
        try:
            return parser.parse_args(argv)
        except SystemExit as exc:
            if exc.code == 0:
                raise
    return None
