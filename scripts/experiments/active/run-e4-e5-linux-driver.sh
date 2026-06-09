#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

TAG="${TAG:-e4e5-linux-$(date +%Y%m%d-%H%M%S)}"
ROOT_LOG="remote-logs/e4-e5-${TAG}"
E4_LOG="remote-logs/e4-sustained-feedback-${TAG}"
E5_LOG="remote-logs/e5-schema-delta-stress-${TAG}"
COMMON_TARGET_DIR="${COMMON_TARGET_DIR:-target}"

mkdir -p "$ROOT_LOG" "$E4_LOG" "$E5_LOG"

record_meta() {
  local dir="$1"
  {
    echo "date=$(date -Is)"
    echo "pwd=$(pwd)"
    echo "tag=${TAG}"
    echo "common_target_dir=${COMMON_TARGET_DIR}"
    git rev-parse HEAD || true
    git status --short || true
    rustc --version || true
    cargo --version || true
    df -h /data || true
  } > "${dir}/run.meta"
}

quote_cmd() {
  printf '%q ' "$@"
  printf '\n'
}

run_cmd() {
  local label="$1"
  local dir="$2"
  shift 2
  mkdir -p "$dir"
  quote_cmd "$@" > "${dir}/command.txt"
  echo "[$(date -Is)] start ${label}" | tee -a "${ROOT_LOG}/progress.log"
  set +e
  /usr/bin/time -v "$@" > "${dir}/stdout.log" 2> "${dir}/stderr.log"
  local status=$?
  set -e
  echo "$status" > "${dir}/exit-code.txt"
  echo "[$(date -Is)] finish ${label} status=${status}" | tee -a "${ROOT_LOG}/progress.log"
  if [[ "$status" != "0" ]]; then
    tail -120 "${dir}/stderr.log" >&2 || true
    return "$status"
  fi
}

record_meta "$ROOT_LOG"
record_meta "$E4_LOG"
record_meta "$E5_LOG"

run_cmd "e4 workload-shift feedback" "${E4_LOG}/workload-shift" \
  env \
    DATE_TAG="$TAG" \
    OUT_DIR="$E4_LOG" \
    OUT_JSON="${E4_LOG}/p3-feedback-workload-shift-${TAG}.json" \
    STORE_DIR="target/p3-feedback-workload-shift-store-${TAG}" \
    CARGO_TARGET_DIR="$COMMON_TARGET_DIR" \
    bash scripts/experiments/active/run-p3-feedback-workload-shift-microbench.sh

run_cmd "e4 feedback-vs-no-feedback" "${E4_LOG}/feedback-vs-no-feedback" \
  env \
    DATE_TAG="$TAG" \
    OUT_DIR="$E4_LOG" \
    OUT_JSON="${E4_LOG}/p3-feedback-vs-no-feedback-${TAG}.json" \
    STORE_DIR="target/p3-feedback-store-${TAG}" \
    NO_FEEDBACK_STORE_DIR="target/p3-no-feedback-store-${TAG}" \
    CARGO_TARGET_DIR="$COMMON_TARGET_DIR" \
    bash scripts/experiments/active/run-p3-feedback-vs-no-feedback-microbench.sh

python3 - "$E4_LOG" "$TAG" <<'PY' > "${E4_LOG}/validation-summary.tsv"
import json
import sys
from pathlib import Path

log = Path(sys.argv[1])
tag = sys.argv[2]

def load(name):
    path = log / name
    with path.open(encoding="utf-8") as f:
        return json.load(f)

def validate_feedback(report, require_no_feedback):
    summary = report["summary"]
    checks = {
        "phase_a_l0_decreased": summary["phase_a_candidate_l0_after"] < summary["phase_a_candidate_l0_before"],
        "phase_b_l0_decreased": summary["phase_b_candidate_l0_after"] < summary["phase_b_candidate_l0_before"],
        "selected_ranges_changed": bool(summary["selected_ranges_changed"]),
        "phase_a_compacted": report["phase_a"]["compaction"]["summary"]["compaction_count"] > 0,
        "phase_b_compacted": report["phase_b"]["compaction"]["summary"]["compaction_count"] > 0,
    }
    if require_no_feedback:
        nf = summary.get("no_feedback")
        checks["has_no_feedback_summary"] = nf is not None
        if nf:
            checks["no_feedback_phase_a_keeps_more_l0_than_feedback"] = (
                nf["phase_a_after_candidate_l0_segments"] >= summary["phase_a_candidate_l0_after"]
            )
            checks["no_feedback_phase_b_keeps_more_l0_than_feedback"] = (
                nf["phase_b_after_candidate_l0_segments"] >= summary["phase_b_candidate_l0_after"]
            )
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise SystemExit(f"failed checks: {failed}")
    return checks

workload = load(f"p3-feedback-workload-shift-{tag}.json")
compare = load(f"p3-feedback-vs-no-feedback-{tag}.json")

print("artifact\tcheck\tvalue")
for artifact, checks in [
    ("workload_shift", validate_feedback(workload, False)),
    ("feedback_vs_no_feedback", validate_feedback(compare, True)),
]:
    for key, value in checks.items():
        print(f"{artifact}\t{key}\t{value}")
PY

touch "${E4_LOG}/PASS"

E5_TESTS=(
  delete_tombstone_hides_latest_edge
  snapshot_read_sees_insert_before_later_delete_across_reopen
  compaction_preserves_old_snapshot_after_tombstone
  degree_change_tombstones_keep_explicit_low_degree_query_conservative
  schema_epoch_snapshot_mixed_delta_survives_compaction_and_reopen
  property_schema_snapshot_mixed_delta_survives_compaction_and_reopen
  public_property_value_query_resolves_edge_label_alias
  public_property_value_query_uses_row_encoding_epoch_after_type_change
  drop_property_hides_current_value_query_but_keeps_topology_readable
)

: > "${E5_LOG}/tests.tsv"
printf "test\tstatus\tlog_dir\n" > "${E5_LOG}/tests.tsv"
for test_name in "${E5_TESTS[@]}"; do
  safe_name="${test_name//[^A-Za-z0-9_]/_}"
  test_dir="${E5_LOG}/${safe_name}"
  run_cmd "e5 ${test_name}" "$test_dir" \
    cargo test --test engine_tests "$test_name" -- --nocapture
  if grep -q "running 0 tests" "${test_dir}/stdout.log" "${test_dir}/stderr.log"; then
    echo "test filter matched zero tests: ${test_name}" >&2
    exit 7
  fi
  printf "%s\tpass\t%s\n" "$test_name" "$test_dir" >> "${E5_LOG}/tests.tsv"
done

touch "${E5_LOG}/PASS"

{
  echo "# E4/E5 Linux Driver Summary"
  echo
  echo "tag=${TAG}"
  echo "e4_log=${E4_LOG}"
  echo "e5_log=${E5_LOG}"
  echo
  echo "## E4"
  cat "${E4_LOG}/validation-summary.tsv"
  echo
  echo "## E5"
  cat "${E5_LOG}/tests.tsv"
} > "${ROOT_LOG}/summary.md"

touch "${ROOT_LOG}/PASS"
echo "PASS ${ROOT_LOG}"
