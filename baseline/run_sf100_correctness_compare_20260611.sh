#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
RUN_ID="${RUN_ID:-qslsm-sf100-correctness-compare-20260611-$(date +%H%M%S)}"
OUT_DIR="${OUT_DIR:-${ROOT}/remote-logs/${RUN_ID}}"
TEMP_STORE_ROOT="${TEMP_STORE_ROOT:-${ROOT}/store/${RUN_ID}}"
SCHEMA_STORE="${SCHEMA_STORE:-${ROOT}/store/qslsm-sf100-strong-baseline-20260610/schema}"
PLAN="${PLAN:-${ROOT}/remote-logs/qslsm-sf100-strong-baseline-20260610/sample-plan-core-s5000.json}"
INPUT="${INPUT:-/data/WorkSpace/ldbc-sf100/social_network}"
BIN="${BIN:-${ROOT}/target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-67108864}"
MIN_AVAILABLE_GIB="${MIN_AVAILABLE_GIB:-220}"
MIN_FREE_GIB="${MIN_FREE_GIB:-180}"

cd "$ROOT"
mkdir -p "$OUT_DIR" "$TEMP_STORE_ROOT"

LOG="${OUT_DIR}/progress.log"
MANIFEST="${OUT_DIR}/manifest.tsv"

log() {
  printf '[%(%Y-%m-%d %H:%M:%S)T] %s\n' -1 "$*" | tee -a "$LOG"
}

available_gib() {
  awk '/MemAvailable/ { printf "%d\n", $2 / 1024 / 1024 }' /proc/meminfo
}

free_gib() {
  df -BG "$ROOT" | awk 'NR == 2 { gsub(/G/, "", $4); print int($4) }'
}

ensure_resources() {
  local avail free
  avail="$(available_gib)"
  free="$(free_gib)"
  log "resource check: mem_available=${avail}GiB disk_free=${free}GiB"
  if (( avail < MIN_AVAILABLE_GIB )); then
    log "FATAL: MemAvailable ${avail}GiB < MIN_AVAILABLE_GIB ${MIN_AVAILABLE_GIB}GiB"
    exit 2
  fi
  if (( free < MIN_FREE_GIB )); then
    log "FATAL: disk free ${free}GiB < MIN_FREE_GIB ${MIN_FREE_GIB}GiB"
    exit 3
  fi
}

safe_rm_store() {
  local store="$1"
  case "$store" in
    "${TEMP_STORE_ROOT}/"*) rm -rf -- "$store" ;;
    *) log "FATAL: refusing to delete unexpected path: $store"; exit 4 ;;
  esac
}

variant_args() {
  case "$1" in
    edge-type-only) printf '%s\n' "edge-type-only|" ;;
    semantic) printf '%s\n' "semantic|--right-semantic-degree-hint" ;;
    budg-b64) printf '%s\n' "semantic-budgeted|--right-semantic-degree-hint --semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files 64" ;;
    budg-b256) printf '%s\n' "semantic-budgeted|--right-semantic-degree-hint --semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files 256" ;;
    *) log "FATAL: unknown variant $1"; exit 5 ;;
  esac
}

run_variant() {
  local name="$1"
  local spec layout rest hint import_flags store
  spec="$(variant_args "$name")"
  layout="${spec%%|*}"
  rest="${spec#*|}"
  hint=""
  import_flags=()
  if [[ -n "$rest" ]]; then
    read -r -a parts <<< "$rest"
    for part in "${parts[@]}"; do
      if [[ "$part" == "--right-semantic-degree-hint" ]]; then
        hint="$part"
      else
        import_flags+=("$part")
      fi
    done
  fi
  store="${TEMP_STORE_ROOT}/${name}"

  log "start ${name} layout=${layout} store=${store}"
  ensure_resources
  safe_rm_store "$store"

  /usr/bin/time -v env SNB_SKIP_ADJ_CACHE=1 "$BIN" --io-backend "$IO_BACKEND" import \
    --input "$INPUT" --data-dir "$store" --relation snb-full \
    --memgraph-bytes "$MEMGRAPH_BYTES" --l0-layout "$layout" "${import_flags[@]}" \
    > "${OUT_DIR}/${name}-import.stdout" 2> "${OUT_DIR}/${name}-import.stderr"

  printf '%s\t%s\t%s\t%s\n' "$name" "$layout" "$store" "$(du -sb "$store" | awk '{print $1}')" >> "$MANIFEST"

  /usr/bin/time -v "$BIN" --io-backend "$IO_BACKEND" stats \
    --data-dir "$store" > "${OUT_DIR}/${name}-stats.json" 2> "${OUT_DIR}/${name}-stats.err"

  ensure_resources
  if [[ -n "$hint" ]]; then
    /usr/bin/time -v env SNB_SKIP_SEM_INDEX=1 "$BIN" --io-backend "$IO_BACKEND" neighbor-compare \
      --left-data-dir "$SCHEMA_STORE" --right-data-dir "$store" \
      --sample-plan "$PLAN" --right-semantic-degree-hint --max-mismatches 1 \
      > "${OUT_DIR}/compare-schema-vs-${name}.json" 2> "${OUT_DIR}/compare-schema-vs-${name}.err"
  else
    /usr/bin/time -v env SNB_SKIP_SEM_INDEX=1 "$BIN" --io-backend "$IO_BACKEND" neighbor-compare \
      --left-data-dir "$SCHEMA_STORE" --right-data-dir "$store" \
      --sample-plan "$PLAN" --max-mismatches 1 \
      > "${OUT_DIR}/compare-schema-vs-${name}.json" 2> "${OUT_DIR}/compare-schema-vs-${name}.err"
  fi

  log "compare ${name} done"
  safe_rm_store "$store"
  log "delete ${store}"
}

main() {
  : > "$LOG"
  printf 'variant\tlayout\tstore\tstore_size_bytes\n' > "$MANIFEST"
  test -x "$BIN"
  test -d "$SCHEMA_STORE"
  test -s "$PLAN"
  log "run_id=${RUN_ID}"
  log "schema_store=${SCHEMA_STORE}"
  log "sample_plan=${PLAN}"
  for variant in edge-type-only budg-b64 budg-b256 semantic; do
    run_variant "$variant"
  done
  log "DONE ${RUN_ID}"
  date -Is > "${OUT_DIR}/DONE"
}

main "$@"
