#!/usr/bin/env bash
set -u

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
TS="${TS:-$(date +%Y%m%d-%H%M%S)}"
E0_DIR="${E0_DIR:-remote-logs/e0-code-readiness-${TS}}"
E1_DIR="${E1_DIR:-remote-logs/e1-targeted-correctness-${TS}}"
E0_STORE="${E0_STORE:-store/e0-p3-feedback-smoke-${TS}}"
E0_SEGMENTS_PER_PHASE="${E0_SEGMENTS_PER_PHASE:-2}"
E0_REPEATS="${E0_REPEATS:-2}"
E0_RANGE_BUCKET_SIZE="${E0_RANGE_BUCKET_SIZE:-256}"

cd "$ROOT"

if [[ -e "$E0_DIR" || -e "$E1_DIR" || -e "$E0_STORE" ]]; then
  echo "refusing to overwrite existing E0/E1 output for TS=${TS}" >&2
  exit 2
fi

mkdir -p "$E0_DIR" "$E1_DIR" store

write_source_stamp() {
  local out_dir="$1"
  local title="$2"
  {
    echo "# ${title}"
    echo
    echo "start_time=$(date -Iseconds)"
    echo "workdir=$(pwd)"
    echo "cargo_version=$(cargo --version 2>&1)"
    echo "rustc_version=$(rustc --version 2>&1)"
    echo "build_profile=dev/default cargo profile"
    echo "binary_path=target/debug/p3-feedback-bench"
    echo
    echo "## Source Status"
    if command -v git >/dev/null 2>&1; then
      echo "git_head=$(git rev-parse HEAD 2>&1)"
      echo 'git_status_short<<EOF'
      git status --short 2>&1
      echo EOF
    else
      echo "git_status=git unavailable"
    fi
    echo
    echo "## Source Hashes"
    sha256sum \
      Cargo.toml \
      Cargo.lock \
      src/graph.rs \
      src/schema.rs \
      src/csr/format.rs \
      src/csr/writer.rs \
      src/csr/reader.rs \
      src/property_encoding.rs \
      src/bin/p3_feedback_bench.rs \
      tests/engine_tests.rs \
      scripts/experiments/active/run-e0-e1-readiness.sh \
      2>&1
    echo
    echo "## Commands"
  } > "${out_dir}/summary.md"
}

run_logged() {
  local out_dir="$1"
  local name="$2"
  shift 2
  local cmd=("$@")
  local stdout="${out_dir}/${name}.stdout.log"
  local stderr="${out_dir}/${name}.stderr.log"

  {
    printf -- "- %q" "${cmd[0]}"
    for arg in "${cmd[@]:1}"; do
      printf " %q" "$arg"
    done
    echo
    echo "  start=$(date -Iseconds)"
  } >> "${out_dir}/summary.md"

  "${cmd[@]}" > "$stdout" 2> "$stderr"
  local code=$?

  {
    echo "  exit_code=${code}"
    echo "  end=$(date -Iseconds)"
    echo "  stdout=${stdout}"
    echo "  stderr=${stderr}"
  } >> "${out_dir}/summary.md"

  return "$code"
}

finish_dir() {
  local out_dir="$1"
  local failed="$2"
  {
    echo
    echo "end_time=$(date -Iseconds)"
    echo "failed=${failed}"
  } >> "${out_dir}/summary.md"

  if [[ "$failed" -eq 0 ]]; then
    echo "PASS" > "${out_dir}/PASS"
  else
    echo "FAIL" > "${out_dir}/FAIL"
  fi
}

write_source_stamp "$E0_DIR" "E0 Code Readiness Gate"
{
  echo "store_dir=${E0_STORE}"
  echo "p3_feedback_output=${E0_DIR}/p3-feedback-smoke.json"
  echo "p3_feedback_flags=--segments-per-phase ${E0_SEGMENTS_PER_PHASE} --repeats ${E0_REPEATS} --range-bucket-size ${E0_RANGE_BUCKET_SIZE}"
  echo
} >> "${E0_DIR}/summary.md"

e0_failed=0
run_logged "$E0_DIR" fmt-check cargo fmt --check || e0_failed=1
run_logged "$E0_DIR" unit-semantic-degree-override \
  cargo test semantic_l0_index_degree_override_keeps_lower_class_mixed_segments --lib -- --nocapture || e0_failed=1
run_logged "$E0_DIR" engine-semantic-degree \
  cargo test --test engine_tests semantic_l0_degree_pruning_keeps_edges_across_flushes -- --nocapture || e0_failed=1
run_logged "$E0_DIR" engine-incremental-index \
  cargo test --test engine_tests incremental_semantic_index_matches_reopen_full_rebuild -- --nocapture || e0_failed=1
run_logged "$E0_DIR" engine-degree-tombstone \
  cargo test --test engine_tests degree_change_tombstones_keep_explicit_low_degree_query_conservative -- --nocapture || e0_failed=1
run_logged "$E0_DIR" p3-feedback-bench-smoke \
  cargo run --bin p3-feedback-bench -- \
    --reset-store \
    --store-dir "$E0_STORE" \
    --output "${E0_DIR}/p3-feedback-smoke.json" \
    --segments-per-phase "$E0_SEGMENTS_PER_PHASE" \
    --repeats "$E0_REPEATS" \
    --range-bucket-size "$E0_RANGE_BUCKET_SIZE" || e0_failed=1
finish_dir "$E0_DIR" "$e0_failed"

if [[ "$e0_failed" -ne 0 ]]; then
  echo "E0 failed: ${E0_DIR}" >&2
  exit 1
fi

write_source_stamp "$E1_DIR" "E1 Targeted Correctness Suite"
e1_failed=0
run_logged "$E1_DIR" engine-tests-all cargo test --test engine_tests -- --nocapture || e1_failed=1
finish_dir "$E1_DIR" "$e1_failed"

if [[ "$e1_failed" -ne 0 ]]; then
  echo "E1 failed: ${E1_DIR}" >&2
  exit 1
fi

echo "E0_PASS_DIR=${E0_DIR}"
echo "E1_PASS_DIR=${E1_DIR}"
