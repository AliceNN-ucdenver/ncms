#!/bin/bash
# Build and run the NCMS + NemoClaw all-in-one Docker container.
#
# Prerequisites:
#   1. Accept SPLADE v3 license at https://huggingface.co/naver/splade-v3
#   2. Create .env with HF_TOKEN=hf_xxxx
#
# Usage:
#   ./build_run_demo.sh          # Build + run (dashboard + demo)
#   ./build_run_demo.sh --no-build  # Run only (skip build)
#   ./build_run_demo.sh api      # Run in API-only mode
#   ./build_run_demo.sh dashboard # Run dashboard without demo

set -e

IMAGE="ncms-nemoclaw:latest"
ENV_FILE=".env"
DOCKERFILE="deployment/nemoclaw/Dockerfile.allinone"
MODE="${1:-demo}"

# Check for --no-build flag
if [ "$1" = "--no-build" ]; then
    MODE="${2:-demo}"
else
    # ── Build ──────────────────────────────────────────────────────────
    if [ ! -f "$ENV_FILE" ]; then
        echo "ERROR: $ENV_FILE not found."
        echo "Create it with: echo 'HF_TOKEN=hf_xxxx' > .env"
        exit 1
    fi

    echo "=== Building $IMAGE ==="
    echo "  Dockerfile: $DOCKERFILE"
    echo "  Secrets:    $ENV_FILE"
    echo ""

    docker build -f "$DOCKERFILE" \
        --secret id=env,src="$ENV_FILE" \
        -t "$IMAGE" .

    echo ""
    echo "=== Build complete ==="
    echo ""
fi

# ── Run ────────────────────────────────────────────────────────────

# Determine ports based on mode
case "$MODE" in
    demo)
        PORTS="-p 8420:8420 -p 8080:8080"
        echo "Starting: Dashboard (http://localhost:8420) + HTTP API (:8080) + Demo"
        ;;
    api)
        PORTS="-p 8080:8080"
        echo "Starting: HTTP API only (http://localhost:8080)"
        ;;
    dashboard)
        PORTS="-p 8420:8420"
        echo "Starting: Dashboard only (http://localhost:8420)"
        ;;
    mcp)
        PORTS=""
        echo "Starting: MCP server (stdio)"
        ;;
    *)
        PORTS="-p 8420:8420 -p 8080:8080"
        echo "Starting: $MODE"
        ;;
esac

# Pass runtime env file if it exists (for OPENAI_API_KEY, LLM overrides, etc.)
ENV_ARGS=""
if [ -f "ncms.env" ]; then
    ENV_ARGS="--env-file ncms.env"
    echo "  Runtime config: ncms.env"
fi

echo ""

docker run --rm $PORTS $ENV_ARGS "$IMAGE" "$MODE"
