#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
RUN_ID="${RUN_ID:-qslsm-sf100-correctness-parallel-20260611-$(date +%H%M%S)}"
OUT_DIR="${OUT_DIR:-${ROOT}/remote-logs/${RUN_ID}}"
TEMP_STORE_ROOT="${TEMP_STORE_ROOT:-${ROOT}/store/${RUN_ID}}"
SCHEMA_STORE="${SCHEMA_STORE:-${ROOT}/store/qslsm-sf100-strong-baseline-20260610/schema}"
PLAN="${PLAN:-${ROOT}/remote-logs/qslsm-sf100-strong-baseline-20260610/sample-plan-core-s5000.json}"
INPUT="${INPUT:-/data/WorkSpace/ldbc-sf100/social_network}"
BIN="${BIN:-${ROOT}/target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-67108864}"
VARIANTS="${VARIANTS:-edge-type-only budg-b64 budg-b256 semantic}"
MAX_JOBS="${MAX_JOBS:-2}"

# With schema kept separately, two full temporary SF100 stores leave roughly
# 120-140 GiB free on the current machine. Keep a floor below that but high
# enough to catch accidental third-store growth.
MIN_AVAILABLE_GIB="${MIN_AVAILABLE_GIB:-220}"
MIN_FREE_GIB="${MIN_FREE_GIB:-100}"

cd "$ROOT"
mkdir -p "$OUT_DIR" "$TEMP_STORE_ROOT"

LOG="${OUT_DIR}/progress.log"
MANIFEST="${OUT_DIR}/manifest.tsv"
COMPARE_LOCK="${OUT_DIR}/compare.lock"

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
  local phase="$1" avail free
  avail="$(available_gib)"
  free="$(free_gib)"
  log "resource check (${phase}): mem_available=${avail}GiB disk_free=${free}GiB"
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

layout_for() {
  case "$1" in
    edge-type-only) printf 'edge-type-only\n' ;;
    semantic) printf 'semantic\n' ;;
    budg-b64|budg-b256) printf 'semantic-budgeted\n' ;;
    *) log "FATAL: unknown variant $1"; exit 5 ;;
  esac
}

import_flags_for() {
  case "$1" in
    budg-b64) printf '%s\n' "--semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files 64" ;;
    budg-b256) printf '%s\n' "--semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files 256" ;;
    *) printf '\n' ;;
  esac
}

needs_degree_hint() {
  case "$1" in
    semantic|budg-b64|budg-b256) return 0 ;;
    *) return 1 ;;
  esac
}

run_variant() {
  local name="$1" layout store import_flags
  layout="$(layout_for "$name")"
  store="${TEMP_STORE_ROOT}/${name}"
  import_flags="$(import_flags_for "$name")"

  log "start ${name} layout=${layout} store=${store}"
  ensure_resources "before import ${name}"
  safe_rm_store "$store"

  if [[ -n "$import_flags" ]]; then
    read -r -a flags <<< "$import_flags"
  else
    flags=()
  fi

  /usr/bin/time -v env SNB_SKIP_ADJ_CACHE=1 "$BIN" --io-backend "$IO_BACKEND" import \
    --input "$INPUT" --data-dir "$store" --relation snb-full \
    --memgraph-bytes "$MEMGRAPH_BYTES" --l0-layout "$layout" "${flags[@]}" \
    > "${OUT_DIR}/${name}-import.stdout" 2> "${OUT_DIR}/${name}-import.stderr"

  printf '%s\t%s\t%s\t%s\n' "$name" "$layout" "$store" "$(du -sb "$store" | awk '{print $1}')" >> "$MANIFEST"

  /usr/bin/time -v "$BIN" --io-backend "$IO_BACKEND" stats \
    --data-dir "$store" > "${OUT_DIR}/${name}-stats.json" 2> "${OUT_DIR}/${name}-stats.err"

  log "waiting compare lock ${name}"
  flock "$COMPARE_LOCK" bash -c '
    set -Eeuo pipefail
    name="$1"; store="$2"; root="$3"; bin="$4"; io_backend="$5"; schema_store="$6"; plan="$7"; out_dir="$8"; log="$9"; min_mem="${10}"; min_disk="${11}"; hint="${12}"
    log_msg() { printf "[%(%Y-%m-%d %H:%M:%S)T] %s\n" -1 "$*" | tee -a "$log"; }
    avail="$(awk "/MemAvailable/ { printf \"%d\\n\", \$2 / 1024 / 1024 }" /proc/meminfo)"
    free="$(df -BG "$root" | awk "NR == 2 { gsub(/G/, \"\", \$4); print int(\$4) }")"
    log_msg "resource check (before compare ${name}): mem_available=${avail}GiB disk_free=${free}GiB"
    if (( avail < min_mem || free < min_disk )); then
      log_msg "FATAL: resource gate failed before compare ${name}"
      exit 6
    fi
    if [[ "$hint" = "1" ]]; then
      /usr/bin/time -v env SNB_SKIP_SEM_INDEX=1 "$bin" --io-backend "$io_backend" neighbor-compare \
        --left-data-dir "$schema_store" --right-data-dir "$store" --sample-plan "$plan" \
        --right-semantic-degree-hint --max-mismatches 1 \
        > "${out_dir}/compare-schema-vs-${name}.json" 2> "${out_dir}/compare-schema-vs-${name}.err"
    else
      /usr/bin/time -v env SNB_SKIP_SEM_INDEX=1 "$bin" --io-backend "$io_backend" neighbor-compare \
        --left-data-dir "$schema_store" --right-data-dir "$store" --sample-plan "$plan" \
        --max-mismatches 1 \
        > "${out_dir}/compare-schema-vs-${name}.json" 2> "${out_dir}/compare-schema-vs-${name}.err"
    fi
    log_msg "compare ${name} done"
  ' _ "$name" "$store" "$ROOT" "$BIN" "$IO_BACKEND" "$SCHEMA_STORE" "$PLAN" "$OUT_DIR" "$LOG" "$MIN_AVAILABLE_GIB" "$MIN_FREE_GIB" "$(needs_degree_hint "$name" && printf 1 || printf 0)"

  safe_rm_store "$store"
  log "delete ${store}"
  log "done ${name}"
}

main() {
  : > "$LOG"
  printf 'variant\tlayout\tstore\tstore_size_bytes\n' > "$MANIFEST"
  test -x "$BIN"
  test -d "$SCHEMA_STORE"
  test -s "$PLAN"
  command -v flock >/dev/null

  log "run_id=${RUN_ID}"
  log "variants=${VARIANTS} max_jobs=${MAX_JOBS}"
  log "schema_store=${SCHEMA_STORE}"
  log "sample_plan=${PLAN}"
  ensure_resources "start"

  local running=0 failures=0
  for variant in $VARIANTS; do
    while (( running >= MAX_JOBS )); do
      if wait -n; then
        running=$((running - 1))
      else
        running=$((running - 1))
        failures=$((failures + 1))
      fi
    done
    run_variant "$variant" &
    running=$((running + 1))
    log "launched ${variant}; running=${running}"
  done

  while (( running > 0 )); do
    if wait -n; then
      running=$((running - 1))
    else
      running=$((running - 1))
      failures=$((failures + 1))
    fi
  done

  if (( failures > 0 )); then
    log "FAILED failures=${failures}"
    exit 7
  fi
  log "DONE ${RUN_ID}"
  date -Is > "${OUT_DIR}/DONE"
}

main "$@"
