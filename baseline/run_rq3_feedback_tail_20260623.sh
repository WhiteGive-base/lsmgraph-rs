#!/usr/bin/env bash
# RQ3 — feedback-vs-no-feedback tail latency under churn (single-variable).
# Holds L0 layout (semantic) and the mixed read/write workload constant; the
# ONLY variable across arms is the L0 compaction strategy:
#   feedback : --compact-best-every-secs N  (score/read-amp targeted compaction)
#   full     : --full-compact-every-secs N  (blind full L0->L1 compaction, fair baseline)
#   none     : neither                       (W9 as-run: pile up L0, prune only)
# All arms run --variant semantic on an identical copy of the same base store.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${ROOT}/target/release/w5_steady_state_real_store"
RUN_ID="${RUN_ID:-rq3-feedback-tail-$(date +%Y%m%d-%H%M%S)}"
LOG_ROOT="${LOG_ROOT:-${ROOT}/remote-logs/${RUN_ID}}"
MODE="${MODE:-smoke}"

# base store: all three arms start from an identical copy (each arm mutates in place)
BASE_STORE="${BASE_STORE:-${ROOT}/store/sf30-base-graph}"
STORE_PARENT="${STORE_PARENT:-${ROOT}/store}"
DATA_ROOT="${DATA_ROOT:-/data/WorkSpace/ldbc-sf30/social_network}"

ARMS="${ARMS:-feedback full none}"

# workload params (aligned with W9 for cross-comparability)
QUERY_RATE="${QUERY_RATE:-200}"
WRITE_RATE="${WRITE_RATE:-200}"
FLUSH_EVERY_WRITES="${FLUSH_EVERY_WRITES:-1024}"
ABORT_MIN_QUERIES="${ABORT_MIN_QUERIES:-1}"
ABORT_MAX_WRITE_OP_MS="${ABORT_MAX_WRITE_OP_MS:-30000}"

# feedback-compaction trigger thresholds. w5 defaults are effectively "never fire"
# (semantic_budget_min_edge_type_score=1e308, l0_ra_min_score=10). p3 lowered these
# to actually exercise compact_best_l0_partition_by_score; we mirror that. Tunable;
# Step-2 smoke calibrates so the feedback arm's compaction count is > 0.
L0_RA_RANGE_BUCKET_SIZE="${L0_RA_RANGE_BUCKET_SIZE:-65536}"
L0_RA_MIN_QUERIES="${L0_RA_MIN_QUERIES:-1}"
L0_RA_MIN_L0_SEGMENTS="${L0_RA_MIN_L0_SEGMENTS:-1}"
L0_RA_MIN_SCORE="${L0_RA_MIN_SCORE:-0.0}"
SB_MIN_EDGE_TYPE_BYTES="${SB_MIN_EDGE_TYPE_BYTES:-1}"
SB_MIN_EDGE_TYPE_SCORE="${SB_MIN_EDGE_TYPE_SCORE:-0.0}"
SB_MIN_EXACT_BYTES="${SB_MIN_EXACT_BYTES:-1}"
SB_MIN_BENEFIT_SCORE="${SB_MIN_BENEFIT_SCORE:-0.0}"

mkdir -p "${LOG_ROOT}"

case "${MODE}" in
  smoke)
    DURATION="${DURATION:-180}"
    CHECKPOINT_SECS="${CHECKPOINT_SECS:-60}"
    COMPACT_SECS="${COMPACT_SECS:-30}"
    ;;
  formal)
    if [[ "${RUN_FORMAL:-0}" != "1" ]]; then
      echo "[rq3] Refusing formal run without RUN_FORMAL=1." | tee "${LOG_ROOT}/ABORT"
      exit 2
    fi
    DURATION="${DURATION:-1800}"
    CHECKPOINT_SECS="${CHECKPOINT_SECS:-300}"
    COMPACT_SECS="${COMPACT_SECS:-60}"
    ;;
  *)
    echo "[rq3] unknown MODE=${MODE}; use smoke or formal" | tee "${LOG_ROOT}/ABORT"
    exit 2
    ;;
esac

echo "[rq3] run_id=${RUN_ID} mode=${MODE} arms='${ARMS}'"
echo "[rq3] base_store=${BASE_STORE} data_root=${DATA_ROOT}"
echo "[rq3] duration=${DURATION}s checkpoint=${CHECKPOINT_SECS}s compact_secs=${COMPACT_SECS}s query_rate=${QUERY_RATE} write_rate=${WRITE_RATE}"
echo "[rq3] log_root=${LOG_ROOT}"
echo "[rq3] stop: base/input missing, /data <200GiB, MemAvailable <80GiB, arm store exists (set RQ3_FORCE_RESET=1), runner timeout, or writer error/slow op."

# preflight
if [[ ! -d "${BASE_STORE}" ]]; then
  echo "[rq3] ABORT: base store missing: ${BASE_STORE}" | tee "${LOG_ROOT}/ABORT"; exit 2
fi
if [[ ! -d "${DATA_ROOT}" ]]; then
  echo "[rq3] ABORT: input missing: ${DATA_ROOT}" | tee "${LOG_ROOT}/ABORT"; exit 2
fi
if command -v df >/dev/null 2>&1 && df -BG /data >/dev/null 2>&1; then
  DATA_AVAIL_GIB="$(df -BG /data | awk 'NR==2 {gsub("G","",$4); print $4}')"
  if [ "${DATA_AVAIL_GIB:-0}" -lt 200 ]; then
    echo "[rq3] ABORT: /data available ${DATA_AVAIL_GIB}GiB < 200GiB" | tee "${LOG_ROOT}/ABORT"; exit 2
  fi
fi
if [ -r /proc/meminfo ]; then
  MEM_AVAILABLE_KIB="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
  if [ "${MEM_AVAILABLE_KIB:-0}" -lt 83886080 ]; then
    echo "[rq3] ABORT: MemAvailable ${MEM_AVAILABLE_KIB}KiB < 80GiB" | tee "${LOG_ROOT}/ABORT"; exit 2
  fi
fi

cd "${ROOT}"
nice -n 10 cargo build --release --bin w5_steady_state_real_store 2>&1 | tee "${LOG_ROOT}/build.log"

arm_compact_flags() {
  case "$1" in
    feedback) echo "--compact-best-every-secs ${COMPACT_SECS} --full-compact-every-secs 0" ;;
    full)     echo "--compact-best-every-secs 0 --full-compact-every-secs ${COMPACT_SECS}" ;;
    none)     echo "--compact-best-every-secs 0 --full-compact-every-secs 0" ;;
    *)        echo "UNKNOWN_ARM" ;;
  esac
}

run_arm() {
  local arm="$1"
  local flags; flags="$(arm_compact_flags "${arm}")"
  if [[ "${flags}" == "UNKNOWN_ARM" ]]; then
    echo "[rq3] ABORT: unknown arm ${arm}" | tee "${LOG_ROOT}/ABORT"; exit 2
  fi
  local store="${STORE_PARENT}/${RUN_ID}-${arm}"
  local output="${LOG_ROOT}/${arm}.jsonl"

  echo "[rq3] arm=${arm} flags='${flags}' store=${store}"
  if [[ -e "${store}" ]]; then
    if [[ "${RQ3_FORCE_RESET:-0}" == "1" ]]; then
      rm -rf "${store}"
    else
      echo "[rq3] ABORT: arm store exists: ${store} (set RQ3_FORCE_RESET=1)" | tee "${LOG_ROOT}/ABORT"; exit 2
    fi
  fi
  echo "[rq3] copying base store -> ${store}"
  cp -a --reflink=auto "${BASE_STORE}" "${store}"

  set +e
  # shellcheck disable=SC2086
  timeout $((DURATION + 1200)) nice -n 10 "${BIN}" \
    --data-dir "${store}" \
    --input "${DATA_ROOT}" \
    --variant semantic \
    --duration-secs "${DURATION}" \
    --checkpoint-secs "${CHECKPOINT_SECS}" \
    --query-rate-per-sec "${QUERY_RATE}" \
    --writes-per-sec "${WRITE_RATE}" \
    --flush-every-writes "${FLUSH_EVERY_WRITES}" \
    ${flags} \
    --l0-ra-range-bucket-size "${L0_RA_RANGE_BUCKET_SIZE}" \
    --l0-ra-min-queries "${L0_RA_MIN_QUERIES}" \
    --l0-ra-min-l0-segments "${L0_RA_MIN_L0_SEGMENTS}" \
    --l0-ra-min-score "${L0_RA_MIN_SCORE}" \
    --semantic-budget-min-edge-type-bytes "${SB_MIN_EDGE_TYPE_BYTES}" \
    --semantic-budget-min-edge-type-score "${SB_MIN_EDGE_TYPE_SCORE}" \
    --semantic-budget-min-exact-bytes "${SB_MIN_EXACT_BYTES}" \
    --semantic-budget-min-benefit-score "${SB_MIN_BENEFIT_SCORE}" \
    --abort-min-queries-per-checkpoint "${ABORT_MIN_QUERIES}" \
    --abort-max-write-op-ms "${ABORT_MAX_WRITE_OP_MS}" \
    --output "${output}" \
    > "${LOG_ROOT}/${arm}.stdout.log" \
    2> "${LOG_ROOT}/${arm}.stderr.log"
  local status=$?
  set -e
  if [[ "${status}" -ne 0 ]]; then
    echo "[rq3] FAILED arm=${arm} exit=${status}" | tee -a "${LOG_ROOT}/FAILED"
    tail -n 20 "${LOG_ROOT}/${arm}.stderr.log" || true
    exit "${status}"
  fi
  echo "[rq3] arm=${arm} done -> ${output}"
}

for arm in ${ARMS}; do
  run_arm "${arm}"
done

SUMMARY="${SUMMARY:-${LOG_ROOT}/summary.md}"
python3 "${ROOT}/baseline/summarize_rq3_feedback_tail_20260623.py" \
  --log-root "${LOG_ROOT}" \
  --arms "${ARMS}" \
  --output "${SUMMARY}" \
  --source-label "${LOG_ROOT}" || echo "[rq3] WARN: summarizer failed (raw JSONL still present)"

touch "${LOG_ROOT}/DONE"
echo "[rq3] DONE ${LOG_ROOT}"
echo "[rq3] summary=${SUMMARY}"
