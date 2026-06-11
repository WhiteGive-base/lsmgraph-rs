#!/usr/bin/env bash
# Generalized E11 SemL0 baseline / ablation matrix runner.
#
# Scale-parameterized; sequential build -> measure -> delete to bound disk usage
# (peak = schema store + 1 candidate store). Worktree-friendly (no hardcoded cwd).
#
# Per variant it writes, under remote-logs/e11-${VID}-${DATE_TAG}/:
#   run.meta, git-head.txt, git-status.txt
#   import.stdout / import.stderr        (import wall result; "snapshot" marks completion)
#   time.log                             (/usr/bin/time -v: import wall time + max RSS)
#   stats.log                            (lsmgraph stats: flush/compaction metrics, levels)
#   file-summary.tsv                     (store bytes, L0 files/bytes, manifest bytes/records)
#   fair-core-s${CORE_SAMPLES}-r{1,2,3}.json       (read-amp, shared sample plan)
#   fair-alltypes-s${ALLTYPES_SAMPLES}-r{1,2,3}.json
#   neighbor-compare-core.json / neighbor-compare-alltypes.json (mismatches vs schema)
#
# The schema_only variant is the reference: it generates the shared sample plans
# and is the left side of every neighbor-compare, so it is kept until the end.
set -euo pipefail

cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"

# ---- parameters (env-driven) -------------------------------------------------
SCALE="${SCALE:-sf10}"
DATA="${DATA:?set DATA to the LDBC social_network dir (must contain dynamic/ and static/)}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-1048576}"
DATE_TAG="${DATE_TAG:-$(date +%Y%m%d)-$(openssl rand -hex 3 2>/dev/null || echo manual)}"
BIN="${BIN:-target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
BUILD_RELEASE="${BUILD_RELEASE:-false}"
DELETE_CANDIDATES_AFTER_CAPTURE="${DELETE_CANDIDATES_AFTER_CAPTURE:-true}"
DELETE_SCHEMA_AFTER_ALL="${DELETE_SCHEMA_AFTER_ALL:-true}"
RUN_LDBC_VALIDATE="${RUN_LDBC_VALIDATE:-false}"
LDBC_QUERIES="${LDBC_QUERIES:-ic4,ic5,ic6,ic10,ic11,ic13,ic14,is3}"
CORE_SAMPLES="${CORE_SAMPLES:-200}"
ALLTYPES_SAMPLES="${ALLTYPES_SAMPLES:-50}"
# Optional cap on variants to run (space/comma list of vids); empty = all.
ONLY_VARIANTS="${ONLY_VARIANTS:-}"
# Skip the per-variant snb_adjacency.bin build+write (not needed for storage-bench /
# neighbor-compare; saves ~36% import time + GBs of redundant writes at scale).
export SNB_SKIP_ADJ_CACHE="${SNB_SKIP_ADJ_CACHE:-1}"
# Read-bench repeats. Byte/segment counters are exact (deterministic) so 1 suffices;
# raise for more latency-percentile stability.
REPEATS="${REPEATS:-3}"
# After import, delete snb_edge_props.jsonl + snb_vertices.jsonl (staging artifacts not
# read by storage-bench / neighbor-compare). Keeps the LSM store (levels/ + MANIFEST) only,
# so two coexisting stores fit on disk at SF100. Disabled when LDBC validation is on.
PRUNE_STORE_STAGING="${PRUNE_STORE_STAGING:-1}"
# Each Engine::open rebuilds the semantic indexes (O(edges): ~14min/80GB at SF30, more at
# SF100). To stay tractable we minimize opens:
#   RUN_STATS=0            skip the supplementary stats open (P2 cost comes from file-summary).
#   RUN_ALLTYPES=1         also run the 34-edge-type "alltypes" read pass (extra open/variant).
#   RUN_NEIGHBOR_COMPARE   correctness pass; "all" | "none" | comma-list of vids. Each adds a
#                          schema re-open + candidate open. Correctness is scale-invariant, so
#                          anchor it at SF1 (all) and skip/spot-check at SF30/SF100.
RUN_STATS="${RUN_STATS:-0}"
RUN_ALLTYPES="${RUN_ALLTYPES:-1}"
RUN_NEIGHBOR_COMPARE="${RUN_NEIGHBOR_COMPARE:-all}"

CORE_EDGE_TYPES="1,2,3,7,8,9,10,11,12"
ALL_EDGE_TYPES="-17,-16,-15,-14,-13,-12,-11,-10,-9,-8,-7,-6,-5,-4,-3,-2,-1,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17"

STORE_PREFIX="store/e11-${SCALE}"
PLAN_DIR="remote-logs/e11-${SCALE}-sample-plans-${DATE_TAG}"
CORE_PLAN="${PLAN_DIR}/${SCALE}-core-s${CORE_SAMPLES}.plan.json"
ALLTYPES_PLAN="${PLAN_DIR}/${SCALE}-alltypes-s${ALLTYPES_SAMPLES}.plan.json"

# ordered: schema_only first (reference). vid:layout
VARIANTS=(
  "schema_only:schema"
  "naive:naive"
  "lsmgraph_style:lsmgraph-style"
  "label_only:label-only"
  "edge_type_only:edge-type-only"
  "degree_only:degree-only"
  "full_semantic:semantic"
  "benefit_scored:semantic-budgeted"
  "full_compact:full-compact"
)

SCHEMA_VID="schema_only"
SCHEMA_STORE="${STORE_PREFIX}-${SCHEMA_VID}-${DATE_TAG}"
SCHEMA_LOG="remote-logs/e11-${SCALE}-${SCHEMA_VID}-${DATE_TAG}"

# ---- helpers -----------------------------------------------------------------
want_variant() {
  local vid="$1"
  [[ -z "$ONLY_VARIANTS" ]] && return 0
  [[ ",${ONLY_VARIANTS//[[:space:]]/,}," == *",${vid},"* ]]
}

want_nc() {
  local vid="$1"
  case "$RUN_NEIGHBOR_COMPARE" in
    all) return 0 ;;
    none|"") return 1 ;;
    *) [[ ",${RUN_NEIGHBOR_COMPARE//[[:space:]]/,}," == *",${vid},"* ]] ;;
  esac
}

require_input() {
  if [[ ! -d "$DATA/dynamic" || ! -d "$DATA/static" ]]; then
    echo "missing expected LDBC dynamic/static directories under ${DATA}" >&2
    exit 2
  fi
  if [[ ! -f "${DATA}/dynamic/person_knows_person_0_0.csv" ]]; then
    echo "missing expected knows file under ${DATA}/dynamic" >&2
    exit 2
  fi
}

build_binary() {
  if [[ "$BUILD_RELEASE" == "true" ]]; then
    mkdir -p "remote-logs/e11-${SCALE}-build-${DATE_TAG}"
    cargo build --release \
      > "remote-logs/e11-${SCALE}-build-${DATE_TAG}/build.stdout" \
      2> "remote-logs/e11-${SCALE}-build-${DATE_TAG}/build.stderr"
  fi
  [[ -x "$BIN" ]] || { echo "binary not found or not executable: ${BIN}" >&2; exit 3; }
}

record_meta() {
  local logdir="$1" store="$2" layout="$3"
  mkdir -p "$logdir"
  {
    echo "date=$(date -Is)"
    echo "scale=${SCALE}"
    echo "pwd=$(pwd)"
    echo "binary=${BIN}"
    echo "io_backend=${IO_BACKEND}"
    echo "data=${DATA}"
    echo "store=${store}"
    echo "layout=${layout}"
    echo "date_tag=${DATE_TAG}"
    echo "memgraph_bytes=${MEMGRAPH_BYTES}"
    echo "core_samples=${CORE_SAMPLES}"
    echo "alltypes_samples=${ALLTYPES_SAMPLES}"
    echo "delete_candidates_after_capture=${DELETE_CANDIDATES_AFTER_CAPTURE}"
    echo "delete_schema_after_all=${DELETE_SCHEMA_AFTER_ALL}"
    df -h /data || true
  } > "${logdir}/run.meta"
  git rev-parse HEAD > "${logdir}/git-head.txt" 2>/dev/null || true
  git status --short > "${logdir}/git-status.txt" 2>/dev/null || true
}

import_complete() { [[ -s "${1}/import.stdout" ]] && grep -q '"snapshot"' "${1}/import.stdout"; }

json_file_valid() {
  local path="$1"
  [[ -s "$path" ]] || return 1
  python3 -m json.tool "$path" > /dev/null 2>&1
}

write_file_summary() {
  local store="$1" logdir="$2"
  {
    printf "store\t%s\n" "$store"
    find "$store" -type f | wc -l | awk '{printf "files\t%s\n", $1}'
    find "$store" -type f -printf '%s\n' | awk '{s+=$1} END{printf "bytes\t%.0f\n", s}'
    if [[ -d "${store}/levels/L0" ]]; then
      find "${store}/levels/L0" -type f | wc -l | awk '{printf "l0_files\t%s\n", $1}'
      find "${store}/levels/L0" -type f -printf '%s\n' | awk '{s+=$1} END{printf "l0_bytes\t%.0f\n", s}'
    else
      printf "l0_files\t0\n"; printf "l0_bytes\t0\n"
    fi
    if [[ -f "${store}/MANIFEST" ]]; then
      stat -c 'manifest_bytes	%s' "${store}/MANIFEST"
      grep -c '"op":"CreateFile"' "${store}/MANIFEST" 2>/dev/null | awk '{printf "manifest_records\t%s\n", $1}'
    fi
  } > "${logdir}/file-summary.tsv"
}

run_import() {
  local vid="$1" layout="$2" store="$3" logdir="$4"
  mkdir -p "$logdir"
  record_meta "$logdir" "$store" "$layout"
  if import_complete "$logdir"; then
    echo "[skip] import ${vid} (already complete)"
  else
    if [[ -e "$store" ]]; then
      echo "store exists but import not marked complete: ${store}" >&2; exit 4
    fi
    echo "[run ] import ${vid} layout=${layout} store=${store}"
    /usr/bin/time -v -o "${logdir}/time.log" \
      "$BIN" --io-backend "$IO_BACKEND" import \
        --input "$DATA" --data-dir "$store" \
        --relation snb-full --memgraph-bytes "$MEMGRAPH_BYTES" \
        --l0-layout "$layout" \
        > "${logdir}/import.stdout" 2> "${logdir}/import.stderr"
    echo "[done] import ${vid}"
  fi
  prune_store_staging "$store"
  if [[ "$RUN_STATS" == "1" ]]; then
    "$BIN" --io-backend "$IO_BACKEND" stats --data-dir "$store" \
      > "${logdir}/stats.log" 2> "${logdir}/stats.err" || true
  fi
  write_file_summary "$store" "$logdir"
}

prune_store_staging() {
  local store="$1"
  [[ "$PRUNE_STORE_STAGING" == "1" ]] || return 0
  [[ "$RUN_LDBC_VALIDATE" == "true" ]] && return 0
  case "$store" in "${STORE_PREFIX}-"*) ;; *) return 0 ;; esac
  rm -f -- "${store}/snb_edge_props.jsonl" "${store}/snb_vertices.jsonl"
}

generate_plans() {
  mkdir -p "$PLAN_DIR"
  # Plan-gen does scan_edges (materializes all edges) purely to write the sample plan; it
  # never uses the semantic indexes, so skip building them here to avoid OOM at SF100 scale.
  if ! json_file_valid "$CORE_PLAN"; then
    echo "[run ] generate core sample plan from schema store"
    SNB_SKIP_SEM_INDEX=1 "$BIN" --io-backend "$IO_BACKEND" storage-bench \
      --data-dir "$SCHEMA_STORE" --edge-types="$CORE_EDGE_TYPES" \
      --samples "$CORE_SAMPLES" --semantic-degree-hint --sample-plan-degree-hint \
      --sample-plan-out "$CORE_PLAN" \
      > "${SCHEMA_LOG}/plan-core.stdout" 2> "${SCHEMA_LOG}/plan-core.stderr"
  fi
  if [[ "$RUN_ALLTYPES" == "1" ]] && ! json_file_valid "$ALLTYPES_PLAN"; then
    echo "[run ] generate alltypes sample plan from schema store"
    SNB_SKIP_SEM_INDEX=1 "$BIN" --io-backend "$IO_BACKEND" storage-bench \
      --data-dir "$SCHEMA_STORE" --edge-types="$ALL_EDGE_TYPES" \
      --samples "$ALLTYPES_SAMPLES" --semantic-degree-hint --sample-plan-degree-hint \
      --sample-plan-out "$ALLTYPES_PLAN" \
      > "${SCHEMA_LOG}/plan-alltypes.stdout" 2> "${SCHEMA_LOG}/plan-alltypes.stderr"
  fi
}

run_fair_reads() {
  local store="$1" logdir="$2"
  for repeat in $(seq 1 "$REPEATS"); do
    local core_out="${logdir}/fair-core-s${CORE_SAMPLES}-r${repeat}.json"
    local all_out="${logdir}/fair-alltypes-s${ALLTYPES_SAMPLES}-r${repeat}.json"
    if ! json_file_valid "$core_out"; then
      "$BIN" --io-backend "$IO_BACKEND" storage-bench \
        --data-dir "$store" --edge-types="$CORE_EDGE_TYPES" \
        --semantic-degree-hint --sample-plan-in "$CORE_PLAN" \
        > "$core_out" 2> "${core_out%.json}.err"
    fi
    if [[ "$RUN_ALLTYPES" == "1" ]] && ! json_file_valid "$all_out"; then
      "$BIN" --io-backend "$IO_BACKEND" storage-bench \
        --data-dir "$store" --edge-types="$ALL_EDGE_TYPES" \
        --semantic-degree-hint --sample-plan-in "$ALLTYPES_PLAN" \
        > "$all_out" 2> "${all_out%.json}.err"
    fi
  done
}

run_ldbc_validate() {
  local store="$1" logdir="$2"
  [[ "$RUN_LDBC_VALIDATE" == "true" ]] || return 0
  for repeat in 1 2 3; do
    local out="${logdir}/ldbc-passing-s3-r${repeat}.json"
    json_file_valid "$out" && continue
    "$BIN" --io-backend "$IO_BACKEND" snb-validate-batch \
      --data-dir "$store" --queries "$LDBC_QUERIES" --max-lines-per-query 3 \
      > "$out" 2> "${out%.json}.err" || true
  done
}

run_neighbor_compare() {
  local vid="$1" store="$2" logdir="$3"
  # Core-plan only: each neighbor-compare re-opens the schema store (expensive reindex),
  # so we verify correctness on the 9 core edge types and skip the alltypes re-open.
  if json_file_valid "${logdir}/neighbor-compare-core.json"; then
    echo "[skip] neighbor-compare ${vid}"; return 0
  fi
  "$BIN" --io-backend "$IO_BACKEND" neighbor-compare \
    --left-data-dir "$SCHEMA_STORE" --right-data-dir "$store" \
    --sample-plan "$CORE_PLAN" --right-semantic-degree-hint \
    > "${logdir}/neighbor-compare-core.json" 2> "${logdir}/neighbor-compare-core.err"
}

assert_generated_store_path() {
  case "$1" in
    "${STORE_PREFIX}-"*) ;;
    *) echo "refuse to delete non-generated store path: $1" >&2; exit 20 ;;
  esac
}

capture_complete() {
  local logdir="$1"
  [[ -s "${logdir}/import.stdout" ]] || return 1
  [[ -s "${logdir}/file-summary.tsv" ]] || return 1
  json_file_valid "${logdir}/fair-core-s${CORE_SAMPLES}-r1.json" || return 1
  if [[ "$RUN_ALLTYPES" == "1" ]]; then
    json_file_valid "${logdir}/fair-alltypes-s${ALLTYPES_SAMPLES}-r1.json" || return 1
  fi
}

delete_store_after_capture() {
  local store="$1" logdir="$2" role="$3"
  assert_generated_store_path "$store"
  if ! capture_complete "$logdir"; then
    echo "refuse to delete ${role}: incomplete capture in ${logdir}" >&2; exit 21
  fi
  if [[ "$role" != "schema" ]] && want_nc "$role"; then
    json_file_valid "${logdir}/neighbor-compare-core.json" || {
      echo "refuse to delete ${role}: missing core neighbor-compare" >&2; exit 22; }
  fi
  echo "[del ] ${role} store: ${store}"
  rm -rf -- "$store"
  [[ ! -e "$store" ]] || { echo "delete failed: ${store}" >&2; exit 23; }
}

# ---- main --------------------------------------------------------------------
main() {
  require_input
  build_binary
  mkdir -p "$PLAN_DIR"
  echo "=== E11 baseline matrix scale=${SCALE} date_tag=${DATE_TAG} memgraph=${MEMGRAPH_BYTES} ==="
  df -h /data || true

  # 1) schema reference: import, keep, generate shared plans, fair reads.
  run_import "$SCHEMA_VID" schema "$SCHEMA_STORE" "$SCHEMA_LOG"
  generate_plans
  run_fair_reads "$SCHEMA_STORE" "$SCHEMA_LOG"
  run_ldbc_validate "$SCHEMA_STORE" "$SCHEMA_LOG"

  # 2) candidates: import -> fair reads -> neighbor-compare(vs schema) -> delete.
  for entry in "${VARIANTS[@]}"; do
    local vid="${entry%%:*}" layout="${entry##*:}"
    [[ "$vid" == "$SCHEMA_VID" ]] && continue
    want_variant "$vid" || { echo "[skip] ${vid} (not in ONLY_VARIANTS)"; continue; }
    local store="${STORE_PREFIX}-${vid}-${DATE_TAG}"
    local logdir="remote-logs/e11-${SCALE}-${vid}-${DATE_TAG}"
    run_import "$vid" "$layout" "$store" "$logdir"
    run_fair_reads "$store" "$logdir"
    if want_nc "$vid"; then run_neighbor_compare "$vid" "$store" "$logdir"; fi
    run_ldbc_validate "$store" "$logdir"
    df -h /data > "${logdir}/df-after-capture.log" || true
    if [[ "$DELETE_CANDIDATES_AFTER_CAPTURE" == "true" ]]; then
      delete_store_after_capture "$store" "$logdir" "$vid"
      df -h /data > "${logdir}/df-after-delete.log" || true
    fi
  done

  # 3) drop schema reference last (after all neighbor-compares done).
  if [[ "$DELETE_SCHEMA_AFTER_ALL" == "true" && -z "$ONLY_VARIANTS" ]]; then
    delete_store_after_capture "$SCHEMA_STORE" "$SCHEMA_LOG" schema
  fi

  echo "=== E11 baseline matrix complete: scale=${SCALE} date_tag=${DATE_TAG} ==="
  echo "logs: remote-logs/e11-*-${DATE_TAG}/"
}

main "$@"
