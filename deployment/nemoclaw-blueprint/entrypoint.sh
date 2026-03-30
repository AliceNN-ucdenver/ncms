#!/bin/bash
set -e

MODE="${1:-serve}"

# ── Helper: wait for HTTP endpoint ──────────────────────────────────────────
wait_for_http() {
  local url="$1" max_wait="${2:-30}" label="${3:-service}"
  for i in $(seq 1 "$max_wait"); do
    if curl -sf "$url" > /dev/null 2>&1; then
      echo "$label ready."
      return 0
    fi
    sleep 1
  done
  echo "WARNING: $label did not become ready within ${max_wait}s"
  return 1
}

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

    wait_for_http "http://localhost:8080/api/v1/health" 30 "MCP HTTP API"

    # Start dashboard with ND agents (Architect, Security, Builder)
    echo "Starting dashboard on :8420 (with ND agents)..."
    uv run ncms dashboard --host 0.0.0.0 --port 8420 --no-open --nd

    # Cleanup on exit
    kill $API_PID 2>/dev/null || true
    ;;

  nemoclaw)
    echo "=== NCMS NemoClaw Full Stack ==="
    echo ""
    echo "  OpenShell + OpenClaw + NCMS MCP + Dashboard"
    echo "  Skills drive agent behavior through MCP tool calls"
    echo ""

    # ── Step 1: Start OpenShell gateway ──────────────────────────────────
    echo "[1/6] Starting OpenShell gateway..."
    if ! command -v openshell >/dev/null 2>&1; then
      echo "ERROR: openshell not found on PATH"
      exit 1
    fi

    # Check Docker socket
    if [ ! -S /var/run/docker.sock ]; then
      echo "ERROR: Docker socket not mounted."
      echo "  Run with: --privileged -v /var/run/docker.sock:/var/run/docker.sock"
      exit 1
    fi

    # Check Docker CLI is available
    if ! command -v docker >/dev/null 2>&1; then
      echo "ERROR: docker CLI not found in PATH"
      exit 1
    fi

    # Verify Docker connectivity
    if ! docker info >/dev/null 2>&1; then
      echo "ERROR: Cannot connect to Docker daemon via socket"
      exit 1
    fi
    echo "  Docker socket OK, daemon reachable."

    # Destroy any stale gateway state before starting fresh
    openshell gateway destroy 2>/dev/null || true
    # Also clean up any leftover OpenShell config that might cause "corrupted state"
    rm -rf /root/.config/openshell/gateways 2>/dev/null || true

    # OpenShell gateway uses its own port (not 8080, which we use for MCP)
    GATEWAY_PORT="${OPENSHELL_GATEWAY_PORT:-9090}"
    echo "  Starting OpenShell gateway on port $GATEWAY_PORT (K3s bootstrap, 30-60s)..."
    openshell gateway start --port "$GATEWAY_PORT" 2>&1
    GATEWAY_RC=$?
    if [ $GATEWAY_RC -eq 0 ]; then
      echo "  OpenShell gateway ready on port $GATEWAY_PORT."
    else
      echo "  WARNING: Gateway start returned exit code $GATEWAY_RC, retrying after destroy..."
      openshell gateway destroy 2>/dev/null || true
      rm -rf /root/.config/openshell/gateways 2>/dev/null || true
      # Kill any leftover openshell cluster containers
      docker ps -a --filter "name=openshell-cluster" -q | xargs docker rm -f 2>/dev/null || true
      sleep 2
      openshell gateway start --port "$GATEWAY_PORT" 2>&1
      GATEWAY_RC=$?
      if [ $GATEWAY_RC -eq 0 ]; then
        echo "  OpenShell gateway ready on port $GATEWAY_PORT (retry succeeded)."
      else
        echo "  ERROR: Gateway start failed after retry (exit code $GATEWAY_RC)."
        echo "  Continuing anyway — sandbox creation will likely fail."
      fi
    fi

    # Fix gateway endpoint: openshell stores 127.0.0.1 which is this container's
    # loopback, NOT the host where the cluster container's port is mapped.
    # Patch to host.docker.internal so OpenShell CLI can reach the gateway.
    GW_META="/root/.config/openshell/gateways/openshell/metadata.json"
    if [ -f "$GW_META" ]; then
      sed -i "s|127.0.0.1|host.docker.internal|g" "$GW_META"
      echo "  Patched gateway endpoint → host.docker.internal:$GATEWAY_PORT"
    fi

    # Verify gateway is reachable
    openshell gateway info 2>&1 || echo "  (gateway info not yet available)"

    # ── Step 2: Start NCMS MCP HTTP server ───────────────────────────────
    MCP_PORT="${NCMS_MCP_PORT:-8080}"
    echo ""
    echo "[2/6] Starting NCMS MCP HTTP server on :$MCP_PORT..."
    uv run ncms serve --transport http --port "$MCP_PORT" --host 0.0.0.0 &
    MCP_PID=$!
    wait_for_http "http://localhost:$MCP_PORT/api/v1/health" 30 "NCMS MCP server"

    # ── Step 3: Load governance-mesh knowledge into NCMS ─────────────────
    echo ""
    echo "[3/6] Loading governance-mesh knowledge..."

    KNOWLEDGE_DIR="/app/knowledge"
    LOADED=0

    # Architecture files
    for f in "$KNOWLEDGE_DIR"/architecture/bar.arch.json; do
      [ -f "$f" ] && uv run ncms load "$f" -d architecture -d calm-model && LOADED=$((LOADED+1))
    done
    for f in "$KNOWLEDGE_DIR"/architecture/ADRs/*.md; do
      [ -f "$f" ] && uv run ncms load "$f" -d architecture -d decisions && LOADED=$((LOADED+1))
    done
    for f in "$KNOWLEDGE_DIR"/architecture/quality-attributes.yaml \
             "$KNOWLEDGE_DIR"/architecture/fitness-functions.yaml; do
      [ -f "$f" ] && uv run ncms load "$f" -d architecture -d quality && LOADED=$((LOADED+1))
    done

    # Security files
    for f in "$KNOWLEDGE_DIR"/security/threat-model.yaml; do
      [ -f "$f" ] && uv run ncms load "$f" -d security -d threats && LOADED=$((LOADED+1))
    done
    for f in "$KNOWLEDGE_DIR"/security/security-controls.yaml; do
      [ -f "$f" ] && uv run ncms load "$f" -d security -d controls && LOADED=$((LOADED+1))
    done
    for f in "$KNOWLEDGE_DIR"/security/compliance-checklist.yaml; do
      [ -f "$f" ] && uv run ncms load "$f" -d security -d compliance && LOADED=$((LOADED+1))
    done

    # Prompts
    for f in "$KNOWLEDGE_DIR"/prompts/architecture.md; do
      [ -f "$f" ] && uv run ncms load "$f" -d architecture -d calm-model && LOADED=$((LOADED+1))
    done
    for f in "$KNOWLEDGE_DIR"/prompts/application-security.md; do
      [ -f "$f" ] && uv run ncms load "$f" -d security -d threats -d controls && LOADED=$((LOADED+1))
    done

    # App config
    for f in "$KNOWLEDGE_DIR"/app.yaml; do
      [ -f "$f" ] && uv run ncms load "$f" -d architecture -d identity-service && LOADED=$((LOADED+1))
    done

    echo "  Loaded $LOADED knowledge files into NCMS."

    # ── Step 4: Configure inference provider ─────────────────────────────
    echo ""
    echo "[4/6] Configuring inference provider..."

    SANDBOX_NAME="ncms-openclaw"
    BLUEPRINT_PATH="${NEMOCLAW_BLUEPRINT_PATH:-/app}"
    PROFILE="${NCMS_NEMOCLAW_PROFILE:-default}"

    # Read provider config from env (set by build_and_run.sh per profile)
    PROVIDER_NAME="${NCMS_PROVIDER_NAME:-dgx-spark}"
    PROVIDER_TYPE="${NCMS_PROVIDER_TYPE:-openai}"
    INFERENCE_ENDPOINT="${NCMS_LLM_API_BASE:-http://spark-ee7d.local:8000/v1}"
    INFERENCE_MODEL="${NCMS_LLM_MODEL:-openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16}"

    # Strip litellm prefix for OpenShell provider model name
    CLEAN_MODEL=$(echo "$INFERENCE_MODEL" | sed 's|^openai/||; s|^ollama_chat/||')

    openshell provider create \
      --name "$PROVIDER_NAME" \
      --type "$PROVIDER_TYPE" \
      --config "OPENAI_BASE_URL=$INFERENCE_ENDPOINT" \
      --credential "OPENAI_API_KEY=${OPENAI_API_KEY:-dummy}" \
      2>&1 || echo "  (provider may already exist, continuing...)"

    echo "  Inference: $PROVIDER_NAME ($PROVIDER_TYPE) -> $CLEAN_MODEL @ $INFERENCE_ENDPOINT"

    # ── Step 5: Create OpenClaw sandbox ──────────────────────────────────
    echo ""
    echo "[5/6] Creating OpenClaw sandbox..."

    # Delete stale sandbox if any
    openshell sandbox delete "$SANDBOX_NAME" 2>/dev/null || true

    echo "  Creating sandbox '$SANDBOX_NAME' (OpenClaw community image)..."

    # Create sandbox with custom policy that allows NCMS MCP access
    openshell sandbox create \
      --name "$SANDBOX_NAME" \
      --from openclaw \
      --provider "$PROVIDER_NAME" \
      --policy /app/policies/openclaw-sandbox.yaml \
      --no-tty \
      2>&1 &
    SANDBOX_PID=$!

    # Wait for sandbox to be Ready
    echo "  Waiting for sandbox to provision..."
    for i in $(seq 1 90); do
      PHASE=$(openshell sandbox get "$SANDBOX_NAME" 2>/dev/null | grep Phase | awk '{print $NF}')
      if [ "$PHASE" = "Ready" ]; then
        echo "  Sandbox '$SANDBOX_NAME' is Ready."
        break
      fi
      if [ "$i" -eq 90 ]; then
        echo "  Sandbox still provisioning after 180s — check: openshell sandbox get $SANDBOX_NAME"
      fi
      sleep 2
    done

    openshell sandbox list 2>&1 || true

    # ── Step 5b: Inject skills and MCP config into sandbox ───────────────
    echo ""
    echo "  Injecting skills and MCP config into sandbox..."

    # Get the agent container ID inside K3s
    CLUSTER="openshell-cluster-openshell"
    CID=$(docker exec "$CLUSTER" crictl ps --name agent -o json 2>/dev/null \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['containers'][0]['id'])" 2>/dev/null || echo "")

    if [ -z "$CID" ]; then
      echo "  WARNING: Could not find agent container in K3s. Skills not injected."
    else
      echo "  Agent container: ${CID:0:12}"

      # Inject NCMS skills (OpenClaw expects SKILL.md in .agents/skills/<name>/)
      for skill_dir in /app/skills/*/; do
        skill_name=$(basename "$skill_dir")
        skill_file=$(ls "$skill_dir"/*.md 2>/dev/null | head -1)
        if [ -n "$skill_file" ]; then
          docker exec "$CLUSTER" crictl exec "$CID" mkdir -p "/sandbox/.agents/skills/$skill_name" 2>/dev/null
          cat "$skill_file" | docker exec -i "$CLUSTER" crictl exec -i "$CID" \
            bash -c "cat > /sandbox/.agents/skills/$skill_name/SKILL.md" 2>/dev/null
          echo "    Skill: $skill_name"
        fi
      done

      # Create skill symlinks for Claude Code
      docker exec "$CLUSTER" crictl exec "$CID" bash -c "
        ln -sf /sandbox/.agents/skills/architect/ /sandbox/.claude/skills/architect 2>/dev/null
        ln -sf /sandbox/.agents/skills/security/ /sandbox/.claude/skills/security 2>/dev/null
        ln -sf /sandbox/.agents/skills/builder/ /sandbox/.claude/skills/builder 2>/dev/null
      " 2>/dev/null

      # Write Claude Code MCP server config (NCMS as HTTP MCP)
      echo "{
  \"mcpServers\": {
    \"ncms\": {
      \"type\": \"url\",
      \"url\": \"http://host.docker.internal:$MCP_PORT/mcp\",
      \"description\": \"NCMS Cognitive Memory — store, search, recall, Knowledge Bus\"
    }
  }
}" | docker exec -i "$CLUSTER" crictl exec -i "$CID" \
        bash -c "cat > /sandbox/.claude/settings.json" 2>/dev/null
      echo "    MCP config: ncms → http://host.docker.internal:$MCP_PORT/mcp"

      echo "  Skills and config injected."
    fi

    # ── Step 6: Start Dashboard ──────────────────────────────────────────
    echo ""
    echo "[6/6] Starting NCMS Dashboard..."

    SKILLS_DIR="$BLUEPRINT_PATH/skills"

    echo ""
    echo "=== NemoClaw Stack Ready ==="
    echo ""
    echo "  MCP Server:  http://localhost:$MCP_PORT (NCMS — 18+ tools)"
    echo "  Dashboard:   http://localhost:8420"
    echo "  Gateway:     OpenShell (K3s cluster, port $GATEWAY_PORT)"
    echo "  Sandbox:     $SANDBOX_NAME"
    echo "  Inference:   $PROVIDER_NAME -> $CLEAN_MODEL @ $INFERENCE_ENDPOINT"
    echo "  Knowledge:   $LOADED files loaded into NCMS"
    echo ""
    echo "  Skills:"
    for skill in "$SKILLS_DIR"/*/*.md; do
      name=$(basename "$(dirname "$skill")")
      echo "    - $name/$(basename "$skill")"
    done
    echo ""
    echo "  Interact:"
    echo "    openshell sandbox list                    # List sandboxes"
    echo "    openshell sandbox connect $SANDBOX_NAME   # Connect to sandbox shell"
    echo "    openshell sandbox get $SANDBOX_NAME       # Sandbox details"
    echo ""

    # Start dashboard (no Python agents — OpenClaw drives skills via MCP)
    echo "Starting dashboard on :8420..."
    uv run ncms dashboard --host 0.0.0.0 --port 8420 --no-open --no-demo

    # Cleanup
    kill $MCP_PID 2>/dev/null || true
    kill $SANDBOX_PID 2>/dev/null || true
    openshell gateway stop 2>/dev/null || true
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
    echo "  uv run ncms serve --transport http --port 8080  # MCP server (HTTP)"
    echo "  uv run ncms dashboard          # Web dashboard"
    echo "  uv run ncms dashboard --nd     # Dashboard with ND agents"
    echo "  uv run ncms demo --nemoclaw-nd # ND agent demo"
    echo "  nemoclaw onboard               # NemoClaw setup wizard"
    echo "  nemoclaw <name> connect        # Connect to sandbox"
    echo "  nemoclaw <name> status         # Sandbox status"
    echo "  openshell gateway start        # Start OpenShell gateway"
    echo "  openshell sandbox list         # List sandboxes"
    echo "  openclaw tui                   # OpenClaw agent TUI"
    echo ""
    exec /bin/bash
    ;;

  *)
    echo "Usage: docker run ncms-nemoclaw:latest [serve|nemoclaw|demo|mcp|blueprint|shell]"
    echo ""
    echo "  serve     - MCP HTTP API + Dashboard with ND agents (default)"
    echo "  nemoclaw  - Full stack: OpenShell + OpenClaw + NCMS MCP + Dashboard"
    echo "  demo      - Run NemoClaw ND autonomous agent demo"
    echo "  mcp       - MCP server (stdio, for direct OpenClaw integration)"
    echo "  blueprint - Run Blueprint Runner (plan/apply/status/rollback)"
    echo "  shell     - Interactive shell with all tools available"
    exit 1
    ;;
esac
