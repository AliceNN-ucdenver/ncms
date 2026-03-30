#!/usr/bin/env bash
# NCMS Agent Sandbox entrypoint — starts bus sidecar + optional OpenClaw.
set -euo pipefail

AGENT_ID="${NCMS_AGENT_ID:-agent}"
DOMAINS="${NCMS_AGENT_DOMAINS:-general}"
# Default to port 9080 for NemoClaw (avoids conflict with OpenClaw on 8080)
# Docker Compose overrides this via NCMS_HUB_URL env var to use 8080
HUB_URL="${NCMS_HUB_URL:-http://localhost:9080}"
SUBSCRIBE="${NCMS_SUBSCRIBE_TO:-}"
LLM_MODEL="${NCMS_LLM_MODEL:-}"
LLM_API_BASE="${NCMS_LLM_API_BASE:-}"
SYSTEM_PROMPT="${NCMS_SYSTEM_PROMPT:-You are a helpful agent. Answer questions based on the provided context.}"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║  NCMS Agent Sandbox: $AGENT_ID"
echo "║  Domains: $DOMAINS"
echo "║  Hub: $HUB_URL"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# Wait for Hub to be ready
echo "[1/2] Waiting for NCMS Hub..."
MAX_WAIT=120
i=0
while [ "$i" -lt "$MAX_WAIT" ]; do
    if curl -sf "${HUB_URL}/api/v1/health" > /dev/null 2>&1; then
        echo "  ✓ Hub is ready"
        break
    fi
    sleep 2
    i=$((i + 2))
done

if [ "$i" -ge "$MAX_WAIT" ]; then
    echo "ERROR: Hub not ready after ${MAX_WAIT}s" >&2
    exit 1
fi

# Build bus-agent command
echo "[2/2] Starting Bus Agent sidecar..."
CMD="uv run ncms bus-agent --hub ${HUB_URL} --agent-id ${AGENT_ID} --domains ${DOMAINS}"

if [ -n "$SUBSCRIBE" ]; then
    CMD="$CMD --subscribe-to ${SUBSCRIBE}"
fi
if [ -n "$LLM_MODEL" ]; then
    CMD="$CMD --llm-model ${LLM_MODEL}"
fi
if [ -n "$LLM_API_BASE" ]; then
    CMD="$CMD --llm-api-base ${LLM_API_BASE}"
fi

echo "  Running: $CMD"
echo ""

# Run bus sidecar in foreground (keeps container alive)
exec $CMD
