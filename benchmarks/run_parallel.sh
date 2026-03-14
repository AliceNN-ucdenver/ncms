#!/usr/bin/env bash
#
# NCMS Parallel Ablation Study Runner
#
# Launches one process per dataset, each with its own:
#   - In-memory database (completely independent)
#   - Timestamped log file
#   - Results JSON and markdown table
#
# Usage:
#   ./benchmarks/run_parallel.sh                           # All 3 datasets in parallel
#   ./benchmarks/run_parallel.sh scifact nfcorpus          # Specific datasets
#   ./benchmarks/run_parallel.sh scifact nfcorpus arguana --verbose
#
# Output structure:
#   benchmarks/results/
#     scifact/
#       ablation_2026-03-14_140532.log        # Full log for this dataset
#       ablation_latest.log -> ...            # Symlink to latest
#       ablation_results.json                 # Results for this dataset
#       ablation_table.md                     # Markdown table
#     nfcorpus/
#       ...
#     arguana/
#       ...
#     parallel_2026-03-14_140532.log          # Combined summary log
#
# Monitor all running datasets:
#   tail -f benchmarks/results/*/ablation_latest.log
#
# Monitor a specific dataset:
#   tail -f benchmarks/results/scifact/ablation_latest.log
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RESULTS_DIR="$SCRIPT_DIR/results"

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
            # Check if it's a valid dataset name
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

# ── Pre-flight checks ─────────────────────────────────────────────────
cd "$PROJECT_ROOT"

if ! command -v uv &>/dev/null; then
    error "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

if ! uv run python -c "import benchmarks" 2>/dev/null; then
    info "Installing benchmark dependencies..."
    uv sync --group bench
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
echo -e "${BOLD}${CYAN}║     NCMS Parallel Ablation Study                        ║${NC}"
echo -e "${BOLD}${CYAN}║     One process per dataset, fully independent          ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
info "Timestamp : $TIMESTAMP"
info "Git SHA   : $GIT_SHA"
info "Datasets  : ${DATASETS[*]} (${#DATASETS[@]} parallel processes)"
info "Results   : $RESULTS_DIR/<dataset>/"
info "Summary   : $SUMMARY_LOG"
echo ""
info "Monitor all:  tail -f $RESULTS_DIR/*/ablation_latest.log"
echo ""

# Write summary log header
{
    echo "NCMS Parallel Ablation Study"
    echo "============================"
    echo "Start     : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Git SHA   : $GIT_SHA"
    echo "Datasets  : ${DATASETS[*]}"
    echo "Processes : ${#DATASETS[@]}"
    echo ""
} > "$SUMMARY_LOG"

# ── Launch parallel processes ──────────────────────────────────────────
PIDS=()
declare -A PID_DATASET

START_TIME=$(date +%s)

for ds in "${DATASETS[@]}"; do
    DS_DIR="$RESULTS_DIR/$ds"

    info "Launching $ds (output: $DS_DIR/)"

    uv run python -m benchmarks.run_ablation \
        --datasets "$ds" \
        --output-dir "$DS_DIR" \
        $EXTRA_ARGS &

    pid=$!
    PIDS+=($pid)
    PID_DATASET[$pid]=$ds

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

for pid in "${PIDS[@]}"; do
    ds="${PID_DATASET[$pid]}"
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
    echo -e "${BOLD}${GREEN}║     All ${#DATASETS[@]} datasets completed successfully              ║${NC}"
    echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
else
    echo -e "${BOLD}${RED}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${RED}║     $FAILURES of ${#DATASETS[@]} datasets failed                           ║${NC}"
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
    echo "    Log     : $DS_DIR/ablation_latest.log"
    echo "    Results : $DS_DIR/ablation_results.json"
    echo "    Table   : $DS_DIR/ablation_table.md"
done
echo ""

exit $FAILURES
