#!/usr/bin/env bash
# NCMS NemoClaw Setup — Docker Hub + NeMo Agent Toolkit Sandboxes.
#
# Architecture:
#   ncms-hub (Docker):   NCMS Hub (API :9080 + Dashboard :8420 + Knowledge Bus)
#   phoenix  (Docker):   Phoenix tracing UI (:6006)
#   ncms-architect:      NemoClaw sandbox (NAT agent + NCMS memory + domain knowledge)
#   ncms-security:       NemoClaw sandbox (NAT agent + NCMS memory + domain knowledge)
#   ncms-builder:        NemoClaw sandbox (NAT agent + NCMS memory + ask/announce tools)
#
# The hub runs as a Docker container because NemoClaw sandboxes are fully
# network-isolated. Sandboxes reach the hub via host.docker.internal:9080.
# LLM calls route through NemoClaw's inference.local proxy → DGX Spark.
#
# Ports:
#   :9080  — NCMS Hub API + Bus API
#   :8420  — NCMS Dashboard
#   :6006  — Phoenix tracing UI
#
# Prerequisites:
#   1. Docker installed and running
#   2. NemoClaw installed + onboarded: curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash
#   3. Inference configured: openshell inference set --provider dgx-spark ...
#   4. HF_TOKEN set for gated model download (SPLADE v3)
#
# Usage:
#   ./setup_nemoclaw.sh                                # Full setup
#   ./setup_nemoclaw.sh --rebuild                      # Teardown + full setup
#   ./setup_nemoclaw.sh --skip-hub                     # Only create agent sandboxes
#   ./setup_nemoclaw.sh --status                       # Show status
#   ./setup_nemoclaw.sh --teardown                     # Remove everything
#   ./setup_nemoclaw.sh --trigger                      # Trigger builder design cycle

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Load .env from project root (API keys, overrides) ───────────────────────
ENV_FILE="${PROJECT_ROOT}/.env"
if [ -f "$ENV_FILE" ]; then
  # Export all non-comment, non-empty lines
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.hub.yaml"

# ── Defaults ────────────────────────────────────────────────────────────────
INFERENCE_ENDPOINT="${INFERENCE_ENDPOINT:-http://spark-ee7d.local:8000/v1}"
INFERENCE_MODEL="${INFERENCE_MODEL:-nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16}"
SKIP_HUB=false
SHOW_STATUS=false
TEARDOWN=false
REBUILD=false
TRIGGER=false

# Ports
NCMS_HUB_PORT="${NCMS_HUB_PORT:-9080}"
NCMS_DASHBOARD_PORT="${NCMS_DASHBOARD_PORT:-8420}"
PHOENIX_PORT="${PHOENIX_PORT:-6006}"

# Sandbox names (RFC 1123)
AGENT_SANDBOXES=("ncms-architect" "ncms-security" "ncms-builder" "ncms-product-owner" "ncms-researcher" "ncms-archeologist")

# Agent config: agent_id|nat_config|knowledge_dir
# knowledge_dir is relative to SCRIPT_DIR/knowledge/
AGENT_CONFIGS=(
  "architect|configs/architect.yml|architecture"
  "security|configs/security.yml|security"
  "builder|configs/builder.yml|"
  "product_owner|configs/product_owner.yml|product-owner"
  "researcher|configs/researcher.yml|"
  "archeologist|configs/archeologist.yml|"
)

# ── Colors ──────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_GREEN=$'\033[32m' C_YELLOW=$'\033[33m' C_RED=$'\033[31m'
  C_CYAN=$'\033[36m' C_BOLD=$'\033[1m' C_DIM=$'\033[2m' C_RESET=$'\033[0m'
else
  C_GREEN='' C_YELLOW='' C_RED='' C_CYAN='' C_BOLD='' C_DIM='' C_RESET=''
fi

info()  { echo "${C_CYAN}[INFO]${C_RESET}  $*"; }
ok()    { echo "${C_GREEN}  ✓${C_RESET}  $*"; }
warn()  { echo "${C_YELLOW}[WARN]${C_RESET}  $*"; }
error() { echo "${C_RED}[ERROR]${C_RESET} $*" >&2; exit 1; }

# ── Parse args ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --endpoint)    INFERENCE_ENDPOINT="$2"; shift 2 ;;
    --model)       INFERENCE_MODEL="$2"; shift 2 ;;
    --skip-hub)    SKIP_HUB=true; shift ;;
    --status)      SHOW_STATUS=true; shift ;;
    --teardown)    TEARDOWN=true; shift ;;
    --rebuild)     REBUILD=true; shift ;;
    --trigger)     TRIGGER=true; shift ;;
    --help|-h)
      sed -n '2,/^$/s/^# //p' "$0"
      exit 0
      ;;
    *) error "Unknown option: $1" ;;
  esac
done

# ── Helpers ─────────────────────────────────────────────────────────────────
command_exists() { command -v "$1" &>/dev/null; }

sandbox_exists() {
  openshell sandbox list 2>/dev/null | grep -q "$1" 2>/dev/null
}

sandbox_run() {
  local name="$1"; shift
  ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "openshell-${name}" "$@"
}

ensure_ssh_config() {
  local name="$1"
  local host="openshell-${name}"
  # Remove stale entry
  sed -i '' "/Host $host/,/^$/d" ~/.ssh/config 2>/dev/null || \
  sed -i "/Host $host/,/^$/d" ~/.ssh/config 2>/dev/null || true
  openshell sandbox ssh-config "$name" >> ~/.ssh/config
  ok "SSH config for $name"
}

check_prerequisites() {
  info "Checking prerequisites..."

  command_exists docker || error "docker not found"
  ok "docker"

  command_exists openshell || error "openshell not found"
  ok "openshell"

  if openshell status &>/dev/null; then
    ok "NemoClaw gateway running"
  else
    warn "Gateway may not be running — openshell gateway start"
  fi

  if curl -sf "${INFERENCE_ENDPOINT%/v1}/health" >/dev/null 2>&1 || \
     curl -sf "${INFERENCE_ENDPOINT}/models" >/dev/null 2>&1; then
    ok "Inference at $INFERENCE_ENDPOINT"
  else
    warn "Inference not reachable at $INFERENCE_ENDPOINT"
  fi
}

# ── Status ──────────────────────────────────────────────────────────────────
show_status() {
  echo ""
  echo "${C_BOLD}╔══════════════════════════════════════════════════════════╗${C_RESET}"
  echo "${C_BOLD}║  NCMS NemoClaw + NAT Status                              ║${C_RESET}"
  echo "${C_BOLD}╚══════════════════════════════════════════════════════════╝${C_RESET}"
  echo ""

  # Docker services
  for svc in ncms-hub phoenix; do
    if docker ps --filter "name=$svc" --format "{{.Status}}" 2>/dev/null | grep -q .; then
      local status
      status=$(docker ps --filter "name=$svc" --format "{{.Status}}")
      echo "  ${C_GREEN}●${C_RESET} $svc (Docker)  $status"
    else
      echo "  ${C_DIM}○${C_RESET} $svc (Docker)  ${C_DIM}not running${C_RESET}"
    fi
  done

  # Hub health
  if curl -sf "http://localhost:${NCMS_HUB_PORT}/api/v1/health" 2>/dev/null; then echo ""; fi

  # Sandboxes
  for sandbox in "${AGENT_SANDBOXES[@]}"; do
    if sandbox_exists "$sandbox"; then
      # Check if NAT agent is running
      local agent_status="${C_DIM}(checking...)${C_RESET}"
      if sandbox_run "$sandbox" "pgrep -f 'nat serve'" &>/dev/null; then
        agent_status="${C_GREEN}NAT agent running${C_RESET}"
      elif sandbox_run "$sandbox" "pgrep -f 'nat run'" &>/dev/null; then
        agent_status="${C_GREEN}NAT agent running${C_RESET}"
      elif sandbox_run "$sandbox" "pgrep -f 'bus-agent'" &>/dev/null; then
        agent_status="${C_YELLOW}bus sidecar running${C_RESET}"
      else
        agent_status="${C_DIM}no agent process${C_RESET}"
      fi
      echo "  ${C_GREEN}●${C_RESET} $sandbox  $agent_status"
    else
      echo "  ${C_DIM}○${C_RESET} $sandbox  ${C_DIM}not created${C_RESET}"
    fi
  done

  echo ""
  info "Dashboard:  http://localhost:$NCMS_DASHBOARD_PORT"
  info "Phoenix:    http://localhost:$PHOENIX_PORT"
  echo ""
}

# ── Teardown ────────────────────────────────────────────────────────────────
teardown() {
  echo ""
  echo "${C_BOLD}Tearing down NCMS NemoClaw setup...${C_RESET}"
  echo ""

  # Stop Docker services
  if docker compose -f "$COMPOSE_FILE" ps -q 2>/dev/null | grep -q .; then
    info "Stopping Docker services..."
    docker compose -f "$COMPOSE_FILE" down -v 2>/dev/null || true
    ok "Docker services stopped"
  fi

  # Stop agent port forwards
  for port in 8001 8002 8003 8004 8005; do
    openshell forward stop "$port" 2>/dev/null || true
  done

  # Delete all sandboxes — try every one unconditionally (don't rely on sandbox_exists)
  for sandbox in "${AGENT_SANDBOXES[@]}"; do
    info "Deleting $sandbox..."
    if openshell sandbox delete "$sandbox" 2>/dev/null; then
      ok "Deleted $sandbox"
    else
      echo "  ${C_DIM}○${C_RESET} $sandbox — not found or already deleted"
    fi
    # Always clean SSH config
    sed -i '' "/Host openshell-${sandbox}/,/^$/d" ~/.ssh/config 2>/dev/null || \
    sed -i "/Host openshell-${sandbox}/,/^$/d" ~/.ssh/config 2>/dev/null || true
  done

  echo ""
  ok "Teardown complete."
}

# ── Providers ──────────────────────────────────────────────────────────────
# Create providers for external API keys (idempotent — skips if exists)
setup_providers() {
  # Tavily search API for Product Owner agent
  if [ -n "${TAVILY_API_KEY:-}" ]; then
    if openshell provider list 2>/dev/null | grep -q "tavily"; then
      ok "Provider 'tavily' already exists"
    else
      info "Creating Tavily provider..."
      openshell provider create \
        --name tavily \
        --type generic \
        --credential "TAVILY_API_KEY=${TAVILY_API_KEY}" \
        && ok "Provider 'tavily' created" \
        || warn "Failed to create tavily provider — web_search will not work"
    fi
  else
    warn "TAVILY_API_KEY not set — Product Owner web_search will be disabled"
  fi

  # GitHub API for Archeologist agent
  if [ -n "${GITHUB_PERSONAL_ACCESS_TOKEN:-}" ]; then
    if openshell provider list 2>/dev/null | grep -q "github"; then
      ok "Provider 'github' already exists"
    else
      info "Creating GitHub provider..."
      openshell provider create \
        --name github \
        --type generic \
        --credential "GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_PERSONAL_ACCESS_TOKEN}" \
        && ok "Provider 'github' created" \
        || warn "Failed to create github provider — archeologist will be rate-limited"
    fi
  else
    warn "GITHUB_PERSONAL_ACCESS_TOKEN not set — Archeologist will use unauthenticated API (60 req/hr)"
  fi
}

# ── Create sandbox ──────────────────────────────────────────────────────────
# Usage: create_sandbox <name> [provider1 provider2 ...]
create_sandbox() {
  local name="$1"; shift
  local policy_file="$SCRIPT_DIR/policies/openclaw-sandbox.yaml"

  if sandbox_exists "$name"; then
    ok "Sandbox exists: $name"
    ensure_ssh_config "$name"
    return 0
  fi

  # Build provider flags
  local provider_args=()
  for p in "$@"; do
    provider_args+=(--provider "$p")
  done

  info "Creating sandbox: $name${provider_args:+ (providers: $*)}"
  openshell sandbox create \
    --from openclaw \
    --name "$name" \
    --policy "$policy_file" \
    ${provider_args[@]+"${provider_args[@]}"} \
    -- true \
    || error "Failed to create sandbox $name"
  ok "Sandbox created: $name"
  ensure_ssh_config "$name"
}

# ── Hub setup (Docker) ──────────────────────────────────────────────────────
setup_hub() {
  info "Starting NCMS Hub + Phoenix containers"
  echo "  Hub:     localhost:$NCMS_HUB_PORT (API) + localhost:$NCMS_DASHBOARD_PORT (Dashboard)"
  echo "  Phoenix: localhost:$PHOENIX_PORT (Tracing UI)"
  echo "  Agents connect via host.docker.internal:$NCMS_HUB_PORT"
  echo ""

  if curl -sf "http://localhost:${NCMS_HUB_PORT}/api/v1/health" > /dev/null 2>&1; then
    ok "Hub already running"
    return 0
  fi

  # Build
  info "Building hub image..."
  local build_args=""
  [ -n "${HF_TOKEN:-}" ] && build_args="--build-arg HF_TOKEN=$HF_TOKEN"
  INFERENCE_ENDPOINT="$INFERENCE_ENDPOINT" \
    docker compose -f "$COMPOSE_FILE" build $build_args 2>&1 | tail -5
  ok "Image built"

  # Start
  info "Starting containers..."
  INFERENCE_ENDPOINT="$INFERENCE_ENDPOINT" \
    docker compose -f "$COMPOSE_FILE" up -d 2>&1
  ok "Containers started"

  # Wait
  info "Waiting for hub..."
  local max_wait=180 i=0
  while [ "$i" -lt "$max_wait" ]; do
    if curl -sf "http://localhost:${NCMS_HUB_PORT}/api/v1/health" > /dev/null 2>&1; then
      ok "Hub ready on :$NCMS_HUB_PORT"
      return 0
    fi
    sleep 2; i=$((i + 2))
  done
  warn "Hub not ready after ${max_wait}s — docker logs ncms-hub"
}

# ── Agent sandbox setup (NAT) ──────────────────────────────────────────────
setup_agent_sandbox() {
  local sandbox_name="$1"
  local agent_id="$2"
  local nat_config="$3"
  local knowledge_dir="$4"  # relative to knowledge/, empty for builder

  info "Setting up NAT agent: $agent_id in $sandbox_name"

  # Attach providers based on agent needs
  local providers=()
  if { [ "$agent_id" = "product_owner" ] || [ "$agent_id" = "researcher" ] || [ "$agent_id" = "archeologist" ]; } && [ -n "${TAVILY_API_KEY:-}" ]; then
    providers+=(tavily)
  fi
  if [ "$agent_id" = "archeologist" ] && [ -n "${GITHUB_PERSONAL_ACCESS_TOKEN:-}" ]; then
    providers+=(github)
  fi
  create_sandbox "$sandbox_name" ${providers[@]+"${providers[@]}"}

  # ── Upload NCMS source + NAT plugin ──
  info "Uploading NCMS + NAT plugin..."
  sandbox_run "$sandbox_name" "mkdir -p /sandbox/ncms /sandbox/configs /sandbox/knowledge" \
    2>/dev/null || true

  # NCMS source
  openshell sandbox upload "$sandbox_name" "$PROJECT_ROOT/src" /sandbox/ncms/src \
    2>/dev/null || true
  openshell sandbox upload "$sandbox_name" "$PROJECT_ROOT/pyproject.toml" /sandbox/ncms \
    2>/dev/null || true
  openshell sandbox upload "$sandbox_name" "$PROJECT_ROOT/uv.lock" /sandbox/ncms \
    2>/dev/null || true
  openshell sandbox upload "$sandbox_name" "$PROJECT_ROOT/README.md" /sandbox/ncms \
    2>/dev/null || true

  # NAT plugin
  openshell sandbox upload "$sandbox_name" \
    "$PROJECT_ROOT/packages/nvidia-nat-ncms" /sandbox/nvidia-nat-ncms \
    2>/dev/null || warn "NAT plugin upload failed"

  # NAT agent config
  if [ -f "$SCRIPT_DIR/$nat_config" ]; then
    openshell sandbox upload "$sandbox_name" \
      "$SCRIPT_DIR/$nat_config" "/sandbox/configs/" \
      2>/dev/null || warn "Config upload failed"
    ok "Config: $nat_config"
  fi

  # ── Upload domain knowledge ──
  if [ -n "$knowledge_dir" ] && [ -d "$SCRIPT_DIR/knowledge/$knowledge_dir" ]; then
    info "Uploading knowledge: $knowledge_dir"
    openshell sandbox upload "$sandbox_name" \
      "$SCRIPT_DIR/knowledge/$knowledge_dir" "/sandbox/knowledge/$knowledge_dir" \
      2>/dev/null || warn "Knowledge upload failed"

    # Also upload shared files (app.yaml, prompts)
    for shared in app.yaml; do
      if [ -f "$SCRIPT_DIR/knowledge/$shared" ]; then
        openshell sandbox upload "$sandbox_name" \
          "$SCRIPT_DIR/knowledge/$shared" "/sandbox/knowledge/$shared" \
          2>/dev/null || true
      fi
    done
    ok "Knowledge uploaded"
  fi

  # ── Install dependencies ──
  info "Installing NCMS..."
  sandbox_run "$sandbox_name" "cd /sandbox/ncms && uv sync 2>&1 | tail -3" \
    || warn "NCMS install failed"

  # Ensure arxiv package is installed (researcher needs it for academic search)
  sandbox_run "$sandbox_name" "cd /sandbox/ncms && uv pip install arxiv 2>&1 | tail -2" \
    || warn "arxiv install failed (non-fatal — researcher will skip ArXiv search)"

  # Install NAT (NeMo Agent Toolkit) + langchain integration
  info "Installing NeMo Agent Toolkit..."
  sandbox_run "$sandbox_name" "cd /sandbox/ncms && \
    uv pip install nvidia-nat-core nvidia-nat-langchain nvidia-nat-opentelemetry 2>&1 | tail -5" \
    || warn "NAT install failed (may need PyPI approval in openshell term)"

  # Install local NAT-NCMS plugin (uses namespace packages — no __init__.py in nat/)
  if sandbox_run "$sandbox_name" "test -d /sandbox/nvidia-nat-ncms" 2>/dev/null; then
    # Clean stale __init__.py that would shadow nat core package
    sandbox_run "$sandbox_name" "\
      rm -f /sandbox/nvidia-nat-ncms/src/nat/__init__.py \
            /sandbox/nvidia-nat-ncms/src/nat/plugins/__init__.py" \
      2>/dev/null || true
    sandbox_run "$sandbox_name" "cd /sandbox/ncms && \
      uv pip install -e /sandbox/nvidia-nat-ncms 2>&1 | tail -3" \
      || warn "NAT-NCMS plugin install failed"
  fi

  # ── Start NAT agent ──
  # The NAT agent handles everything: bus registration, SSE listener,
  # knowledge loading (via NCMSMemoryEditor), and LLM reasoning.
  # Config file determines the agent's role, domains, and tools.
  # Each agent listens on a unique port so openshell forward can map 1:1
  # Architect: 8001, Security: 8002, Builder: 8003
  local agent_port
  case "$agent_id" in
    architect)      agent_port=8001 ;;
    security)       agent_port=8002 ;;
    builder)        agent_port=8003 ;;
    product_owner)  agent_port=8004 ;;
    researcher)     agent_port=8005 ;;
    archeologist)   agent_port=8006 ;;
    *)              agent_port=8000 ;;
  esac

  info "Starting NAT agent on port $agent_port..."
  local config_file
  config_file=$(basename "$nat_config")

  # Start NAT agent via fastapi frontend (stays running as HTTP server)
  # Provider-injected env vars (e.g. TAVILY_API_KEY) are available automatically.
  # NAT_PORT tells the SSE listener which port to self-call /generate on.
  # Write startup script to sandbox (avoids quoting issues with nested ssh)
  sandbox_run "$sandbox_name" "cat > /tmp/start-nat.sh << 'SCRIPT'
#!/bin/bash
LOG=/tmp/ncms-nat-agent.log
echo \"[NAT] Starting \$1 on port \$2 at \$(date)\" >> \$LOG
cd /sandbox/ncms
NAT_PORT=\$2 /sandbox/.venv/bin/nat start fastapi \
  --config_file /sandbox/configs/\$3 \
  --host 0.0.0.0 --port \$2 \
  >> \$LOG 2>&1
EXIT_CODE=\$?
echo \"[NAT] Process exited with code \$EXIT_CODE at \$(date)\" >> \$LOG
echo \"[NAT] Last 5 dmesg lines:\" >> \$LOG
dmesg 2>/dev/null | tail -5 >> \$LOG
SCRIPT
chmod +x /tmp/start-nat.sh" 2>/dev/null

  sandbox_run "$sandbox_name" \
    "nohup /tmp/start-nat.sh $agent_id $agent_port $config_file > /dev/null 2>&1 & echo 'NAT agent started for $agent_id on port $agent_port'" \
    || warn "NAT agent failed"

  # ── Wait for NAT agent to be ready before port forward ──
  info "Waiting for NAT agent $agent_id to start listening on port $agent_port..."
  local wait_i=0 wait_max=60
  while [ "$wait_i" -lt "$wait_max" ]; do
    if sandbox_run "$sandbox_name" "curl -sf http://localhost:$agent_port/health" &>/dev/null; then
      ok "NAT agent $agent_id is healthy"
      break
    fi
    sleep 2; wait_i=$((wait_i + 2))
  done
  if [ "$wait_i" -ge "$wait_max" ]; then
    warn "NAT agent $agent_id did not become healthy in ${wait_max}s — port forward may fail"
  else
    sleep 3  # Let the socket stabilize before forwarding
  fi

  # ── Port forward for direct /generate access ──
  # Dashboard calls agents directly (not through the bus) for LLM reasoning.
  # openshell forward maps host:PORT → sandbox:PORT (same port, 1:1)
  openshell forward stop "$agent_port" "$sandbox_name" 2>/dev/null || true
  openshell forward start -d "$agent_port" "$sandbox_name" 2>/dev/null \
    && ok "Port forward localhost:$agent_port → sandbox:$agent_port" \
    || warn "Port forward $agent_port failed — dashboard chat may not work for $agent_id"

  ok "NAT agent $agent_id ready in $sandbox_name"
  echo ""
}

# ── Trigger builder design cycle ────────────────────────────────────────────
trigger_builder() {
  echo ""
  echo "${C_BOLD}Triggering builder design cycle...${C_RESET}"
  echo ""

  local hub_url="http://localhost:${NCMS_HUB_PORT}"

  # Step 1: Builder asks architect
  info "Builder asking architect about service boundaries..."
  local arch_answer
  arch_answer=$(curl -sf -X POST "$hub_url/api/v1/bus/ask" \
    -H 'Content-Type: application/json' \
    -d '{"from_agent":"builder","question":"What are the service boundaries, API patterns, and relevant ADRs for the identity service?","domains":["architecture","decisions"],"timeout_ms":60000}' 2>&1)
  echo "  Architect: $(echo "$arch_answer" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("content","no answer")[:200])' 2>/dev/null || echo "$arch_answer" | head -c 200)"
  echo ""

  # Step 2: Builder asks security
  info "Builder asking security about auth requirements..."
  local sec_answer
  sec_answer=$(curl -sf -X POST "$hub_url/api/v1/bus/ask" \
    -H 'Content-Type: application/json' \
    -d '{"from_agent":"builder","question":"What OWASP threats and STRIDE mitigations are required for a JWT authentication microservice?","domains":["security","threats"],"timeout_ms":60000}' 2>&1)
  echo "  Security: $(echo "$sec_answer" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("content","no answer")[:200])' 2>/dev/null || echo "$sec_answer" | head -c 200)"
  echo ""

  # Step 3: Builder announces design
  info "Builder announcing design decision..."
  curl -sf -X POST "$hub_url/api/v1/bus/announce" \
    -H 'Content-Type: application/json' \
    -d '{"from_agent":"builder","content":"Identity service design: JWT RS256 + RBAC with PostgreSQL backing store. API: /v1/register, /v1/login, /v1/refresh, /v1/me. Rate limiting on auth endpoints. Input validation via Joi. CORS restricted to known origins.","domains":["identity-service","implementation","architecture","security"]}' \
    > /dev/null 2>&1
  ok "Design announced to all agents"

  echo ""
  echo "  ${C_GREEN}Check results:${C_RESET}"
  echo "    Dashboard:  http://localhost:$NCMS_DASHBOARD_PORT"
  echo "    Phoenix:    http://localhost:$PHOENIX_PORT"
  echo ""
}

# ── Main ────────────────────────────────────────────────────────────────────
main() {
  echo ""
  echo "${C_BOLD}╔══════════════════════════════════════════════════════════╗${C_RESET}"
  echo "${C_BOLD}║  NCMS NemoClaw + NeMo Agent Toolkit Demo                ║${C_RESET}"
  echo "${C_BOLD}╚══════════════════════════════════════════════════════════╝${C_RESET}"
  echo ""
  echo "  Hub:        ncms-hub (Docker) + Phoenix tracing"
  echo "  Agents:     ${AGENT_SANDBOXES[*]} (NemoClaw + NAT)"
  echo "  Inference:  $INFERENCE_MODEL"
  echo "  Endpoint:   $INFERENCE_ENDPOINT"
  echo ""

  # Handle flags
  if [ "$SHOW_STATUS" = true ]; then show_status; exit 0; fi
  if [ "$TEARDOWN" = true ]; then teardown; exit 0; fi
  if [ "$TRIGGER" = true ]; then trigger_builder; exit 0; fi
  if [ "$REBUILD" = true ]; then
    teardown
    echo ""
    info "Rebuilding..."
    echo ""
  fi

  check_prerequisites

  # Step 0: Providers (API keys for external services)
  setup_providers

  # Step 1: Hub + Phoenix
  if [ "$SKIP_HUB" = false ]; then
    echo ""
    echo "${C_BOLD}━━━ Step 1/7: NCMS Hub + Phoenix (Docker) ━━━${C_RESET}"
    setup_hub
  else
    info "Skipping hub (--skip-hub)"
    echo ""
  fi

  # Step 2-4: Agent sandboxes
  local step=2
  for i in "${!AGENT_SANDBOXES[@]}"; do
    local sandbox="${AGENT_SANDBOXES[$i]}"
    local config="${AGENT_CONFIGS[$i]}"
    IFS='|' read -r agent_id nat_config knowledge_dir <<< "$config"

    local total=$((${#AGENT_SANDBOXES[@]} + 1))
    echo "${C_BOLD}━━━ Step $step/$total: $agent_id Agent (NemoClaw + NAT) ━━━${C_RESET}"
    setup_agent_sandbox "$sandbox" "$agent_id" "$nat_config" "$knowledge_dir"
    step=$((step + 1))
  done

  # Wait for agents to connect
  info "Waiting for agents to connect to hub..."
  local max_wait=60 i=0
  while [ "$i" -lt "$max_wait" ]; do
    local count
    count=$(curl -sf "http://localhost:${NCMS_HUB_PORT}/api/v1/health" 2>/dev/null \
      | python3 -c 'import sys,json; print(json.load(sys.stdin).get("agent_count",0))' 2>/dev/null || echo 0)
    if [ "$count" -ge 5 ]; then
      ok "All 3 agents connected"
      break
    fi
    echo "  ${C_DIM}  $count/3 agents connected...${C_RESET}"
    sleep 5; i=$((i + 5))
  done
  if [ "$i" -ge "$max_wait" ]; then
    local count
    count=$(curl -sf "http://localhost:${NCMS_HUB_PORT}/api/v1/health" 2>/dev/null \
      | python3 -c 'import sys,json; print(json.load(sys.stdin).get("agent_count",0))' 2>/dev/null || echo 0)
    warn "$count/3 agents connected (some may need proxy approval)"
    warn "Check NemoClaw terminal for pending network rules"
  fi

  # Summary
  echo ""
  echo "${C_BOLD}╔══════════════════════════════════════════════════════════╗${C_RESET}"
  echo "${C_BOLD}║  NCMS NemoClaw Demo Ready                                ║${C_RESET}"
  echo "${C_BOLD}╚══════════════════════════════════════════════════════════╝${C_RESET}"
  echo ""
  echo "  ${C_GREEN}Services:${C_RESET}"
  echo "    NCMS API:    http://localhost:$NCMS_HUB_PORT"
  echo "    Dashboard:   http://localhost:$NCMS_DASHBOARD_PORT"
  echo "    Phoenix:     http://localhost:$PHOENIX_PORT"
  echo ""
  echo "  ${C_GREEN}Agent Sandboxes:${C_RESET}"
  for sandbox in "${AGENT_SANDBOXES[@]}"; do
    echo "    Connect:     openshell sandbox connect $sandbox"
  done
  echo ""
  echo "  ${C_GREEN}Trigger Design Cycle:${C_RESET}"
  echo "    $0 --trigger"
  echo ""
  echo "  ${C_GREEN}Commands:${C_RESET}"
  echo "    Status:      $0 --status"
  echo "    Rebuild:     $0 --rebuild"
  echo "    Teardown:    $0 --teardown"
  echo ""
  echo "  ${C_YELLOW}Note:${C_RESET} First run may require approving network rules"
  echo "  in the NemoClaw terminal (host.docker.internal:9080 for Python)"
  echo ""
}

main
