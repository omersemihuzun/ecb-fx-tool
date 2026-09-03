#!/usr/bin/env bash
# Start the service. Creates a virtualenv on first run.
set -euo pipefail
cd "$(dirname "$0")"

source ./scripts/venv.sh
ensure_venv

PORT="${PORT:-8080}"
echo "fx-tool listening on http://127.0.0.1:${PORT} (upstream: ${FX_UPSTREAM_BASE:-https://api.frankfurter.dev})"
exec "$VENV_PY" -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port "$PORT"
