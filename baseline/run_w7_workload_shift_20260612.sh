#!/usr/bin/env bash
set -euo pipefail

# W7 formal SF30 runs should reuse this runner with larger inputs after W6.
# This script intentionally defaults to SF1-class synthetic smoke only.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="${RUN_ID:-w7-workload-shift-smoke-$(date +%Y%m%d-%H%M%S)}"
LOG_ROOT="${LOG_ROOT:-${ROOT_DIR}/remote-logs/${RUN_ID}}"
STORE_DIR="${STORE_DIR:-${ROOT_DIR}/target/${RUN_ID}-store}"
OUTPUT="${OUTPUT:-${LOG_ROOT}/w3-workload-shift-smoke.json}"
PHASE_FLUSHES="${PHASE_FLUSHES:-4}"
QUERIES_PER_FLUSH="${QUERIES_PER_FLUSH:-3}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"

mkdir -p "${LOG_ROOT}"

echo "[w7-workload-shift] run_id=${RUN_ID}"
echo "[w7-workload-shift] ETA: smoke expected <5 min on SF1-class synthetic data; abort timeout=${TIMEOUT_SECONDS}s"
echo "[w7-workload-shift] progress: variants=feedback-only,static-budgeted,no-feedback phase_flushes=${PHASE_FLUSHES} queries_per_flush=${QUERIES_PER_FLUSH}"
echo "[w7-workload-shift] abort: /data available <200GiB, MemAvailable <80GiB, or timeout"

if command -v df >/dev/null 2>&1 && df -BG /data >/dev/null 2>&1; then
  DATA_AVAIL_GIB="$(df -BG /data | awk 'NR==2 {gsub("G","",$4); print $4}')"
  if [ "${DATA_AVAIL_GIB:-0}" -lt 200 ]; then
    echo "[w7-workload-shift] ABORT: /data available ${DATA_AVAIL_GIB}GiB < 200GiB" | tee "${LOG_ROOT}/ABORT"
    exit 2
  fi
fi

if [ -r /proc/meminfo ]; then
  MEM_AVAILABLE_KIB="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
  if [ "${MEM_AVAILABLE_KIB:-0}" -lt 83886080 ]; then
    echo "[w7-workload-shift] ABORT: MemAvailable ${MEM_AVAILABLE_KIB}KiB < 80GiB" | tee "${LOG_ROOT}/ABORT"
    exit 2
  fi
fi

cd "${ROOT_DIR}"

echo "[w7-workload-shift] building/running debug smoke"
timeout "${TIMEOUT_SECONDS}" nice -n 10 cargo run --bin w3-workload-shift -- \
  --store-dir "${STORE_DIR}" \
  --output "${OUTPUT}" \
  --reset-store \
  --phase-flushes "${PHASE_FLUSHES}" \
  --queries-per-flush "${QUERIES_PER_FLUSH}" \
  2>&1 | tee "${LOG_ROOT}/runner.log"

printf "output=%s\nstore_dir=%s\n" "${OUTPUT}" "${STORE_DIR}" > "${LOG_ROOT}/DONE"
echo "[w7-workload-shift] DONE ${OUTPUT}"
