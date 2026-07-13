#!/usr/bin/env python3
"""Compatibility entrypoint for the canonical Codex priority sync module."""

from hermes_cli.codex_priority_sync import main


if __name__ == "__main__":
    raise SystemExit(main())
