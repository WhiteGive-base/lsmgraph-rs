#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
RUN_ID="${RUN_ID:-w14-sf30-type-only-streaming-$(date +%Y%m%d-%H%M%S)}"
LOG_ROOT="${LOG_ROOT:-$ROOT/remote-logs/$RUN_ID}"
STORE_ROOT="${STORE_ROOT:-$ROOT/store/$RUN_ID}"
INPUT="${INPUT:-/data/WorkSpace/ldbc-sf30/social_network}"
BIN="${BIN:-$ROOT/target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
CSR_METADATA_CACHE_ENTRIES="${CSR_METADATA_CACHE_ENTRIES:-4096}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-67108864}"
IMPORT_TIMEOUT_SECONDS="${IMPORT_TIMEOUT_SECONDS:-3600}"
BENCH_TIMEOUT_SECONDS="${BENCH_TIMEOUT_SECONDS:-3600}"
COMPARE_TIMEOUT_SECONDS="${COMPARE_TIMEOUT_SECONDS:-600}"
SAMPLES="${SAMPLES:-100}"
EDGE_TYPE="${EDGE_TYPE:-3}"
DIGEST_COMPARE="${DIGEST_COMPARE:-$ROOT/baseline/sf100_digest_compare.py}"

cd "$ROOT"
mkdir -p "$LOG_ROOT" "$STORE_ROOT"

FAILED_MARKER="$LOG_ROOT/FAILED"
DONE_MARKER="$LOG_ROOT/DONE"
PASSED_MARKER="$LOG_ROOT/PASSED"
MANIFEST="$LOG_ROOT/manifest.tsv"
PLAN="$LOG_ROOT/type-only/sample-plan.json"
SCENARIO_DIR="$LOG_ROOT/type-only"
mkdir -p "$SCENARIO_DIR"
printf 'variant\tkind\tstore\tartifact\tbytes\n' > "$MANIFEST"

trap 'status=$?; if [[ "$status" -ne 0 && ! -f "$FAILED_MARKER" && ! -f "$DONE_MARKER" ]]; then printf "%s\tunexpected exit status=%s\n" "$(date -Is)" "$status" > "$FAILED_MARKER"; fi' EXIT

log() {
  echo "[w14-stream] $*" >&2
}

fail() {
  echo "[w14-stream] FAIL: $*" >&2
  printf '%s\t%s\n' "$(date -Is)" "$*" > "$FAILED_MARKER"
  exit 1
}

json_valid() {
  python3 -m json.tool "$1" >/dev/null
}

data_free_gib() {
  df -BG /data/WorkSpace | awk 'NR==2 { gsub(/G/, "", $4); print $4 }'
}

mem_available_gib() {
  awk '/MemAvailable:/ { printf "%d\n", $2 / 1024 / 1024 }' /proc/meminfo
}

resource_gate() {
  local label="$1" free_gib mem_gib
  free_gib="$(data_free_gib)"
  mem_gib="$(mem_available_gib)"
  log "resource label=${label} data_free_gib=${free_gib} mem_available_gib=${mem_gib}"
  if (( free_gib < 200 )); then
    fail "/data free below 200GiB at ${label}"
  fi
  if (( mem_gib < 80 )); then
    fail "MemAvailable below 80GiB at ${label}"
  fi
}

run_timed() {
  local timeout_seconds="$1" label="$2"
  shift 2
  log "start ${label} ts=$(date -Is)"
  timeout "$timeout_seconds" nice -n 10 "$@"
  log "done ${label} ts=$(date -Is)"
}

safe_rm_store() {
  local store="$1"
  case "$store" in
    "$STORE_ROOT"/*) rm -rf -- "$store" ;;
    *) fail "refusing to delete non-run store: $store" ;;
  esac
}

layout_for() {
  case "$1" in
    schema) printf 'schema' ;;
    edge-type-only) printf 'edge-type-only' ;;
    budg-b64) printf 'semantic-budgeted' ;;
    semantic) printf 'semantic' ;;
    *) fail "unknown variant $1" ;;
  esac
}

import_extra_for() {
  case "$1" in
    budg-b64) printf '%s\n' "--semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files 64" ;;
    *) printf '\n' ;;
  esac
}

record_manifest() {
  local variant="$1" kind="$2" store="$3" artifact="$4" bytes=0
  [[ -d "$store" ]] && bytes="$(du -sb "$store" | awk '{print $1}')"
  printf '%s\t%s\t%s\t%s\t%s\n' "$variant" "$kind" "$store" "$artifact" "$bytes" >> "$MANIFEST"
}

import_variant() {
  local variant="$1" store="$STORE_ROOT/$1" layout extra
  local extra_args=()
  layout="$(layout_for "$variant")"
  extra="$(import_extra_for "$variant")"
  if [[ -n "$extra" ]]; then
    read -r -a extra_args <<< "$extra"
  fi
  safe_rm_store "$store"
  resource_gate "before-import-${variant}"
  run_timed "$IMPORT_TIMEOUT_SECONDS" "import-${variant}" \
    env SNB_SKIP_ADJ_CACHE=1 "$BIN" \
      --io-backend "$IO_BACKEND" \
      --csr-metadata-cache-entries "$CSR_METADATA_CACHE_ENTRIES" \
      import \
      --input "$INPUT" \
      --data-dir "$store" \
      --relation snb-full \
      --memgraph-bytes "$MEMGRAPH_BYTES" \
      --l0-layout "$layout" \
      "${extra_args[@]}" \
      > "$LOG_ROOT/${variant}-import.json" \
      2> "$LOG_ROOT/${variant}-import.stderr"
  json_valid "$LOG_ROOT/${variant}-import.json" || fail "invalid import JSON for ${variant}"
  record_manifest "$variant" import "$store" "$LOG_ROOT/${variant}-import.json"
}

bench_variant() {
  local variant="$1" store="$STORE_ROOT/$1" layout out
  layout="$(layout_for "$variant")"
  out="$SCENARIO_DIR/${variant}.json"
  resource_gate "before-bench-${variant}"
  run_timed "$BENCH_TIMEOUT_SECONDS" "bench-${variant}" \
    env LSMGRAPH_LOG_OPEN_PHASES=1 "$BIN" \
      --io-backend "$IO_BACKEND" \
      --csr-metadata-cache-entries "$CSR_METADATA_CACHE_ENTRIES" \
      storage-bench \
      --data-dir "$store" \
      --sample-plan-in "$PLAN" \
      --warmup-runs 0 \
      --repeats 1 \
      --l0-layout "$layout" \
      --edge-type "$EDGE_TYPE" \
      --emit-result-digests \
      > "$out" \
      2> "$SCENARIO_DIR/${variant}-bench.stderr"
  json_valid "$out" || fail "invalid bench JSON for ${variant}"
  record_manifest "$variant" bench "$store" "$out"
}

digest_compare_variant() {
  local variant="$1" out="$SCENARIO_DIR/digest-compare-schema-vs-${variant}.json"
  [[ "$variant" == "schema" ]] && return 0
  resource_gate "before-digest-compare-${variant}"
  run_timed "$COMPARE_TIMEOUT_SECONDS" "digest-compare-${variant}" \
    python3 "$DIGEST_COMPARE" \
      --baseline "$SCENARIO_DIR/schema.json" \
      --variant "$SCENARIO_DIR/${variant}.json" \
      --max-mismatches 5 \
      --out "$out" \
      >/dev/null
  json_valid "$out" || fail "invalid digest compare JSON for ${variant}"
  python3 - "$out" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
if data.get("mismatches") != 0:
    raise SystemExit(f"mismatches={data.get('mismatches')}")
if data.get("checked", 0) <= 0:
    raise SystemExit("checked=0")
PY
  record_manifest "$variant" digest-compare "$STORE_ROOT/$variant" "$out"
}

generate_plan_from_schema() {
  resource_gate before-plan-schema
  run_timed "$BENCH_TIMEOUT_SECONDS" sample-plan-schema \
    env SNB_SKIP_SEM_INDEX=1 LSMGRAPH_LOG_OPEN_PHASES=1 "$BIN" \
      --io-backend "$IO_BACKEND" \
      --csr-metadata-cache-entries "$CSR_METADATA_CACHE_ENTRIES" \
      storage-bench \
      --data-dir "$STORE_ROOT/schema" \
      --samples "$SAMPLES" \
      --warmup-runs 0 \
      --repeats 1 \
      --sample-plan-out "$PLAN" \
      --edge-type "$EDGE_TYPE" \
      > "$SCENARIO_DIR/schema-plan-source.json" \
      2> "$SCENARIO_DIR/schema-plan-source.stderr"
  json_valid "$PLAN" || fail "invalid sample plan"
  json_valid "$SCENARIO_DIR/schema-plan-source.json" || fail "invalid schema plan-source JSON"
}

log "run_id=$RUN_ID"
log "log_root=$LOG_ROOT"
log "store_root=$STORE_ROOT"
log "streaming=1 scale=sf30 scenario=type-only samples=$SAMPLES edge_type=$EDGE_TYPE"
log "abort: /data free <200GiB, MemAvailable <80GiB, timeout, invalid JSON, or digest mismatch"

resource_gate start
run_timed "$BENCH_TIMEOUT_SECONDS" build cargo build --release

import_variant schema
generate_plan_from_schema
bench_variant schema
safe_rm_store "$STORE_ROOT/schema"
resource_gate after-delete-schema

for variant in edge-type-only budg-b64 semantic; do
  import_variant "$variant"
  bench_variant "$variant"
  digest_compare_variant "$variant"
  safe_rm_store "$STORE_ROOT/$variant"
  resource_gate "after-delete-${variant}"
done

date -Is > "$DONE_MARKER"
date -Is > "$PASSED_MARKER"
log "complete log_root=$LOG_ROOT"
