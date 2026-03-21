#!/bin/bash
set -e

MODE="${1:-demo}"

case "$MODE" in
  demo)
    echo "=== NCMS + NemoClaw All-in-One ==="
    echo ""
    echo "Starting services:"
    echo "  - HTTP API on :8080"
    echo "  - Dashboard on :8420 (open http://localhost:8420)"
    echo ""

    # Start HTTP API in background
    uv run ncms serve --transport http --port 8080 --host 0.0.0.0 &
    API_PID=$!

    # Wait for API to be ready
    for i in $(seq 1 30); do
      if curl -sf http://localhost:8080/api/v1/health > /dev/null 2>&1; then
        echo "HTTP API ready on :8080"
        break
      fi
      sleep 1
    done

    # Start dashboard with NemoClaw demo
    echo "Starting dashboard with NemoClaw demo on :8420..."
    uv run ncms dashboard --host 0.0.0.0 --port 8420 --no-open --demo

    # Cleanup
    kill $API_PID 2>/dev/null || true
    ;;

  api)
    echo "Starting NCMS HTTP API server on :8080..."
    exec uv run ncms serve --transport http --port 8080 --host 0.0.0.0
    ;;

  mcp)
    echo "Starting NCMS MCP server (stdio)..."
    exec uv run ncms serve
    ;;

  dashboard)
    echo "Starting NCMS Dashboard on :8420..."
    exec uv run ncms dashboard --host 0.0.0.0 --port 8420 --no-open "${@:2}"
    ;;

  nemoclaw-demo)
    echo "Running NemoClaw multi-agent demo..."
    exec uv run ncms demo --nemoclaw
    ;;

  shell)
    exec /bin/bash
    ;;

  *)
    echo "Usage: docker run ncms-nemoclaw:latest [demo|api|mcp|dashboard|nemoclaw-demo|shell]"
    echo ""
    echo "  demo           - Dashboard + HTTP API + NemoClaw demo (default)"
    echo "  api            - HTTP REST API only (:8080)"
    echo "  mcp            - MCP server (stdio, for OpenClaw integration)"
    echo "  dashboard      - Dashboard only (:8420)"
    echo "  nemoclaw-demo  - Terminal NemoClaw demo (Rich output)"
    echo "  shell          - Interactive shell"
    exit 1
    ;;
esac
