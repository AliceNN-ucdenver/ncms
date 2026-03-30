#!/usr/bin/env bash
# NCMS Hub entrypoint — starts HTTP API + Dashboard (single process, shared EventLog)
# then loads knowledge files via the API.
set -euo pipefail

echo "╔══════════════════════════════════════════════════════════╗"
echo "║  NCMS Hub — Cognitive Memory + Knowledge Bus             ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── Helper ────────────────────────────────────────────────────────────────
wait_for_http() {
    local url="$1" max="${2:-60}" i=0
    while [ "$i" -lt "$max" ]; do
        if curl -sf "$url" > /dev/null 2>&1; then return 0; fi
        sleep 1; i=$((i + 1))
    done
    echo "ERROR: $url not ready after ${max}s" >&2; return 1
}

# ── Step 1: Start NCMS HTTP API + Dashboard (single process) ────────────
HUB_PORT="${NCMS_HUB_PORT:-9080}"
DASH_PORT="${NCMS_DASHBOARD_PORT:-8420}"
echo "[1/2] Starting NCMS HTTP API on :${HUB_PORT} + Dashboard on :${DASH_PORT}..."
uv run ncms serve --transport http --port "$HUB_PORT" --host 0.0.0.0 --dashboard-port "$DASH_PORT" &
SERVER_PID=$!

wait_for_http "http://localhost:${HUB_PORT}/api/v1/health" 120
echo "  ✓ NCMS HTTP API ready (API :${HUB_PORT}, Dashboard :${DASH_PORT})"

# ── Step 2: Ready for agents ──────────────────────────────────────────────
# Knowledge is loaded by each agent sandbox on startup (not the hub).
# Each agent owns its domain knowledge and loads it via the HTTP API.
echo ""
echo "  Hub API:   http://localhost:${HUB_PORT}"
echo "  Dashboard: http://localhost:${DASH_PORT}"
echo "  Bus SSE:   http://localhost:${HUB_PORT}/api/v1/bus/subscribe?agent_id=<id>"
echo ""
echo "  Waiting for agent sandboxes to connect..."
echo ""

# Wait for the server process (keeps container alive)
wait $SERVER_PID
