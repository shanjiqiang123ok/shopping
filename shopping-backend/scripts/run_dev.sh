#!/usr/bin/env bash
# Start both services locally: no Docker, no MySQL.
#   ./scripts/run_dev.sh                 # agent uses the in-process driver
#   BACKEND_DRIVER=http ./scripts/run_dev.sh
set -euo pipefail
cd "$(dirname "$0")/.."

AGENT_PORT="${AGENT_PORT:-8000}"
BUSINESS_PORT="${BUSINESS_PORT:-8001}"
BACKEND_DRIVER="${BACKEND_DRIVER:-inprocess}"
export BACKEND_DRIVER

uv run python -m uvicorn shopping_backend.business_api.main:app --host 127.0.0.1 --port "$BUSINESS_PORT" &
BUSINESS_PID=$!

uv run python -m uvicorn shopping_backend.agent_api.main:app --host 127.0.0.1 --port "$AGENT_PORT" &
AGENT_PID=$!

cleanup() {
  kill "$BUSINESS_PID" "$AGENT_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo
echo "  agent     http://127.0.0.1:${AGENT_PORT}   (BACKEND_DRIVER=${BACKEND_DRIVER})"
echo "  business  http://127.0.0.1:${BUSINESS_PORT}"
echo "  docs      http://127.0.0.1:${AGENT_PORT}/docs"
echo
echo "  curl example:"
echo "    curl -s -X POST http://127.0.0.1:${AGENT_PORT}/api/shopping-agent/run \\"
echo "      -H 'Authorization: Bearer dev-token' -H 'Content-Type: application/json' \\"
echo "      -d '{\"user_id\":\"u_1001\",\"session_id\":\"s_demo\",\"query\":\"推荐 2000 元以内的手机\"}'"
echo
wait
