#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
RUN_ID="${RUN_ID:-qslsm-sf100-maintenance-table-20260612}"
OUT_DIR="${OUT_DIR:-${ROOT}/remote-logs/${RUN_ID}}"
TEMP_STORE_ROOT="${TEMP_STORE_ROOT:-${ROOT}/store/${RUN_ID}}"
INPUT="${INPUT:-/data/WorkSpace/ldbc-sf100/social_network}"
BIN="${BIN:-${ROOT}/target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-67108864}"
VARIANTS="${VARIANTS:-schema edge-type-only semantic budg-b64 budg-b256 budg-b1024}"
MIN_AVAILABLE_GIB="${MIN_AVAILABLE_GIB:-320}"
MIN_FREE_GIB="${MIN_FREE_GIB:-170}"

cd "$ROOT"
mkdir -p "$OUT_DIR" "$TEMP_STORE_ROOT"

LOG="${OUT_DIR}/progress.log"
TABLE="${OUT_DIR}/maintenance-table.tsv"

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
    schema) printf 'schema\n' ;;
    edge-type-only) printf 'edge-type-only\n' ;;
    semantic) printf 'semantic\n' ;;
    budg-b64|budg-b256|budg-b1024) printf 'semantic-budgeted\n' ;;
    *) log "FATAL: unknown variant $1"; exit 5 ;;
  esac
}

import_flags_for() {
  case "$1" in
    budg-b64) printf '%s\n' "--semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files 64" ;;
    budg-b256) printf '%s\n' "--semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files 256" ;;
    budg-b1024) printf '%s\n' "--semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files 1024" ;;
    *) printf '\n' ;;
  esac
}

extract_time_metric() {
  local file="$1" label="$2"
  grep -F "$label" "$file" | tail -n 1 | awk -F ': ' '{print $NF}'
}

run_variant() {
  local name="$1" layout store import_flags stderr stdout manifest_bytes l0_files store_bytes elapsed max_rss exit_status
  layout="$(layout_for "$name")"
  store="${TEMP_STORE_ROOT}/${name}"
  import_flags="$(import_flags_for "$name")"
  stdout="${OUT_DIR}/${name}-import.stdout"
  stderr="${OUT_DIR}/${name}-import.stderr"

  log "start ${name} layout=${layout}"
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
    > "$stdout" 2> "$stderr"

  test -s "${store}/MANIFEST"
  store_bytes="$(du -sb "$store" | awk '{print $1}')"
  manifest_bytes="$(stat -c '%s' "${store}/MANIFEST")"
  l0_files="$(wc -l < "${store}/MANIFEST")"
  elapsed="$(extract_time_metric "$stderr" 'Elapsed (wall clock) time')"
  max_rss="$(extract_time_metric "$stderr" 'Maximum resident set size')"
  exit_status="$(extract_time_metric "$stderr" 'Exit status')"

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$name" "$layout" "$store_bytes" "$manifest_bytes" "$l0_files" \
    "$elapsed" "$max_rss" "$exit_status" "$store" >> "$TABLE"

  safe_rm_store "$store"
  log "delete ${store}"
  log "done ${name} store_bytes=${store_bytes} manifest_bytes=${manifest_bytes} l0_files=${l0_files} elapsed=${elapsed} max_rss_kb=${max_rss}"
}

main() {
  : > "$LOG"
  printf 'variant\tlayout\tstore_bytes\tmanifest_bytes\tl0_files\telapsed_wall\tmax_rss_kb\texit_status\tstore\n' > "$TABLE"
  test -x "$BIN"
  ensure_resources "start"
  log "run_id=${RUN_ID}"
  log "variants=${VARIANTS}"
  for variant in $VARIANTS; do
    run_variant "$variant"
  done
  log "DONE ${RUN_ID}"
  date -Is > "${OUT_DIR}/DONE"
}

main "$@"
