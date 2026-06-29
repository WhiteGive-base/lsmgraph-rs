#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${ROOT}/target/release/w5_steady_state_real_store"
RUN_ID="${RUN_ID:-w9-steady-state-$(date +%Y%m%d-%H%M%S)}"
LOG_ROOT="${LOG_ROOT:-${ROOT}/remote-logs/${RUN_ID}}"
MODE="${MODE:-smoke}"
DATA_ROOT="${DATA_ROOT:-}"
STORE_ROOT="${STORE_ROOT:-/data/WorkSpace/lsmgraph-rs/store}"
SMOKE_STORE="${SMOKE_STORE:-${STORE_ROOT}/sf1-base-graph}"
SF30_STORE_ROOT="${SF30_STORE_ROOT:-${STORE_ROOT}}"
SCHEMA_STORE="${SCHEMA_STORE:-${SF30_STORE_ROOT}/sf30-schema}"
BUDG_B64_STORE="${BUDG_B64_STORE:-${SF30_STORE_ROOT}/sf30-budg-b64}"
SEMANTIC_STORE="${SEMANTIC_STORE:-${SF30_STORE_ROOT}/sf30-semantic}"
QUERY_RATE="${QUERY_RATE:-200}"
WRITE_RATE="${WRITE_RATE:-200}"
CHECKPOINT_SECS="${CHECKPOINT_SECS:-30}"
FLUSH_EVERY_WRITES="${FLUSH_EVERY_WRITES:-1024}"
COMPACT_EVERY_SECS="${COMPACT_EVERY_SECS:-0}"
ABORT_MIN_QUERIES="${ABORT_MIN_QUERIES:-1}"
ABORT_MAX_WRITE_OP_MS="${ABORT_MAX_WRITE_OP_MS:-30000}"

mkdir -p "${LOG_ROOT}"

echo "W5/W9 steady-state runner"
echo "mode=${MODE}"
echo "log_root=${LOG_ROOT}"
echo "ETA smoke: 3-5 min; formal SF30: 30-60 min per variant plus build/open overhead"
echo "Abort conditions: checkpoint queries < ${ABORT_MIN_QUERIES}; writer op > ${ABORT_MAX_WRITE_OP_MS}ms is counted as stall; runner aborts on writer errors."
echo "Progress signals: JSONL checkpoints include elapsed_secs, candidate_l0_segments, latency p50/p99, L0 files, compaction rewrite bytes, flush-stall proxy, io.write_bytes."

if command -v df >/dev/null 2>&1 && df -BG /data >/dev/null 2>&1; then
  DATA_AVAIL_GIB="$(df -BG /data | awk 'NR==2 {gsub("G","",$4); print $4}')"
  if [ "${DATA_AVAIL_GIB:-0}" -lt 200 ]; then
    echo "ABORT: /data available ${DATA_AVAIL_GIB}GiB < 200GiB" | tee "${LOG_ROOT}/ABORT"
    exit 2
  fi
fi

if [ -r /proc/meminfo ]; then
  MEM_AVAILABLE_KIB="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
  if [ "${MEM_AVAILABLE_KIB:-0}" -lt 83886080 ]; then
    echo "ABORT: MemAvailable ${MEM_AVAILABLE_KIB}KiB < 80GiB" | tee "${LOG_ROOT}/ABORT"
    exit 2
  fi
fi

nice -n 10 cargo build --release --bin w5_steady_state_real_store

run_variant() {
  local variant="$1"
  local store="$2"
  local duration="$3"
  local output="${LOG_ROOT}/${variant}.jsonl"
  echo "running variant=${variant} store=${store} duration=${duration}s output=${output}"
  nice -n 10 "${BIN}" \
    --data-dir "${store}" \
    --input "${DATA_ROOT}" \
    --variant "${variant}" \
    --duration-secs "${duration}" \
    --checkpoint-secs "${CHECKPOINT_SECS}" \
    --query-rate-per-sec "${QUERY_RATE}" \
    --writes-per-sec "${WRITE_RATE}" \
    --flush-every-writes "${FLUSH_EVERY_WRITES}" \
    --compact-best-every-secs "${COMPACT_EVERY_SECS}" \
    --abort-min-queries-per-checkpoint "${ABORT_MIN_QUERIES}" \
    --abort-max-write-op-ms "${ABORT_MAX_WRITE_OP_MS}" \
    --output "${output}"
}

case "${MODE}" in
  smoke)
    DATA_ROOT="${DATA_ROOT:-/data/WorkSpace/ldbc-sf1/social_network}"
    DURATION="${DURATION:-180}"
    STORE="${STORE:-${SMOKE_STORE}}"
    VARIANT="${VARIANT:-schema}"
    run_variant "${VARIANT}" "${STORE}" "${DURATION}"
    ;;
  formal)
    if [[ "${RUN_FORMAL:-0}" != "1" ]]; then
      echo "Refusing to start formal SF30 run without RUN_FORMAL=1." >&2
      exit 2
    fi
    DATA_ROOT="${DATA_ROOT:-/data/WorkSpace/ldbc-sf30/social_network}"
    DURATION="${DURATION:-3600}"
    echo "formal stores: schema=${SCHEMA_STORE} budg-b64=${BUDG_B64_STORE} semantic=${SEMANTIC_STORE}"
    for store in "${SCHEMA_STORE}" "${BUDG_B64_STORE}" "${SEMANTIC_STORE}"; do
      if [[ ! -d "${store}" ]]; then
        echo "ABORT: formal store missing: ${store}" | tee "${LOG_ROOT}/ABORT"
        exit 2
      fi
    done
    run_variant "schema" "${SCHEMA_STORE}" "${DURATION}"
    run_variant "budg-b64" "${BUDG_B64_STORE}" "${DURATION}"
    run_variant "semantic" "${SEMANTIC_STORE}" "${DURATION}"
    ;;
  *)
    echo "unknown MODE=${MODE}; use smoke or formal" >&2
    exit 2
    ;;
esac

touch "${LOG_ROOT}/DONE"
echo "DONE ${LOG_ROOT}"
