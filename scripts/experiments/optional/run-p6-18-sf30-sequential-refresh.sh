#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

DATE_TAG="${DATE_TAG:-20260604-p6-18}"
BIN="${BIN:-target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
DATA="${DATA:-/data/WorkSpace/ldbc-sf30/social_network}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-1048576}"
BUILD_RELEASE="${BUILD_RELEASE:-true}"

RUN_SCHEMA="${RUN_SCHEMA:-true}"
RUN_SEMANTIC="${RUN_SEMANTIC:-true}"
RUN_BUDGETED="${RUN_BUDGETED:-true}"
RUN_LDBC_VALIDATE="${RUN_LDBC_VALIDATE:-false}"

SCHEMA_SEMANTIC_DEGREE_HINT="${SCHEMA_SEMANTIC_DEGREE_HINT:-false}"
SCHEMA_SAMPLE_PLAN_DEGREE_HINT="${SCHEMA_SAMPLE_PLAN_DEGREE_HINT:-true}"
CANDIDATE_SEMANTIC_DEGREE_HINT="${CANDIDATE_SEMANTIC_DEGREE_HINT:-true}"
NEIGHBOR_COMPARE_RIGHT_SEMANTIC_DEGREE_HINT="${NEIGHBOR_COMPARE_RIGHT_SEMANTIC_DEGREE_HINT:-true}"

DELETE_CANDIDATES_AFTER_CAPTURE="${DELETE_CANDIDATES_AFTER_CAPTURE:-false}"
DELETE_SCHEMA_AFTER_ALL="${DELETE_SCHEMA_AFTER_ALL:-false}"

CORE_EDGE_TYPES="1,2,3,7,8,9,10,11,12"
ALL_EDGE_TYPES="-17,-16,-15,-14,-13,-12,-11,-10,-9,-8,-7,-6,-5,-4,-3,-2,-1,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17"
LDBC_QUERIES="ic4,ic5,ic6,ic10,ic11,ic13,ic14,is3"

SCORED_EDGE_TYPE_BYTES="${SCORED_EDGE_TYPE_BYTES:-1099511627776}"
SCORED_EDGE_TYPE_SCORE="${SCORED_EDGE_TYPE_SCORE:-1024.0}"
SCORED_CORE_WEIGHT="${SCORED_CORE_WEIGHT:-4.0}"
SCORED_REVERSE_CORE_WEIGHT="${SCORED_REVERSE_CORE_WEIGHT:-2.0}"
SCORED_OTHER_WEIGHT="${SCORED_OTHER_WEIGHT:-0.1}"
SCORED_EXACT_BYTES="${SCORED_EXACT_BYTES:-0}"
SCORED_DEGREE_SCORE="${SCORED_DEGREE_SCORE:-0.0}"
SCORED_DEGREE_WEIGHT="${SCORED_DEGREE_WEIGHT:-1.0}"
SCORED_MAX_EXTRA_L0_FILES="${SCORED_MAX_EXTRA_L0_FILES:-}"
SCORED_EDGE_TYPE_ALLOWLIST="${SCORED_EDGE_TYPE_ALLOWLIST:-}"

PLAN_DIR="remote-logs/p6-18-sf30-sample-plans-${DATE_TAG}"
CORE_PLAN="${PLAN_DIR}/sf30-core-s200.plan.json"
ALLTYPES_PLAN="${PLAN_DIR}/sf30-alltypes-s50.plan.json"

SCHEMA_STORE="store/p6-18-sf30-schema-${DATE_TAG}"
SEMANTIC_STORE="store/p6-18-sf30-semantic-${DATE_TAG}"
BUDGETED_STORE="store/p6-18-sf30-budgeted-${DATE_TAG}"

SCHEMA_LOG="remote-logs/p6-18-sf30-schema-${DATE_TAG}"
SEMANTIC_LOG="remote-logs/p6-18-sf30-semantic-${DATE_TAG}"
BUDGETED_LOG="remote-logs/p6-18-sf30-budgeted-${DATE_TAG}"

require_input() {
  if [[ ! -d "$DATA/dynamic" || ! -d "$DATA/static" ]]; then
    echo "missing expected LDBC dynamic/static directories under ${DATA}" >&2
    exit 2
  fi
  if [[ ! -f "${DATA}/dynamic/person_0_0.csv" ]]; then
    echo "missing expected SF30 person file under ${DATA}/dynamic" >&2
    exit 2
  fi
  if [[ ! -f "${DATA}/dynamic/person_knows_person_0_0.csv" ]]; then
    echo "missing expected SF30 knows file under ${DATA}/dynamic" >&2
    exit 2
  fi
}

build_binary() {
  if [[ "$BUILD_RELEASE" == "true" ]]; then
    mkdir -p "remote-logs/p6-18-build-${DATE_TAG}"
    cargo build --release \
      > "remote-logs/p6-18-build-${DATE_TAG}/build.stdout" \
      2> "remote-logs/p6-18-build-${DATE_TAG}/build.stderr"
  fi
  if [[ ! -x "$BIN" ]]; then
    echo "binary not found or not executable: ${BIN}" >&2
    exit 3
  fi
}

record_meta() {
  local logdir="$1"
  local store="$2"
  local layout="$3"
  mkdir -p "$logdir"
  {
    echo "date=$(date -Is)"
    echo "pwd=$(pwd)"
    echo "binary=${BIN}"
    echo "io_backend=${IO_BACKEND}"
    echo "data=${DATA}"
    echo "store=${store}"
    echo "layout=${layout}"
    echo "date_tag=${DATE_TAG}"
    echo "memgraph_bytes=${MEMGRAPH_BYTES}"
    echo "delete_candidates_after_capture=${DELETE_CANDIDATES_AFTER_CAPTURE}"
    echo "delete_schema_after_all=${DELETE_SCHEMA_AFTER_ALL}"
    echo "semantic_budget_min_edge_type_bytes=${SCORED_EDGE_TYPE_BYTES}"
    echo "semantic_budget_min_edge_type_score=${SCORED_EDGE_TYPE_SCORE}"
    echo "semantic_budget_core_edge_weight=${SCORED_CORE_WEIGHT}"
    echo "semantic_budget_reverse_core_edge_weight=${SCORED_REVERSE_CORE_WEIGHT}"
    echo "semantic_budget_other_edge_weight=${SCORED_OTHER_WEIGHT}"
    echo "semantic_budget_min_exact_bytes=${SCORED_EXACT_BYTES}"
    echo "semantic_budget_min_benefit_score=${SCORED_DEGREE_SCORE}"
    echo "semantic_budget_degree_weight=${SCORED_DEGREE_WEIGHT}"
    echo "semantic_budget_max_extra_l0_files=${SCORED_MAX_EXTRA_L0_FILES:-none}"
    echo "semantic_budget_edge_type_allowlist=${SCORED_EDGE_TYPE_ALLOWLIST:-none}"
    echo "schema_semantic_degree_hint=${SCHEMA_SEMANTIC_DEGREE_HINT}"
    echo "schema_sample_plan_degree_hint=${SCHEMA_SAMPLE_PLAN_DEGREE_HINT}"
    echo "candidate_semantic_degree_hint=${CANDIDATE_SEMANTIC_DEGREE_HINT}"
    echo "neighbor_compare_right_semantic_degree_hint=${NEIGHBOR_COMPARE_RIGHT_SEMANTIC_DEGREE_HINT}"
    df -h /data
  } > "${logdir}/run.meta"
  git rev-parse HEAD > "${logdir}/git-head.txt" 2> "${logdir}/git-head.err" || true
  git status --short > "${logdir}/git-status.txt" 2> "${logdir}/git-status.err" || true
}

import_complete() {
  local logdir="$1"
  [[ -s "${logdir}/import.stdout" ]] && grep -q '"snapshot"' "${logdir}/import.stdout"
}

json_file_valid() {
  local path="$1"
  [[ -s "$path" ]] || return 1
  python3 -m json.tool "$path" > /dev/null 2>&1
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
      grep -c '"op":"CreateFile"' "${store}/MANIFEST" | awk '{printf "manifest_records\t%s\n", $1}'
      grep -c '"degree_class_exact":true' "${store}/MANIFEST" | awk '{printf "degree_exact_files\t%s\n", $1}'
      grep -c '"edge_type_partition":0' "${store}/MANIFEST" | awk '{printf "mixed_edge_files\t%s\n", $1}'
    fi
  } > "${logdir}/file-summary.tsv"
}

budget_args() {
  local args=(
    --semantic-budget-min-edge-type-bytes "$SCORED_EDGE_TYPE_BYTES"
    --semantic-budget-min-edge-type-score "$SCORED_EDGE_TYPE_SCORE"
    --semantic-budget-core-edge-weight "$SCORED_CORE_WEIGHT"
    --semantic-budget-reverse-core-edge-weight "$SCORED_REVERSE_CORE_WEIGHT"
    --semantic-budget-other-edge-weight "$SCORED_OTHER_WEIGHT"
    --semantic-budget-min-exact-bytes "$SCORED_EXACT_BYTES"
    --semantic-budget-min-benefit-score "$SCORED_DEGREE_SCORE"
    --semantic-budget-degree-weight "$SCORED_DEGREE_WEIGHT"
  )
  if [[ -n "$SCORED_MAX_EXTRA_L0_FILES" ]]; then
    args+=(--semantic-budget-max-extra-l0-files "$SCORED_MAX_EXTRA_L0_FILES")
  fi
  if [[ -n "$SCORED_EDGE_TYPE_ALLOWLIST" ]]; then
    args+=(--semantic-budget-edge-type-allowlist "$SCORED_EDGE_TYPE_ALLOWLIST")
  fi
  printf '%s\n' "${args[@]}"
}

run_import() {
  local name="$1"
  local layout="$2"
  local store="$3"
  local logdir="$4"

  mkdir -p "$logdir"
  record_meta "$logdir" "$store" "$layout"
  if import_complete "$logdir"; then
    echo "skip completed import ${name}"
  else
    if [[ -e "$store" ]]; then
      echo "store exists but import is not marked complete: ${store}" >&2
      exit 4
    fi
    echo "start import ${name} layout=${layout} store=${store}"
    mapfile -t extra_args < <(budget_args)
    /usr/bin/time -v -o "${logdir}/time.log" \
      "$BIN" --io-backend "$IO_BACKEND" import \
        --input "$DATA" \
        --data-dir "$store" \
        --relation snb-full \
        --memgraph-bytes "$MEMGRAPH_BYTES" \
        --l0-layout "$layout" \
        "${extra_args[@]}" \
        > "${logdir}/import.stdout" 2> "${logdir}/import.stderr"
    echo "finish import ${name}"
  fi

  "$BIN" --io-backend "$IO_BACKEND" stats \
    --data-dir "$store" \
    > "${logdir}/stats.log" 2> "${logdir}/stats.err"
  write_file_summary "$store" "$logdir"
}

run_storage_plan_out() {
  local store="$1"
  local logdir="$2"
  local kind="$3"
  local edge_types="$4"
  local samples="$5"
  local plan="$6"
  local hint_mode="${7:-schema}"
  local out="${logdir}/fair-${kind}-s${samples}-r1.json"
  local err="${logdir}/fair-${kind}-s${samples}-r1.err"

  if json_file_valid "$out" && json_file_valid "$plan"; then
    echo "skip storage ${kind} plan generation ${logdir}"
    return
  fi
  mkdir -p "$(dirname "$plan")"
  local hint_args=()
  case "$hint_mode" in
    schema)
      [[ "$SCHEMA_SEMANTIC_DEGREE_HINT" == "true" ]] && hint_args+=(--semantic-degree-hint)
      [[ "$SCHEMA_SAMPLE_PLAN_DEGREE_HINT" == "true" ]] && hint_args+=(--sample-plan-degree-hint)
      ;;
    candidate)
      [[ "$CANDIDATE_SEMANTIC_DEGREE_HINT" == "true" ]] && hint_args+=(--semantic-degree-hint)
      ;;
    *)
      echo "unknown storage hint mode: ${hint_mode}" >&2
      exit 6
      ;;
  esac
  "$BIN" --io-backend "$IO_BACKEND" storage-bench \
    --data-dir "$store" \
    --edge-types="$edge_types" \
    --samples "$samples" \
    "${hint_args[@]}" \
    --sample-plan-out "$plan" \
    > "$out" 2> "$err"
}

run_storage_plan_in() {
  local store="$1"
  local logdir="$2"
  local kind="$3"
  local edge_types="$4"
  local samples="$5"
  local repeat="$6"
  local plan="$7"
  local hint_mode="${8:-candidate}"
  local out="${logdir}/fair-${kind}-s${samples}-r${repeat}.json"
  local err="${logdir}/fair-${kind}-s${samples}-r${repeat}.err"

  if json_file_valid "$out"; then
    echo "skip storage ${kind} r${repeat} ${logdir}"
    return
  fi
  if ! json_file_valid "$plan"; then
    echo "missing or invalid sample plan: ${plan}" >&2
    exit 5
  fi
  local hint_args=()
  case "$hint_mode" in
    schema)
      [[ "$SCHEMA_SEMANTIC_DEGREE_HINT" == "true" ]] && hint_args+=(--semantic-degree-hint)
      ;;
    candidate)
      [[ "$CANDIDATE_SEMANTIC_DEGREE_HINT" == "true" ]] && hint_args+=(--semantic-degree-hint)
      ;;
    *)
      echo "unknown storage hint mode: ${hint_mode}" >&2
      exit 6
      ;;
  esac
  "$BIN" --io-backend "$IO_BACKEND" storage-bench \
    --data-dir "$store" \
    --edge-types="$edge_types" \
    "${hint_args[@]}" \
    --sample-plan-in "$plan" \
    > "$out" 2> "$err"
}

run_ldbc_validate() {
  local store="$1"
  local logdir="$2"
  local repeat="$3"
  local out="${logdir}/ldbc-passing-s3-r${repeat}.json"
  local err="${logdir}/ldbc-passing-s3-r${repeat}.err"

  if [[ "$RUN_LDBC_VALIDATE" != "true" ]]; then
    return
  fi
  "$BIN" --io-backend "$IO_BACKEND" snb-validate-batch \
    --data-dir "$store" \
    --queries "$LDBC_QUERIES" \
    --max-lines-per-query 3 \
    > "$out" 2> "$err"
}

run_schema_reads() {
  run_storage_plan_out "$SCHEMA_STORE" "$SCHEMA_LOG" core "$CORE_EDGE_TYPES" 200 "$CORE_PLAN" schema
  run_storage_plan_out "$SCHEMA_STORE" "$SCHEMA_LOG" alltypes "$ALL_EDGE_TYPES" 50 "$ALLTYPES_PLAN" schema
  run_ldbc_validate "$SCHEMA_STORE" "$SCHEMA_LOG" 1
  for repeat in 2 3; do
    run_storage_plan_in "$SCHEMA_STORE" "$SCHEMA_LOG" core "$CORE_EDGE_TYPES" 200 "$repeat" "$CORE_PLAN" schema
    run_storage_plan_in "$SCHEMA_STORE" "$SCHEMA_LOG" alltypes "$ALL_EDGE_TYPES" 50 "$repeat" "$ALLTYPES_PLAN" schema
    run_ldbc_validate "$SCHEMA_STORE" "$SCHEMA_LOG" "$repeat"
  done
}

run_candidate_reads() {
  local store="$1"
  local logdir="$2"
  for repeat in 1 2 3; do
    run_storage_plan_in "$store" "$logdir" core "$CORE_EDGE_TYPES" 200 "$repeat" "$CORE_PLAN" candidate
    run_storage_plan_in "$store" "$logdir" alltypes "$ALL_EDGE_TYPES" 50 "$repeat" "$ALLTYPES_PLAN" candidate
    run_ldbc_validate "$store" "$logdir" "$repeat"
  done
}

run_neighbor_compare() {
  local name="$1"
  local store="$2"
  local logdir="$3"
  local right_hint_args=()
  [[ "$NEIGHBOR_COMPARE_RIGHT_SEMANTIC_DEGREE_HINT" == "true" ]] && right_hint_args+=(--right-semantic-degree-hint)

  if json_file_valid "${logdir}/neighbor-compare-core.json" && json_file_valid "${logdir}/neighbor-compare-alltypes.json"; then
    echo "skip neighbor compare for ${name}"
    return
  fi

  "$BIN" --io-backend "$IO_BACKEND" neighbor-compare \
    --left-data-dir "$SCHEMA_STORE" \
    --right-data-dir "$store" \
    --sample-plan "$CORE_PLAN" \
    "${right_hint_args[@]}" \
    > "${logdir}/neighbor-compare-core.json" 2> "${logdir}/neighbor-compare-core.err"

  "$BIN" --io-backend "$IO_BACKEND" neighbor-compare \
    --left-data-dir "$SCHEMA_STORE" \
    --right-data-dir "$store" \
    --sample-plan "$ALLTYPES_PLAN" \
    "${right_hint_args[@]}" \
    > "${logdir}/neighbor-compare-alltypes.json" 2> "${logdir}/neighbor-compare-alltypes.err"

  echo "neighbor compare complete for ${name}"
}

summarize_candidates() {
  local store="$1"
  local logdir="$2"
  local diag="${store}/budgeted-edge-candidates.tsv"
  if [[ ! -s "$diag" ]]; then
    return
  fi
  python3 - "$diag" "$logdir" <<'PY'
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

diag = Path(sys.argv[1])
logdir = Path(sys.argv[2])
rows = list(csv.DictReader(diag.open(encoding="utf-8"), delimiter="\t"))

reasons = Counter(row.get("reason", "") for row in rows)
with (logdir / "candidate-reason-counts.tsv").open("w", encoding="utf-8") as out:
    out.write("reason\tcount\n")
    for reason, count in sorted(reasons.items()):
        out.write(f"{reason}\t{count}\n")

selected = defaultdict(lambda: {"rows": 0, "group_edges": 0, "group_bytes": 0, "max_score": 0.0})
for row in rows:
    if row.get("selected") != "true":
        continue
    entry = selected[(row.get("src_label", ""), row.get("edge_type", ""))]
    entry["rows"] += 1
    entry["group_edges"] += int(row.get("group_edges", "0") or 0)
    entry["group_bytes"] += int(row.get("group_bytes", "0") or 0)
    entry["max_score"] = max(entry["max_score"], float(row.get("score", "0") or 0))

with (logdir / "selected-edge-types.tsv").open("w", encoding="utf-8") as out:
    out.write("src_label\tedge_type\tcandidate_rows\tgroup_edges\tgroup_bytes\tmax_score\n")
    for (src_label, edge_type), entry in sorted(selected.items(), key=lambda item: (int(item[0][0]), int(item[0][1]))):
        out.write(
            f"{src_label}\t{edge_type}\t{entry['rows']}\t{entry['group_edges']}\t{entry['group_bytes']}\t{entry['max_score']:.6f}\n"
        )
PY
}

assert_generated_store_path() {
  local store="$1"
  case "$store" in
    store/p6-18-sf30-*) ;;
    *)
      echo "refuse to delete non-P6.18 generated store path: ${store}" >&2
      exit 20
      ;;
  esac
}

capture_complete() {
  local logdir="$1"
  [[ -s "${logdir}/import.stdout" ]] || return 1
  [[ -s "${logdir}/stats.log" ]] || return 1
  [[ -s "${logdir}/file-summary.tsv" ]] || return 1
  json_file_valid "${logdir}/fair-core-s200-r1.json" || return 1
  json_file_valid "${logdir}/fair-alltypes-s50-r1.json" || return 1
}

delete_store_after_capture() {
  local store="$1"
  local logdir="$2"
  local role="$3"

  assert_generated_store_path "$store"
  if ! capture_complete "$logdir"; then
    echo "refuse to delete ${role}; captured logs are incomplete: ${logdir}" >&2
    exit 21
  fi
  if [[ "$role" != "schema" ]]; then
    [[ -s "${logdir}/neighbor-compare-core.json" ]] || {
      echo "refuse to delete ${role}; missing core neighbor compare" >&2
      exit 22
    }
    [[ -s "${logdir}/neighbor-compare-alltypes.json" ]] || {
      echo "refuse to delete ${role}; missing alltypes neighbor compare" >&2
      exit 22
    }
  fi
  echo "delete captured ${role} store: ${store}"
  rm -rf -- "$store"
  [[ ! -e "$store" ]] || {
    echo "delete failed: ${store}" >&2
    exit 23
  }
}

run_candidate() {
  local name="$1"
  local layout="$2"
  local store="$3"
  local logdir="$4"

  run_import "$name" "$layout" "$store" "$logdir"
  run_candidate_reads "$store" "$logdir"
  run_neighbor_compare "$name" "$store" "$logdir"
  summarize_candidates "$store" "$logdir"
  df -h /data > "${logdir}/df-after-capture.log"

  if [[ "$DELETE_CANDIDATES_AFTER_CAPTURE" == "true" ]]; then
    delete_store_after_capture "$store" "$logdir" "$name"
    df -h /data > "${logdir}/df-after-delete.log"
  fi
}

main() {
  require_input
  build_binary
  mkdir -p "$PLAN_DIR"
  df -h /data

  if [[ "$RUN_SCHEMA" == "true" ]]; then
    run_import schema schema "$SCHEMA_STORE" "$SCHEMA_LOG"
    run_schema_reads
  elif [[ ! -d "$SCHEMA_STORE" ]]; then
    echo "RUN_SCHEMA=false but schema store is missing: ${SCHEMA_STORE}" >&2
    exit 30
  fi

  if [[ "$RUN_SEMANTIC" == "true" ]]; then
    run_candidate semantic semantic "$SEMANTIC_STORE" "$SEMANTIC_LOG"
  fi

  if [[ "$RUN_BUDGETED" == "true" ]]; then
    run_candidate budgeted semantic-budgeted "$BUDGETED_STORE" "$BUDGETED_LOG"
  fi

  if [[ "$DELETE_SCHEMA_AFTER_ALL" == "true" ]]; then
    delete_store_after_capture "$SCHEMA_STORE" "$SCHEMA_LOG" schema
  fi

  df -h /data
}

main "$@"
