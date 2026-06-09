#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

BASE_DATE_TAG="${BASE_DATE_TAG:-20260604}"
DATE_TAG="${DATE_TAG:-20260604}"
BIN="${BIN:-target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
DATA="${DATA:-/data/WorkSpace/dgs/data/social_network_tugraph}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-1048576}"

CORE_EDGE_TYPES="1,2,3,7,8,9,10,11,12"
ALL_EDGE_TYPES="-17,-16,-15,-14,-13,-12,-11,-10,-9,-8,-7,-6,-5,-4,-3,-2,-1,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17"

SCHEMA_STORE="store/p1-c1-sf1-schema-${BASE_DATE_TAG}"
CORE_PLAN="remote-logs/p1-c1-sample-plans-${BASE_DATE_TAG}/sf1-core-s200.plan.json"
ALLTYPES_PLAN="remote-logs/p1-c1-sample-plans-${BASE_DATE_TAG}/sf1-alltypes-s50.plan.json"

require_base() {
  for path in "$SCHEMA_STORE" "$CORE_PLAN" "$ALLTYPES_PLAN"; do
    if [[ ! -e "$path" ]]; then
      echo "missing required baseline artifact: $path" >&2
      exit 2
    fi
  done
}

record_meta() {
  local logdir="$1"
  local edge_type_bytes="$2"
  local exact="$3"
  local score="$4"
  local weight="$5"
  mkdir -p "$logdir"
  {
    echo "date=$(date -Is)"
    echo "pwd=$(pwd)"
    echo "binary=${BIN}"
    echo "io_backend=${IO_BACKEND}"
    echo "data=${DATA}"
    echo "baseline_schema_store=${SCHEMA_STORE}"
    echo "core_plan=${CORE_PLAN}"
    echo "alltypes_plan=${ALLTYPES_PLAN}"
    echo "memgraph_bytes=${MEMGRAPH_BYTES}"
    echo "semantic_budget_min_edge_type_bytes=${edge_type_bytes}"
    echo "semantic_budget_min_exact_bytes=${exact}"
    echo "semantic_budget_min_benefit_score=${score}"
    echo "semantic_budget_degree_weight=${weight}"
  } > "${logdir}/run.meta"
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
    if [[ -f "${store}/MANIFEST" ]]; then
      grep -c 'degree_class_exact":true' "${store}/MANIFEST" | awk '{printf "degree_exact_files\t%s\n", $1}'
    fi
  } > "${logdir}/file-summary.tsv"
}

run_variant() {
  local name="$1"
  local edge_type_bytes="$2"
  local exact="$3"
  local score="$4"
  local weight="$5"
  local store="store/p1-c1-sf1-budgeted-${name}-${DATE_TAG}"
  local logdir="remote-logs/p1-c1-sf1-budgeted-${name}-${DATE_TAG}"

  mkdir -p "$logdir"
  record_meta "$logdir" "$edge_type_bytes" "$exact" "$score" "$weight"
  if import_complete "$logdir"; then
    echo "skip completed import ${name}"
  else
    if [[ -e "$store" ]]; then
      echo "store exists but import is not marked complete: ${store}" >&2
      exit 3
    fi
    /usr/bin/time -v -o "${logdir}/time.log" \
      "$BIN" --io-backend "$IO_BACKEND" import \
        --input "$DATA" \
        --data-dir "$store" \
        --relation snb-full \
        --memgraph-bytes "$MEMGRAPH_BYTES" \
        --l0-layout semantic-budgeted \
        --semantic-budget-min-edge-type-bytes "$edge_type_bytes" \
        --semantic-budget-min-exact-bytes "$exact" \
        --semantic-budget-min-benefit-score "$score" \
        --semantic-budget-degree-weight "$weight" \
        > "${logdir}/import.stdout" 2> "${logdir}/import.stderr"
  fi

  "$BIN" --io-backend "$IO_BACKEND" stats \
    --data-dir "$store" \
    > "${logdir}/stats.log" 2> "${logdir}/stats.err"
  write_file_summary "$store" "$logdir"

  "$BIN" --io-backend "$IO_BACKEND" storage-bench \
    --data-dir "$store" \
    --edge-types="$CORE_EDGE_TYPES" \
    --semantic-degree-hint \
    --sample-plan-in "$CORE_PLAN" \
    > "${logdir}/fair-core-s200.json" 2> "${logdir}/fair-core-s200.err"

  "$BIN" --io-backend "$IO_BACKEND" storage-bench \
    --data-dir "$store" \
    --edge-types="$ALL_EDGE_TYPES" \
    --semantic-degree-hint \
    --sample-plan-in "$ALLTYPES_PLAN" \
    > "${logdir}/fair-alltypes-s50.json" 2> "${logdir}/fair-alltypes-s50.err"

  "$BIN" --io-backend "$IO_BACKEND" neighbor-compare \
    --left-data-dir "$SCHEMA_STORE" \
    --right-data-dir "$store" \
    --sample-plan "$CORE_PLAN" \
    --right-semantic-degree-hint \
    > "${logdir}/neighbor-compare-core.json" 2> "${logdir}/neighbor-compare-core.err"

  "$BIN" --io-backend "$IO_BACKEND" neighbor-compare \
    --left-data-dir "$SCHEMA_STORE" \
    --right-data-dir "$store" \
    --sample-plan "$ALLTYPES_PLAN" \
    --right-semantic-degree-hint \
    > "${logdir}/neighbor-compare-alltypes.json" 2> "${logdir}/neighbor-compare-alltypes.err"
}

main() {
  require_base
  run_variant edge1m_exact1m_score05_weight1 1048576 1048576 0.5 1.0
  run_variant edge1m_exact1m_score025_weight1 1048576 1048576 0.25 1.0
  run_variant edge4m_exact4m_score05_weight2 4194304 4194304 0.5 2.0
  run_variant edge0_exact0_score0_weight1 0 0 0.0 1.0
}

main "$@"
