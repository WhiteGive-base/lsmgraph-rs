#!/usr/bin/env bash
set -euo pipefail

# W14: semantic necessity matrix.
# Default SCALE=sf30 is a smoke/formal sanity run that reuses frozen W8 SF30
# stores for schema/budg-b64/semantic and imports only edge-type-only.
# SCALE=sf100 requires W14_ALLOW_SF100=1 and runs variants sequentially.

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
SCALE="${SCALE:-sf30}"
INPUT_SF1="${INPUT_SF1:-/data/WorkSpace/ldbc-sf1/social_network}"
INPUT_SF30="${INPUT_SF30:-/data/WorkSpace/ldbc-sf30/social_network}"
INPUT_SF100="${INPUT_SF100:-/data/WorkSpace/ldbc-sf100/social_network}"
RUN_ID="${RUN_ID:-w14-semantic-necessity-${SCALE}-$(date +%Y%m%d-%H%M%S)}"
LOG_ROOT="${LOG_ROOT:-$ROOT/remote-logs/$RUN_ID}"
STORE_ROOT="${STORE_ROOT:-$ROOT/store/$RUN_ID}"
BIN="${BIN:-$ROOT/target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
CSR_METADATA_CACHE_ENTRIES="${CSR_METADATA_CACHE_ENTRIES:-4096}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-67108864}"
IMPORT_TIMEOUT_SECONDS="${IMPORT_TIMEOUT_SECONDS:-10800}"
BENCH_TIMEOUT_SECONDS="${BENCH_TIMEOUT_SECONDS:-7200}"
COMPARE_TIMEOUT_SECONDS="${COMPARE_TIMEOUT_SECONDS:-7200}"
SAMPLES="${SAMPLES:-}"
WARMUP_RUNS="${WARMUP_RUNS:-}"
REPEATS="${REPEATS:-}"
PROPERTY_ID="${PROPERTY_ID:-5}"
PROPERTY_VALUE_I64="${PROPERTY_VALUE_I64:-42}"
PROPERTY_DEFAULT_I64="${PROPERTY_DEFAULT_I64:-42}"
W8_STORE_ROOT="${W8_STORE_ROOT:-$ROOT/store/w8-property-2hop-20260614-2025}"
SF100_SCHEMA_STORE="${SF100_SCHEMA_STORE:-$ROOT/store/qslsm-sf100-strong-baseline-20260610/schema}"
W14_IMPORT_ONLY="${W14_IMPORT_ONLY:-0}"
W14_SKIP_ADJ_CACHE="${W14_SKIP_ADJ_CACHE:-1}"
W14_SKIP_SEM_INDEX_FOR_PLAN="${W14_SKIP_SEM_INDEX_FOR_PLAN:-1}"
VARIANTS="${VARIANTS:-schema edge-type-only budg-b64 semantic}"
VARIANTS="${VARIANTS//,/ }"
SCENARIOS="${SCENARIOS:-type-only type-src-label dst-label-only property-required property-equality degree-class}"
SCENARIOS="${SCENARIOS//,/ }"
# W14 Step B correctness path. Default: compare schema vs variant from the storage-bench
# result digests (no second engine open). neighbor-compare is kept only as a debug fallback,
# triggered automatically on a digest mismatch or forced via W14_NEIGHBOR_COMPARE=1.
W14_EMIT_DIGESTS="${W14_EMIT_DIGESTS:-1}"
W14_NEIGHBOR_COMPARE="${W14_NEIGHBOR_COMPARE:-0}"
W14_LOG_OPEN_PHASES="${W14_LOG_OPEN_PHASES:-1}"
W14_PLAN_CACHE_ROOT="${W14_PLAN_CACHE_ROOT:-}"
DIGEST_COMPARE="${DIGEST_COMPARE:-$ROOT/baseline/sf100_digest_compare.py}"

if [[ "$SCALE" == "sf100" ]]; then
  [[ "${W14_ALLOW_SF100:-0}" == "1" ]] || {
    echo "[w14] refuse SF100 without W14_ALLOW_SF100=1" >&2
    exit 2
  }
  INPUT="${INPUT:-$INPUT_SF100}"
  SAMPLES="${SAMPLES:-5000}"
  WARMUP_RUNS="${WARMUP_RUNS:-1}"
  REPEATS="${REPEATS:-3}"
elif [[ "$SCALE" == "sf1" ]]; then
  # SF1 validation gate: small, fast, fresh-imported variant stores under STORE_ROOT.
  INPUT="${INPUT:-$INPUT_SF1}"
  SAMPLES="${SAMPLES:-200}"
  WARMUP_RUNS="${WARMUP_RUNS:-0}"
  REPEATS="${REPEATS:-1}"
else
  INPUT="${INPUT:-$INPUT_SF30}"
  SAMPLES="${SAMPLES:-1000}"
  WARMUP_RUNS="${WARMUP_RUNS:-0}"
  REPEATS="${REPEATS:-1}"
fi

mkdir -p "$LOG_ROOT" "$STORE_ROOT"
cd "$ROOT"

FAILED_MARKER="$LOG_ROOT/FAILED"
DONE_MARKER="$LOG_ROOT/DONE"
MANIFEST="$LOG_ROOT/manifest.tsv"
printf 'variant\tkind\tstore\tartifact\tbytes\n' > "$MANIFEST"

trap 'status=$?; if [[ "$status" -ne 0 && ! -f "$FAILED_MARKER" && ! -f "$DONE_MARKER" ]]; then printf "%s\tunexpected exit status=%s\n" "$(date -Is)" "$status" > "$FAILED_MARKER"; fi' EXIT

log() {
  echo "[w14] $*" >&2
}

is_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

fail() {
  echo "[w14] FAIL: $*" >&2
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

variant_store() {
  local variant="$1"
  case "$SCALE" in
    sf30)
      case "$variant" in
        schema) printf '%s/schema\n' "$W8_STORE_ROOT" ;;
        budg-b64) printf '%s/budg-b64\n' "$W8_STORE_ROOT" ;;
        semantic) printf '%s/semantic\n' "$W8_STORE_ROOT" ;;
        edge-type-only) printf '%s/edge-type-only\n' "$STORE_ROOT" ;;
        *) fail "unknown variant ${variant}" ;;
      esac
      ;;
    sf100)
      case "$variant" in
        schema)
          if [[ -n "$SF100_SCHEMA_STORE" ]]; then
            printf '%s\n' "$SF100_SCHEMA_STORE"
          else
            printf '%s/schema\n' "$STORE_ROOT"
          fi
          ;;
        *) printf '%s/%s\n' "$STORE_ROOT" "$variant" ;;
      esac
      ;;
    *)
      # sf1 (and any generic scale): every variant is imported fresh under STORE_ROOT.
      printf '%s/%s\n' "$STORE_ROOT" "$variant"
      ;;
  esac
}

variant_layout() {
  case "$1" in
    schema) printf 'schema' ;;
    edge-type-only) printf 'edge-type-only' ;;
    budg-b64) printf 'semantic-budgeted' ;;
    semantic) printf 'semantic' ;;
    *) fail "unknown variant $1" ;;
  esac
}

variant_import_extra() {
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

build_release() {
  resource_gate build
  run_timed "$BENCH_TIMEOUT_SECONDS" build cargo build --release
}

ensure_store() {
  local variant="$1" store layout extra
  local import_env=()
  store="$(variant_store "$variant")"
  layout="$(variant_layout "$variant")"
  if [[ -f "$store/MANIFEST" ]]; then
    log "reuse store variant=${variant} store=${store}"
    record_manifest "$variant" reuse "$store" EXISTS
    return 0
  fi
  resource_gate "before-import-${variant}"
  mkdir -p "$(dirname "$store")"
  extra="$(variant_import_extra "$variant")"
  read -r -a extra_args <<< "$extra"
  if [[ "$W14_SKIP_ADJ_CACHE" == "1" || "$W14_SKIP_ADJ_CACHE" == "true" ]]; then
    import_env+=(SNB_SKIP_ADJ_CACHE=1)
  fi
  log "import env variant=${variant} W14_SKIP_ADJ_CACHE=${W14_SKIP_ADJ_CACHE} import_env=${import_env[*]:-none}"
  run_timed "$IMPORT_TIMEOUT_SECONDS" "import-${variant}" \
    env "${import_env[@]}" "$BIN" \
      --io-backend "$IO_BACKEND" \
      --csr-metadata-cache-entries "$CSR_METADATA_CACHE_ENTRIES" \
      import \
      --input "$INPUT" \
      --data-dir "$store" \
      --relation snb-full \
      --memgraph-bytes "$MEMGRAPH_BYTES" \
      --l0-layout "$layout" \
      "${extra_args[@]}" \
      > "$LOG_ROOT/${variant}-import.json"
  json_valid "$LOG_ROOT/${variant}-import.json" || fail "invalid import JSON for ${variant}"
  record_manifest "$variant" import "$store" "$LOG_ROOT/${variant}-import.json"
}

scenario_args() {
  local edge_type="$1" src_label="$2" dst_label="$3" property_mode="$4" degree_hint="$5" force_signature="$6"
  SCENARIO_ARGS=()
  [[ -n "$edge_type" ]] && SCENARIO_ARGS+=(--edge-type "$edge_type")
  [[ -n "$src_label" ]] && SCENARIO_ARGS+=(--src-label "$src_label")
  [[ -n "$dst_label" ]] && SCENARIO_ARGS+=(--dst-label "$dst_label")
  if [[ "$property_mode" != "none" ]]; then
    SCENARIO_ARGS+=(--property-predicate-mode "$property_mode")
    SCENARIO_ARGS+=(--property-id "$PROPERTY_ID")
    SCENARIO_ARGS+=(--property-value-i64 "$PROPERTY_VALUE_I64")
    SCENARIO_ARGS+=(--property-default-i64 "$PROPERTY_DEFAULT_I64")
  fi
  if [[ "$degree_hint" == "1" ]]; then
    SCENARIO_ARGS+=(--semantic-degree-hint --sample-plan-degree-hint)
  fi
  if [[ "$force_signature" == "1" ]]; then
    SCENARIO_ARGS+=(--force-signature)
  fi
}

compare_args() {
  local property_mode="$1" degree_hint="$2"
  COMPARE_ARGS=()
  [[ "$degree_hint" == "1" ]] && COMPARE_ARGS+=(--right-semantic-degree-hint)
  if [[ "$property_mode" != "none" ]]; then
    COMPARE_ARGS+=(--property-predicate-mode "$property_mode")
    COMPARE_ARGS+=(--property-id "$PROPERTY_ID")
    COMPARE_ARGS+=(--property-value-i64 "$PROPERTY_VALUE_I64")
    COMPARE_ARGS+=(--property-default-i64 "$PROPERTY_DEFAULT_I64")
  fi
}

run_bench() {
  local scenario_dir="$1" label="$2" variant="$3" plan="$4"
  local store out layout
  local bench_env=() bench_extra=()
  store="$(variant_store "$variant")"
  layout="$(variant_layout "$variant")"
  out="$scenario_dir/${variant}.json"
  if is_true "$W14_EMIT_DIGESTS"; then
    bench_extra+=(--emit-result-digests)
  fi
  if is_true "$W14_LOG_OPEN_PHASES"; then
    bench_env+=(LSMGRAPH_LOG_OPEN_PHASES=1)
  fi
  resource_gate "before-bench-${label}-${variant}"
  run_timed "$BENCH_TIMEOUT_SECONDS" "bench-${label}-${variant}" \
    env "${bench_env[@]}" "$BIN" \
      --io-backend "$IO_BACKEND" \
      --csr-metadata-cache-entries "$CSR_METADATA_CACHE_ENTRIES" \
      storage-bench \
      --data-dir "$store" \
      --sample-plan-in "$plan" \
      --warmup-runs "$WARMUP_RUNS" \
      --repeats "$REPEATS" \
      --l0-layout "$layout" \
      "${SCENARIO_ARGS[@]}" \
      "${bench_extra[@]}" \
      2> "$scenario_dir/${variant}-bench.stderr" \
      > "$out"
  json_valid "$out" || fail "invalid bench JSON for ${label}/${variant}"
  record_manifest "$variant" "bench-${label}" "$store" "$out"
}

run_compare() {
  local scenario_dir="$1" label="$2" variant="$3" plan="$4" property_mode="$5" degree_hint="$6"
  local left right out
  [[ "$variant" == "schema" ]] && return 0
  left="$(variant_store schema)"
  right="$(variant_store "$variant")"
  out="$scenario_dir/compare-schema-vs-${variant}.json"
  compare_args "$property_mode" "$degree_hint"
  resource_gate "before-compare-${label}-${variant}"
  run_timed "$COMPARE_TIMEOUT_SECONDS" "compare-${label}-${variant}" \
    "$BIN" \
      --io-backend "$IO_BACKEND" \
      --csr-metadata-cache-entries "$CSR_METADATA_CACHE_ENTRIES" \
      neighbor-compare \
      --left-data-dir "$left" \
      --right-data-dir "$right" \
      --sample-plan "$plan" \
      "${COMPARE_ARGS[@]}" \
      --max-mismatches 1 \
      > "$out"
  json_valid "$out" || fail "invalid compare JSON for ${label}/${variant}"
  python3 - "$out" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
if data.get("mismatches") != 0:
    raise SystemExit(f"mismatches={data.get('mismatches')}")
if data.get("checked", 0) <= 0:
    raise SystemExit("checked=0")
PY
  record_manifest "$variant" "compare-${label}" "$right" "$out"
}

run_digest_compare() {
  local scenario_dir="$1" label="$2" variant="$3" plan="$4" property_mode="$5" degree_hint="$6"
  local schema_json variant_json out
  [[ "$variant" == "schema" ]] && return 0
  schema_json="$scenario_dir/schema.json"
  variant_json="$scenario_dir/${variant}.json"
  out="$scenario_dir/digest-compare-schema-vs-${variant}.json"
  [[ -f "$schema_json" ]] || fail "missing schema bench JSON for ${label} (need it for digest compare)"
  [[ -f "$variant_json" ]] || fail "missing ${variant} bench JSON for ${label}"
  log "digest-compare label=${label} variant=${variant}"
  if python3 "$DIGEST_COMPARE" \
      --baseline "$schema_json" \
      --variant "$variant_json" \
      --max-mismatches 5 \
      --out "$out" >/dev/null; then
    record_manifest "$variant" "digest-compare-${label}" "$(variant_store "$variant")" "$out"
    return 0
  fi
  log "digest mismatch label=${label} variant=${variant}; running neighbor-compare debug preview"
  run_compare "$scenario_dir" "$label" "$variant" "$plan" "$property_mode" "$degree_hint" || true
  fail "digest compare mismatch for ${label}/${variant} (see ${out})"
}

run_scenario() {
  local label="$1" edge_type="$2" src_label="$3" dst_label="$4" property_mode="$5" degree_hint="$6" force_signature="$7"
  local scenario_dir="$LOG_ROOT/$label" plan="$LOG_ROOT/$label/sample-plan.json"
  local cached_plan=""
  local plan_env=()
  mkdir -p "$scenario_dir"
  scenario_args "$edge_type" "$src_label" "$dst_label" "$property_mode" "$degree_hint" "$force_signature"
  if [[ -n "$W14_PLAN_CACHE_ROOT" ]]; then
    cached_plan="$W14_PLAN_CACHE_ROOT/$label/sample-plan.json"
  fi
  if [[ -n "$cached_plan" && -s "$cached_plan" ]]; then
    log "reuse sample-plan label=${label} cached_plan=${cached_plan}"
    cp "$cached_plan" "$plan"
    json_valid "$plan" || fail "invalid cached sample plan for ${label}: ${cached_plan}"
  else
    if is_true "$W14_SKIP_SEM_INDEX_FOR_PLAN"; then
      plan_env+=(SNB_SKIP_SEM_INDEX=1)
    fi
    log "sample-plan env label=${label} W14_SKIP_SEM_INDEX_FOR_PLAN=${W14_SKIP_SEM_INDEX_FOR_PLAN} plan_env=${plan_env[*]:-none}"
    resource_gate "before-plan-${label}"
    run_timed "$BENCH_TIMEOUT_SECONDS" "sample-plan-${label}" \
      env "${plan_env[@]}" "$BIN" \
        --io-backend "$IO_BACKEND" \
        --csr-metadata-cache-entries "$CSR_METADATA_CACHE_ENTRIES" \
        storage-bench \
        --data-dir "$(variant_store schema)" \
        --samples "$SAMPLES" \
        --warmup-runs 0 \
        --repeats 1 \
        --sample-plan-out "$plan" \
        "${SCENARIO_ARGS[@]}" \
        > "$scenario_dir/schema-plan-source.json"
    json_valid "$plan" || fail "invalid sample plan for ${label}"
    json_valid "$scenario_dir/schema-plan-source.json" || fail "invalid schema plan-source JSON for ${label}"
  fi
  for variant in $VARIANTS; do
    run_bench "$scenario_dir" "$label" "$variant" "$plan"
  done
  if is_true "$W14_EMIT_DIGESTS"; then
    for variant in $VARIANTS; do
      run_digest_compare "$scenario_dir" "$label" "$variant" "$plan" "$property_mode" "$degree_hint"
    done
  fi
  # neighbor-compare is the standalone fallback. It re-opens two engines per variant, so it only
  # runs when digests are disabled (no digest path) or explicitly forced for cross-validation.
  if is_true "$W14_NEIGHBOR_COMPARE" || ! is_true "$W14_EMIT_DIGESTS"; then
    for variant in $VARIANTS; do
      run_compare "$scenario_dir" "$label" "$variant" "$plan" "$property_mode" "$degree_hint"
    done
  fi
}

run_named_scenario() {
  case "$1" in
    type-only) run_scenario type-only 3 "" "" none 0 0 ;;
    type-src-label) run_scenario type-src-label 3 3 "" none 0 1 ;;
    dst-label-only) run_scenario dst-label-only "" "" 7 none 0 1 ;;
    property-required) run_scenario property-required 1 "" "" required-property 0 1 ;;
    property-equality) run_scenario property-equality 1 "" "" equality 0 0 ;;
    degree-class) run_scenario degree-class 3 "" "" none 1 1 ;;
    *) fail "unknown scenario $1" ;;
  esac
}

log "run_id=$RUN_ID"
log "scale=$SCALE input=$INPUT"
log "log_root=$LOG_ROOT"
log "store_root=$STORE_ROOT"
log "CSR_METADATA_CACHE_ENTRIES=${CSR_METADATA_CACHE_ENTRIES}"
log "W14_IMPORT_ONLY=${W14_IMPORT_ONLY}"
log "W14_SKIP_ADJ_CACHE=${W14_SKIP_ADJ_CACHE} (sets SNB_SKIP_ADJ_CACHE=1 for imports when enabled)"
log "W14_SKIP_SEM_INDEX_FOR_PLAN=${W14_SKIP_SEM_INDEX_FOR_PLAN} (sets SNB_SKIP_SEM_INDEX=1 for sample-plan only when enabled)"
log "W14_EMIT_DIGESTS=${W14_EMIT_DIGESTS} (Step B compares schema vs variant from bench result digests; no second engine open)"
log "W14_NEIGHBOR_COMPARE=${W14_NEIGHBOR_COMPARE} (standalone neighbor-compare; default off, auto-runs as debug preview on digest mismatch)"
log "W14_LOG_OPEN_PHASES=${W14_LOG_OPEN_PHASES} (sets LSMGRAPH_LOG_OPEN_PHASES=1 so each bench logs Engine::open phase timings)"
log "W14_PLAN_CACHE_ROOT=${W14_PLAN_CACHE_ROOT:-none} (when set, reuses <cache>/<scenario>/sample-plan.json if present)"
log "ETA: setup/build 10-30 min; SF30 edge-type-only import 20-60 min if needed; SF100 formal 15-25h."
log "abort: MemAvailable <80GiB, /data free <200GiB, single import >${IMPORT_TIMEOUT_SECONDS}s, compare mismatch, or missing telemetry."

resource_gate start
build_release
for variant in $VARIANTS; do
  ensure_store "$variant"
done

if is_true "$W14_IMPORT_ONLY"; then
  date -Is > "$LOG_ROOT/IMPORT_ONLY_DONE"
  date -Is > "$DONE_MARKER"
  log "complete import-only log_root=$LOG_ROOT"
  exit 0
fi

for scenario in $SCENARIOS; do
  run_named_scenario "$scenario"
done

date -Is > "$DONE_MARKER"
log "complete log_root=$LOG_ROOT"
