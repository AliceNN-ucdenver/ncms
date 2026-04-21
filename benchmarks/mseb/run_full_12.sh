#!/usr/bin/env bash
# Full-scale 12-cell re-run on build/ (not build_mini/).
# 4 domains x {temporal-on, temporal-off, mem0} = 12 cells, 747 locked gold queries.
# Writes per-cell predictions + summary.md to benchmarks/results/mseb/full12/.
#
# Cell labels match the NCMSConfig master flag.  Every run-log opens
# with the line "NCMS runtime config: temporal_enabled=... slm_enabled=..."
# — grep that to verify what actually ran.

set -eu
cd /Users/shawnmccarthy/ncms

TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG_DIR=benchmarks/mseb/run-logs
OUT_DIR=benchmarks/results/mseb/full12
mkdir -p "$LOG_DIR" "$OUT_DIR"

# Tombstone log — tee of cell progress so `tail -f` gives one feed.
MAIN_LOG="$LOG_DIR/full12-$TS.log"
echo "[$(date -u +%H:%M:%S)] full-12 starting, TS=$TS" | tee "$MAIN_LOG"

run() {
    local d=$1 b=$2 a=$3 c=$4
    local log="$LOG_DIR/full12-$d-$c-$TS.log"
    local -a extra
    case "$c" in
        temporal-on)  extra=(--backend ncms --adapter-domain "$a") ;;
        temporal-off) extra=(--backend ncms --adapter-domain "$a" --temporal-off) ;;
        mem0)    extra=(--backend mem0) ;;
    esac
    local start=$(date +%s)
    echo "[$(date -u +%H:%M:%S)] BEGIN $d / $c" | tee -a "$MAIN_LOG"
    uv run python -m benchmarks.mseb.harness \
        --domain "$d" --build-dir "$b" --out-dir "$OUT_DIR" \
        "${extra[@]}" > "$log" 2>&1 \
        && echo "  -> ok (see $log)" | tee -a "$MAIN_LOG" \
        || echo "  -> FAILED exit=$? see $log" | tee -a "$MAIN_LOG"
    local elapsed=$(( $(date +%s) - start ))
    echo "[$(date -u +%H:%M:%S)] END   $d / $c  (${elapsed}s)" | tee -a "$MAIN_LOG"
    grep -A 4 '"overall"' "$log" | head -6 | tee -a "$MAIN_LOG" || true
    echo "" | tee -a "$MAIN_LOG"
}

# softwaredev first (smallest) so early failures surface fast.
# Then swe, clinical, convo (largest).
for cfg in temporal-on temporal-off mem0; do
    run main_softwaredev benchmarks/mseb_softwaredev/build software_dev   $cfg
    run main_swe         benchmarks/mseb_swe/build         swe_diff       $cfg
    run main_clinical    benchmarks/mseb_clinical/build    clinical       $cfg
    run main_convo       benchmarks/mseb_convo/build       conversational $cfg
done

echo "[$(date -u +%H:%M:%S)] full-12 complete, TS=$TS" | tee -a "$MAIN_LOG"
echo "Results in $OUT_DIR" | tee -a "$MAIN_LOG"
