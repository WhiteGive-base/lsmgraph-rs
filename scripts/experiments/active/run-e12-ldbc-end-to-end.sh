#!/usr/bin/env bash
# Run LDBC SNB end-to-end benchmarks across all E11 SemL0 variants.
#
# Required env vars:
#   VARIANT_TAG   DATE_TAG of the completed E11 baseline matrix run
#                 (e.g. "20260606-477d61"). Stores are expected at
#                 store/e11-<variant>-${VARIANT_TAG}.
#
# Optional env vars (defaults as shown):
#   SF10_THREADS="4 8 16"
#   SF30_THREADS="8"
#   WARMUP=200
#   OPERATION_COUNT=1000
#   IO_BACKEND=direct
#   BUILD_RELEASE=false
#   RUN_SF10=true
#   RUN_SF30=true
#   DRIVER_SCRIPT=deps/ldbc_snb_interactive_impls/lsmgraph/run_high_thread_benchmarks.sh
#
# Output:
#   remote-logs/e12-<variant>-${VARIANT_TAG}/   per-variant logs and metrics
#   remote-logs/e12-merged-summary-${VARIANT_TAG}/  comparison tables (ldbc-comparison.tsv/md)

set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

# ---------------------------------------------------------------------------
# Validate required inputs
# ---------------------------------------------------------------------------
if [[ -z "${VARIANT_TAG:-}" ]]; then
  echo "ERROR: VARIANT_TAG is required (DATE_TAG of completed E11 baseline matrix run)" >&2
  echo "Example: VARIANT_TAG=20260606-477d61 bash $0" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
ROOT_LOG_BASE="remote-logs"
MERGED_SUMMARY="${ROOT_LOG_BASE}/e12-merged-summary-${VARIANT_TAG}"
DRIVER_SCRIPT="${DRIVER_SCRIPT:-deps/ldbc_snb_interactive_impls/lsmgraph/run_high_thread_benchmarks.sh}"
ANALYSIS_SCRIPT="scripts/analysis/current/summarize-end-to-end-evidence.py"
BIN="${BIN:-target/release/lsmgraph}"

SF10_THREADS="${SF10_THREADS:-4 8 16}"
SF30_THREADS="${SF30_THREADS:-8}"
WARMUP="${WARMUP:-200}"
OPERATION_COUNT="${OPERATION_COUNT:-1000}"
IO_BACKEND="${IO_BACKEND:-direct}"
BUILD_RELEASE="${BUILD_RELEASE:-false}"
RUN_SF10="${RUN_SF10:-true}"
RUN_SF30="${RUN_SF30:-true}"

# ---------------------------------------------------------------------------
# Variants: (name -> store suffix).  Only variants that have a completed
# store under store/e11-<name>-${VARIANT_TAG} will be run.
# ---------------------------------------------------------------------------
declare -a VARIANTS=(
  "naive"
  "lsmgraph-style"
  "schema-only"
  "benefit-scored"
  "full-semantic"
  "full-compact"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2
}

record_variant_meta() {
  local variant="$1"
  local out_dir="$2"
  mkdir -p "$out_dir"
  {
    echo "date=$(date -Is)"
    echo "pwd=$(pwd)"
    echo "variant=${variant}"
    echo "variant_tag=${VARIANT_TAG}"
    echo "bin=${BIN}"
    echo "io_backend=${IO_BACKEND}"
    echo "sf10_threads=${SF10_THREADS}"
    echo "sf30_threads=${SF30_THREADS}"
    echo "warmup=${WARMUP}"
    echo "operation_count=${OPERATION_COUNT}"
    echo "run_sf10=${RUN_SF10}"
    echo "run_sf30=${RUN_SF30}"
    echo "build_release=${BUILD_RELEASE}"
    echo "driver_script=${DRIVER_SCRIPT}"
    echo "e11_variant_tag=${VARIANT_TAG}"
    "${BIN}" --version 2>&1 || true
  } > "${out_dir}/run.meta"
  git rev-parse HEAD > "${out_dir}/git-head.txt"  2> "${out_dir}/git-head.err"  || true
  git status --short   > "${out_dir}/git-status.txt" 2> "${out_dir}/git-status.err" || true
  git diff HEAD --stat > "${out_dir}/git-diff-stat.txt" 2> "${out_dir}/git-diff-stat.err" || true
}

run_ldbc_variant() {
  local variant="$1"
  local store="$2"
  local out_dir="$3"

  log "=== LDBC E12 ${variant} ==="
  log "  store : ${store}"
  log "  out   : ${out_dir}"

  mkdir -p "$out_dir"
  record_variant_meta "$variant" "$out_dir"

  # Fresh OUT_DIR per variant (don't overwrite)
  set +e
  OUT_DIR="$out_dir" \
    SF10_BASE_STORE="$store" \
    SF30_BASE_STORE="$store" \
    SF10_THREADS="$SF10_THREADS" \
    SF30_THREADS="$SF30_THREADS" \
    WARMUP="$WARMUP" \
    OPERATION_COUNT="$OPERATION_COUNT" \
    IO_BACKEND="$IO_BACKEND" \
    BUILD_RELEASE="$BUILD_RELEASE" \
    RUN_SF10="$RUN_SF10" \
    RUN_SF30="$RUN_SF30" \
    bash "$DRIVER_SCRIPT" \
    > "${out_dir}/runner.stdout" \
    2> "${out_dir}/runner.stderr"
  local status=$?
  set -e
  echo "$status" > "${out_dir}/driver-exit-code.txt"

  if [[ "$status" -ne 0 ]]; then
    log "WARNING: LDBC driver for ${variant} exited ${status}"
    log "  (check ${out_dir}/runner.stderr for details)"
    # Don't fail the whole run — record and continue
  else
    log "  LDBC driver completed successfully for ${variant}"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

mkdir -p "$MERGED_SUMMARY"
log "E12 LDBC end-to-end start"
log "  variant_tag  : ${VARIANT_TAG}"
log "  merged out   : ${MERGED_SUMMARY}"

# Record top-level metadata
{
  echo "date=$(date -Is)"
  echo "pwd=$(pwd)"
  echo "variant_tag=${VARIANT_TAG}"
  echo "bin=${BIN}"
  echo "io_backend=${IO_BACKEND}"
  echo "sf10_threads=${SF10_THREADS}"
  echo "sf30_threads=${SF30_THREADS}"
  echo "warmup=${WARMUP}"
  echo "operation_count=${OPERATION_COUNT}"
  echo "run_sf10=${RUN_SF10}"
  echo "run_sf30=${RUN_SF30}"
  echo "build_release=${BUILD_RELEASE}"
  echo "e11_variant_tag=${VARIANT_TAG}"
  echo "driver_script=${DRIVER_SCRIPT}"
  echo "analysis_script=${ANALYSIS_SCRIPT}"
  "${BIN}" --version 2>&1 || true
} > "${MERGED_SUMMARY}/run.meta"
git rev-parse HEAD > "${MERGED_SUMMARY}/git-head.txt"  2> "${MERGED_SUMMARY}/git-head.err"  || true
git status --short   > "${MERGED_SUMMARY}/git-status.txt" 2> "${MERGED_SUMMARY}/git-status.err" || true

declare -a RAN_VARIANTS=()
declare -a LDBC_DIRS=()

for variant in "${VARIANTS[@]}"; do
  store="store/e11-${variant}-${VARIANT_TAG}"
  out_dir="remote-logs/e12-${variant}-${VARIANT_TAG}"

  if [[ ! -d "$store" ]]; then
    log "SKIP ${variant}: store not found at ${store}"
    continue
  fi

  if [[ -d "$out_dir" && -f "${out_dir}/PASS" ]]; then
    log "SKIP ${variant}: output already complete at ${out_dir}/PASS"
  else
    run_ldbc_variant "$variant" "$store" "$out_dir"
    touch "${out_dir}/PASS"
  fi

  RAN_VARIANTS+=("$variant")
  LDBC_DIRS+=("$out_dir")
done

if [[ ${#RAN_VARIANTS[@]} -eq 0 ]]; then
  log "ERROR: no variants found with tag ${VARIANT_TAG}"
  exit 2
fi

log "Ran variants: ${RAN_VARIANTS[*]}"

# ---------------------------------------------------------------------------
# Build merged comparison via summarize-end-to-end-evidence.py
# ---------------------------------------------------------------------------
log "Running ${ANALYSIS_SCRIPT} to produce merged comparison..."

ANALYSIS_OUT="${MERGED_SUMMARY}/analysis"
mkdir -p "$ANALYSIS_OUT"

# Build --ldbc-dir args for all variant output dirs
LDBC_ARG_LIST=()
for dir in "${LDBC_DIRS[@]}"; do
  LDBC_ARG_LIST+=("--ldbc-dir" "$dir")
done

python3 "$ANALYSIS_SCRIPT" \
  --out-dir "$ANALYSIS_OUT" \
  "${LDBC_ARG_LIST[@]}" \
  > "${MERGED_SUMMARY}/analysis.stdout" \
  2> "${MERGED_SUMMARY}/analysis.stderr"

# ---------------------------------------------------------------------------
# Produce the comparison TSV/MD that merges all variants side-by-side
# ---------------------------------------------------------------------------
python3 - "$MERGED_SUMMARY" "${LDBC_DIRS[@]}" <<'PYTHON'
"""Build a side-by-side variant comparison TSV and Markdown from ldbc-end-to-end-summary.tsv files."""
import csv
import sys
from pathlib import Path

out_base = Path(sys.argv[1])
ldbc_dirs = [Path(d) for d in sys.argv[2:]]

SUMMARY_TSV = "ldbc-end-to-end-summary.tsv"
OPS_TSV = "ldbc-operation-latency-summary.tsv"

def read_tsv(path: Path) -> list[dict[str, str]]:
    rows = []
    if not path.exists():
        return rows
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            rows.append(row)
    return rows

# Collect all case-level summaries
case_rows_by_case = {}
for ldbc_dir in ldbc_dirs:
    ts = ldbc_dir / SUMMARY_TSV
    for row in read_tsv(ts):
        case_rows_by_case[row.get("case", "")] = row

# Collect all operation-level rows
op_rows_by_variant_scale_threads = {}
for ldbc_dir in ldbc_dirs:
    ts = ldbc_dir / OPS_TSV
    for row in read_tsv(ts):
        key = (row.get("case", ""), row.get("operation", ""))
        op_rows_by_variant_scale_threads[key] = row

# ---------- Build ldbc-comparison.tsv ----------
all_cases = sorted(case_rows_by_case.keys(),
                   key=lambda c: (c.split("-")[0] if c else "", c))

key_fields = ["case", "scale", "threads", "driver_status", "operation_count", "qps",
              "queries", "updates", "slow_requests",
              "read_lock_wait_avg_us", "read_lock_wait_max_us",
              "write_lock_wait_avg_us", "write_lock_wait_max_us",
              "query_exec_avg_us", "query_exec_max_us",
              "update_exec_avg_us", "update_exec_max_us"]

comp_tsv = out_base / "ldbc-comparison.tsv"
with comp_tsv.open("w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=key_fields, delimiter="\t", extrasaction="ignore")
    writer.writeheader()
    for case in all_cases:
        writer.writerow(case_rows_by_case[case])
print(f"Written {comp_tsv}")

# ---------- Build ldbc-comparison.md ----------
variant_names = sorted({row.get("case", "").split("-tc")[0]
                         for row in case_rows_by_case.values()})

comp_md = out_base / "ldbc-comparison.md"
with comp_md.open("w", encoding="utf-8") as fh:
    fh.write("# LDBC SNB End-to-End Comparison — E12\n\n")

    # --- Summary table ---
    fh.write("## Throughput & General Metrics\n\n")
    fh.write("| Variant | Scale | Threads | QPS | Queries | Updates | Slow | "
             "Read lock avg/max us | Write lock avg/max us | Query avg/max us | "
             "Update avg/max us |\n")
    fh.write("|---|---:|---:|---:|---:|---:|---:|---|---|---|---|---|\n")
    for case in all_cases:
        row = case_rows_by_case.get(case, {})
        variant = case.split("-tc")[0] if case else ""
        fh.write(
            f"| {variant} | {row.get('scale','')} | {row.get('threads','')} | "
            f"{row.get('qps','')} | {row.get('queries','')} | {row.get('updates','')} | "
            f"{row.get('slow_requests','')} | "
            f"{row.get('read_lock_wait_avg_us','')}/{row.get('read_lock_wait_max_us','')} | "
            f"{row.get('write_lock_wait_avg_us','')}/{row.get('write_lock_wait_max_us','')} | "
            f"{row.get('query_exec_avg_us','')}/{row.get('query_exec_max_us','')} | "
            f"{row.get('update_exec_avg_us','')}/{row.get('update_exec_max_us','')} |\n"
        )
    fh.write("\n")

    # --- Per-operation latency table (key reads) ---
    KEY_READS = [
        "LdbcQuery1", "LdbcQuery3", "LdbcQuery8", "LdbcQuery12",
        "LdbcShortQuery1PersonProfile", "LdbcShortQuery3PersonFriends",
    ]
    KEY_UPDATES = [
        "LdbcUpdate1AddPerson", "LdbcUpdate2AddPostLike",
        "LdbcUpdate6AddPost", "LdbcUpdate7AddComment",
    ]

    all_ops = sorted(set(row.get("operation", "")
                         for row in op_rows_by_variant_scale_threads.values()))
    ops_to_show = [op for op in all_ops
                   if op in KEY_READS or op in KEY_UPDATES]

    if ops_to_show:
        fh.write("## Operation Latency — Key Queries\n\n")
        fh.write("| Case | Operation | Count | Mean ms | P50 ms | P95 ms | P99 ms |\n")
        fh.write("|---|---|---:|---:|---:|---:|---:|\n")
        for case in all_cases:
            for op in ops_to_show:
                key = (case, op)
                row = op_rows_by_variant_scale_threads.get(key, {})
                if row.get("count"):
                    fh.write(
                        f"| {case} | {op} | {row.get('count','')} | "
                        f"{row.get('mean_ms','')} | {row.get('p50_ms','')} | "
                        f"{row.get('p95_ms','')} | {row.get('p99_ms','')} |\n"
                    )
        fh.write("\n")

    fh.write(f"_Generated from E11 variant tag `{Path('.').name}` — "
             f"{len(all_cases)} cases across {len(variant_names)} variants_\n")

print(f"Written {comp_md}")
PYTHON

log "E12 complete"
log "  merged summary : ${MERGED_SUMMARY}"
log "  comparison     : ${MERGED_SUMMARY}/ldbc-comparison.tsv"
log "  comparison     : ${MERGED_SUMMARY}/ldbc-comparison.md"
for variant in "${RAN_VARIANTS[@]}"; do
  log "  ${variant} : remote-logs/e12-${variant}-${VARIANT_TAG}/"
done
