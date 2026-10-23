#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLING_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
exec python3 -B "${TOOLING_ROOT}/templates/macos-app/shared/tests/run_checks.py" "$@"
