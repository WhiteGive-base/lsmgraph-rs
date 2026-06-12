#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
RUN_ID="${RUN_ID:-livegraph-sf100-stream-20260612}"
OUT_DIR="${OUT_DIR:-${ROOT}/remote-logs/${RUN_ID}}"
STORE="${STORE:-${ROOT}/store/qslsm-sf100-strong-baseline-20260610/schema}"
BIN="${BIN:-${ROOT}/target/release/lsmgraph}"
RAW_EDGES="${RAW_EDGES:-${OUT_DIR}/edges-raw.tsv}"
DENSE_EDGES="${DENSE_EDGES:-${OUT_DIR}/edges-dense.txt}"
SAMPLES="${SAMPLES:-1000}"
MIN_AVAILABLE_GIB="${MIN_AVAILABLE_GIB:-430}"
MIN_FREE_GIB_START="${MIN_FREE_GIB_START:-500}"
MIN_FREE_GIB_DRIVER="${MIN_FREE_GIB_DRIVER:-430}"
DELETE_RAW_AFTER_CONVERT="${DELETE_RAW_AFTER_CONVERT:-1}"
DELETE_SCHEMA_STORE_BEFORE_DRIVER="${DELETE_SCHEMA_STORE_BEFORE_DRIVER:-0}"

cd "$ROOT"
mkdir -p "$OUT_DIR"

LOG="${OUT_DIR}/progress.log"
LG_BLOCK="${OUT_DIR}/livegraph-block"
LG_WAL="${OUT_DIR}/livegraph-wal"

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
  local phase="$1" min_free="$2" avail free
  avail="$(available_gib)"
  free="$(free_gib)"
  log "resource check (${phase}): mem_available=${avail}GiB disk_free=${free}GiB"
  if (( avail < MIN_AVAILABLE_GIB )); then
    log "FATAL: MemAvailable ${avail}GiB < MIN_AVAILABLE_GIB ${MIN_AVAILABLE_GIB}GiB"
    exit 2
  fi
  if (( free < min_free )); then
    log "FATAL: disk free ${free}GiB < required ${min_free}GiB"
    exit 3
  fi
}

safe_rm() {
  local path="$1"
  case "$path" in
    "${OUT_DIR}/"*) rm -rf -- "$path" ;;
    *) log "FATAL: refusing to delete unexpected path: $path"; exit 4 ;;
  esac
}

delete_schema_store_if_enabled() {
  if [[ "$DELETE_SCHEMA_STORE_BEFORE_DRIVER" != "1" ]]; then
    return
  fi
  if [[ "$STORE" != "${ROOT}/store/qslsm-sf100-strong-baseline-20260610/schema" ]]; then
    log "FATAL: refusing schema-store delete for unexpected STORE=$STORE"
    exit 5
  fi
  log "delete schema store before driver: $STORE"
  rm -rf -- "$STORE"
}

validate_outputs() {
  python3 - "$OUT_DIR" <<'PY'
import json
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
scan = json.loads((out / "scan.json").read_text())
convert = json.loads((out / "convert-summary.json").read_text())
livegraph = json.loads((out / "livegraph-sf100-stream.json").read_text())

errors = []
if convert["edge_count"] != livegraph["edge_count"]:
    errors.append(
        f"dense/livegraph edge_count mismatch: {convert['edge_count']} vs {livegraph['edge_count']}"
    )
if scan["directed_edges"] < convert["edge_count"]:
    errors.append(
        f"scan directed_edges {scan['directed_edges']} < dumped edge_count {convert['edge_count']}"
    )
if livegraph.get("peak_rss_kb", 0) <= 0:
    errors.append("missing LiveGraph peak_rss_kb")
if not livegraph.get("benchmarks"):
    errors.append("missing LiveGraph benchmark entries")

if errors:
    raise SystemExit("; ".join(errors))

print(
    "validated "
    f"scan_edges={scan['directed_edges']} dense_edges={convert['edge_count']} "
    f"vertices={convert['vertex_count']} livegraph_peak_rss_kb={livegraph['peak_rss_kb']}"
)
PY
}

main() {
  : > "$LOG"
  test -x "$BIN"
  test -d "$STORE"
  test -x baseline/external-drivers/livegraph_driver_stream
  test -s deps/LiveGraph/build/liblivegraph.so
  test -s baseline/convert_livegraph_edges.py

  log "run_id=${RUN_ID}"
  log "store=${STORE}"
  log "out_dir=${OUT_DIR}"
  ensure_resources "start" "$MIN_FREE_GIB_START"

  env SNB_SKIP_SEM_INDEX=1 /usr/bin/time -v "$BIN" --io-backend blocking scan \
    --data-dir "$STORE" --dump-edges "$RAW_EDGES" \
    > "${OUT_DIR}/scan.json" 2> "${OUT_DIR}/scan.stderr"

  ensure_resources "before convert" 120
  /usr/bin/time -v python3 baseline/convert_livegraph_edges.py \
    --input "$RAW_EDGES" --output "$DENSE_EDGES" --summary "${OUT_DIR}/convert-summary.json" \
    > "${OUT_DIR}/convert.stdout" 2> "${OUT_DIR}/convert.stderr"

  if [[ "$DELETE_RAW_AFTER_CONVERT" == "1" ]]; then
    log "delete raw edges after convert: $RAW_EDGES"
    safe_rm "$RAW_EDGES"
  fi

  delete_schema_store_if_enabled
  ensure_resources "before livegraph stream driver" "$MIN_FREE_GIB_DRIVER"
  safe_rm "$LG_BLOCK"
  safe_rm "$LG_WAL"

  LD_LIBRARY_PATH="${ROOT}/deps/LiveGraph/build" /usr/bin/time -v \
    baseline/external-drivers/livegraph_driver_stream \
      --edges "$DENSE_EDGES" --samples "$SAMPLES" \
      --block-path "$LG_BLOCK" --wal-path "$LG_WAL" \
      --output "${OUT_DIR}/livegraph-sf100-stream.json" \
    > "${OUT_DIR}/livegraph.stdout" 2> "${OUT_DIR}/livegraph.stderr"

  du -sb "$LG_BLOCK" "$LG_WAL" > "${OUT_DIR}/livegraph-footprint.tsv" 2>/dev/null || true
  validate_outputs | tee -a "$LOG"
  log "DONE ${RUN_ID}"
  date -Is > "${OUT_DIR}/DONE"
}

main "$@"
