#!/usr/bin/env bash
# ============================================================================
# Phase Benchmark Runner
# ============================================================================
# Runs benchmark suites and saves results to a phase-specific directory.
# Reuses the same harnesses as run_phase0.sh but parameterized by phase name.
#
# Usage:
#   ./benchmarks/run_phase.sh phase4              # Full suite (skip MAB)
#   ./benchmarks/run_phase.sh phase4 --quick      # Quick mode (test subsets)
#   ./benchmarks/run_phase.sh phase4 hub beir     # Specific suites only
#
# Output:
#   benchmarks/results/<phase>/                   # All results collected here
#
# Compare with baseline:
#   ./benchmarks/compare_phases.sh phase0_baseline phase4
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Load .env if present (for HF_TOKEN, LLM endpoints, etc.)
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$PROJECT_ROOT/.env"
    set +a
fi

# UV binary
UV="${UV:-/Users/shawnmccarthy/.local/bin/uv}"

# ── Parse arguments ─────────────────────────────────────────────────────────

if [ $# -lt 1 ]; then
    echo "Usage: $0 <phase-name> [--quick] [suite ...]"
    echo ""
    echo "Examples:"
    echo "  $0 phase4                    # All suites (skip MAB)"
    echo "  $0 phase4 --quick            # Quick mode"
    echo "  $0 phase4 hub beir locomo    # Specific suites"
    exit 1
fi

PHASE="$1"
shift

QUICK=false
SUITES=()
for arg in "$@"; do
    case "$arg" in
        --quick) QUICK=true ;;
        *) SUITES+=("$arg") ;;
    esac
done

# Default: all suites except MAB (takes too long for regression checks)
if [ ${#SUITES[@]} -eq 0 ]; then
    SUITES=(hub beir locomo locomo-plus longmemeval)
fi

# Output directory
PHASE_DIR="benchmarks/results/$PHASE"
mkdir -p "$PHASE_DIR"

TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")
LOG_FILE="$PHASE_DIR/${PHASE}_${TIMESTAMP}.log"

# Use offline mode for transformers
export TRANSFORMERS_OFFLINE=1
export HF_HUB_OFFLINE=1

# ── Per-phase feature flags ─────────────────────────────────────────────────
# Enable features introduced in each phase so benchmarks measure their impact.
# Env vars are picked up by NCMSConfig (pydantic Settings with NCMS_ prefix).

case "$PHASE" in
    phase4*)
        export NCMS_CONTENT_CLASSIFICATION_ENABLED=true
        export NCMS_TEMPORAL_ENABLED=true
        ;;
    phase5*)
        export NCMS_CONTENT_CLASSIFICATION_ENABLED=true
        export NCMS_TEMPORAL_ENABLED=true
        export NCMS_SCORING_WEIGHT_HIERARCHY=0.5
        ;;
esac

# ── Logging ─────────────────────────────────────────────────────────────────

log() {
    local msg="[$(date +%H:%M:%S)] $*"
    echo "$msg" | tee -a "$LOG_FILE"
}

log "============================================================"
log "NCMS Benchmark Suite — $PHASE"
log "============================================================"
log "  Timestamp : $TIMESTAMP"
log "  Git SHA   : $(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
log "  Python    : $($UV run python --version 2>&1)"
log "  Quick mode: $QUICK"
log "  Suites    : ${SUITES[*]}"
log "  Output    : $PHASE_DIR"
log "============================================================"
log ""

QUICK_FLAG=""
if [ "$QUICK" = true ]; then
    QUICK_FLAG="--test"
    log "QUICK MODE: Running with --test flag (subset of data)"
    log ""
fi

# ── Suite runners ───────────────────────────────────────────────────────────

PASSED=0
FAILED=0
SKIPPED=0

run_suite() {
    local name="$1"
    shift
    local cmd="$*"

    local suite_slug
    suite_slug=$(echo "$name" | tr '[:upper:]' '[:lower:]' | tr ' ' '_' | tr -cd '[:alnum:]_')
    local suite_log="$PHASE_DIR/${PHASE}_${suite_slug}_${TIMESTAMP}.log"

    log "──────────────────────────────────────────────────────────"
    log "Starting: $name"
    log "  Command: $cmd"
    log ""

    local start_time
    start_time=$(date +%s)

    if eval "$cmd" > "$suite_log" 2>&1; then
        local end_time duration
        end_time=$(date +%s)
        duration=$((end_time - start_time))
        log "  PASSED: $name (${duration}s)"
        PASSED=$((PASSED + 1))
    else
        local exit_code=$?
        local end_time duration
        end_time=$(date +%s)
        duration=$((end_time - start_time))
        log "  FAILED: $name (${duration}s, exit=$exit_code) — check $suite_log"
        FAILED=$((FAILED + 1))
    fi
    log ""
}

# ── Run selected suites ────────────────────────────────────────────────────

for suite in "${SUITES[@]}"; do
    case "$suite" in
        hub)
            run_suite "Hub Replay" \
                "$UV run python -m benchmarks hub --output-dir $PHASE_DIR --verbose"
            ;;
        beir)
            run_suite "BEIR SciFact" \
                "$UV run python -m benchmarks.beir.run_ablation --datasets scifact --output-dir $PHASE_DIR --verbose"
            ;;
        locomo)
            run_suite "LoCoMo Standard" \
                "$UV run python -m benchmarks locomo $QUICK_FLAG --output-dir $PHASE_DIR --verbose"
            ;;
        locomo-plus)
            run_suite "LoCoMo-Plus Cognitive" \
                "$UV run python -m benchmarks locomo --plus $QUICK_FLAG --output-dir $PHASE_DIR --verbose"
            ;;
        longmemeval)
            run_suite "LongMemEval" \
                "$UV run python -m benchmarks longmemeval $QUICK_FLAG --output-dir $PHASE_DIR --verbose"
            ;;
        mab)
            run_suite "MemoryAgentBench" \
                "$UV run python -m benchmarks mab $QUICK_FLAG --output-dir $PHASE_DIR --verbose"
            ;;
        swebench)
            run_suite "SWE-bench Django" \
                "$UV run python -m benchmarks swebench --output-dir $PHASE_DIR --verbose"
            ;;
        *)
            log "  SKIPPED: Unknown suite '$suite'"
            SKIPPED=$((SKIPPED + 1))
            ;;
    esac
done

# ── Summary ─────────────────────────────────────────────────────────────────

log "============================================================"
log "$PHASE Benchmark Complete"
log "============================================================"
log "  Passed  : $PASSED"
log "  Failed  : $FAILED"
log "  Skipped : $SKIPPED"
log "  Results : $PHASE_DIR/"
log ""
log "Compare with baseline:"
log "  ./benchmarks/compare_phases.sh phase0_baseline $PHASE"
log "============================================================"

if [ "$FAILED" -gt 0 ]; then
    log ""
    log "WARNING: $FAILED suite(s) failed — review logs in $PHASE_DIR/"
    exit 1
fi
