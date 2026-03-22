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

    # Start dashboard (foreground, no demo agents)
    echo "Starting dashboard on :8420..."
    uv run ncms dashboard --host 0.0.0.0 --port 8420 --no-open --no-demo

    # Cleanup
    kill $API_PID 2>/dev/null || true
    ;;

  mcp)
    echo "Starting NCMS MCP server (stdio)..."
    exec uv run ncms serve
    ;;

  shell)
    exec /bin/bash
    ;;

  *)
    echo "Usage: docker run ncms-nemoclaw:latest [serve|mcp|shell]"
    echo ""
    echo "  serve  - MCP HTTP API + Dashboard (default)"
    echo "  mcp    - MCP server (stdio, for OpenClaw integration)"
    echo "  shell  - Interactive shell"
    exit 1
    ;;
esac
