#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

DATA="/data/WorkSpace/ldbc-sf30/social_network"
MEMGRAPH_BYTES=1048576
CORE_EDGE_TYPES="1,2,3,7,8,9,10,11,12"
ALL_EDGE_TYPES="-17,-16,-15,-14,-13,-12,-11,-10,-9,-8,-7,-6,-5,-4,-3,-2,-1,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17"

require_input() {
  if [[ ! -f "${DATA}/dynamic/person_0_0.csv" || ! -f "${DATA}/dynamic/person_knows_person_0_0.csv" ]]; then
    echo "missing expected LDBC SF30 files under ${DATA}" >&2
    exit 2
  fi
}

import_complete() {
  local logdir="$1"
  [[ -s "${logdir}/import.stdout" ]] && grep -q '"snapshot"' "${logdir}/import.stdout"
}

run_import() {
  local name="$1"
  local layout="$2"
  local store="$3"
  local logdir="$4"

  mkdir -p "$logdir"
  if import_complete "$logdir"; then
    echo "skip completed import ${name}"
  else
    if [[ -e "$store" ]]; then
      echo "store exists but import is not marked complete: ${store}" >&2
      exit 3
    fi
    echo "start import ${name} layout=${layout} store=${store}"
    /usr/bin/time -v -o "${logdir}/time.log" \
      target/release/lsmgraph --io-backend blocking import \
        --input "$DATA" \
        --data-dir "$store" \
        --relation snb-full \
        --memgraph-bytes "$MEMGRAPH_BYTES" \
        --l0-layout "$layout" \
        > "${logdir}/import.stdout" 2> "${logdir}/import.stderr"
    echo "finish import ${name}"
  fi

  if [[ ! -s "${logdir}/stats.log" ]]; then
    target/release/lsmgraph --io-backend blocking stats \
      --data-dir "$store" \
      > "${logdir}/stats.log" 2> "${logdir}/stats.err"
  fi

  if [[ ! -s "${logdir}/file-summary.tsv" ]]; then
    {
      printf "store\t%s\n" "$store"
      find "$store" -type f | wc -l | awk '{printf "files\t%s\n", $1}'
      find "$store" -type f -printf '%s\n' | awk '{s+=$1} END{printf "bytes\t%.0f\n", s}'
      find "$store" -maxdepth 1 -type f -name 'l0_*.csr' | wc -l | awk '{printf "l0_files\t%s\n", $1}'
      find "$store" -maxdepth 1 -type f -name 'l0_*.csr' -printf '%s\n' | awk '{s+=$1} END{printf "l0_bytes\t%.0f\n", s}'
      if [[ -f "${store}/MANIFEST" ]]; then
        stat -c 'manifest_bytes	%s' "${store}/MANIFEST"
      fi
    } > "${logdir}/file-summary.tsv"
  fi
}

run_storage() {
  local store="$1"
  local logdir="$2"
  local kind="$3"
  local edge_types="$4"
  local samples="$5"
  local out="${logdir}/fair-${kind}-s${samples}.json"
  local err="${logdir}/fair-${kind}-s${samples}.err"

  if [[ -s "$out" ]]; then
    echo "skip storage ${kind} ${logdir}"
    return
  fi

  echo "run storage ${kind} ${logdir}"
  target/release/lsmgraph --io-backend blocking storage-bench \
    --data-dir "$store" \
    --edge-types="$edge_types" \
    --samples "$samples" \
    --semantic-degree-hint \
    > "$out" 2> "$err"
}

run_layout() {
  local name="$1"
  local layout="$2"
  local store="$3"
  local logdir="$4"

  run_import "$name" "$layout" "$store" "$logdir"
  run_storage "$store" "$logdir" core "$CORE_EDGE_TYPES" 200
  run_storage "$store" "$logdir" alltypes "$ALL_EDGE_TYPES" 50
}

require_input
df -h /data /data/WorkSpace

run_layout \
  "schema" \
  "schema" \
  "store/codex-qslsm-sf30-full-schema-v12-20260603" \
  "remote-logs/qslsm-sf30-full-schema-v12-20260603"

run_layout \
  "full-semantic" \
  "semantic" \
  "store/codex-qslsm-sf30-full-semantic-v12-20260603" \
  "remote-logs/qslsm-sf30-full-semantic-v12-20260603"

run_layout \
  "budgeted-semantic" \
  "semantic-budgeted" \
  "store/codex-qslsm-sf30-full-budgeted-v12-20260603" \
  "remote-logs/qslsm-sf30-full-budgeted-v12-20260603"

df -h /data /data/WorkSpace
