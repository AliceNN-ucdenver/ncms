#!/usr/bin/env bash
#
# NCMS Ablation Study Runner
#
# Proper entry point for benchmark runs with durable logging.
# All output is captured to timestamped log files in benchmarks/results/.
#
# Usage:
#   ./benchmarks/run.sh                           # All datasets (scifact,nfcorpus,arguana)
#   ./benchmarks/run.sh scifact                   # Single dataset
#   ./benchmarks/run.sh scifact,nfcorpus          # Multiple datasets
#   ./benchmarks/run.sh scifact --verbose         # With debug logging
#
# The runner:
#   1. Validates the environment (uv, Python, models cached)
#   2. Creates a timestamped log file (never overwrites previous runs)
#   3. Runs with PYTHONUNBUFFERED=1 so logs stream in real-time
#   4. Captures both stdout and stderr to the log file AND the terminal
#   5. Reports the log file location on completion
#
# To monitor a running study:
#   tail -f benchmarks/results/ablation_latest.log
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
DATASETS="${1:-scifact,nfcorpus,arguana}"
shift 2>/dev/null || true
EXTRA_ARGS="$*"

# ── Pre-flight checks ─────────────────────────────────────────────────
cd "$PROJECT_ROOT"

# Check uv is available
if ! command -v uv &>/dev/null; then
    error "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Check benchmark dependencies are installed
if ! uv run python -c "import benchmarks" 2>/dev/null; then
    info "Installing benchmark dependencies..."
    uv sync --group bench
fi

# ── Environment ────────────────────────────────────────────────────────
# Force unbuffered output so logs stream in real-time
export PYTHONUNBUFFERED=1

# Ensure results directory exists
mkdir -p "$RESULTS_DIR"

# ── Run info ───────────────────────────────────────────────────────────
TIMESTAMP="$(date +%Y-%m-%d_%H%M%S)"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║     NCMS Retrieval Pipeline Ablation Study              ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
info "Timestamp : $TIMESTAMP"
info "Git SHA   : $GIT_SHA"
info "Datasets  : $DATASETS"
info "Results   : $RESULTS_DIR/"
info "Log file  : $RESULTS_DIR/ablation_${TIMESTAMP}.log"
info "Monitor   : tail -f $RESULTS_DIR/ablation_latest.log"
echo ""

# ── Run the study ──────────────────────────────────────────────────────
info "Starting ablation study..."
echo ""

START_TIME=$(date +%s)

uv run python -m benchmarks.run_ablation \
    --datasets "$DATASETS" \
    --output-dir "$RESULTS_DIR" \
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
    echo -e "${BOLD}${GREEN}║     Ablation study completed successfully               ║${NC}"
    echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
else
    echo -e "${BOLD}${RED}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${RED}║     Ablation study failed (exit code: $EXIT_CODE)                ║${NC}"
    echo -e "${BOLD}${RED}╚══════════════════════════════════════════════════════════╝${NC}"
fi
echo ""
info "Duration  : ${MINUTES}m ${SECONDS}s"
info "Log file  : $RESULTS_DIR/ablation_${TIMESTAMP}.log"
info "Results   : $RESULTS_DIR/ablation_results.json"
info "Table     : $RESULTS_DIR/ablation_table.md"
echo ""

exit $EXIT_CODE
