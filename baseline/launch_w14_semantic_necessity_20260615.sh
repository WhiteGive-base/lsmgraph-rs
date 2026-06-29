#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
SCALE="${SCALE:-sf30}"
RUN_ID="${RUN_ID:-w14-semantic-necessity-${SCALE}-$(date +%Y%m%d-%H%M%S)}"
LOG_ROOT="${LOG_ROOT:-$ROOT/remote-logs/$RUN_ID}"
STORE_ROOT="${STORE_ROOT:-$ROOT/store/$RUN_ID}"

mkdir -p "$LOG_ROOT" "$STORE_ROOT"
nohup env \
  ROOT="$ROOT" \
  SCALE="$SCALE" \
  RUN_ID="$RUN_ID" \
  LOG_ROOT="$LOG_ROOT" \
  STORE_ROOT="$STORE_ROOT" \
  INPUT="${INPUT:-}" \
  SAMPLES="${SAMPLES:-}" \
  WARMUP_RUNS="${WARMUP_RUNS:-}" \
  REPEATS="${REPEATS:-}" \
  VARIANTS="${VARIANTS:-}" \
  W14_ALLOW_SF100="${W14_ALLOW_SF100:-0}" \
  bash "$ROOT/baseline/run_w14_semantic_necessity_20260615.sh" \
  > "$LOG_ROOT/runner.log" 2>&1 < /dev/null &
pid="$!"
printf '%s\n' "$pid" > "$LOG_ROOT/PID"
printf '%s\n' "$RUN_ID"
printf '%s\n' "$LOG_ROOT"
