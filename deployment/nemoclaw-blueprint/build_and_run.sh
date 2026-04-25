#!/bin/bash
# Build and run the NCMS NemoClaw multi-agent demo (Docker Compose).
#
# Architecture: 4 containers
#   ncms-hub:  NCMS Hub (MCP HTTP :8080 + Dashboard :8420 + Knowledge Bus)
#   architect: Architect agent sandbox (bus sidecar)
#   security:  Security agent sandbox (bus sidecar)
#   builder:   Builder agent sandbox (bus sidecar)
#
# All inference routes to DGX Spark (Nemotron 3 Nano via vLLM).
# No Anthropic API calls.
#
# Prerequisites:
#   1. Accept SPLADE v3 license at https://huggingface.co/naver/splade-v3
#   2. Create .env in project root with HF_TOKEN=hf_xxxx (for SPLADE gated model)
#
# Usage:
#   ./build_and_run.sh                    # Build + run (DGX Spark default)
#   ./build_and_run.sh --no-build         # Run only (skip build)
#   ./build_and_run.sh --build-only       # Build images without running
#   ./build_and_run.sh --profile ollama   # Use Ollama instead of DGX Spark
#   ./build_and_run.sh --down             # Stop and remove all containers
#   ./build_and_run.sh --logs             # Tail logs from all containers

set -e

# ── Defaults ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.nemoclaw.yaml"
ENV_FILE=""
PROFILE="default"
BUILD=true
RUN=true

# ── Parse args ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build)
      BUILD=false
      shift
      ;;
    --build-only)
      RUN=false
      shift
      ;;
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    --down)
      echo "Stopping NemoClaw containers..."
      docker compose -f "$COMPOSE_FILE" down
      exit 0
      ;;
    --logs)
      docker compose -f "$COMPOSE_FILE" logs -f
      exit 0
      ;;
    --help|-h)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --no-build          Skip building images (use existing)"
      echo "  --build-only        Build images without running"
      echo "  --profile <name>    Inference profile: default|ollama|nim (default: default)"
      echo "  --env-file FILE     Environment file (auto-detected from ../../.env)"
      echo "  --down              Stop and remove all containers"
      echo "  --logs              Tail logs from all containers"
      echo ""
      echo "Profiles:"
      echo "  default   DGX Spark — Nemotron 3 Nano at spark-ee7d.local:8000"
      echo "  ollama    Ollama — qwen3.5:35b-a3b at host.docker.internal:11434"
      echo "  nim       NVIDIA NIM — cloud API (requires NVIDIA_API_KEY)"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# ── Auto-detect .env ────────────────────────────────────────────────────
if [ -z "$ENV_FILE" ] && [ -f "$PROJECT_ROOT/.env" ]; then
  ENV_FILE="$PROJECT_ROOT/.env"
  echo "Auto-detected .env at $ENV_FILE"
fi

# Load .env for build args (HF_TOKEN)
if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
  echo "Loading environment from $ENV_FILE"
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

# ── Profile → env vars ───────────────────────────────────────────────────
export_vars=()

case "$PROFILE" in
  default)
    export LLM_MODEL="openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
    export LLM_API_BASE="http://spark-ee7d.local:8000/v1"
    ;;
  ollama)
    export LLM_MODEL="ollama_chat/qwen3.5:35b-a3b"
    export LLM_API_BASE="http://host.docker.internal:11434"
    ;;
  nim)
    export LLM_MODEL="openai/nvidia/llama-3.1-nemotron-70b-instruct"
    export LLM_API_BASE="https://integrate.api.nvidia.com/v1"
    ;;
  *)
    echo "Unknown profile: $PROFILE"
    exit 1
    ;;
esac

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  NCMS NemoClaw Multi-Agent Demo                        ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "  Project:    $PROJECT_ROOT"
echo "  Compose:    $COMPOSE_FILE"
echo "  Profile:    $PROFILE"
echo "  LLM Model:  $LLM_MODEL"
echo "  LLM Base:   $LLM_API_BASE"
echo "  SPLADE:     $([ -n "$HF_TOKEN" ] && echo 'yes (HF_TOKEN set)' || echo 'no (skipped)')"
echo ""

# ── Adapter prerequisite check ───────────────────────────────────────────
# The hub.Dockerfile COPYs v9 LoRA adapters from
# adapters/checkpoints/<domain>/v9/.  These are gitignored binaries
# (regeneratable training outputs); a fresh clone must run
# `ncms adapters train` for each domain before the docker build can
# succeed.  We check up-front so the operator gets a clear error
# instead of a cryptic Docker COPY failure 5 minutes into the build.
ADAPTER_DOMAINS=(clinical conversational software_dev)
MISSING_ADAPTERS=()
for d in "${ADAPTER_DOMAINS[@]}"; do
  if [ ! -f "$PROJECT_ROOT/adapters/checkpoints/$d/v9/manifest.json" ]; then
    MISSING_ADAPTERS+=("$d")
  fi
done
if [ "$BUILD" = true ] && [ "${#MISSING_ADAPTERS[@]}" -gt 0 ]; then
  echo ""
  echo "ERROR: missing v9 adapter checkpoint(s) for domain(s):"
  for d in "${MISSING_ADAPTERS[@]}"; do
    echo "  - $d  (expected $PROJECT_ROOT/adapters/checkpoints/$d/v9/)"
  done
  echo ""
  echo "Build fresh adapters with:"
  for d in "${MISSING_ADAPTERS[@]}"; do
    echo "  uv run ncms adapters train --domain $d --version v9"
  done
  echo ""
  echo "(adapters/checkpoints/ is gitignored by design — the binaries"
  echo " regenerate cleanly from the v9 SDG corpora at"
  echo " adapters/corpora/v9/<domain>/sdg.jsonl)"
  exit 1
fi

# ── Build ───────────────────────────────────────────────────────────────
if [ "$BUILD" = true ]; then
  echo "Building Docker images..."
  echo "  v9 adapters: ${ADAPTER_DOMAINS[*]} (all present)"
  echo ""

  BUILD_ARGS=()
  if [ -n "$HF_TOKEN" ]; then
    BUILD_ARGS+=(--build-arg "HF_TOKEN=$HF_TOKEN")
  fi

  docker compose -f "$COMPOSE_FILE" build "${BUILD_ARGS[@]}"

  echo ""
  echo "Images built successfully."
  echo ""
fi

if [ "$RUN" = false ]; then
  echo "Build complete (--build-only). Exiting."
  exit 0
fi

# ── Run ─────────────────────────────────────────────────────────────────
echo "Starting NemoClaw containers..."
echo ""

docker compose -f "$COMPOSE_FILE" up -d

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  NemoClaw Demo Started                                 ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "  Dashboard:  http://localhost:8420"
echo "  Hub API:    http://localhost:8080"
echo "  Health:     http://localhost:8080/api/v1/health"
echo ""
echo "  Agents:"
echo "    architect → architecture, calm-model, quality, decisions"
echo "    security  → security, threats, compliance, controls"
echo "    builder   → identity-service, implementation"
echo ""
echo "  LLM:        $LLM_MODEL"
echo "  Profile:    $PROFILE"
echo ""
echo "  Commands:"
echo "    Logs:     docker compose -f $COMPOSE_FILE logs -f"
echo "    Hub logs: docker compose -f $COMPOSE_FILE logs -f ncms-hub"
echo "    Status:   docker compose -f $COMPOSE_FILE ps"
echo "    Stop:     $0 --down"
echo ""
echo "  Test bus ask:"
echo "    curl -X POST http://localhost:8080/api/v1/bus/ask \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"from_agent\":\"user\",\"question\":\"What authentication method should we use?\",\"domains\":[\"architecture\"]}'"
echo ""
