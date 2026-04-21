#!/usr/bin/env bash
# Main 12-cell re-run with new harness (predictions dump + per-class metrics).
# Fresh minis where each query carries a query_class tag.
# Skips mem0-full (user said we can rerun that later).

set -eu
cd /Users/shawnmccarthy/ncms

TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG_DIR=benchmarks/mseb/run-logs
OUT_DIR=benchmarks/results/mseb/main12
mkdir -p "$LOG_DIR" "$OUT_DIR"

run() {
    local d=$1 b=$2 a=$3 c=$4
    local log="$LOG_DIR/main12-$d-$c-$TS.log"
    local -a extra
    case "$c" in
        tlg-on)  extra=(--backend ncms --adapter-domain "$a") ;;
        tlg-off) extra=(--backend ncms --adapter-domain "$a" --tlg-off) ;;
        mem0)    extra=(--backend mem0) ;;
    esac
    echo "[$(date -u +%H:%M:%S)] $d / $c"
    uv run python -m benchmarks.mseb.harness \
        --domain "$d" --build-dir "$b" --out-dir "$OUT_DIR" \
        "${extra[@]}" > "$log" 2>&1 || echo "  FAILED exit=$? see $log"
    grep -A 4 '"overall"' "$log" | head -6 || true
    echo
}

for cfg in tlg-on tlg-off mem0; do
    run main_softwaredev benchmarks/mseb_softwaredev/build_mini software_dev   $cfg
    run main_swe         benchmarks/mseb_swe/build_mini         swe_diff       $cfg
    run main_clinical    benchmarks/mseb_clinical/build_mini    clinical       $cfg
    run main_convo       benchmarks/mseb_convo/build_mini       conversational $cfg
done

echo "[$(date -u +%H:%M:%S)] main 12 complete"
