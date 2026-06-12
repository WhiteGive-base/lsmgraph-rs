#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
RUN_ID="${RUN_ID:-qslsm-sf100-correctness-csr-20260611}"
OUT_DIR="${OUT_DIR:-${ROOT}/remote-logs/${RUN_ID}}"
TEMP_STORE_ROOT="${TEMP_STORE_ROOT:-${ROOT}/store/${RUN_ID}}"
SCHEMA_STORE="${SCHEMA_STORE:-${ROOT}/store/qslsm-sf100-strong-baseline-20260610/schema}"
PLAN="${PLAN:-${ROOT}/remote-logs/qslsm-sf100-strong-baseline-20260610/sample-plan-core-s5000.json}"
INPUT="${INPUT:-/data/WorkSpace/ldbc-sf100/social_network}"
BIN="${BIN:-${ROOT}/target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-67108864}"
VARIANT="${1:-${VARIANT:-}}"
SAMPLES_PER_EDGE_TYPE="${SAMPLES_PER_EDGE_TYPE:-100}"

MIN_AVAILABLE_GIB="${MIN_AVAILABLE_GIB:-320}"
MIN_FREE_GIB="${MIN_FREE_GIB:-170}"

cd "$ROOT"
mkdir -p "$OUT_DIR" "$TEMP_STORE_ROOT"

LOG="${OUT_DIR}/${VARIANT:-unknown}.progress.log"
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

validate_compare_json() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

errors = []
if data.get("checked", 0) <= 0:
    errors.append("checked is zero")
if data.get("mismatches") != 0:
    errors.append(f"mismatches={data.get('mismatches')}")
if data.get("left_edges_total", 0) <= 0:
    errors.append("left_edges_total is zero")
if data.get("left_edges_total") != data.get("right_edges_total"):
    errors.append(
        f"edge totals differ: left={data.get('left_edges_total')} right={data.get('right_edges_total')}"
    )

if errors:
    raise SystemExit("; ".join(errors))

print(
    "validated "
    f"checked={data['checked']} mismatches={data['mismatches']} "
    f"edges={data['left_edges_total']} elapsed_s={data.get('elapsed_s')}"
)
PY
}

main() {
  if [[ -z "$VARIANT" ]]; then
    log "FATAL: VARIANT is required"
    exit 6
  fi

  local layout store import_flags compare_json
  layout="$(layout_for "$VARIANT")"
  store="${TEMP_STORE_ROOT}/${VARIANT}"
  import_flags="$(import_flags_for "$VARIANT")"
  compare_json="${OUT_DIR}/csr-compare-schema-vs-${VARIANT}-s${SAMPLES_PER_EDGE_TYPE}.json"

  test -x "$BIN"
  test -d "$SCHEMA_STORE"
  test -s "$PLAN"
  test -s "${ROOT}/baseline/sf100_sampled_csr_compare.py"

  log "run_id=${RUN_ID} variant=${VARIANT} layout=${layout}"
  log "schema_store=${SCHEMA_STORE}"
  log "sample_plan=${PLAN} samples_per_edge_type=${SAMPLES_PER_EDGE_TYPE}"
  ensure_resources "before import ${VARIANT}"
  safe_rm_store "$store"

  if [[ -n "$import_flags" ]]; then
    read -r -a flags <<< "$import_flags"
  else
    flags=()
  fi

  /usr/bin/time -v env SNB_SKIP_ADJ_CACHE=1 "$BIN" --io-backend "$IO_BACKEND" import \
    --input "$INPUT" --data-dir "$store" --relation snb-full \
    --memgraph-bytes "$MEMGRAPH_BYTES" --l0-layout "$layout" "${flags[@]}" \
    > "${OUT_DIR}/${VARIANT}-import.stdout" 2> "${OUT_DIR}/${VARIANT}-import.stderr"

  printf '%s\t%s\t%s\t%s\n' "$VARIANT" "$layout" "$store" "$(du -sb "$store" | awk '{print $1}')" >> "$MANIFEST"

  ensure_resources "before csr compare ${VARIANT}"
  /usr/bin/time -v python3 baseline/sf100_sampled_csr_compare.py \
    --left-store "$SCHEMA_STORE" --right-store "$store" \
    --sample-plan "$PLAN" --samples-per-edge-type "$SAMPLES_PER_EDGE_TYPE" \
    --out "$compare_json" \
    > "${OUT_DIR}/csr-compare-schema-vs-${VARIANT}-s${SAMPLES_PER_EDGE_TYPE}.stdout" \
    2> "${OUT_DIR}/csr-compare-schema-vs-${VARIANT}-s${SAMPLES_PER_EDGE_TYPE}.stderr"

  validate_compare_json "$compare_json" | tee -a "$LOG"
  safe_rm_store "$store"
  log "delete ${store}"
  log "DONE ${VARIANT}"
}

main "$@"
