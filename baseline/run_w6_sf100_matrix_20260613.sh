#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
SCALE="${SCALE:-sf100}"
DRY_RUN="${DRY_RUN:-0}"
RUN_ID="${RUN_ID:-w6-${SCALE}-matrix-$(date +%Y%m%d-%H%M%S)}"
OUT_DIR="${OUT_DIR:-${ROOT}/remote-logs/${RUN_ID}}"
STORE_ROOT="${STORE_ROOT:-${ROOT}/store/${RUN_ID}}"
INPUT="${INPUT:-/data/WorkSpace/ldbc-${SCALE}/social_network}"
BIN="${BIN:-${ROOT}/target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-67108864}"
EDGE_TYPES="${EDGE_TYPES:-1,2,3,7,8,9,10,11,12}"
SAMPLES="${SAMPLES:-$([[ "$DRY_RUN" == "1" ]] && echo 20 || echo 5000)}"
REPEATS="${REPEATS:-3}"
IMPORT_TIMEOUT="${IMPORT_TIMEOUT:-$([[ "$SCALE" == "sf100" ]] && echo 120m || echo 30m)}"
RUN_COMPARE="${RUN_COMPARE:-$([[ "$DRY_RUN" == "1" ]] && echo 1 || echo 0)}"
KEEP_STORES="${KEEP_STORES:-0}"
MIN_FREE_GIB="${MIN_FREE_GIB:-200}"
MIN_MEM_GIB="${MIN_MEM_GIB:-80}"

PLAN="${OUT_DIR}/sample-plan-core-s${SAMPLES}.json"
PROGRESS="${OUT_DIR}/progress.log"
MANIFEST="${OUT_DIR}/manifest.tsv"

cd "$ROOT"

if [[ "$SCALE" == "sf100" && "${W6_ALLOW_SF100:-0}" != "1" ]]; then
  echo "Refusing to start SF100 without W6_ALLOW_SF100=1. For dry-run use SCALE=sf1 DRY_RUN=1." >&2
  exit 64
fi

mkdir -p "$OUT_DIR" "$STORE_ROOT"

log() {
  printf '[%(%Y-%m-%d %H:%M:%S)T] %s\n' -1 "$*" | tee -a "$PROGRESS"
}

die() {
  log "FATAL: $*"
  exit 1
}

available_gib() {
  awk '/MemAvailable/ { printf "%d\n", $2 / 1024 / 1024 }' /proc/meminfo
}

free_gib() {
  df -BG /data | awk 'NR == 2 { gsub(/G/, "", $4); print int($4) }'
}

ensure_resources() {
  local phase="$1" mem free
  mem="$(available_gib)"
  free="$(free_gib)"
  log "resource phase=${phase} mem_available=${mem}GiB disk_free=${free}GiB"
  (( mem >= MIN_MEM_GIB )) || die "MemAvailable ${mem}GiB < ${MIN_MEM_GIB}GiB"
  (( free >= MIN_FREE_GIB )) || die "disk free ${free}GiB < ${MIN_FREE_GIB}GiB"
}

json_valid() {
  [[ -s "$1" ]] && python3 -m json.tool "$1" >/dev/null 2>&1
}

safe_delete_store() {
  local store="$1"
  [[ "$KEEP_STORES" == "0" ]] || return 0
  case "$store" in
    "${STORE_ROOT}/"*) rm -rf -- "$store" ;;
    *) die "refuse to delete non-W6 store path: ${store}" ;;
  esac
}

variant_spec() {
  case "$1" in
    schema) printf 'schema|schema|\n' ;;
    naive) printf 'naive|naive|\n' ;;
    kv-lsm) printf 'kv-lsm|kv-lsm|\n' ;;
    edge-type-only) printf 'edge-type-only|edge-type-only|\n' ;;
    semantic) printf 'semantic|semantic|\n' ;;
    budg-b64) printf 'semantic-budgeted|semantic-budgeted|--semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files 64\n' ;;
    budg-b256) printf 'semantic-budgeted|semantic-budgeted|--semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files 256\n' ;;
    budg-b1024) printf 'semantic-budgeted|semantic-budgeted|--semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files 1024\n' ;;
    oracle) printf 'oracle|oracle|\n' ;;
    *) die "unknown variant: $1" ;;
  esac
}

record_manifest() {
  local variant="$1" layout="$2" kind="$3" store="$4" artifact="$5"
  local bytes=0
  [[ -d "$store" ]] && bytes="$(du -sb "$store" | awk '{print $1}')"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$variant" "$layout" "$kind" "$store" "$artifact" "$bytes" >> "$MANIFEST"
}

import_variant() {
  local variant="$1" store="$2" layout="$3" import_flags="$4"
  local stdout="${OUT_DIR}/${variant}-import.stdout"
  local stderr="${OUT_DIR}/${variant}-import.stderr"

  if [[ -d "$store" && -n "$(find "$store" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    log "import skip variant=${variant} store=${store}"
    record_manifest "$variant" "$layout" import "$store" EXISTS
    return 0
  fi

  ensure_resources "before-import-${variant}"
  log "import start variant=${variant} layout=${layout} timeout=${IMPORT_TIMEOUT}"
  read -r -a flags <<< "$import_flags"
  timeout "$IMPORT_TIMEOUT" /usr/bin/time -v env SNB_SKIP_ADJ_CACHE=1 \
    "$BIN" --io-backend "$IO_BACKEND" import \
      --input "$INPUT" \
      --data-dir "$store" \
      --relation snb-full \
      --memgraph-bytes "$MEMGRAPH_BYTES" \
      --l0-layout "$layout" \
      "${flags[@]}" \
      > "$stdout" 2> "$stderr"
  grep -q '"snapshot"' "$stdout" || die "import ${variant} did not emit snapshot JSON"
  log "import done variant=${variant}"
  record_manifest "$variant" "$layout" import "$store" "$stderr"
}

bench_variant() {
  local variant="$1" store="$2" bench_layout="$3"
  local out="${OUT_DIR}/${variant}-bench.json"
  local err="${OUT_DIR}/${variant}-bench.err"

  ensure_resources "before-bench-${variant}"
  log "bench start variant=${variant} repeats=${REPEATS} bench_layout=${bench_layout}"
  "$BIN" --io-backend "$IO_BACKEND" storage-bench \
    --data-dir "$store" \
    --edge-types "$EDGE_TYPES" \
    --semantic-degree-hint \
    --sample-plan-in "$PLAN" \
    --repeats "$REPEATS" \
    --l0-layout "$bench_layout" \
    > "$out" 2> "$err"
  json_valid "$out" || die "invalid bench JSON for ${variant}"
  log "bench done variant=${variant}"
  record_manifest "$variant" "$bench_layout" bench "$store" "$out"
}

compare_variant() {
  local variant="$1" store="$2" bench_layout="$3"
  [[ "$RUN_COMPARE" == "1" && "$variant" != "schema" ]] || return 0
  local out="${OUT_DIR}/compare-schema-vs-${variant}.json"
  local err="${OUT_DIR}/compare-schema-vs-${variant}.err"
  local extra=()
  case "$bench_layout" in
    semantic|semantic-budgeted|oracle) extra=(--right-semantic-degree-hint) ;;
  esac
  ensure_resources "before-compare-${variant}"
  log "compare start schema vs ${variant}"
  "$BIN" --io-backend "$IO_BACKEND" neighbor-compare \
    --left-data-dir "${STORE_ROOT}/schema" \
    --right-data-dir "$store" \
    --sample-plan "$PLAN" \
    "${extra[@]}" \
    --max-mismatches 1 \
    > "$out" 2> "$err"
  json_valid "$out" || die "invalid compare JSON for ${variant}"
  python3 - "$out" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
if data.get("mismatches") != 0:
    raise SystemExit(f"mismatches={data.get('mismatches')}")
if data.get("checked", 0) <= 0:
    raise SystemExit("checked=0")
PY
  log "compare done schema vs ${variant}"
  record_manifest "$variant" "$bench_layout" compare "$store" "$out"
}

run_sentinel() {
  local label="$1"
  local out="${OUT_DIR}/sentinel-${label}.json"
  local err="${OUT_DIR}/sentinel-${label}.err"
  log "sentinel start ${label}"
  "$BIN" --io-backend "$IO_BACKEND" storage-bench \
    --data-dir "${STORE_ROOT}/schema" \
    --edge-types "$EDGE_TYPES" \
    --semantic-degree-hint \
    --sample-plan-in "$PLAN" \
    --repeats "$REPEATS" \
    --l0-layout schema \
    > "$out" 2> "$err"
  json_valid "$out" || die "invalid sentinel JSON ${label}"
  log "sentinel done ${label}"
  record_manifest "sentinel-${label}" schema bench "${STORE_ROOT}/schema" "$out"
}

generate_plan() {
  if json_valid "$PLAN"; then
    log "sample plan exists: $PLAN"
    return 0
  fi
  log "sample-plan start samples=${SAMPLES}"
  SNB_SKIP_SEM_INDEX=1 "$BIN" --io-backend "$IO_BACKEND" storage-bench \
    --data-dir "${STORE_ROOT}/schema" \
    --edge-types "$EDGE_TYPES" \
    --samples "$SAMPLES" \
    --semantic-degree-hint \
    --sample-plan-degree-hint \
    --sample-plan-out "$PLAN" \
    > "${OUT_DIR}/plan.stdout" 2> "${OUT_DIR}/plan.stderr"
  json_valid "$PLAN" || die "invalid sample plan"
  log "sample-plan done"
}

run_variant() {
  local variant="$1"
  local spec import_layout bench_layout import_flags store
  spec="$(variant_spec "$variant")"
  import_layout="${spec%%|*}"
  spec="${spec#*|}"
  bench_layout="${spec%%|*}"
  import_flags="${spec#*|}"
  store="${STORE_ROOT}/${variant}"
  import_variant "$variant" "$store" "$import_layout" "$import_flags"
  bench_variant "$variant" "$store" "$bench_layout"
  compare_variant "$variant" "$store" "$bench_layout"
  if [[ "$variant" != "schema" ]]; then
    safe_delete_store "$store"
    log "store deleted variant=${variant} store=${store}"
  fi
}

main() {
  : > "$PROGRESS"
  printf 'variant\tlayout\tkind\tstore\tartifact\tstore_size_bytes\n' > "$MANIFEST"
  log "run_id=${RUN_ID} scale=${SCALE} dry_run=${DRY_RUN} input=${INPUT}"
  log "samples=${SAMPLES} repeats=${REPEATS} edge_types=${EDGE_TYPES} compare=${RUN_COMPARE}"
  test -x "$BIN" || die "binary not executable: $BIN"
  test -d "$INPUT/dynamic" || die "missing input dynamic dir: $INPUT"
  ensure_resources start

  import_variant schema "${STORE_ROOT}/schema" schema ""
  generate_plan
  bench_variant schema "${STORE_ROOT}/schema" schema
  run_sentinel A1

  run_variant naive
  run_variant kv-lsm
  run_variant edge-type-only
  run_sentinel B
  run_variant semantic
  run_variant budg-b64
  run_variant budg-b256
  run_variant budg-b1024
  run_variant oracle
  run_sentinel A2

  date -Is > "${OUT_DIR}/DONE"
  log "DONE run_id=${RUN_ID} out=${OUT_DIR}"
}

main "$@"
