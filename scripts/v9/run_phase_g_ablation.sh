#!/usr/bin/env bash
# Phase G ablation runner — runs ONE SLM-signal-isolation ablation
# across all three v9 domains using the MSEB main12-mini harness.
#
# Usage:
#   scripts/v9/run_phase_g_ablation.sh <ablation_label> [extra harness flags...]
#
# Example:
#   scripts/v9/run_phase_g_ablation.sh A_no_populate_domains --no-populate-domains
#   scripts/v9/run_phase_g_ablation.sh B_no_recon_penalty --no-reconciliation-penalty
#   scripts/v9/run_phase_g_ablation.sh C_no_hier_bonus --hierarchy-weight 0.0
#   scripts/v9/run_phase_g_ablation.sh D_high_threshold --slm-confidence-threshold 0.7
#
# Each cell uses the NCMS backend with SLM ON (default), but applies
# the ablation flag(s) on top.  Output goes to
# benchmarks/results/mseb/phase_g_ablations/<label>_<ts>/

set -eu
cd /Users/shawnmccarthy/ncms

if [[ "${1:-}" == "" ]]; then
    echo "usage: $0 <ablation_label> [extra harness flags...]"
    exit 1
fi
LABEL="$1"
shift
EXTRA_FLAGS=("$@")

TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG_DIR=benchmarks/mseb/run-logs
OUT_DIR="benchmarks/results/mseb/phase_g_ablations/${LABEL}_${TS}"
mkdir -p "$LOG_DIR" "$OUT_DIR"

run_cell() {
    local d=$1 b=$2 a=$3
    local log="$LOG_DIR/phase-g-${LABEL}-${d}-${TS}.log"
    echo "[$(date -u +%H:%M:%S)] ${LABEL} / ${d}"
    # ${EXTRA_FLAGS[@]+"${EXTRA_FLAGS[@]}"} expands to nothing when
    # the array is empty — required under `set -u` so a no-flag call
    # (validating a code-side fix) doesn't trip "unbound variable".
    uv run python -m benchmarks.mseb.harness \
        --domain "$d" --build-dir "$b" --out-dir "$OUT_DIR" \
        --backend ncms --adapter-domain "$a" \
        ${EXTRA_FLAGS[@]+"${EXTRA_FLAGS[@]}"} \
        > "$log" 2>&1 \
        || echo "  FAILED exit=$? see $log"
    grep -A 4 '"overall"' "$log" 2>/dev/null | head -5 \
        || echo "  (no metrics block)"
    echo
}

run_cell main_softwaredev benchmarks/mseb_softwaredev/build_mini software_dev
run_cell main_clinical    benchmarks/mseb_clinical/build_mini    clinical
run_cell main_convo       benchmarks/mseb_convo/build_mini       conversational

echo "[$(date -u +%H:%M:%S)] ablation ${LABEL} complete"
echo "Results: $OUT_DIR"
