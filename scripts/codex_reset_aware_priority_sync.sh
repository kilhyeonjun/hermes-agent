#!/usr/bin/env bash
set -euo pipefail

agent_root="${HERMES_AGENT_ROOT:-${HOME}/.hermes/hermes-agent}"
python_bin="${HERMES_AGENT_PYTHON:-${agent_root}/venv/bin/python}"
if [[ ! -x "${python_bin}" && -x "${agent_root}/.venv/bin/python" ]]; then
  python_bin="${agent_root}/.venv/bin/python"
fi
if [[ ! -x "${python_bin}" ]]; then
  echo "Codex priority sync Python is unavailable" >&2
  exit 1
fi

exec "${python_bin}" -m hermes_cli.codex_priority_sync "$@"
