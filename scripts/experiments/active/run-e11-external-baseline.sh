#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

DATE_TAG="${DATE_TAG:-$(date +%Y%m%d)-$(openssl rand -hex 3)}"
OUTPUT_DIR="remote-logs/e11-external-baseline-${DATE_TAG}"
DATA="${DATA:-/data/WorkSpace/dgs/data/social_network_tugraph}"

mkdir -p "$OUTPUT_DIR"

{
  echo "E11 External Baseline Run"
  echo "date=$(date -Is)"
  echo "date_tag=${DATE_TAG}"
  echo "output_dir=${OUTPUT_DIR}"
  echo "data=${DATA}"
} > "${OUTPUT_DIR}/run.meta"

log_cmd() {
  local label="$1"
  local dir="$2"
  shift 2
  mkdir -p "$dir"
  echo "[$(date -Is)] start ${label}" | tee -a "${OUTPUT_DIR}/progress.log"
  set +e
  "$@" > "${dir}/stdout.log" 2> "${dir}/stderr.log"
  local status=$?
  set -e
  echo "$status" > "${dir}/exit-code.txt"
  echo "[$(date -Is)] finish ${label} status=${status}" | tee -a "${OUTPUT_DIR}/progress.log"
  return "$status"
}

has_livegraph() {
  [[ -d "/data/WorkSpace/LiveGraph" ]]
}

has_teseo() {
  [[ -d "/data/WorkSpace/teseo" ]]
}

has_graphone() {
  [[ -d "/data/WorkSpace/GraphOne" ]]
}

has_llama() {
  [[ -d "/data/WorkSpace/LLAMA" ]]
}

if ! has_livegraph && ! has_teseo && ! has_graphone && ! has_llama; then
  {
    echo ""
    echo "## Result"
    echo "status=NO_EXTERNAL_SYSTEMS"
    echo "message=No external graph systems (LiveGraph, Teseo, GraphOne, LLAMA) are available."
    echo "artifacts="
  } >> "${OUTPUT_DIR}/run.meta"

  echo "FAIL" > "${OUTPUT_DIR}/FAIL"
  echo "No external systems found. Check check-e11-external-systems.sh output for details."
  echo "Report written to: ${OUTPUT_DIR}/run.meta"
  exit 1
fi

if has_livegraph; then
  log_cmd "livegraph" "${OUTPUT_DIR}/livegraph" \
    bash /data/WorkSpace/LiveGraph/scripts/benchmark-snb.sh \
      "$DATA" \
      "${OUTPUT_DIR}/livegraph" || true
fi

if has_teseo; then
  log_cmd "teseo" "${OUTPUT_DIR}/teseo" \
    bash /data/WorkSpace/teseo/scripts/benchmark-snb.sh \
      "$DATA" \
      "${OUTPUT_DIR}/teseo" || true
fi

if has_graphone; then
  log_cmd "graphone" "${OUTPUT_DIR}/graphone" \
    bash /data/WorkSpace/GraphOne/scripts/benchmark-snb.sh \
      "$DATA" \
      "${OUTPUT_DIR}/graphone" || true
fi

if has_llama; then
  log_cmd "llama" "${OUTPUT_DIR}/llama" \
    bash /data/WorkSpace/LLAMA/scripts/benchmark-snb.sh \
      "$DATA" \
      "${OUTPUT_DIR}/llama" || true
fi

{
  echo ""
  echo "## Result"
  echo "status=COMPLETED"
  echo "message=External baseline run completed."
  echo "artifacts=livegraph=${OUTPUT_DIR}/livegraph teseo=${OUTPUT_DIR}/teseo graphone=${OUTPUT_DIR}/graphone llama=${OUTPUT_DIR}/llama"
} >> "${OUTPUT_DIR}/run.meta"

echo "PASS" > "${OUTPUT_DIR}/PASS"
echo "External baseline run complete. Results in: ${OUTPUT_DIR}"
echo "date_tag=${DATE_TAG}"
