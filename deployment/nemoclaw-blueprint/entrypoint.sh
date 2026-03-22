#!/bin/bash
set -e

MODE="${1:-serve}"

case "$MODE" in
  serve)
    echo "=== NCMS NemoClaw Blueprint ==="
    echo ""
    echo "Starting services:"
    echo "  - MCP HTTP API on :8080"
    echo "  - Dashboard on :8420 (open http://localhost:8420)"
    echo ""
    echo "NemoClaw available: $(command -v nemoclaw >/dev/null 2>&1 && echo 'yes' || echo 'no')"
    echo ""

    # Start MCP HTTP API in background
    uv run ncms serve --transport http --port 8080 --host 0.0.0.0 &
    API_PID=$!

    # Wait for API to be ready
    for i in $(seq 1 30); do
      if curl -sf http://localhost:8080/api/v1/health > /dev/null 2>&1; then
        echo "MCP HTTP API ready on :8080"
        break
      fi
      sleep 1
    done

    # Start dashboard with demo agents (foreground)
    echo "Starting dashboard on :8420 (with demo agents)..."
    uv run ncms dashboard --host 0.0.0.0 --port 8420 --no-open

    # Cleanup on exit
    kill $API_PID 2>/dev/null || true
    ;;

  demo)
    echo "=== NCMS NemoClaw ND Demo ==="
    echo ""
    echo "Running autonomous multi-agent demo..."
    echo ""
    exec uv run ncms demo --nemoclaw-nd
    ;;

  mcp)
    echo "Starting NCMS MCP server (stdio)..."
    exec uv run ncms serve
    ;;

  blueprint)
    echo "Running NemoClaw Blueprint Runner..."
    shift
    exec uv run python /app/orchestrator/runner.py "$@"
    ;;

  shell)
    echo "=== NCMS NemoClaw Shell ==="
    echo ""
    echo "Available commands:"
    echo "  uv run ncms serve              # MCP server (stdio)"
    echo "  uv run ncms dashboard          # Web dashboard"
    echo "  uv run ncms demo --nemoclaw-nd # ND agent demo"
    echo "  nemoclaw onboard               # NemoClaw setup wizard"
    echo "  nemoclaw <name> connect        # Connect to sandbox"
    echo "  nemoclaw <name> status         # Sandbox status"
    echo ""
    exec /bin/bash
    ;;

  *)
    echo "Usage: docker run ncms-nemoclaw:latest [serve|demo|mcp|blueprint|shell]"
    echo ""
    echo "  serve     - MCP HTTP API + Dashboard (default)"
    echo "  demo      - Run NemoClaw ND autonomous agent demo"
    echo "  mcp       - MCP server (stdio, for OpenClaw integration)"
    echo "  blueprint - Run Blueprint Runner (plan/apply/status/rollback)"
    echo "  shell     - Interactive shell with all tools available"
    exit 1
    ;;
esac
