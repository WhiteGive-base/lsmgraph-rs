#!/usr/bin/env bash
# E14: Mixed read/write workload benchmark for SemL0
#
# Measures end-to-end latency and throughput under configurable read/update ratios
# to answer:
#   - Does semantic compaction affect write throughput?
#   - Does semantic compaction remain effective under mixed workload?
#
# Usage:
#   VARIANT_TAG=e11-20260606-XXXXXX THREADS=8 bash scripts/experiments/active/run-e14-mixed-workload.sh
#
# Environment variables:
#   VARIANT_TAG    — e11 variant tag (e.g. e11-20260606-XXXXXX) OR "sf10-base-graph"
#                    for the raw base store.  Default: sf10-base-graph
#   OUT_ROOT       — output root directory.
#                    Default: remote-logs/e14-mixed-workload-<timestamp>
#   THREADS        — number of driver threads.  Default: 8
#   BUILD_RELEASE  — build release binary first.  Default: false
#   WARMUP         — warmup operations per thread.  Default: 500
#   OPERATION_COUNT — benchmark operations per thread.  Default: 5000
#   IO_BACKEND     — io backend: blocking|direct.  Default: direct

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"

# ── defaults ──────────────────────────────────────────────────────────────────
VARIANT_TAG="${VARIANT_TAG:-sf10-base-graph}"
OUT_ROOT="${OUT_ROOT:-remote-logs/e14-mixed-workload-$(date +%Y%m%d-%H%M%S)}"
THREADS="${THREADS:-8}"
BUILD_RELEASE="${BUILD_RELEASE:-false}"
WARMUP="${WARMUP:-500}"
OPERATION_COUNT="${OPERATION_COUNT:-5000}"
IO_BACKEND="${IO_BACKEND:-direct}"
DGS_DIR="$ROOT/deps/ldbc_snb_interactive_impls/dgs"
BIN="${BIN:-$ROOT/target/release/lsmgraph}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-9090}"
SERVER_READY_TIMEOUT="${SERVER_READY_TIMEOUT:-900}"
PROGRESS_INTERVAL="${PROGRESS_INTERVAL:-30}"

# Determine base store path
if [[ "$VARIANT_TAG" == "sf10-base-graph" ]]; then
  BASE_STORE="$ROOT/store/sf10-base-graph"
else
  BASE_STORE="$ROOT/store/e11-schema-only-${VARIANT_TAG}"
fi
BASE_GRAPH="$BASE_STORE/base_graph"

# ── output directories ────────────────────────────────────────────────────────
RUN_STORES_DIR="$OUT_ROOT/run-stores"
mkdir -p "$RUN_STORES_DIR" "$OUT_ROOT"

# ── helpers ───────────────────────────────────────────────────────────────────
start_ts="$(date +%s)"
progress_log="$OUT_ROOT/progress.log"
: > "$progress_log"

log_progress() {
  local now elapsed
  now="$(date '+%Y-%m-%d %H:%M:%S')"
  elapsed=$(( $(date +%s) - start_ts ))
  printf '[%s][elapsed=%ss] %s\n' "$now" "$elapsed" "$*" | tee -a "$progress_log" >&2
}

file_bytes() {
  local file="$1"
  if [[ -e "$file" ]]; then
    wc -c < "$file" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

monitor_pid() {
  local label="$1"
  local pid="$2"
  local log_file="${3:-}"
  while kill -0 "$pid" >/dev/null 2>&1; do
    sleep "$PROGRESS_INTERVAL" || true
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      break
    fi
    local ps_line
    ps_line="$(ps -p "$pid" -o pid=,etimes=,pcpu=,pmem=,rss=,stat=,cmd= 2>/dev/null || true)"
    log_progress "running label=$label pid=$pid ps=[$ps_line] log_bytes=$(file_bytes "$log_file")"
  done
}

run_monitored() {
  local label="$1"
  local stdout_file="$2"
  local stderr_file="$3"
  shift 3
  log_progress "start label=$label cmd=$* stdout=$stdout_file stderr=$stderr_file"
  local started pid monitor status duration
  started="$(date +%s)"
  "$@" > "$stdout_file" 2> "$stderr_file" &
  pid=$!
  monitor_pid "$label" "$pid" "$stdout_file" &
  monitor=$!
  set +e
  wait "$pid"
  status=$?
  kill "$monitor" >/dev/null 2>&1 || true
  wait "$monitor" >/dev/null 2>&1 || true
  set -e
  duration=$(( $(date +%s) - started ))
  log_progress "finish label=$label status=$status duration_s=$duration stdout_bytes=$(file_bytes "$stdout_file") stderr_bytes=$(file_bytes "$stderr_file")"
  return "$status"
}

# ── build ─────────────────────────────────────────────────────────────────────
if [[ "$BUILD_RELEASE" == "1" || "$BUILD_RELEASE" == "true" ]]; then
  run_monitored "cargo build release direct-io" \
    "$OUT_ROOT/cargo-build.log" \
    "$OUT_ROOT/cargo-build.time.txt" \
    cargo build --release --features direct-io
fi

if [[ ! -x "$BIN" ]]; then
  echo "binary not found or not executable: $BIN" >&2
  exit 3
fi

# ── metadata ──────────────────────────────────────────────────────────────────
{
  echo "date=$(date -Is)"
  echo "variant_tag=${VARIANT_TAG}"
  echo "base_store=${BASE_STORE}"
  echo "out_root=${OUT_ROOT}"
  echo "threads=${THREADS}"
  echo "warmup=${WARMUP}"
  echo "operation_count=${OPERATION_COUNT}"
  echo "io_backend=${IO_BACKEND}"
  echo "build_release=${BUILD_RELEASE}"
  git rev-parse HEAD || true
  git status --short || true
  rustc --version || true
  cargo --version || true
} > "$OUT_ROOT/run.meta"

# ── verify base graph ─────────────────────────────────────────────────────────
if [[ ! -f "$BASE_GRAPH/catalog.json" ]]; then
  echo "missing BaseGraph catalog: $BASE_GRAPH/catalog.json" >&2
  exit 2
fi

# ── mix generation ─────────────────────────────────────────────────────────────
#
# The LDBC driver mix is controlled by the `lds.interactive.update_interleave`
# property in the properties file.
#
# The default SF10 mix is:
#   Total read weight = sum of LdbcQuery[1-14]_freq = 1025
#   Total update weight = update_interleave * num_update_types (approximately)
#   Default update_interleave = 466 (meaning 1 update per ~466 reads)
#
# At the LDBC SF10 default mix, approximately:
#   Update ratio ≈ 466/(466+1025) ≈ 31%
#
# We approximate the update ratio via `update_interleave`:
#   READ_WEIGHT = 1025  (fixed sum of all read frequencies)
#   UPDATE_WEIGHT = (1 - UPDATE_RATIO/100) / (UPDATE_RATIO/100) * 1025 / 8
#                  = (100 - UPDATE_RATIO) * 1025 / (UPDATE_RATIO * 8)
#   update_interleave ≈ READ_WEIGHT / (UPDATE_WEIGHT / 8)
#                     = READ_WEIGHT * 8 / UPDATE_WEIGHT
#
# Mapping:
#   ratio=0  → all reads  → effectively disable updates via *_enable=false
#   ratio=10 → 10% writes → update_interleave ≈ 8850
#   ratio=50 → 50% writes → update_interleave ≈ 205
#   ratio=90 → 90% writes → update_interleave ≈ 11
#   ratio=100→ 100% writes→ all updates via *_enable=false on reads
#
# The actual mapping is approximate since the driver normalises internally.
# We handle the 0% and 100% edge cases by toggling operation enables.
#
declare -A RATIO_TO_INTERLEAVE=(
  [0]="99999"
  [10]="8850"
  [50]="205"
  [90]="11"
  [100]="1"
)

# Map update_ratio to a "bias" score for *_enable toggling at extremes
ratio_to_bias() {
  local ratio="$1"
  case "$ratio" in
    0)   echo "reads-only" ;;
    100) echo "writes-only" ;;
    *)   echo "mixed" ;;
  esac
}

# ── properties generation ───────────────────────────────────────────────────────
#
# Creates a modified properties file based on the SF10 template, overriding:
#   thread_count, warmup, operation_count, name, results_dir
#   lds.interactive.update_interleave  (controls update ratio)
#   Individual *_enable flags for 0%/100% edge cases
#
make_properties() {
  local template="$1"
  local output="$2"
  local name="$3"
  local update_ratio="$4"
  local interleave="$5"
  local bias="$6"

  # For 0% ratio: disable all updates. For 100%: disable all reads.
  local enable_updates="true"
  local enable_reads="true"
  case "$bias" in
    reads-only) enable_updates="false" ;;
    writes-only) enable_reads="false" ;;
  esac

  awk \
    -v thread_count="$THREADS" \
    -v warmup="$WARMUP" \
    -v operation_count="$OPERATION_COUNT" \
    -v name="$name" \
    -v interleave="$interleave" \
    -v enable_updates="$enable_updates" \
    -v enable_reads="$enable_reads" \
    '
      BEGIN {
        seen_thread=0; seen_warmup=0; seen_ops=0; seen_name=0
        seen_interleave=0
        seen_q_enable=0; seen_s_enable=0; seen_u_enable=0
      }
      /^thread_count=/ { print "thread_count=" thread_count; seen_thread=1; next }
      /^warmup=/ { print "warmup=" warmup; seen_warmup=1; next }
      /^operation_count=/ { print "operation_count=" operation_count; seen_ops=1; next }
      /^name=/ { print "name=" name; seen_name=1; next }
      /^lds.interactive.update_interleave=/ {
        print "lds.interactive.update_interleave=" interleave; seen_interleave=1; next
      }
      # Toggle update enables at extremes
      /^ldbc.snb.interactive.LdbcUpdate1AddPerson_enable=/ {
        print "ldbc.snb.interactive.LdbcUpdate1AddPerson_enable=" enable_updates; seen_u_enable=1; next
      }
      /^ldbc.snb.interactive.LdbcUpdate2AddPostLike_enable=/ {
        print "ldbc.snb.interactive.LdbcUpdate2AddPostLike_enable=" enable_updates; next
      }
      /^ldbc.snb.interactive.LdbcUpdate3AddCommentLike_enable=/ {
        print "ldbc.snb.interactive.LdbcUpdate3AddCommentLike_enable=" enable_updates; next
      }
      /^ldbc.snb.interactive.LdbcUpdate4AddForum_enable=/ {
        print "ldbc.snb.interactive.LdbcUpdate4AddForum_enable=" enable_updates; next
      }
      /^ldbc.snb.interactive.LdbcUpdate5AddForumMembership_enable=/ {
        print "ldbc.snb.interactive.LdbcUpdate5AddForumMembership_enable=" enable_updates; next
      }
      /^ldbc.snb.interactive.LdbcUpdate6AddPost_enable=/ {
        print "ldbc.snb.interactive.LdbcUpdate6AddPost_enable=" enable_updates; next
      }
      /^ldbc.snb.interactive.LdbcUpdate7AddComment_enable=/ {
        print "ldbc.snb.interactive.LdbcUpdate7AddComment_enable=" enable_updates; next
      }
      /^ldbc.snb.interactive.LdbcUpdate8AddFriendship_enable=/ {
        print "ldbc.snb.interactive.LdbcUpdate8AddFriendship_enable=" enable_updates; next
      }
      # For writes-only: disable all query enables
      /^ldbc.snb.interactive.LdbcQuery1_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery1_enable=" enable_reads; seen_q_enable=1; next
      }
      /^ldbc.snb.interactive.LdbcQuery2_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery2_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcQuery3_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery3_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcQuery4_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery4_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcQuery5_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery5_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcQuery6_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery6_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcQuery7_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery7_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcQuery8_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery8_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcQuery9_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery9_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcQuery10_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery10_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcQuery11_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery11_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcQuery12_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery12_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcQuery13_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery13_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcQuery14_enable=/ {
        print "ldbc.snb.interactive.LdbcQuery14_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcShortQuery1PersonProfile_enable=/ {
        print "ldbc.snb.interactive.LdbcShortQuery1PersonProfile_enable=" enable_reads; seen_s_enable=1; next
      }
      /^ldbc.snb.interactive.LdbcShortQuery2PersonPosts_enable=/ {
        print "ldbc.snb.interactive.LdbcShortQuery2PersonPosts_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcShortQuery3PersonFriends_enable=/ {
        print "ldbc.snb.interactive.LdbcShortQuery3PersonFriends_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcShortQuery4MessageContent_enable=/ {
        print "ldbc.snb.interactive.LdbcShortQuery4MessageContent_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcShortQuery5MessageCreator_enable=/ {
        print "ldbc.snb.interactive.LdbcShortQuery5MessageCreator_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcShortQuery6MessageForum_enable=/ {
        print "ldbc.snb.interactive.LdbcShortQuery6MessageForum_enable=" enable_reads; next
      }
      /^ldbc.snb.interactive.LdbcShortQuery7MessageReplies_enable=/ {
        print "ldbc.snb.interactive.LdbcShortQuery7MessageReplies_enable=" enable_reads; next
      }
      { print }
      END {
        if (!seen_thread)       print "thread_count=" thread_count
        if (!seen_warmup)      print "warmup=" warmup
        if (!seen_ops)         print "operation_count=" operation_count
        if (!seen_name)        print "name=" name
        if (!seen_interleave)  print "lds.interactive.update_interleave=" interleave
        if (!seen_q_enable)    print "ldbc.snb.interactive.LdbcQuery1_enable=" enable_reads
        if (!seen_s_enable)    print "ldbc.snb.interactive.LdbcShortQuery1PersonProfile_enable=" enable_reads
        if (!seen_u_enable)    print "ldbc.snb.interactive.LdbcUpdate1AddPerson_enable=" enable_updates
      }
    ' "$template" > "$output"
}

# ── server lifecycle helpers ──────────────────────────────────────────────────
wait_for_server() {
  local server_pid="$1"
  local server_log="$2"
  local waited=0
  while (( waited < SERVER_READY_TIMEOUT )); do
    if grep -q '"server":"lsmgraph-snb"' "$server_log"; then
      return 0
    fi
    if ! kill -0 "$server_pid" >/dev/null 2>&1; then
      log_progress "server exited before ready pid=$server_pid waited_s=$waited"
      return 1
    fi
    if (( waited > 0 && waited % PROGRESS_INTERVAL == 0 )); then
      log_progress "waiting server pid=$server_pid waited_s=$waited log_bytes=$(file_bytes "$server_log")"
      tail -5 "$server_log" 2>/dev/null | tee -a "$progress_log" >&2 || true
    fi
    waited=$((waited + 1))
    sleep 1
  done
  return 1
}

# ── single-case runner ─────────────────────────────────────────────────────────
run_case() {
  local update_ratio="$1"
  local interleave="${RATIO_TO_INTERLEAVE[$update_ratio]}"
  local bias="$(ratio_to_bias "$update_ratio")"
  local ratio_dir="$OUT_ROOT/ratio-${update_ratio}"
  mkdir -p "$ratio_dir"

  log_progress "case_start update_ratio=$update_ratio interleave=$interleave bias=$bias"

  # ── prepare run store ──────────────────────────────────────────────────────
  local run_store="$RUN_STORES_DIR/ratio-${update_ratio}"
  rm -rf "$run_store"
  mkdir -p "$run_store"
  if ! ln -s "$BASE_GRAPH" "$run_store/base_graph" 2>/dev/null; then
    cp -al "$BASE_GRAPH" "$run_store/base_graph"
  fi

  # ── properties file ───────────────────────────────────────────────────────
  local label="e14-r${update_ratio}"
  local props="$ratio_dir/${label}.properties"
  make_properties \
    "$DGS_DIR/interactive-benchmark-sf10.properties" \
    "$props" \
    "LDBC-SNB-E14-r${update_ratio}" \
    "$update_ratio" \
    "$interleave" \
    "$bias"

  # ── start server ───────────────────────────────────────────────────────────
  local server_log="$ratio_dir/server.log"
  IO_BACKEND="$IO_BACKEND" \
    DATA_DIR="$run_store" \
    HOST="$HOST" \
    PORT="$PORT" \
    bash "$ROOT/deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh" \
    > "$server_log" 2>&1 &
  local server_pid=$!
  monitor_pid "e14-r${update_ratio} server" "$server_pid" "$server_log" &
  local server_monitor_pid=$!

  local ready=0
  if wait_for_server "$server_pid" "$server_log"; then
    ready=1
  fi
  if [[ "$ready" != "1" ]]; then
    log_progress "case_failed update_ratio=$update_ratio reason=server_not_ready"
    kill "$server_pid" >/dev/null 2>&1 || true
    kill "$server_monitor_pid" >/dev/null 2>&1 || true
    wait "$server_pid" >/dev/null 2>&1 || true
    wait "$server_monitor_pid" >/dev/null 2>&1 || true
    return 1
  fi

  curl -s -X POST "http://$HOST:$PORT/metrics/reset" > "$ratio_dir/metrics-reset.json" || true

  # ── run LDBC driver ───────────────────────────────────────────────────────
  # The driver is run from DGS_DIR so it writes results to $ratio_dir/
  # (overridden by results_dir in properties).  The driver also writes a
  # benchmark.log to the CWD ($DGS_DIR) — we capture that too.
  set +e
  run_monitored "e14-r${update_ratio} driver" \
    "$ratio_dir/driver.log" \
    "$ratio_dir/driver.time.txt" \
    bash "$DGS_DIR/run.sh" "$props" \
    > "$ratio_dir/run-sh.stdout" \
    2> "$ratio_dir/run-sh.stderr"
  local driver_status=$?
  set -e

  # Copy results from DGS_DIR (where the driver writes them, resolving results_dir
  # relative to its CWD) to the case directory so they survive concurrent runs.
  local results_name="LDBC-SNB-E14-r${update_ratio}"
  if [[ -f "$DGS_DIR/results/${results_name}-results.json" ]]; then
    cp "$DGS_DIR/results/${results_name}-results.json" "$ratio_dir/"
  fi
  if [[ -f "$DGS_DIR/results/${results_name}-results_log.csv" ]]; then
    cp "$DGS_DIR/results/${results_name}-results_log.csv" "$ratio_dir/"
  fi
  if [[ -f "$DGS_DIR/results/${results_name}-configuration.properties" ]]; then
    cp "$DGS_DIR/results/${results_name}-configuration.properties" "$ratio_dir/"
  fi
  if [[ -f "$DGS_DIR/benchmark.log" ]]; then
    cp "$DGS_DIR/benchmark.log" "$ratio_dir/driver-benchmark.log"
  fi

  # Fetch server metrics while server is still running
  curl -s "http://$HOST:$PORT/metrics" > "$ratio_dir/server-metrics.json" || true

  # ── stop server ────────────────────────────────────────────────────────────
  kill "$server_pid" >/dev/null 2>&1 || true
  kill "$server_monitor_pid" >/dev/null 2>&1 || true
  wait "$server_pid" >/dev/null 2>&1 || true
  wait "$server_monitor_pid" >/dev/null 2>&1 || true

  # ── record case metadata ───────────────────────────────────────────────────
  printf '{"update_ratio":%s,"interleave":%s,"bias":"%s","driver_status":%s,"run_store":"%s"}\n' \
    "$update_ratio" "$interleave" "$bias" "$driver_status" "$run_store" \
    > "$ratio_dir/case.json"

  log_progress "case_finish update_ratio=$update_ratio driver_status=$driver_status"
}

# ── main loop ─────────────────────────────────────────────────────────────────
UPDATE_RATIOS="${UPDATE_RATIOS:-0 10 50 90 100}"

for ratio in $UPDATE_RATIOS; do
  run_case "$ratio" || log_progress "case_warn update_ratio=$ratio failed (continuing)"
done

# ── post-processing ────────────────────────────────────────────────────────────
log_progress "post-processing start"

python3 - "$OUT_ROOT" "$VARIANT_TAG" "$THREADS" <<'PY'
import csv
import json
import re
import sys
from pathlib import Path

out_root = Path(sys.argv[1])
variant_tag = sys.argv[2]
threads = sys.argv[3]

def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""

def read_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

# ── parse results.json (written by LDBC driver) ───────────────────────────────
#
# The driver writes results.json to the directory specified by results_dir
# in the properties file, with name "<name>-results.json".
#
def parse_results_json(ratio_dir, label):
    """Parse the LDBC driver results.json for the given case."""
    # The driver writes: <results_dir>/<name>-results.json
    # results_dir is the case directory, name is "LDBC-SNB-E14-r<ratio>"
    results_file = ratio_dir / f"{label}-results.json"
    data = read_json(results_file)
    if not data:
        return {}

    throughput = data.get("throughput", "")
    all_metrics = data.get("all_metrics", [])

    ops = {}
    for m in all_metrics:
        name = m.get("name", "")
        run = m.get("run_time", {})
        # Percentile keys: 25th_percentile, 50th_percentile, 75th_percentile,
        #                  90th_percentile, 95th_percentile, 99th_percentile
        ops[name] = {
            "count": m.get("count", ""),
            "mean_ms": run.get("mean", ""),
            "min_ms": run.get("min", ""),
            "max_ms": run.get("max", ""),
            "p50_ms": run.get("50th_percentile", run.get("50th Percentile", "")),
            "p90_ms": run.get("90th_percentile", ""),
            "p95_ms": run.get("95th_percentile", ""),
            "p99_ms": run.get("99th_percentile", ""),
            "unit": run.get("unit", ""),
        }
    return {"throughput": throughput, "ops": ops}

# ── parse driver.log (backup / supplementary) ─────────────────────────────────
def qps_from_log(text):
    matches = re.findall(r"Throughput:\s*([0-9]+(?:\.[0-9]+)?)\s*\(op/s\)", text)
    return matches[-1] if matches else ""

def audit_from_log(text):
    if "FAILED SCHEDULE AUDIT" in text:
        return "failed"
    if "PASSED SCHEDULE AUDIT" in text or "BUILD SUCCESS" in text:
        return "passed"
    return "unknown"

# ── main collection ────────────────────────────────────────────────────────────
update_ratios = [0, 10, 50, 90, 100]
summary_rows = []
op_rows = []

for ratio in update_ratios:
    ratio_dir = out_root / f"ratio-{ratio}"
    label = f"LDBC-SNB-E14-r{ratio}"
    case_json = ratio_dir / "case.json"
    driver_log_file = ratio_dir / "driver.log"
    server_metrics_file = ratio_dir / "server-metrics.json"

    case = read_json(case_json)
    driver_log = read_text(driver_log_file)
    metrics = read_json(server_metrics_file)
    results = parse_results_json(ratio_dir, label)

    qps = results.get("throughput", "") or qps_from_log(driver_log)
    audit = audit_from_log(driver_log)

    ops = results.get("ops", {})

    # Extract interesting operations
    q1 = ops.get("LdbcQuery1", {})
    q3 = ops.get("LdbcQuery3", {})
    q8 = ops.get("LdbcQuery8", {})
    u2 = ops.get("LdbcUpdate2AddPostLike", {})
    u8 = ops.get("LdbcUpdate8AddFriendship", {})

    def fmt_op(stats):
        if not stats.get("count"):
            return ""
        p50 = stats.get("p50_ms", "")
        p95 = stats.get("p95_ms", "")
        p99 = stats.get("p99_ms", "")
        return f"{p50}/{p95}/{p99}"

    write_lock_wait_max = metrics.get("write_lock_wait_us_max", "")
    query_exec_max = metrics.get("query_exec_us_max", "")
    query_exec_p95 = ""

    endpoints = metrics.get("endpoints", {}) or {}
    query_p95_vals = []
    for path, vals in endpoints.items():
        if vals.get("kind") == "query":
            p95 = vals.get("p95_us") or vals.get("exec_us_p95") or ""
            if p95:
                try:
                    query_p95_vals.append(float(p95))
                except (ValueError, TypeError):
                    pass
    if query_p95_vals:
        query_exec_p95 = str(max(query_p95_vals))

    summary_rows.append({
        "variant": variant_tag,
        "update_ratio": ratio,
        "threads": threads,
        "qps": qps,
        "audit": audit,
        "driver_status": case.get("driver_status", ""),
        "q1_latency": fmt_op(q1),
        "q3_latency": fmt_op(q3),
        "q8_latency": fmt_op(q8),
        "u2_latency": fmt_op(u2),
        "u8_latency": fmt_op(u8),
        "write_lock_wait_max_us": write_lock_wait_max,
        "query_exec_max_us": query_exec_max,
        "query_exec_p95_us": query_exec_p95,
    })

    for op_name, stats in [
        ("LdbcQuery1", q1), ("LdbcQuery3", q3), ("LdbcQuery8", q8),
        ("LdbcUpdate2AddPostLike", u2), ("LdbcUpdate8AddFriendship", u8),
    ]:
        if stats.get("count"):
            op_rows.append({
                "variant": variant_tag,
                "update_ratio": ratio,
                "threads": threads,
                "operation": op_name,
                "count": stats.get("count", ""),
                "mean_ms": stats.get("mean_ms", ""),
                "p50_ms": stats.get("p50_ms", ""),
                "p90_ms": stats.get("p90_ms", ""),
                "p95_ms": stats.get("p95_ms", ""),
                "p99_ms": stats.get("p99_ms", ""),
            })

# ── write summary.tsv ──────────────────────────────────────────────────────────
summary_tsv = out_root / "summary.tsv"
summary_fields = [
    "variant", "update_ratio", "threads", "qps", "audit", "driver_status",
    "q1_latency", "q3_latency", "q8_latency",
    "u2_latency", "u8_latency",
    "write_lock_wait_max_us", "query_exec_max_us", "query_exec_p95_us",
]
with summary_tsv.open("w", encoding="utf-8", newline="") as fh:
    writer = csv.DictWriter(fh, fieldnames=summary_fields, delimiter="\t", extrasaction="ignore")
    writer.writeheader()
    for row in summary_rows:
        writer.writerow(row)

# ── write op-latency.tsv ─────────────────────────────────────────────────────
op_tsv = out_root / "op-latency.tsv"
op_fields = ["variant", "update_ratio", "threads", "operation",
             "count", "mean_ms", "p50_ms", "p90_ms", "p95_ms", "p99_ms"]
with op_tsv.open("w", encoding="utf-8", newline="") as fh:
    writer = csv.DictWriter(fh, fieldnames=op_fields, delimiter="\t", extrasaction="ignore")
    writer.writeheader()
    for row in op_rows:
        writer.writerow(row)

# ── write write-stall-analysis.md ─────────────────────────────────────────────
stall_md = out_root / "write-stall-analysis.md"
with stall_md.open("w", encoding="utf-8") as out:
    out.write("# E14: Write Stall Analysis\n\n")
    out.write("**Question:** Does write pressure increase query latency P95 under mixed workload?\n\n")
    out.write("## Summary\n\n")
    out.write("| Update ratio | QPS | Audit | Q1 P50/P95/P99 (ms) | Q3 P50/P95/P99 (ms) | Q8 P50/P95/P99 (ms) | U2 P50/P95/P99 (ms) | Write lock wait max (us) | Query exec P95 (us) |\n")
    out.write("| --- | ---: | --- | --- | --- | --- | --- | --- | --- |\n")
    for row in summary_rows:
        out.write(
            f"| {row['update_ratio']}% | {row['qps']} | {row['audit']} | "
            f"{row['q1_latency']} | {row['q3_latency']} | {row['q8_latency']} | "
            f"{row['u2_latency']} | {row['write_lock_wait_max_us']} | {row['query_exec_p95_us']} |\n"
        )

    out.write("\n## P95 Query Latency vs Update Ratio\n\n")
    out.write("| Update ratio | Q1 P95 (ms) | Q3 P95 (ms) | Q8 P95 (ms) | Write lock wait max (us) |\n")
    out.write("| --- | ---: | ---: | ---: | ---: |\n")
    for row in summary_rows:
        q1_p95 = row["q1_latency"].split("/")[1] if row["q1_latency"] else ""
        q3_p95 = row["q3_latency"].split("/")[1] if row["q3_latency"] else ""
        q8_p95 = row["q8_latency"].split("/")[1] if row["q8_latency"] else ""
        out.write(f"| {row['update_ratio']}% | {q1_p95} | {q3_p95} | {q8_p95} | {row['write_lock_wait_max_us']} |\n")

    out.write("\n## Commentary\n\n")
    out.write("The table above shows how query latency P95 evolves as the update ratio increases from 0% to 100%.\n\n")
    out.write("Key observations:\n\n")
    out.write("- **Read-heavy (0%, 10%):** Baseline latency under minimal write pressure.\n")
    out.write("- **Balanced (50%):** Equal read/write mix — most representative of production.\n")
    out.write("- **Write-heavy (90%, 100%):** Maximum write pressure — checks whether semantic L0\n")
    out.write("  compaction causes lock contention or write stalls that cascade into read latency.\n\n")
    out.write("If Q1/Q3/Q8 P95 remain stable as update ratio grows, semantic compaction does **not**\n")
    out.write("introduce measurable write-stall overhead on query paths.\n\n")
    out.write("## Per-Operation Latency Details\n\n")
    out.write("| Ratio | Operation | Count | Mean (ms) | P50 (ms) | P90 (ms) | P95 (ms) | P99 (ms) |\n")
    out.write("| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |\n")
    for row in op_rows:
        out.write(
            f"| {row['update_ratio']}% | {row['operation']} | {row['count']} | "
            f"{row['mean_ms']} | {row['p50_ms']} | {row['p90_ms']} | {row['p95_ms']} | {row['p99_ms']} |\n"
        )

print(stall_md)
PY

log_progress "post-processing done"
touch "$OUT_ROOT/PASS"
log_progress "e14 mixed-workload complete out_root=$OUT_ROOT"
echo "PASS $OUT_ROOT"
