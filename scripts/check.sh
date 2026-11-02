#!/usr/bin/env bash
# Validate the marketplace and every plugin, then run each plugin's tests.
# Exits nonzero on the first failure. Run from anywhere.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if command -v claude >/dev/null 2>&1; then
  echo "== claude plugin validate ."
  claude plugin validate .
  for plugin in plugins/*/; do
    echo "== claude plugin validate ${plugin%/}"
    claude plugin validate "${plugin%/}"
  done
else
  echo "warning: claude not on PATH; skipping plugin validation" >&2
fi

for plugin in plugins/*/; do
  echo "== tests: ${plugin%/}"
  (cd "$plugin" && python3 -m unittest discover -s tests)
done
