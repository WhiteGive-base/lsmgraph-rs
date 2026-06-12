#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

TAG="${TAG:-e9-ldbc-end-to-end-$(date +%Y%m%d-%H%M%S)}"
ROOT_LOG="${ROOT_LOG:-remote-logs/${TAG}}"
BENCH_OUT="${BENCH_OUT:-${ROOT_LOG}/high-thread}"
SUMMARY_OUT="${SUMMARY_OUT:-${ROOT_LOG}/summary}"

RUN_SF10="${RUN_SF10:-false}"
RUN_SF30="${RUN_SF30:-true}"
SF10_THREADS="${SF10_THREADS:-1 4 8}"
SF30_THREADS="${SF30_THREADS:-1 4 8}"
WARMUP="${WARMUP:-500}"
OPERATION_COUNT="${OPERATION_COUNT:-5000}"
IO_BACKEND="${IO_BACKEND:-direct}"
BUILD_RELEASE="${BUILD_RELEASE:-true}"

abs_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$(pwd)" "$1" ;;
  esac
}

ROOT_LOG="$(abs_path "$ROOT_LOG")"
BENCH_OUT="$(abs_path "$BENCH_OUT")"
SUMMARY_OUT="$(abs_path "$SUMMARY_OUT")"

mkdir -p "$ROOT_LOG" "$SUMMARY_OUT"

{
  echo "date=$(date -Is)"
  echo "pwd=$(pwd)"
  echo "tag=${TAG}"
  echo "bench_out=${BENCH_OUT}"
  echo "run_sf10=${RUN_SF10}"
  echo "run_sf30=${RUN_SF30}"
  echo "sf10_threads=${SF10_THREADS}"
  echo "sf30_threads=${SF30_THREADS}"
  echo "warmup=${WARMUP}"
  echo "operation_count=${OPERATION_COUNT}"
  echo "io_backend=${IO_BACKEND}"
  git rev-parse HEAD || true
  git status --short || true
  rustc --version || true
  cargo --version || true
  df -h /data || true
} > "${ROOT_LOG}/run.meta"

set +e
OUT_DIR="$BENCH_OUT" \
RUN_SF10="$RUN_SF10" \
RUN_SF30="$RUN_SF30" \
SF10_THREADS="$SF10_THREADS" \
SF30_THREADS="$SF30_THREADS" \
WARMUP="$WARMUP" \
OPERATION_COUNT="$OPERATION_COUNT" \
IO_BACKEND="$IO_BACKEND" \
BUILD_RELEASE="$BUILD_RELEASE" \
  bash deps/ldbc_snb_interactive_impls/lsmgraph/run_high_thread_benchmarks.sh \
  > "${ROOT_LOG}/runner.stdout" \
  2> "${ROOT_LOG}/runner.stderr"
status=$?
set -e
echo "$status" > "${ROOT_LOG}/driver-exit-code.txt"
if [[ "$status" != "0" ]]; then
  tail -120 "${ROOT_LOG}/runner.stderr" >&2 || true
  exit "$status"
fi

python3 scripts/analysis/current/summarize-end-to-end-evidence.py \
  --out-dir "$SUMMARY_OUT" \
  --ldbc-dir "$BENCH_OUT" \
  > "${ROOT_LOG}/summary-path.txt"

if [[ ! -s "${SUMMARY_OUT}/ldbc-end-to-end-summary.tsv" ]]; then
  echo "missing LDBC end-to-end summary" >&2
  exit 10
fi
if [[ "$(wc -l < "${SUMMARY_OUT}/ldbc-end-to-end-summary.tsv")" -le 1 ]]; then
  echo "LDBC end-to-end summary has no case rows" >&2
  exit 11
fi

touch "${ROOT_LOG}/PASS"
echo "PASS ${ROOT_LOG}"
