#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
RUN_ID="${RUN_ID:-w13-schema-evolution-$(date +%Y%m%d-%H%M%S)}"
LOG_ROOT="${LOG_ROOT:-${ROOT}/remote-logs/${RUN_ID}}"
CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-${ROOT}/target}"
MIN_FREE_GIB="${MIN_FREE_GIB:-200}"
MIN_MEM_GIB="${MIN_MEM_GIB:-80}"
TEST_TIMEOUT_SECONDS="${TEST_TIMEOUT_SECONDS:-1800}"

mkdir -p "${LOG_ROOT}"

on_error() {
  local status=$?
  {
    echo "date=$(date -Is)"
    echo "status=${status}"
  } > "${LOG_ROOT}/FAILED"
  echo "[w13] FAILED status=${status} log_root=${LOG_ROOT}" >&2
  exit "${status}"
}
trap on_error ERR

cd "${ROOT}"

data_free_gib() {
  df -BG /data/WorkSpace | awk 'NR==2 { gsub(/G/, "", $4); print int($4) }'
}

mem_available_gib() {
  awk '/MemAvailable:/ { printf "%d\n", $2 / 1024 / 1024 }' /proc/meminfo
}

resource_gate() {
  local free_gib mem_gib
  free_gib="$(data_free_gib)"
  mem_gib="$(mem_available_gib)"
  echo "[w13] resource free_data_gib=${free_gib} mem_available_gib=${mem_gib}"
  if (( free_gib < MIN_FREE_GIB )); then
    echo "[w13] abort: /data free ${free_gib}GiB < ${MIN_FREE_GIB}GiB" >&2
    exit 2
  fi
  if (( mem_gib < MIN_MEM_GIB )); then
    echo "[w13] abort: MemAvailable ${mem_gib}GiB < ${MIN_MEM_GIB}GiB" >&2
    exit 2
  fi
}

record_meta() {
  {
    echo "date=$(date -Is)"
    echo "pwd=$(pwd)"
    echo "run_id=${RUN_ID}"
    echo "log_root=${LOG_ROOT}"
    echo "cargo_target_dir=${CARGO_TARGET_DIR}"
    git rev-parse HEAD || true
    git status --short || true
    rustc --version || true
    cargo --version || true
    df -h /data /data/WorkSpace /tmp || true
    awk '/MemAvailable:/ {print}' /proc/meminfo || true
  } > "${LOG_ROOT}/run.meta"
}

quote_cmd() {
  printf '%q ' "$@"
  printf '\n'
}

run_test() {
  local test_name="$1"
  local safe_name="${test_name//[^A-Za-z0-9_]/_}"
  local test_dir="${LOG_ROOT}/${safe_name}"
  mkdir -p "${test_dir}"
  resource_gate
  quote_cmd timeout "${TEST_TIMEOUT_SECONDS}" env CARGO_TARGET_DIR="${CARGO_TARGET_DIR}" cargo test --test engine_tests "${test_name}" -- --nocapture > "${test_dir}/command.txt"
  echo "[w13] start test=${test_name} ts=$(date -Is)" | tee -a "${LOG_ROOT}/progress.log"
  set +e
  /usr/bin/time -v timeout "${TEST_TIMEOUT_SECONDS}" env CARGO_TARGET_DIR="${CARGO_TARGET_DIR}" \
    cargo test --test engine_tests "${test_name}" -- --nocapture \
    > "${test_dir}/stdout.log" 2> "${test_dir}/stderr.log"
  local status=$?
  set -e
  echo "${status}" > "${test_dir}/exit-code.txt"
  echo "[w13] finish test=${test_name} status=${status} ts=$(date -Is)" | tee -a "${LOG_ROOT}/progress.log"
  if (( status != 0 )); then
    tail -120 "${test_dir}/stderr.log" >&2 || true
    exit "${status}"
  fi
  if grep -q "running 0 tests" "${test_dir}/stdout.log" "${test_dir}/stderr.log"; then
    echo "[w13] test filter matched zero tests: ${test_name}" >&2
    exit 7
  fi
  printf "%s\tpass\t%s\n" "${test_name}" "${test_dir}" >> "${LOG_ROOT}/tests.tsv"
}

TESTS=(
  schema_epoch_snapshot_mixed_delta_survives_compaction_and_reopen
  property_schema_snapshot_mixed_delta_survives_compaction_and_reopen
  schema_epoch_change_keeps_old_segments_readable
  schema_catalog_persists_changes_and_drives_new_segment_epoch
  schema_evolution_report_summarizes_epoch_and_pruning_boundaries
  property_encoding_change_advances_catalog_and_segment_epochs
  public_property_value_query_resolves_edge_label_alias
  public_property_value_query_uses_row_encoding_epoch_after_type_change
  drop_property_hides_current_value_query_but_keeps_topology_readable
  new_edge_label_prunes_exact_segments_but_reads_mixed_segments
)

echo "[w13] run_id=${RUN_ID}"
echo "[w13] log_root=${LOG_ROOT}"
echo "[w13] ETA: minutes to low hours depending on cargo rebuild state; per-test timeout=${TEST_TIMEOUT_SECONDS}s"
echo "[w13] abort: /data free <${MIN_FREE_GIB}GiB, MemAvailable <${MIN_MEM_GIB}GiB, test failure, or zero-test filter"

record_meta
resource_gate

printf "test\tstatus\tlog_dir\n" > "${LOG_ROOT}/tests.tsv"
for test_name in "${TESTS[@]}"; do
  run_test "${test_name}"
done

{
  echo "# W13 Schema Evolution Summary"
  echo
  echo "run_id=${RUN_ID}"
  echo "log_root=${LOG_ROOT}"
  echo
  echo "## Tests"
  echo
  cat "${LOG_ROOT}/tests.tsv"
  echo
  echo "## Claim Mapping"
  echo
  echo "- schema epochs advance on logical schema changes: covered by schema catalog/report tests."
  echo "- old segments remain readable under stored schema_epoch: covered by mixed epoch and old-segment readability tests."
  echo "- alias/drop behavior is explicit: covered by public edge-label alias and drop-property tests."
  echo "- property encoding epochs remain decodable inside the implemented fixed-width boundary: covered by property encoding tests."
  echo "- exact pruning remains conservative under new edge labels and mixed metadata: covered by new-edge-label pruning test."
} > "${LOG_ROOT}/summary.md"

date -Is > "${LOG_ROOT}/DONE"
echo "[w13] DONE ${LOG_ROOT}"
