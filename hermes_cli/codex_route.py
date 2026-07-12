"""Top-level CLI adapter for Codex credential routing control."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

CONTROL_SCRIPT = Path.home() / ".hermes" / "scripts" / "codex_route_control.py"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes codex-route",
        description="Show or set Codex routing across Hermes profiles and CLIProxy.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="status",
        choices=["status", "auto", "personal", "company"],
    )
    args = parser.parse_args(argv)

    if not CONTROL_SCRIPT.exists():
        print(f"Codex routing control script not found: {CONTROL_SCRIPT}", file=sys.stderr)
        return 1

    try:
        proc = subprocess.run(
            [sys.executable, str(CONTROL_SCRIPT), args.mode],
            text=True,
            capture_output=True,
            timeout=180,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Codex routing command failed: {exc}", file=sys.stderr)
        return 1

    output = proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
