#!/usr/bin/env bash
set -u

# SF100 strong baseline / SemL0 ablation runner (real LDBC SF100, disk-safe).
#
# Rewritten 2026-06-10 from the earlier ~SF1 version. Key differences:
#   * INPUT defaults to real LDBC SF100 (dynamic/+static/), not the small tugraph set.
#   * MEMGRAPH_BYTES=64MB (keeps L0 ~1-2k segments at SF100, like the e11 matrix).
#   * SF100 feasibility env: SNB_SKIP_ADJ_CACHE for every import; SNB_SKIP_SEM_INDEX
#     only around plan generation (its bench output is discarded).
#   * Disk-safe: import -> stats -> bench(read via shared plan) -> [compare] -> DELETE,
#     one variant at a time; only the `schema` reference store is kept.
#   * Budget sweep: semantic-budgeted is run as budg-bN variants with the byte gate
#     disabled (min-edge-type-bytes 0) so --semantic-budget-max-extra-l0-files is the
#     single controllable cost axis (read-amp vs L0 file budget trade-off curve).
#
# Usage (after `cargo build --release` in /data/WorkSpace/lsmgraph-rs):
#   SAMPLES=5000 SCALE=sf100 baseline/codex_qslsm_sf100_strong_baseline.sh
# Smoke:
#   SAMPLES=50 SCALE=sf1 INPUT=/data/WorkSpace/ldbc-sf1/social_network \
#     baseline/codex_qslsm_sf100_strong_baseline.sh

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
SCALE="${SCALE:-sf100}"
INPUT="${INPUT:-/data/WorkSpace/ldbc-${SCALE}/social_network}"
RUN_ID="${RUN_ID:-qslsm-${SCALE}-strong-baseline-$(date +%Y%m%d-%H%M%S)}"
SAMPLES="${SAMPLES:-50}"
EDGE_TYPES="${EDGE_TYPES:-1,2,3,7,8,9,10,11,12}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-67108864}"
IO_BACKEND="${IO_BACKEND:-blocking}"
KV_STYLE_SCRIPT="${KV_STYLE_SCRIPT:-baseline/kv_style_baseline.py}"
BIN="${BIN:-target/release/lsmgraph}"

# Internal controllable baselines to run (schema is always run as the reference).
# Trim this to reuse a previous run's identical-layout variants (e.g. reuse e11's
# naive/lsmgraph-style/label-only/degree-only and only run the SemL0-critical ones).
BASELINE_VARIANTS="${BASELINE_VARIANTS:-naive lsmgraph-style label-only edge-type-only degree-only semantic}"

# Budget sweep points (extra L0 file budget). Byte gate is disabled so this is the
# only knob. budg-b0 == schema-equivalent; large budget -> full-semantic read-amp.
BUDGET_SWEEP="${BUDGET_SWEEP:-0 32 64 128 256 512 1024}"

# Correctness spot-check: neighbor-compare opens TWO SF100 engines at once (each
# rebuilds the ~260GB degree directory) so it is OFF by default at SF100 (correctness
# is scale-invariant and already verified mismatches=0 at SF1/SF30). Set RUN_COMPARE=1
# to spot-check; it runs with SNB_SKIP_SEM_INDEX on both sides to halve memory.
RUN_COMPARE="${RUN_COMPARE:-0}"
RUN_FULL_COMPACT="${RUN_FULL_COMPACT:-0}"   # disk-heavy (~2x store); off by default
RUN_FEEDBACK="${RUN_FEEDBACK:-1}"

# Conservative feedback thresholds (override for smoke: RA_MIN_QUERIES=1 RA_MIN_SCORE=0).
RA_MIN_QUERIES="${RA_MIN_QUERIES:-10}"
RA_MIN_SCORE="${RA_MIN_SCORE:-10}"
RA_MIN_L0_SEGMENTS="${RA_MIN_L0_SEGMENTS:-2}"

LOG_ROOT="${LOG_ROOT:-${ROOT}/remote-logs}"
OUT_DIR="${OUT_DIR:-${LOG_ROOT}/${RUN_ID}}"
STORE_ROOT="${STORE_ROOT:-store/${RUN_ID}}"
PROGRESS="${OUT_DIR}/progress.log"
MANIFEST="${OUT_DIR}/manifest.tsv"
PLAN="${OUT_DIR}/sample-plan-core-s${SAMPLES}.json"
SCHEMA_STORE="${STORE_ROOT}/schema"

cd "$ROOT" || exit 1
mkdir -p "$OUT_DIR" "$STORE_ROOT"

log() { printf '[%(%Y-%m-%d %H:%M:%S)T] %s\n' -1 "$*" | tee -a "$PROGRESS"; }
die() { log "FATAL: $*"; exit 1; }

store_size_bytes() { [ -d "$1" ] && du -sb "$1" | awk '{print $1}' || printf '0'; }

run_logged() {  # label cmd...
  local label="$1"; shift
  local out="${OUT_DIR}/${label}.json" err="${OUT_DIR}/${label}.err"
  log "start ${label}"
  "$@" >"$out" 2>"$err"
  local status=$?
  log "finish ${label} status=${status}"
  [ "$status" -ne 0 ] && die "${label} failed; see ${err}"
  printf '%s' "$out"
}

record_manifest() {  # variant layout kind store file
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" "$(store_size_bytes "$4")" >> "$MANIFEST"
}

import_variant() {  # name layout extra-flags...
  local name="$1" layout="$2"; shift 2
  local store="${STORE_ROOT}/${name}"
  log "import ${name} (layout=${layout}) ${*}"
  SNB_SKIP_ADJ_CACHE=1 "$BIN" --io-backend "$IO_BACKEND" import \
    --input "$INPUT" --data-dir "$store" --relation snb-full \
    --memgraph-bytes "$MEMGRAPH_BYTES" --l0-layout "$layout" "$@" \
    > "${OUT_DIR}/${name}-import.stdout" 2> "${OUT_DIR}/${name}-import.stderr" \
    || die "import ${name} failed"
  record_manifest "$name" "$layout" "import" "$store" "${OUT_DIR}/${name}-import.stderr"
}

bench_variant() {  # name layout
  local name="$1" layout="$2" store="${STORE_ROOT}/${name}"
  run_logged "${name}-stats" "$BIN" --io-backend "$IO_BACKEND" stats --data-dir "$store" >/dev/null
  record_manifest "$name" "$layout" "stats" "$store" "${OUT_DIR}/${name}-stats.json"
  run_logged "${name}-bench" "$BIN" --io-backend "$IO_BACKEND" storage-bench \
    --data-dir "$store" --edge-types "$EDGE_TYPES" --sample-plan-in "$PLAN" >/dev/null
  record_manifest "$name" "$layout" "bench" "$store" "${OUT_DIR}/${name}-bench.json"
}

compare_variant() {  # name
  [ "$RUN_COMPARE" = "1" ] || return 0
  local name="$1" store="${STORE_ROOT}/${name}"
  log "neighbor-compare schema vs ${name} (SNB_SKIP_SEM_INDEX both sides)"
  SNB_SKIP_SEM_INDEX=1 "$BIN" --io-backend "$IO_BACKEND" neighbor-compare \
    --left-data-dir "$SCHEMA_STORE" --right-data-dir "$store" \
    --sample-plan "$PLAN" --max-mismatches 1 \
    > "${OUT_DIR}/compare-schema-vs-${name}.json" 2> "${OUT_DIR}/compare-schema-vs-${name}.err" \
    || die "compare ${name} failed"
}

del_store() { local s="$1"; [ "$s" = "$SCHEMA_STORE" ] && return 0; log "delete ${s}"; rm -rf "$s"; }

process_variant() {  # name layout extra-flags...
  local name="$1" layout="$2"; shift 2
  import_variant "$name" "$layout" "$@"
  bench_variant "$name" "$layout"
  compare_variant "$name"
  del_store "${STORE_ROOT}/${name}"
}

main() {
  : > "$PROGRESS"
  printf 'variant\tlayout\tkind\tstore\tfile\tstore_size_bytes\n' > "$MANIFEST"
  log "run_id=${RUN_ID} scale=${SCALE} input=${INPUT}"
  log "samples=${SAMPLES} edge_types=${EDGE_TYPES} memgraph_bytes=${MEMGRAPH_BYTES} budget_sweep='${BUDGET_SWEEP}'"
  {
    echo "{\"run_id\":\"${RUN_ID}\",\"scale\":\"${SCALE}\",\"input\":\"${INPUT}\",\"samples\":${SAMPLES},"
    echo "\"edge_types\":\"${EDGE_TYPES}\",\"memgraph_bytes\":${MEMGRAPH_BYTES},\"budget_sweep\":\"${BUDGET_SWEEP}\","
    echo "\"run_compare\":${RUN_COMPARE},\"started_at\":\"$(date -Is)\"}"
  } > "${OUT_DIR}/run-config.json"

  # 1) schema reference first (kept for the whole run), then the shared sample plan.
  import_variant "schema" "schema"
  bench_variant "schema" "schema"
  log "generate sample plan from schema (SNB_SKIP_SEM_INDEX for plan-gen only)"
  SNB_SKIP_SEM_INDEX=1 "$BIN" --io-backend "$IO_BACKEND" storage-bench \
    --data-dir "$SCHEMA_STORE" --edge-types "$EDGE_TYPES" --samples "$SAMPLES" \
    --semantic-degree-hint --sample-plan-degree-hint --sample-plan-out "$PLAN" \
    > "${OUT_DIR}/plan.stdout" 2> "${OUT_DIR}/plan.stderr" || die "plan-gen failed"
  # re-bench schema on the shared plan for an apples-to-apples read-amp number
  run_logged "schema-bench" "$BIN" --io-backend "$IO_BACKEND" storage-bench \
    --data-dir "$SCHEMA_STORE" --edge-types "$EDGE_TYPES" --sample-plan-in "$PLAN" >/dev/null

  # 2) internal controllable baselines (import -> bench -> [compare] -> delete).
  want() { case " $BASELINE_VARIANTS " in *" $1 "*) return 0;; *) return 1;; esac; }
  want naive          && process_variant "naive"          "naive"
  want lsmgraph-style && process_variant "lsmgraph-style" "lsmgraph-style"
  want label-only     && process_variant "label-only"     "label-only"
  want edge-type-only && process_variant "edge-type-only" "edge-type-only"
  want degree-only    && process_variant "degree-only"    "degree-only"
  want semantic       && process_variant "semantic"       "semantic"

  # 3) SemL0 budget sweep (byte gate disabled -> file budget is the cost axis).
  for b in $BUDGET_SWEEP; do
    process_variant "budg-b${b}" "semantic-budgeted" \
      --semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files "$b"
  done

  # 4) kv-style appendix simulation (derived from the schema bench json).
  if [ -f "$KV_STYLE_SCRIPT" ]; then
    log "kv-style encoding simulation (appendix)"
    python3 "$KV_STYLE_SCRIPT" --input "${OUT_DIR}/schema-bench.json" \
      --output "${OUT_DIR}/kv-style-bench.json" --key-layout edge_type_src_dst_ts \
      > "${OUT_DIR}/kv-style.stdout" 2> "${OUT_DIR}/kv-style.err" || log "kv-style failed (non-fatal)"
  fi

  # 5) feedback (no-feedback vs feedback) on schema + a mid budget point.
  if [ "$RUN_FEEDBACK" = "1" ]; then
    for fb in schema:schema "budg-b256:semantic-budgeted"; do
      name="${fb%%:*}"; layout="${fb#*:}"; store="${STORE_ROOT}/${name}"
      [ -d "$store" ] || { log "feedback skip ${name} (store deleted)"; continue; }
      run_logged "${name}-feedback-bench" "$BIN" --io-backend "$IO_BACKEND" storage-bench \
        --data-dir "$store" --edge-types "$EDGE_TYPES" --sample-plan-in "$PLAN" \
        --auto-compact --ra-min-queries "$RA_MIN_QUERIES" --ra-min-score "$RA_MIN_SCORE" \
        --ra-min-l0-segments "$RA_MIN_L0_SEGMENTS" >/dev/null
    done
  fi

  # 6) full-compact upper baseline last (disk-heavy). Skips neighbor-compare.
  if [ "$RUN_FULL_COMPACT" = "1" ]; then
    log "full-compact upper baseline (needs ~2x store free)"
    import_variant "full-compact" "full-compact"
    bench_variant "full-compact" "full-compact"
    del_store "${STORE_ROOT}/full-compact"
  fi

  del_store "$SCHEMA_STORE"
  log "done ${RUN_ID}; outputs in ${OUT_DIR}"
}

main "$@"
