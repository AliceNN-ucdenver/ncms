#!/bin/bash
# Build and run the NCMS NemoClaw Blueprint container.
#
# Usage:
#   ./build_and_run.sh                        # Build + run dashboard (DGX Spark)
#   ./build_and_run.sh --profile ollama       # Build + run with Ollama
#   ./build_and_run.sh --profile nim          # Build + run with NVIDIA NIM
#   ./build_and_run.sh --hf-token hf_xxxx    # Build with SPLADE model
#   ./build_and_run.sh --demo                 # Run ND agent demo instead of dashboard
#   ./build_and_run.sh --shell                # Interactive shell
#   ./build_and_run.sh --build-only           # Build image without running
#
# The script auto-detects a .env file in the project root (../../.env).
# Override with --env-file <path>.
#
# .env variables:
#   HF_TOKEN          — HuggingFace token for SPLADE v3 (gated model, build-time)
#   NVIDIA_API_KEY    — For NIM profile (runtime)
#   OPENAI_API_KEY    — For custom OpenAI-compatible endpoints (runtime)

set -e

# ── Defaults ──────────────────────────────────────────────────────────────
IMAGE="ncms-nemoclaw:latest"
CONTAINER="ncms-nemoclaw"
PROFILE="default"
MODE="serve"
BUILD_ONLY=false
HF_TOKEN="${HF_TOKEN:-}"
ENV_FILE=""

# ── Parse args ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    --hf-token)
      HF_TOKEN="$2"
      shift 2
      ;;
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    --demo)
      MODE="demo"
      shift
      ;;
    --shell)
      MODE="shell"
      shift
      ;;
    --build-only)
      BUILD_ONLY=true
      shift
      ;;
    --help|-h)
      echo "Usage: $0 [--profile default|ollama|nim|vllm] [--hf-token TOKEN] [--env-file FILE] [--demo] [--shell] [--build-only]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# ── Resolve paths ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DOCKERFILE="$SCRIPT_DIR/Dockerfile"

# ── Auto-detect .env from project root ────────────────────────────────────
if [ -z "$ENV_FILE" ] && [ -f "$PROJECT_ROOT/.env" ]; then
  ENV_FILE="$PROJECT_ROOT/.env"
  echo "Auto-detected .env at $ENV_FILE"
fi

# ── Load .env file ────────────────────────────────────────────────────────
if [ -n "$ENV_FILE" ]; then
  if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: env file not found: $ENV_FILE"
    exit 1
  fi
  echo "Loading environment from $ENV_FILE"
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

echo ""
echo "=== NCMS NemoClaw Blueprint ==="
echo ""
echo "  Project:    $PROJECT_ROOT"
echo "  Dockerfile: $DOCKERFILE"
echo "  Image:      $IMAGE"
echo "  Profile:    $PROFILE"
echo "  Mode:       $MODE"
echo "  SPLADE:     $([ -n "$HF_TOKEN" ] && echo 'yes (HF_TOKEN set)' || echo 'no (skipped)')"
echo "  Env file:   ${ENV_FILE:-none}"
echo ""

# ── Build ─────────────────────────────────────────────────────────────────
echo "Building Docker image..."
BUILD_ARGS=()
if [ -n "$HF_TOKEN" ]; then
  BUILD_ARGS+=(--build-arg "HF_TOKEN=$HF_TOKEN")
fi

docker build \
  -f "$DOCKERFILE" \
  "${BUILD_ARGS[@]}" \
  -t "$IMAGE" \
  "$PROJECT_ROOT"

echo ""
echo "Image built: $IMAGE"

if [ "$BUILD_ONLY" = true ]; then
  echo "Build complete (--build-only). Exiting."
  exit 0
fi

# ── Stop existing container ───────────────────────────────────────────────
if docker inspect "$CONTAINER" > /dev/null 2>&1; then
  echo "Stopping existing container..."
  docker rm -f "$CONTAINER" > /dev/null 2>&1 || true
fi

# ── Profile → env vars ───────────────────────────────────────────────────
ENV_ARGS=()

case "$PROFILE" in
  default)
    ENV_ARGS+=(-e "NCMS_LLM_MODEL=openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16")
    ENV_ARGS+=(-e "NCMS_LLM_API_BASE=http://spark-ee7d.local:8000/v1")
    ;;
  ollama)
    ENV_ARGS+=(-e "NCMS_LLM_MODEL=ollama_chat/qwen3.5:35b-a3b")
    ENV_ARGS+=(-e "NCMS_LLM_API_BASE=")
    ;;
  nim)
    ENV_ARGS+=(-e "NCMS_LLM_MODEL=openai/nvidia/llama-3.1-nemotron-70b-instruct")
    ENV_ARGS+=(-e "NCMS_LLM_API_BASE=https://integrate.api.nvidia.com/v1")
    [ -n "$NVIDIA_API_KEY" ] && ENV_ARGS+=(-e "NVIDIA_API_KEY=$NVIDIA_API_KEY")
    ;;
  vllm)
    ENV_ARGS+=(-e "NCMS_LLM_MODEL=openai/nvidia/nemotron-3-nano-30b-a3b")
    ENV_ARGS+=(-e "NCMS_LLM_API_BASE=http://localhost:8000/v1")
    ;;
  *)
    echo "Unknown profile: $PROFILE"
    exit 1
    ;;
esac

# Pass through secrets if set
[ -n "$OPENAI_API_KEY" ] && ENV_ARGS+=(-e "OPENAI_API_KEY=$OPENAI_API_KEY")
[ -n "$NVIDIA_API_KEY" ] && ENV_ARGS+=(-e "NVIDIA_API_KEY=$NVIDIA_API_KEY")
[ -n "$HF_TOKEN" ] && ENV_ARGS+=(-e "HF_TOKEN=$HF_TOKEN")

# Pass --env-file to Docker for any additional vars
if [ -n "$ENV_FILE" ]; then
  ENV_ARGS+=(--env-file "$ENV_FILE")
fi

# ── Run ───────────────────────────────────────────────────────────────────
echo ""
echo "Starting container ($MODE)..."

RUN_ARGS=(
  --name "$CONTAINER"
  -p 8420:8420
  -p 8080:8080
  -v ncms-data:/app/data
  "${ENV_ARGS[@]}"
)

case "$MODE" in
  serve)
    docker run -d "${RUN_ARGS[@]}" "$IMAGE"
    echo ""
    echo "=== Container started ==="
    echo ""
    echo "  Dashboard:  http://localhost:8420"
    echo "  MCP HTTP:   http://localhost:8080"
    echo "  Profile:    $PROFILE"
    echo ""
    echo "  Logs:       docker logs -f $CONTAINER"
    echo "  Stop:       docker rm -f $CONTAINER"
    echo ""
    ;;
  demo)
    docker run -it --rm "${RUN_ARGS[@]}" "$IMAGE" demo
    ;;
  shell)
    docker run -it --rm "${RUN_ARGS[@]}" "$IMAGE" shell
    ;;
esac
