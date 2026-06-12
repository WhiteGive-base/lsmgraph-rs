#!/usr/bin/env bash
set -euo pipefail

# W8 formal runner for W4 property predicates + 2-hop typed expansion.
# This script is intentionally not started by W4. It follows the C9
# storage-bench protocol and leaves SF30 execution to W8.

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
INPUT="${INPUT:-/data/WorkSpace/ldbc-sf30/social_network}"
RUN_ID="${RUN_ID:-w8-property-2hop-$(date +%Y%m%d-%H%M%S)}"
LOG_ROOT="${LOG_ROOT:-$ROOT/remote-logs/$RUN_ID}"
STORE_ROOT="${STORE_ROOT:-$ROOT/store/$RUN_ID}"
BIN="${BIN:-$ROOT/target/release/lsmgraph}"
SAMPLES="${SAMPLES:-5000}"
WARMUP_RUNS="${WARMUP_RUNS:-1}"
REPEATS="${REPEATS:-3}"
EDGE_TYPE="${EDGE_TYPE:-1}"
PROPERTY_ID="${PROPERTY_ID:-5}"
PROPERTY_VALUE_I64="${PROPERTY_VALUE_I64:-42}"
PROPERTY_DEFAULT_I64="${PROPERTY_DEFAULT_I64:-42}"
TWO_HOP_FANOUT="${TWO_HOP_FANOUT:-64}"
IMPORT_RELATION="${IMPORT_RELATION:-snb-full}"
ABORT_TIMEOUT_SECONDS="${ABORT_TIMEOUT_SECONDS:-10800}"

mkdir -p "$LOG_ROOT" "$STORE_ROOT"

echo "[w8] run_id=$RUN_ID"
echo "[w8] input=$INPUT"
echo "[w8] log_root=$LOG_ROOT"
echo "[w8] store_root=$STORE_ROOT"
echo "[w8] ETA: SF30 import is expected to take about 20-40 min per variant; bench is expected to take 10-20 min per variant."
echo "[w8] Abort conditions: /data free <200GiB, MemAvailable <80GiB, or any single variant exceeds ${ABORT_TIMEOUT_SECONDS}s."
echo "[w8] Note: property predicate runs require CSR property-value sections; JSONL-only SNB edge props are not enough for equality/presence hits."

data_free_gib() {
  df -BG /data/WorkSpace | awk 'NR==2 { gsub(/G/, "", $4); print $4 }'
}

mem_available_gib() {
  awk '/MemAvailable:/ { printf "%d\n", $2 / 1024 / 1024 }' /proc/meminfo
}

resource_gate() {
  local free_gib mem_gib
  free_gib="$(data_free_gib)"
  mem_gib="$(mem_available_gib)"
  echo "[w8] resource_gate free_data_gib=$free_gib mem_available_gib=$mem_gib"
  if [ "$free_gib" -lt 200 ]; then
    echo "[w8] abort: /data free space below 200GiB" >&2
    exit 2
  fi
  if [ "$mem_gib" -lt 80 ]; then
    echo "[w8] abort: MemAvailable below 80GiB" >&2
    exit 2
  fi
}

run_cmd() {
  local label="$1"
  shift
  echo "[w8] start label=$label ts=$(date -Is)"
  timeout "$ABORT_TIMEOUT_SECONDS" nice -n 10 "$@"
  echo "[w8] done label=$label ts=$(date -Is)"
}

build_release() {
  resource_gate
  run_cmd build cargo build --release
}

variant_import_flags() {
  case "$1" in
    schema)
      echo "--l0-layout schema"
      ;;
    budg-b64)
      echo "--l0-layout semantic-budgeted --semantic-budget-max-extra-l0-files 64"
      ;;
    semantic)
      echo "--l0-layout semantic"
      ;;
    *)
      echo "[w8] unknown variant $1" >&2
      exit 2
      ;;
  esac
}

run_variant() {
  local variant="$1"
  local store="$STORE_ROOT/$variant"
  local variant_log="$LOG_ROOT/$variant"
  mkdir -p "$variant_log"

  resource_gate
  if [ ! -f "$store/MANIFEST" ]; then
    read -r -a import_flags <<<"$(variant_import_flags "$variant")"
    run_cmd "import-$variant" "$BIN" import \
      --input "$INPUT" \
      --data-dir "$store" \
      --relation "$IMPORT_RELATION" \
      "${import_flags[@]}" \
      >"$variant_log/import.json"
  else
    echo "[w8] reuse existing store variant=$variant store=$store"
  fi

  for mode in required-property presence equality absent-default; do
    resource_gate
    run_cmd "bench-$variant-property-$mode" "$BIN" storage-bench \
      --data-dir "$store" \
      --samples "$SAMPLES" \
      --warmup-runs "$WARMUP_RUNS" \
      --repeats "$REPEATS" \
      --edge-type "$EDGE_TYPE" \
      --workload-mode one-hop \
      --property-predicate-mode "$mode" \
      --property-id "$PROPERTY_ID" \
      --property-value-i64 "$PROPERTY_VALUE_I64" \
      --property-default-i64 "$PROPERTY_DEFAULT_I64" \
      >"$variant_log/property-$mode.json"
  done

  resource_gate
  run_cmd "bench-$variant-2hop" "$BIN" storage-bench \
    --data-dir "$store" \
    --samples "$SAMPLES" \
    --warmup-runs "$WARMUP_RUNS" \
    --repeats "$REPEATS" \
    --edge-type "$EDGE_TYPE" \
    --workload-mode two-hop \
    --two-hop-fanout "$TWO_HOP_FANOUT" \
    >"$variant_log/2hop-typed.json"
}

build_release
for variant in schema budg-b64 semantic; do
  run_variant "$variant"
done

date -Is >"$LOG_ROOT/DONE"
echo "[w8] complete log_root=$LOG_ROOT"
