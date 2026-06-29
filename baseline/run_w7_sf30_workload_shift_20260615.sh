#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${ROOT}/target/release/w7-sf30-workload-shift"
RUN_ID="${RUN_ID:-w7-sf30-workload-shift-$(date +%Y%m%d-%H%M%S)}"
LOG_ROOT="${LOG_ROOT:-${ROOT}/remote-logs/${RUN_ID}}"
MODE="${MODE:-smoke}"
DATA_ROOT="${DATA_ROOT:-/data/WorkSpace/ldbc-sf30/social_network}"
STORE_DIR="${STORE_DIR:-${ROOT}/target/${RUN_ID}-store}"
OUTPUT="${OUTPUT:-${LOG_ROOT}/w7-sf30-workload-shift.json}"
SUMMARY="${SUMMARY:-${LOG_ROOT}/summary.md}"

mkdir -p "${LOG_ROOT}"

echo "[w7-sf30] run_id=${RUN_ID}"
echo "[w7-sf30] mode=${MODE}"
echo "[w7-sf30] log_root=${LOG_ROOT}"
echo "[w7-sf30] data_root=${DATA_ROOT}"
echo "[w7-sf30] store_dir=${STORE_DIR}"
echo "[w7-sf30] ETA smoke: 5-15 min including build; formal: 30-120 min depending scan rows and phase size."
echo "[w7-sf30] stop: /data available <200GiB, MemAvailable <80GiB, timeout, JSON invalid, no hot partition compaction in feedback-only, or missing phase-B migration."

if command -v df >/dev/null 2>&1 && df -BG /data >/dev/null 2>&1; then
  DATA_AVAIL_GIB="$(df -BG /data | awk 'NR==2 {gsub("G","",$4); print $4}')"
  if [ "${DATA_AVAIL_GIB:-0}" -lt 200 ]; then
    echo "[w7-sf30] ABORT: /data available ${DATA_AVAIL_GIB}GiB < 200GiB" | tee "${LOG_ROOT}/ABORT"
    exit 2
  fi
fi

if [ -r /proc/meminfo ]; then
  MEM_AVAILABLE_KIB="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
  if [ "${MEM_AVAILABLE_KIB:-0}" -lt 83886080 ]; then
    echo "[w7-sf30] ABORT: MemAvailable ${MEM_AVAILABLE_KIB}KiB < 80GiB" | tee "${LOG_ROOT}/ABORT"
    exit 2
  fi
fi

case "${MODE}" in
  smoke)
    TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
    PHASE_FLUSHES="${PHASE_FLUSHES:-2}"
    HOT_SOURCES="${HOT_SOURCES:-4}"
    EDGES_PER_SOURCE_PER_FLUSH="${EDGES_PER_SOURCE_PER_FLUSH:-1}"
    QUERIES_PER_SOURCE="${QUERIES_PER_SOURCE:-2}"
    MAX_SCAN_ROWS_PER_PHASE="${MAX_SCAN_ROWS_PER_PHASE:-200000}"
    ;;
  formal)
    if [[ "${RUN_FORMAL:-0}" != "1" ]]; then
      echo "[w7-sf30] Refusing formal run without RUN_FORMAL=1." | tee "${LOG_ROOT}/ABORT"
      exit 2
    fi
    TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-7200}"
    PHASE_FLUSHES="${PHASE_FLUSHES:-8}"
    HOT_SOURCES="${HOT_SOURCES:-64}"
    EDGES_PER_SOURCE_PER_FLUSH="${EDGES_PER_SOURCE_PER_FLUSH:-1}"
    QUERIES_PER_SOURCE="${QUERIES_PER_SOURCE:-8}"
    MAX_SCAN_ROWS_PER_PHASE="${MAX_SCAN_ROWS_PER_PHASE:-2000000}"
    ;;
  *)
    echo "[w7-sf30] unknown MODE=${MODE}; use smoke or formal" | tee "${LOG_ROOT}/ABORT"
    exit 2
    ;;
esac

echo "[w7-sf30] params phase_flushes=${PHASE_FLUSHES} hot_sources=${HOT_SOURCES} edges_per_source_per_flush=${EDGES_PER_SOURCE_PER_FLUSH} queries_per_source=${QUERIES_PER_SOURCE} max_scan_rows=${MAX_SCAN_ROWS_PER_PHASE} timeout=${TIMEOUT_SECONDS}s"

cd "${ROOT}"
nice -n 10 cargo build --release --bin w7-sf30-workload-shift 2>&1 | tee "${LOG_ROOT}/build.log"

set +e
timeout "${TIMEOUT_SECONDS}" nice -n 10 "${BIN}" \
  --input "${DATA_ROOT}" \
  --store-dir "${STORE_DIR}" \
  --output "${OUTPUT}" \
  --reset-store \
  --phase-flushes "${PHASE_FLUSHES}" \
  --hot-sources-per-phase "${HOT_SOURCES}" \
  --edges-per-source-per-flush "${EDGES_PER_SOURCE_PER_FLUSH}" \
  --queries-per-source "${QUERIES_PER_SOURCE}" \
  --max-scan-rows-per-phase "${MAX_SCAN_ROWS_PER_PHASE}" \
  > "${LOG_ROOT}/runner.stdout.json" \
  2> "${LOG_ROOT}/runner.stderr.log"
STATUS=$?
set -e
cat "${LOG_ROOT}/runner.stderr.log"

if [[ "${STATUS}" -ne 0 ]]; then
  echo "[w7-sf30] FAILED runner exit=${STATUS}" | tee "${LOG_ROOT}/FAILED"
  exit "${STATUS}"
fi

python3 -m json.tool "${OUTPUT}" >/dev/null
python3 baseline/summarize_w7_sf30_workload_shift_20260615.py \
  --input "${OUTPUT}" \
  --output "${SUMMARY}" \
  --source-label "${LOG_ROOT}"

if [[ "${MODE}" == "formal" ]] && grep -q "Gate: FALLBACK" "${SUMMARY}"; then
  echo "[w7-sf30] FAILED summary gate fallback" | tee "${LOG_ROOT}/FAILED"
  exit 3
fi

printf "output=%s\nsummary=%s\nstore_dir=%s\n" "${OUTPUT}" "${SUMMARY}" "${STORE_DIR}" > "${LOG_ROOT}/DONE"
echo "[w7-sf30] DONE ${LOG_ROOT}"
