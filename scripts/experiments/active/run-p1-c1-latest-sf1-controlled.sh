#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

DATE_TAG="${DATE_TAG:-20260603}"
BIN="${BIN:-target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
DATA="${DATA:-/data/WorkSpace/dgs/data/social_network_tugraph}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-1048576}"
BUILD_RELEASE="${BUILD_RELEASE:-true}"

CORE_EDGE_TYPES="1,2,3,7,8,9,10,11,12"
ALL_EDGE_TYPES="-17,-16,-15,-14,-13,-12,-11,-10,-9,-8,-7,-6,-5,-4,-3,-2,-1,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17"
LDBC_QUERIES="ic4,ic5,ic6,ic10,ic11,ic13,ic14,is3"

DEFAULT_EXACT_BYTES="${DEFAULT_EXACT_BYTES:-4194304}"
DEFAULT_BENEFIT_SCORE="${DEFAULT_BENEFIT_SCORE:-1.0}"
DEFAULT_DEGREE_WEIGHT="${DEFAULT_DEGREE_WEIGHT:-1.0}"

PLAN_DIR="remote-logs/p1-c1-sample-plans-${DATE_TAG}"
CORE_PLAN="${PLAN_DIR}/sf1-core-s200.plan.json"
ALLTYPES_PLAN="${PLAN_DIR}/sf1-alltypes-s50.plan.json"

SCHEMA_STORE="store/p1-c1-sf1-schema-${DATE_TAG}"
SEMANTIC_STORE="store/p1-c1-sf1-semantic-${DATE_TAG}"
BUDGETED_STORE="store/p1-c1-sf1-budgeted-${DATE_TAG}"

SCHEMA_LOG="remote-logs/p1-c1-sf1-schema-${DATE_TAG}"
SEMANTIC_LOG="remote-logs/p1-c1-sf1-semantic-${DATE_TAG}"
BUDGETED_LOG="remote-logs/p1-c1-sf1-budgeted-${DATE_TAG}"

require_input() {
  if [[ ! -d "$DATA/dynamic" || ! -d "$DATA/static" ]]; then
    echo "missing expected LDBC dynamic/static directories under ${DATA}" >&2
    exit 2
  fi
}

build_binary() {
  if [[ "$BUILD_RELEASE" == "true" ]]; then
    mkdir -p "remote-logs/p1-c1-build-${DATE_TAG}"
    cargo build --release \
      > "remote-logs/p1-c1-build-${DATE_TAG}/build.stdout" \
      2> "remote-logs/p1-c1-build-${DATE_TAG}/build.stderr"
  fi
  if [[ ! -x "$BIN" ]]; then
    echo "binary not found or not executable: ${BIN}" >&2
    exit 3
  fi
}

record_meta() {
  local logdir="$1"
  mkdir -p "$logdir"
  {
    echo "date=$(date -Is)"
    echo "pwd=$(pwd)"
    echo "binary=${BIN}"
    echo "io_backend=${IO_BACKEND}"
    echo "data=${DATA}"
    echo "memgraph_bytes=${MEMGRAPH_BYTES}"
    echo "semantic_budget_min_exact_bytes=${DEFAULT_EXACT_BYTES}"
    echo "semantic_budget_min_benefit_score=${DEFAULT_BENEFIT_SCORE}"
    echo "semantic_budget_degree_weight=${DEFAULT_DEGREE_WEIGHT}"
    "${BIN}" --version || true
  } > "${logdir}/run.meta" 2>&1
  git rev-parse HEAD > "${logdir}/git-head.txt" 2> "${logdir}/git-head.err" || true
  git status --short > "${logdir}/git-status.txt" 2> "${logdir}/git-status.err" || true
}

import_complete() {
  local logdir="$1"
  [[ -s "${logdir}/import.stdout" ]] && grep -q '"snapshot"' "${logdir}/import.stdout"
}

write_file_summary() {
  local store="$1"
  local logdir="$2"
  {
    printf "store\t%s\n" "$store"
    find "$store" -type f | wc -l | awk '{printf "files\t%s\n", $1}'
    find "$store" -type f -printf '%s\n' | awk '{s+=$1} END{printf "bytes\t%.0f\n", s}'
    if [[ -d "${store}/levels/L0" ]]; then
      find "${store}/levels/L0" -type f | wc -l | awk '{printf "l0_files\t%s\n", $1}'
      find "${store}/levels/L0" -type f -printf '%s\n' | awk '{s+=$1} END{printf "l0_bytes\t%.0f\n", s}'
    else
      printf "l0_files\t0\n"
      printf "l0_bytes\t0\n"
    fi
    if [[ -f "${store}/MANIFEST" ]]; then
      stat -c 'manifest_bytes	%s' "${store}/MANIFEST"
    fi
  } > "${logdir}/file-summary.tsv"
}

run_import() {
  local name="$1"
  local layout="$2"
  local store="$3"
  local logdir="$4"

  mkdir -p "$logdir"
  record_meta "$logdir"
  if import_complete "$logdir"; then
    echo "skip completed import ${name}"
  else
    if [[ -e "$store" ]]; then
      echo "store exists but import is not marked complete: ${store}" >&2
      exit 4
    fi
    echo "start import ${name} layout=${layout}"
    /usr/bin/time -v -o "${logdir}/time.log" \
      "${BIN}" --io-backend "$IO_BACKEND" import \
        --input "$DATA" \
        --data-dir "$store" \
        --relation snb-full \
        --memgraph-bytes "$MEMGRAPH_BYTES" \
        --l0-layout "$layout" \
        --semantic-budget-min-exact-bytes "$DEFAULT_EXACT_BYTES" \
        --semantic-budget-min-benefit-score "$DEFAULT_BENEFIT_SCORE" \
        --semantic-budget-degree-weight "$DEFAULT_DEGREE_WEIGHT" \
        > "${logdir}/import.stdout" 2> "${logdir}/import.stderr"
    echo "finish import ${name}"
  fi

  if [[ ! -s "${logdir}/stats.log" ]]; then
    "${BIN}" --io-backend "$IO_BACKEND" stats \
      --data-dir "$store" \
      > "${logdir}/stats.log" 2> "${logdir}/stats.err"
  fi
  write_file_summary "$store" "$logdir"
}

run_storage_with_plan_out() {
  local store="$1"
  local logdir="$2"
  local kind="$3"
  local edge_types="$4"
  local samples="$5"
  local plan="$6"
  local out="${logdir}/fair-${kind}-s${samples}-r1.json"
  local err="${logdir}/fair-${kind}-s${samples}-r1.err"

  if [[ -s "$out" && -s "$plan" ]]; then
    echo "skip storage ${kind} plan generation ${logdir}"
    return
  fi
  mkdir -p "$(dirname "$plan")"
  "${BIN}" --io-backend "$IO_BACKEND" storage-bench \
    --data-dir "$store" \
    --edge-types="$edge_types" \
    --samples "$samples" \
    --semantic-degree-hint \
    --sample-plan-out "$plan" \
    > "$out" 2> "$err"
}

run_storage_with_plan_in() {
  local store="$1"
  local logdir="$2"
  local kind="$3"
  local edge_types="$4"
  local samples="$5"
  local repeat="$6"
  local plan="$7"
  local out="${logdir}/fair-${kind}-s${samples}-r${repeat}.json"
  local err="${logdir}/fair-${kind}-s${samples}-r${repeat}.err"

  if [[ -s "$out" ]]; then
    echo "skip storage ${kind} r${repeat} ${logdir}"
    return
  fi
  if [[ ! -s "$plan" ]]; then
    echo "missing sample plan: ${plan}" >&2
    exit 5
  fi
  "${BIN}" --io-backend "$IO_BACKEND" storage-bench \
    --data-dir "$store" \
    --edge-types="$edge_types" \
    --semantic-degree-hint \
    --sample-plan-in "$plan" \
    > "$out" 2> "$err"
}

run_ldbc() {
  local store="$1"
  local logdir="$2"
  local repeat="$3"
  local out="${logdir}/ldbc-passing-s3-r${repeat}.json"
  local err="${logdir}/ldbc-passing-s3-r${repeat}.err"

  if [[ -s "$out" ]]; then
    echo "skip ldbc r${repeat} ${logdir}"
    return
  fi
  "${BIN}" --io-backend "$IO_BACKEND" snb-validate-batch \
    --data-dir "$store" \
    --queries "$LDBC_QUERIES" \
    --max-lines-per-query 3 \
    > "$out" 2> "$err"
}

run_schema_reads() {
  run_storage_with_plan_out "$SCHEMA_STORE" "$SCHEMA_LOG" core "$CORE_EDGE_TYPES" 200 "$CORE_PLAN"
  run_storage_with_plan_out "$SCHEMA_STORE" "$SCHEMA_LOG" alltypes "$ALL_EDGE_TYPES" 50 "$ALLTYPES_PLAN"
  run_ldbc "$SCHEMA_STORE" "$SCHEMA_LOG" 1
  for repeat in 2 3; do
    run_storage_with_plan_in "$SCHEMA_STORE" "$SCHEMA_LOG" core "$CORE_EDGE_TYPES" 200 "$repeat" "$CORE_PLAN"
    run_storage_with_plan_in "$SCHEMA_STORE" "$SCHEMA_LOG" alltypes "$ALL_EDGE_TYPES" 50 "$repeat" "$ALLTYPES_PLAN"
    run_ldbc "$SCHEMA_STORE" "$SCHEMA_LOG" "$repeat"
  done
}

run_layout_reads() {
  local store="$1"
  local logdir="$2"
  for repeat in 1 2 3; do
    run_storage_with_plan_in "$store" "$logdir" core "$CORE_EDGE_TYPES" 200 "$repeat" "$CORE_PLAN"
    run_storage_with_plan_in "$store" "$logdir" alltypes "$ALL_EDGE_TYPES" 50 "$repeat" "$ALLTYPES_PLAN"
    run_ldbc "$store" "$logdir" "$repeat"
  done
}

run_neighbor_compare() {
  local right_name="$1"
  local right_store="$2"
  local out_core="remote-logs/p1-c1-sf1-neighbor-compare-${right_name}-core-${DATE_TAG}.json"
  local out_all="remote-logs/p1-c1-sf1-neighbor-compare-${right_name}-alltypes-${DATE_TAG}.json"

  "${BIN}" --io-backend "$IO_BACKEND" neighbor-compare \
    --left-data-dir "$SCHEMA_STORE" \
    --right-data-dir "$right_store" \
    --sample-plan "$CORE_PLAN" \
    --right-semantic-degree-hint \
    > "$out_core" 2> "${out_core%.json}.err"

  "${BIN}" --io-backend "$IO_BACKEND" neighbor-compare \
    --left-data-dir "$SCHEMA_STORE" \
    --right-data-dir "$right_store" \
    --sample-plan "$ALLTYPES_PLAN" \
    --right-semantic-degree-hint \
    > "$out_all" 2> "${out_all%.json}.err"
}

main() {
  require_input
  build_binary
  mkdir -p "$PLAN_DIR"

  run_import schema schema "$SCHEMA_STORE" "$SCHEMA_LOG"
  run_import semantic semantic "$SEMANTIC_STORE" "$SEMANTIC_LOG"
  run_import budgeted semantic-budgeted "$BUDGETED_STORE" "$BUDGETED_LOG"

  run_schema_reads
  run_layout_reads "$SEMANTIC_STORE" "$SEMANTIC_LOG"
  run_layout_reads "$BUDGETED_STORE" "$BUDGETED_LOG"

  run_neighbor_compare semantic "$SEMANTIC_STORE"
  run_neighbor_compare budgeted "$BUDGETED_STORE"

  echo "P1 SF1 controlled run complete"
  echo "stores:"
  echo "  $SCHEMA_STORE"
  echo "  $SEMANTIC_STORE"
  echo "  $BUDGETED_STORE"
  echo "logs:"
  echo "  $SCHEMA_LOG"
  echo "  $SEMANTIC_LOG"
  echo "  $BUDGETED_LOG"
}

main "$@"

