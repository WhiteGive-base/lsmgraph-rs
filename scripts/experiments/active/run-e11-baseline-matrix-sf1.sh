#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

DATE_TAG="${DATE_TAG:-$(date +%Y%m%d)-$(openssl rand -hex 3)}"
BIN="${BIN:-target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
DATA="${DATA:-/data/WorkSpace/dgs/data/social_network_tugraph}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-1048576}"
BUILD_RELEASE="${BUILD_RELEASE:-true}"

CORE_EDGE_TYPES="1,2,3,7,8,9,10,11,12"
ALL_EDGE_TYPES="-17,-16,-15,-14,-13,-12,-11,-10,-9,-8,-7,-6,-5,-4,-3,-2,-1,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17"
LDBC_QUERIES="ic4,ic5,ic6,ic10,ic11,ic13,ic14,is3"

declare -A VARIANTS=(
  [naive]="naive"
  [lsmgraph-style]="lsmgraph-style"
  [schema-only]="schema"
  [label-only]="label-only"
  [edge-type-only]="edge-type-only"
  [degree-only]="degree-only"
  [full-semantic]="semantic"
  [benefit-scored]="semantic-budgeted"
  [full-compact]="full-compact"
)

PLAN_DIR="remote-logs/e11-sample-plans-${DATE_TAG}"
CORE_PLAN="${PLAN_DIR}/sf1-core-s200.plan.json"
ALLTYPES_PLAN="${PLAN_DIR}/sf1-alltypes-s50.plan.json"

require_input() {
  if [[ ! -d "$DATA/dynamic" || ! -d "$DATA/static" ]]; then
    echo "missing expected LDBC dynamic/static directories under ${DATA}" >&2
    exit 2
  fi
}

build_binary() {
  if [[ "$BUILD_RELEASE" == "true" ]]; then
    mkdir -p "remote-logs/e11-build-${DATE_TAG}"
    cargo build --release \
      > "remote-logs/e11-build-${DATE_TAG}/build.stdout" \
      2> "remote-logs/e11-build-${DATE_TAG}/build.stderr"
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
    echo "date_tag=${DATE_TAG}"
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
      stat -c 'manifest_bytes\t%s' "${store}/MANIFEST"
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

generate_sample_plans() {
  local schema_logdir="$1"
  local schema_store="$2"

  mkdir -p "$PLAN_DIR"

  if [[ ! -s "$CORE_PLAN" ]]; then
    echo "generate core sample plan from schema store"
    "${BIN}" --io-backend "$IO_BACKEND" storage-bench \
      --data-dir "$schema_store" \
      --edge-types="$CORE_EDGE_TYPES" \
      --samples 200 \
      --semantic-degree-hint \
      --sample-plan-out "$CORE_PLAN" \
      > "${schema_logdir}/plan-core.stdout" 2> "${schema_logdir}/plan-core.stderr"
  fi

  if [[ ! -s "$ALLTYPES_PLAN" ]]; then
    echo "generate alltypes sample plan from schema store"
    "${BIN}" --io-backend "$IO_BACKEND" storage-bench \
      --data-dir "$schema_store" \
      --edge-types="$ALL_EDGE_TYPES" \
      --samples 50 \
      --semantic-degree-hint \
      --sample-plan-out "$ALLTYPES_PLAN" \
      > "${schema_logdir}/plan-alltypes.stdout" 2> "${schema_logdir}/plan-alltypes.stderr"
  fi
}

run_storage() {
  local name="$1"
  local store="$2"
  local logdir="$3"
  local kind="$4"
  local edge_types="$5"
  local samples="$6"
  local repeat="$7"
  local plan="$8"
  local out="${logdir}/fair-${kind}-s${samples}-r${repeat}.json"

  if [[ -s "$out" ]]; then
    echo "skip storage ${name} ${kind} r${repeat}"
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
    > "$out" 2> "${logdir}/fair-${kind}-s${samples}-r${repeat}.err"
}

run_storage_bench_all() {
  local name="$1"
  local store="$2"
  local logdir="$3"

  for repeat in 1 2 3; do
    run_storage "$name" "$store" "$logdir" core "$CORE_EDGE_TYPES" 200 "$repeat" "$CORE_PLAN"
    run_storage "$name" "$store" "$logdir" alltypes "$ALL_EDGE_TYPES" 50 "$repeat" "$ALLTYPES_PLAN"
  done
}

run_ldbc() {
  local name="$1"
  local store="$2"
  local logdir="$3"
  local repeat="$4"
  local out="${logdir}/ldbc-passing-s3-r${repeat}.json"

  if [[ -s "$out" ]]; then
    echo "skip ldbc ${name} r${repeat}"
    return
  fi
  "${BIN}" --io-backend "$IO_BACKEND" snb-validate-batch \
    --data-dir "$store" \
    --queries "$LDBC_QUERIES" \
    --max-lines-per-query 3 \
    > "$out" 2> "${logdir}/ldbc-passing-s3-r${repeat}.err"
}

run_neighbor_compare() {
  local name="$1"
  local right_store="$2"
  local right_logdir="$3"
  local schema_store="$4"

  local out_core="${right_logdir}/neighbor-compare-core.json"
  local out_all="${right_logdir}/neighbor-compare-alltypes.json"

  if [[ ! -s "$out_core" ]]; then
    "${BIN}" --io-backend "$IO_BACKEND" neighbor-compare \
      --left-data-dir "$schema_store" \
      --right-data-dir "$right_store" \
      --sample-plan "$CORE_PLAN" \
      --right-semantic-degree-hint \
      > "$out_core" 2> "${out_core%.json}.err"
  fi

  if [[ ! -s "$out_all" ]]; then
    "${BIN}" --io-backend "$IO_BACKEND" neighbor-compare \
      --left-data-dir "$schema_store" \
      --right-data-dir "$right_store" \
      --sample-plan "$ALLTYPES_PLAN" \
      --right-semantic-degree-hint \
      > "$out_all" 2> "${out_all%.json}.err"
  fi
}

main() {
  require_input
  build_binary

  SCHEMA_STORE=""
  SCHEMA_LOGDIR=""

  for name in "${!VARIANTS[@]}"; do
    layout="${VARIANTS[$name]}"
    store="store/e11-${name}-${DATE_TAG}"
    logdir="remote-logs/e11-${name}-${DATE_TAG}"
    run_import "$name" "$layout" "$store" "$logdir"
    if [[ "$name" == "schema-only" ]]; then
      SCHEMA_STORE="$store"
      SCHEMA_LOGDIR="$logdir"
    fi
  done

  if [[ -z "$SCHEMA_STORE" ]]; then
    echo "schema-only store not found" >&2
    exit 6
  fi

  generate_sample_plans "$SCHEMA_LOGDIR" "$SCHEMA_STORE"

  for name in "${!VARIANTS[@]}"; do
    store="store/e11-${name}-${DATE_TAG}"
    logdir="remote-logs/e11-${name}-${DATE_TAG}"
    run_storage_bench_all "$name" "$store" "$logdir"
  done

  for name in "${!VARIANTS[@]}"; do
    store="store/e11-${name}-${DATE_TAG}"
    logdir="remote-logs/e11-${name}-${DATE_TAG}"
    for repeat in 1 2 3; do
      run_ldbc "$name" "$store" "$logdir" "$repeat"
    done
  done

  for name in "${!VARIANTS[@]}"; do
    if [[ "$name" != "schema-only" ]]; then
      store="store/e11-${name}-${DATE_TAG}"
      logdir="remote-logs/e11-${name}-${DATE_TAG}"
      run_neighbor_compare "$name" "$store" "$logdir" "$SCHEMA_STORE"
    fi
  done

  echo "E11 baseline matrix complete"
  echo "date_tag=${DATE_TAG}"
  for name in "${!VARIANTS[@]}"; do
    echo "  ${name}: store/e11-${name}-${DATE_TAG}"
  done
}

main "$@"
