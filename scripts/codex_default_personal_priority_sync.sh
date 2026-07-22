#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export HERMES_HOME="${HERMES_DEFAULT_HOME:-${HOME}/.hermes}"
exec "${script_dir}/codex_reset_aware_priority_sync.sh" \
  --profile-account personal \
  --skip-cliproxy \
  "$@"
