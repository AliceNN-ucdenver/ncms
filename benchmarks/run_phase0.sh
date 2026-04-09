#!/usr/bin/env bash
# ============================================================================
# Phase 0 Baseline Benchmark Suite
# ============================================================================
# Runs all benchmark harnesses against the current NCMS system to establish
# baseline measurements before any resilience work begins.
#
# Usage:
#   ./benchmarks/run_phase0.sh              # Full suite (all benchmarks)
#   ./benchmarks/run_phase0.sh --quick      # Quick mode (test subsets only)
#   ./benchmarks/run_phase0.sh hub          # Single benchmark
#   ./benchmarks/run_phase0.sh hub locomo   # Specific benchmarks
#
# Output:
#   benchmarks/results/phase0_baseline/     # All results collected here
#   benchmarks/results/phase0_baseline/phase0_summary.md  # Summary report
#
# Prerequisites:
#   - SPLADE model: request access at https://huggingface.co/naver/splade-v3
#   - HF_TOKEN in .env for model downloads
#   - DGX Spark at spark-ee7d.local:8000 for LLM judge scoring (optional)
#   - ~2-4 hours for full suite on Apple Silicon
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Output directory for all Phase 0 results
PHASE0_DIR="benchmarks/results/phase0_baseline"
mkdir -p "$PHASE0_DIR"

# Timestamp for this run
TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")
LOG_FILE="$PHASE0_DIR/phase0_${TIMESTAMP}.log"

# UV binary
UV="${UV:-/Users/shawnmccarthy/.local/bin/uv}"

# Parse arguments
QUICK=false
SUITES=()
for arg in "$@"; do
    case "$arg" in
        --quick) QUICK=true ;;
        *) SUITES+=("$arg") ;;
    esac
done

# Default: run all suites
if [ ${#SUITES[@]} -eq 0 ]; then
    SUITES=(hub beir locomo locomo-plus longmemeval mab)
fi

# Use offline mode for transformers to avoid HF timeout checks
export TRANSFORMERS_OFFLINE=1
export HF_HUB_OFFLINE=1

# ── Logging ──────────────────────────────────────────────────────────────────

log() {
    local msg="[$(date +%H:%M:%S)] $*"
    echo "$msg" | tee -a "$LOG_FILE"
}

log "============================================================"
log "NCMS Phase 0 Baseline Benchmark Suite"
log "============================================================"
log "  Timestamp : $TIMESTAMP"
log "  Git SHA   : $(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
log "  Python    : $($UV run python --version 2>&1)"
log "  Quick mode: $QUICK"
log "  Suites    : ${SUITES[*]}"
log "  Output    : $PHASE0_DIR"
log "============================================================"
log ""

# ── Quick mode flags ─────────────────────────────────────────────────────────

QUICK_FLAG=""
if [ "$QUICK" = true ]; then
    QUICK_FLAG="--test"
    log "QUICK MODE: Running with --test flag (subset of data)"
    log ""
fi

# ── Suite runners ────────────────────────────────────────────────────────────

PASSED=0
FAILED=0
SKIPPED=0

run_suite() {
    local name="$1"
    shift
    local cmd="$*"

    log "──────────────────────────────────────────────────────────"
    log "Starting: $name"
    log "  Command: $cmd"
    log ""

    local start_time=$(date +%s)

    if eval "$cmd" >> "$LOG_FILE" 2>&1; then
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))
        log "  PASSED: $name (${duration}s)"
        PASSED=$((PASSED + 1))
    else
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))
        log "  FAILED: $name (${duration}s) — check $LOG_FILE for details"
        FAILED=$((FAILED + 1))
    fi
    log ""
}

# ── Run selected suites ─────────────────────────────────────────────────────

for suite in "${SUITES[@]}"; do
    case "$suite" in
        hub)
            run_suite "Hub Replay" \
                "$UV run python -m benchmarks hub --output-dir $PHASE0_DIR --verbose"
            ;;
        beir)
            if [ "$QUICK" = true ]; then
                run_suite "BEIR SciFact (quick)" \
                    "$UV run python -m benchmarks.beir.run_ablation --datasets scifact --output-dir $PHASE0_DIR --verbose"
            else
                run_suite "BEIR SciFact" \
                    "$UV run python -m benchmarks.beir.run_ablation --datasets scifact --output-dir $PHASE0_DIR --verbose"
            fi
            ;;
        locomo)
            run_suite "LoCoMo Standard" \
                "$UV run python -m benchmarks locomo $QUICK_FLAG --output-dir $PHASE0_DIR --verbose"
            ;;
        locomo-plus)
            run_suite "LoCoMo-Plus Cognitive" \
                "$UV run python -m benchmarks locomo --plus $QUICK_FLAG --output-dir $PHASE0_DIR --verbose"
            ;;
        longmemeval)
            run_suite "LongMemEval" \
                "$UV run python -m benchmarks longmemeval $QUICK_FLAG --output-dir $PHASE0_DIR --verbose"
            ;;
        mab)
            run_suite "MemoryAgentBench" \
                "$UV run python -m benchmarks mab $QUICK_FLAG --output-dir $PHASE0_DIR --verbose"
            ;;
        swebench)
            run_suite "SWE-bench Django" \
                "$UV run python -m benchmarks swebench --output-dir $PHASE0_DIR --verbose"
            ;;
        dream)
            run_suite "Dream Cycle" \
                "$UV run python -m benchmarks dream --output-dir $PHASE0_DIR --verbose"
            ;;
        *)
            log "  SKIPPED: Unknown suite '$suite'"
            SKIPPED=$((SKIPPED + 1))
            ;;
    esac
done

# ── Summary ──────────────────────────────────────────────────────────────────

log "============================================================"
log "Phase 0 Baseline Complete"
log "============================================================"
log "  Passed  : $PASSED"
log "  Failed  : $FAILED"
log "  Skipped : $SKIPPED"
log "  Log     : $LOG_FILE"
log "  Results : $PHASE0_DIR/"
log "============================================================"

# List all result files
log ""
log "Result files:"
find "$PHASE0_DIR" -name "*.json" -o -name "*.md" | sort | while read -r f; do
    log "  $f"
done

# Exit code reflects failures
if [ "$FAILED" -gt 0 ]; then
    log ""
    log "WARNING: $FAILED suite(s) failed — review log for details"
    exit 1
fi
