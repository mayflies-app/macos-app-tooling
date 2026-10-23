#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  '') ;;
  --help|-h) printf 'Usage: scripts/check.sh\nOffline shared-tool tests and workflow checks; no app checkouts required.\n'; exit 0 ;;
  *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
esac
[[ $# -eq 0 ]] || { printf 'Unexpected arguments\n' >&2; exit 2; }
TOOLING_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$TOOLING_ROOT"
python3 -B -m unittest discover -s templates/macos-app/tests -p 'test_*.py'
if command -v actionlint >/dev/null; then
  actionlint -config-file /dev/null -ignore 'label "xcode-27" is unknown' \
    .github/workflows/*.yml templates/macos-app/ci.yml templates/macos-app/release.yml
else
  printf 'SKIP: actionlint is not installed\n'
fi
printf 'PASS: shared-tool checks completed\n'
