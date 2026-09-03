#!/usr/bin/env bash
# Run the test suite. No network access is required or attempted.
set -euo pipefail
cd "$(dirname "$0")"

source ./scripts/venv.sh
ensure_venv

exec "$VENV_PY" -m pytest "$@"
