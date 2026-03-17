#!/usr/bin/env bash
#
# NCMS Parallel Dream Cycle Experiment Runner
#
# Launches one process per dataset, each with its own:
#   - In-memory database (completely independent)
#   - Timestamped log file
#   - Results JSON and markdown table
#
# Usage:
#   ./benchmarks/run_dream_parallel.sh                          # All 3 datasets
#   ./benchmarks/run_dream_parallel.sh scifact nfcorpus         # Specific datasets
#   ./benchmarks/run_dream_parallel.sh scifact --verbose
#
# LLM Override:
#   LLM_MODEL=ollama_chat/qwen3.5:35b-a3b LLM_API_BASE="" ./benchmarks/run_dream_parallel.sh
#
# Monitor all running datasets:
#   tail -f benchmarks/results/dream/*/dream_latest.log
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
ALL_DATASETS=(scifact nfcorpus arguana)
DATASETS=()
EXTRA_ARGS=""

for arg in "$@"; do
    case "$arg" in
        --verbose|-v)
            EXTRA_ARGS="$EXTRA_ARGS --verbose"
            ;;
        *)
            found=false
            for valid in "${ALL_DATASETS[@]}"; do
                if [ "$arg" = "$valid" ]; then
                    DATASETS+=("$arg")
                    found=true
                    break
                fi
            done
            if [ "$found" = false ]; then
                error "Unknown argument or dataset: $arg"
                error "Valid datasets: ${ALL_DATASETS[*]}"
                exit 1
            fi
            ;;
    esac
done

# Default to all datasets if none specified
if [ ${#DATASETS[@]} -eq 0 ]; then
    DATASETS=("${ALL_DATASETS[@]}")
fi

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

# Check LLM connectivity
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

TIMESTAMP="$(date +%Y-%m-%d_%H%M%S)"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
SUMMARY_LOG="$RESULTS_DIR/parallel_${TIMESTAMP}.log"

# Create per-dataset output directories
for ds in "${DATASETS[@]}"; do
    mkdir -p "$RESULTS_DIR/$ds"
done

# ── Banner ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║  NCMS Parallel Dream Cycle Experiment                   ║${NC}"
echo -e "${BOLD}${CYAN}║  One process per dataset, fully independent             ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
info "Timestamp : $TIMESTAMP"
info "Git SHA   : $GIT_SHA"
info "Datasets  : ${DATASETS[*]} (${#DATASETS[@]} parallel processes)"
info "LLM Model : $LLM_MODEL"
info "LLM API   : $LLM_API_BASE"
info "Results   : $RESULTS_DIR/<dataset>/"
info "Summary   : $SUMMARY_LOG"
echo ""
info "Monitor all:  tail -f $RESULTS_DIR/*/dream_latest.log"
echo ""

# Write summary log header
{
    echo "NCMS Parallel Dream Cycle Experiment"
    echo "====================================="
    echo "Start     : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Git SHA   : $GIT_SHA"
    echo "LLM Model : $LLM_MODEL"
    echo "Datasets  : ${DATASETS[*]}"
    echo "Processes : ${#DATASETS[@]}"
    echo ""
} > "$SUMMARY_LOG"

# ── Launch parallel processes ──────────────────────────────────────────
# Use parallel arrays (Bash 3.2 compat — no declare -A)
PIDS=()
PID_NAMES=()

START_TIME=$(date +%s)

for ds in "${DATASETS[@]}"; do
    DS_DIR="$RESULTS_DIR/$ds"

    info "Launching $ds (output: $DS_DIR/)"

    uv run python -m benchmarks.run_dream \
        --datasets "$ds" \
        --output-dir "$DS_DIR" \
        --llm-model "$LLM_MODEL" \
        --llm-api-base "$LLM_API_BASE" \
        $EXTRA_ARGS &

    pid=$!
    PIDS+=($pid)
    PID_NAMES+=("$ds")

    echo "  PID $pid -> $ds" >> "$SUMMARY_LOG"
done

echo "" >> "$SUMMARY_LOG"
echo ""
info "All ${#DATASETS[@]} processes launched. PIDs: ${PIDS[*]}"
info "Waiting for completion..."
echo ""

# ── Wait for all processes ─────────────────────────────────────────────
FAILURES=0
SUCCEEDED=()
FAILED=()

for i in "${!PIDS[@]}"; do
    pid="${PIDS[$i]}"
    ds="${PID_NAMES[$i]}"
    if wait "$pid"; then
        info "$ds completed successfully (PID $pid)"
        echo "PASS: $ds (PID $pid)" >> "$SUMMARY_LOG"
        SUCCEEDED+=("$ds")
    else
        exit_code=$?
        error "$ds failed with exit code $exit_code (PID $pid)"
        echo "FAIL: $ds (PID $pid, exit $exit_code)" >> "$SUMMARY_LOG"
        FAILED+=("$ds")
        FAILURES=$((FAILURES + 1))
    fi
done

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
MINUTES=$((DURATION / 60))
SECONDS=$((DURATION % 60))

# ── Summary ────────────────────────────────────────────────────────────
echo ""
{
    echo ""
    echo "Results"
    echo "======="
    echo "Duration  : ${MINUTES}m ${SECONDS}s"
    echo "Succeeded : ${#SUCCEEDED[@]} (${SUCCEEDED[*]:-none})"
    echo "Failed    : ${#FAILED[@]} (${FAILED[*]:-none})"
    echo "End       : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "$SUMMARY_LOG"

if [ $FAILURES -eq 0 ]; then
    echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${GREEN}║  All ${#DATASETS[@]} datasets completed successfully              ║${NC}"
    echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
else
    echo -e "${BOLD}${RED}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${RED}║  $FAILURES of ${#DATASETS[@]} datasets failed                           ║${NC}"
    echo -e "${BOLD}${RED}╚══════════════════════════════════════════════════════════╝${NC}"
fi
echo ""
info "Duration  : ${MINUTES}m ${SECONDS}s (wall clock, parallel)"
info "Summary   : $SUMMARY_LOG"
echo ""
info "Per-dataset results:"
for ds in "${DATASETS[@]}"; do
    DS_DIR="$RESULTS_DIR/$ds"
    echo -e "  ${CYAN}$ds${NC}"
    echo "    Log     : $DS_DIR/dream_latest.log"
    echo "    Results : $DS_DIR/dream_results.json"
    echo "    Table   : $DS_DIR/dream_table.md"
done
echo ""

exit $FAILURES
