#!/usr/bin/env bash
#
# NCMS Dream Cycle / Consolidation Experiment Runner
#
# Proper entry point for dream cycle benchmark runs with durable logging.
# Requires an LLM endpoint (DGX Spark or Ollama) for consolidation synthesis.
#
# Usage:
#   ./benchmarks/run_dream.sh                           # All datasets
#   ./benchmarks/run_dream.sh scifact                   # Single dataset
#   ./benchmarks/run_dream.sh scifact,nfcorpus          # Multiple datasets
#   ./benchmarks/run_dream.sh scifact --verbose         # With debug logging
#
# LLM Override:
#   LLM_MODEL=ollama_chat/qwen3.5:35b-a3b LLM_API_BASE="" ./benchmarks/run_dream.sh
#
# Monitor:
#   tail -f benchmarks/results/dream/dream_latest.log
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RESULTS_DIR="$SCRIPT_DIR/results/dream"

# ── Colors ─────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Parse arguments ────────────────────────────────────────────────────
DATASETS="${1:-scifact,nfcorpus,arguana}"
shift 2>/dev/null || true
EXTRA_ARGS="$*"

# LLM configuration (env vars with defaults)
LLM_MODEL="${LLM_MODEL:-openai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16}"
LLM_API_BASE="${LLM_API_BASE:-http://spark-ee7d.local:8000/v1}"

# ── Pre-flight checks ─────────────────────────────────────────────────
cd "$PROJECT_ROOT"

# Load .env if present (HF_TOKEN, etc.)
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi

if ! command -v uv &>/dev/null; then
    error "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

if ! uv run python -c "import benchmarks" 2>/dev/null; then
    info "Installing benchmark dependencies..."
    uv sync --group bench
fi

# Check LLM connectivity (skip for Ollama which doesn't need api_base)
if [ -n "$LLM_API_BASE" ]; then
    info "Checking LLM connectivity at $LLM_API_BASE ..."
    if curl -sf "$LLM_API_BASE/models" > /dev/null 2>&1; then
        info "  LLM endpoint is reachable"
    else
        warn "  LLM endpoint not reachable at $LLM_API_BASE"
        warn "  Consolidation LLM calls may fail — continuing anyway"
    fi
fi

# ── Environment ────────────────────────────────────────────────────────
export PYTHONUNBUFFERED=1

mkdir -p "$RESULTS_DIR"

# ── Run info ───────────────────────────────────────────────────────────
TIMESTAMP="$(date +%Y-%m-%d_%H%M%S)"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║  NCMS Dream Cycle / Consolidation Experiment            ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
info "Timestamp : $TIMESTAMP"
info "Git SHA   : $GIT_SHA"
info "Datasets  : $DATASETS"
info "LLM Model : $LLM_MODEL"
info "LLM API   : $LLM_API_BASE"
info "Results   : $RESULTS_DIR/"
info "Log file  : $RESULTS_DIR/dream_${TIMESTAMP}.log"
info "Monitor   : tail -f $RESULTS_DIR/dream_latest.log"
echo ""

# ── Run the experiment ────────────────────────────────────────────────
info "Starting dream cycle experiment..."
echo ""

START_TIME=$(date +%s)

uv run python -m benchmarks.dream.run_dream \
    --datasets "$DATASETS" \
    --output-dir "$RESULTS_DIR" \
    --llm-model "$LLM_MODEL" \
    --llm-api-base "$LLM_API_BASE" \
    $EXTRA_ARGS

EXIT_CODE=$?
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
MINUTES=$((DURATION / 60))
SECONDS=$((DURATION % 60))

# ── Summary ────────────────────────────────────────────────────────────
echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${GREEN}║  Dream experiment completed successfully                ║${NC}"
    echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
else
    echo -e "${BOLD}${RED}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${RED}║  Dream experiment failed (exit code: $EXIT_CODE)                 ║${NC}"
    echo -e "${BOLD}${RED}╚══════════════════════════════════════════════════════════╝${NC}"
fi
echo ""
info "Duration  : ${MINUTES}m ${SECONDS}s"
info "Log file  : $RESULTS_DIR/dream_${TIMESTAMP}.log"
info "Results   : $RESULTS_DIR/dream_results.json"
info "Table     : $RESULTS_DIR/dream_table.md"
echo ""

exit $EXIT_CODE
