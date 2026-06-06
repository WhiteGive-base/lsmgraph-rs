#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

CORE_EDGE_TYPES="1,2,3,7,8,9,10,11,12"
ALL_EDGE_TYPES="-17,-16,-15,-14,-13,-12,-11,-10,-9,-8,-7,-6,-5,-4,-3,-2,-1,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17"
LDBC_QUERIES="ic4,ic5,ic6,ic10,ic11,ic13,ic14,is3"

copy_r1() {
  local logdir="$1"
  for base in fair-core-s200 fair-alltypes-s50 ldbc-passing-s3; do
    if [[ -f "${logdir}/${base}.json" && ! -f "${logdir}/${base}-r1.json" ]]; then
      cp "${logdir}/${base}.json" "${logdir}/${base}-r1.json"
    fi
    if [[ -f "${logdir}/${base}.err" && ! -f "${logdir}/${base}-r1.err" ]]; then
      cp "${logdir}/${base}.err" "${logdir}/${base}-r1.err"
    fi
  done
}

run_storage() {
  local store="$1"
  local logdir="$2"
  local kind="$3"
  local edge_types="$4"
  local samples="$5"
  local repeat="$6"
  local out="${logdir}/fair-${kind}-s${samples}-r${repeat}.json"
  local err="${logdir}/fair-${kind}-s${samples}-r${repeat}.err"

  if [[ -s "$out" ]]; then
    echo "skip $out"
    return
  fi

  echo "run storage ${logdir} ${kind} r${repeat}"
  target/release/lsmgraph --io-backend blocking storage-bench \
    --data-dir "$store" \
    --edge-types="$edge_types" \
    --samples "$samples" \
    --semantic-degree-hint \
    > "$out" 2> "$err"
}

run_ldbc() {
  local store="$1"
  local logdir="$2"
  local repeat="$3"
  local out="${logdir}/ldbc-passing-s3-r${repeat}.json"
  local err="${logdir}/ldbc-passing-s3-r${repeat}.err"

  if [[ -s "$out" ]]; then
    echo "skip $out"
    return
  fi

  echo "run ldbc ${logdir} r${repeat}"
  target/release/lsmgraph --io-backend blocking snb-validate-batch \
    --data-dir "$store" \
    --queries "$LDBC_QUERIES" \
    --max-lines-per-query 3 \
    > "$out" 2> "$err"
}

run_layout() {
  local store="$1"
  local logdir="$2"

  copy_r1 "$logdir"
  for repeat in 2 3; do
    run_storage "$store" "$logdir" core "$CORE_EDGE_TYPES" 200 "$repeat"
    run_storage "$store" "$logdir" alltypes "$ALL_EDGE_TYPES" 50 "$repeat"
    run_ldbc "$store" "$logdir" "$repeat"
  done
}

run_layout \
  "store/codex-qslsm-sf1-full-schema-v10-20260603" \
  "remote-logs/qslsm-sf1-full-schema-v10-20260603"

run_layout \
  "store/codex-qslsm-sf1-full-semantic-v10-20260603" \
  "remote-logs/qslsm-sf1-full-semantic-v10-20260603"

run_layout \
  "store/codex-qslsm-sf1-full-budgeted-v9b-20260603" \
  "remote-logs/qslsm-sf1-full-budgeted-v9b-20260603"
