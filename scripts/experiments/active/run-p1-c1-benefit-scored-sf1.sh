#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

BASE_DATE_TAG="${BASE_DATE_TAG:-20260604}"
DATE_TAG="${DATE_TAG:-20260604}"
THRESHOLD_DATE_TAG="${THRESHOLD_DATE_TAG:-20260604}"
INCLUDE_THRESHOLD_ROWS="${INCLUDE_THRESHOLD_ROWS:-true}"
BIN="${BIN:-target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
DATA="${DATA:-/data/WorkSpace/dgs/data/social_network_tugraph}"
MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-1048576}"

CORE_EDGE_TYPES="1,2,3,7,8,9,10,11,12"
ALL_EDGE_TYPES="-17,-16,-15,-14,-13,-12,-11,-10,-9,-8,-7,-6,-5,-4,-3,-2,-1,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17"

SCHEMA_STORE="store/p1-c1-sf1-schema-${BASE_DATE_TAG}"
CORE_PLAN="remote-logs/p1-c1-sample-plans-${BASE_DATE_TAG}/sf1-core-s200.plan.json"
ALLTYPES_PLAN="remote-logs/p1-c1-sample-plans-${BASE_DATE_TAG}/sf1-alltypes-s50.plan.json"

SCORED_NAME="${SCORED_NAME:-score1024_edge1t_core4_rev2_other01_exact0}"
SCORED_EDGE_TYPE_BYTES="${SCORED_EDGE_TYPE_BYTES:-1099511627776}"
SCORED_EDGE_TYPE_SCORE="${SCORED_EDGE_TYPE_SCORE:-1024.0}"
SCORED_CORE_WEIGHT="${SCORED_CORE_WEIGHT:-4.0}"
SCORED_REVERSE_CORE_WEIGHT="${SCORED_REVERSE_CORE_WEIGHT:-2.0}"
SCORED_OTHER_WEIGHT="${SCORED_OTHER_WEIGHT:-0.1}"
SCORED_MAX_EXTRA_L0_FILES="${SCORED_MAX_EXTRA_L0_FILES:-}"
SCORED_EDGE_TYPE_ALLOWLIST="${SCORED_EDGE_TYPE_ALLOWLIST:-}"
SCORED_EXACT_BYTES="${SCORED_EXACT_BYTES:-0}"
SCORED_DEGREE_SCORE="${SCORED_DEGREE_SCORE:-0.0}"
SCORED_DEGREE_WEIGHT="${SCORED_DEGREE_WEIGHT:-1.0}"

require_base() {
  for path in "$SCHEMA_STORE" "$CORE_PLAN" "$ALLTYPES_PLAN" "$BIN"; do
    if [[ ! -e "$path" ]]; then
      echo "missing required artifact: $path" >&2
      exit 2
    fi
  done
  if ! "$BIN" import --help | grep -q 'semantic-budget-min-edge-type-score'; then
    echo "release binary does not expose --semantic-budget-min-edge-type-score; rebuild target/release/lsmgraph first" >&2
    exit 4
  fi
  if ! "$BIN" import --help | grep -q 'semantic-budget-max-extra-l0-files'; then
    echo "release binary does not expose --semantic-budget-max-extra-l0-files; rebuild target/release/lsmgraph first" >&2
    exit 4
  fi
  if ! "$BIN" import --help | grep -q 'semantic-budget-edge-type-allowlist'; then
    echo "release binary does not expose --semantic-budget-edge-type-allowlist; rebuild target/release/lsmgraph first" >&2
    exit 4
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
    echo "baseline_schema_store=${SCHEMA_STORE}"
    echo "base_date_tag=${BASE_DATE_TAG}"
    echo "date_tag=${DATE_TAG}"
    echo "threshold_date_tag=${THRESHOLD_DATE_TAG}"
    echo "include_threshold_rows=${INCLUDE_THRESHOLD_ROWS}"
    echo "core_plan=${CORE_PLAN}"
    echo "alltypes_plan=${ALLTYPES_PLAN}"
    echo "memgraph_bytes=${MEMGRAPH_BYTES}"
    echo "scored_name=${SCORED_NAME}"
    echo "semantic_budget_min_edge_type_bytes=${SCORED_EDGE_TYPE_BYTES}"
    echo "semantic_budget_min_edge_type_score=${SCORED_EDGE_TYPE_SCORE}"
    echo "semantic_budget_core_edge_weight=${SCORED_CORE_WEIGHT}"
    echo "semantic_budget_reverse_core_edge_weight=${SCORED_REVERSE_CORE_WEIGHT}"
    echo "semantic_budget_other_edge_weight=${SCORED_OTHER_WEIGHT}"
    echo "semantic_budget_max_extra_l0_files=${SCORED_MAX_EXTRA_L0_FILES:-none}"
    echo "semantic_budget_edge_type_allowlist=${SCORED_EDGE_TYPE_ALLOWLIST:-none}"
    echo "semantic_budget_min_exact_bytes=${SCORED_EXACT_BYTES}"
    echo "semantic_budget_min_benefit_score=${SCORED_DEGREE_SCORE}"
    echo "semantic_budget_degree_weight=${SCORED_DEGREE_WEIGHT}"
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
  local manifest="${store}/MANIFEST"
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
    if [[ -f "$manifest" ]]; then
      stat -c 'manifest_bytes	%s' "$manifest"
      grep -c '"op":"CreateFile"' "$manifest" | awk '{printf "manifest_records\t%s\n", $1}'
      grep -c '"degree_class_exact":true' "$manifest" | awk '{printf "degree_exact_files\t%s\n", $1}'
      grep -c '"edge_type_partition":0' "$manifest" | awk '{printf "mixed_edge_files\t%s\n", $1}'
      python3 - "$manifest" <<'PY'
import json
import sys
from collections import Counter

manifest = sys.argv[1]
edge_exact = 0
degree = Counter()
for line in open(manifest, encoding="utf-8"):
    try:
        meta = json.loads(line)["meta"]
    except Exception:
        continue
    if meta.get("edge_type_partition") != 0:
        edge_exact += 1
    degree[meta.get("degree_class", "unknown")] += 1
print(f"edge_exact_files\t{edge_exact}")
for key in ("low", "medium", "high", "mixed", "unknown"):
    print(f"degree_class_{key}_files\t{degree.get(key, 0)}")
PY
    fi
  } > "${logdir}/file-summary.tsv"
}

summarize_candidates() {
  local store="$1"
  local logdir="$2"
  local diag="${store}/budgeted-edge-candidates.tsv"
  if [[ ! -s "$diag" ]]; then
    echo "missing diagnostics: $diag" >&2
    return 1
  fi
  python3 - "$diag" "$logdir" <<'PY'
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

diag = Path(sys.argv[1])
logdir = Path(sys.argv[2])
rows = list(csv.DictReader(diag.open(encoding="utf-8"), delimiter="\t"))

reasons = Counter(row["reason"] for row in rows)
with (logdir / "candidate-reason-counts.tsv").open("w", encoding="utf-8") as out:
    out.write("reason\tcount\n")
    for reason, count in sorted(reasons.items()):
        out.write(f"{reason}\t{count}\n")

selected = defaultdict(lambda: {"rows": 0, "group_edges": 0, "group_bytes": 0, "max_score": 0.0})
for row in rows:
    if row["selected"] != "true":
        continue
    entry = selected[(row["src_label"], row["edge_type"])]
    entry["rows"] += 1
    entry["group_edges"] += int(row["group_edges"])
    entry["group_bytes"] += int(row["group_bytes"])
    entry["max_score"] = max(entry["max_score"], float(row["score"]))

with (logdir / "selected-edge-types.tsv").open("w", encoding="utf-8") as out:
    out.write("src_label\tedge_type\tcandidate_rows\tgroup_edges\tgroup_bytes\tmax_score\n")
    for (src_label, edge_type), entry in sorted(selected.items(), key=lambda item: (int(item[0][0]), int(item[0][1]))):
        out.write(
            f"{src_label}\t{edge_type}\t{entry['rows']}\t{entry['group_edges']}\t{entry['group_bytes']}\t{entry['max_score']:.6f}\n"
        )
PY
}

run_scored_variant() {
  local store="store/p1-c1-sf1-budgeted-${SCORED_NAME}-${DATE_TAG}"
  local logdir="remote-logs/p1-c1-sf1-budgeted-${SCORED_NAME}-${DATE_TAG}"

  mkdir -p "$logdir"
  record_meta "$logdir"
  if import_complete "$logdir"; then
    echo "skip completed import ${SCORED_NAME}"
  else
    if [[ -e "$store" ]]; then
      echo "store exists but import is not marked complete: ${store}" >&2
      exit 3
    fi
    local budget_args=()
    if [[ -n "$SCORED_MAX_EXTRA_L0_FILES" ]]; then
      budget_args+=(--semantic-budget-max-extra-l0-files "$SCORED_MAX_EXTRA_L0_FILES")
    fi
    local allowlist_args=()
    if [[ -n "$SCORED_EDGE_TYPE_ALLOWLIST" ]]; then
      allowlist_args+=(--semantic-budget-edge-type-allowlist "$SCORED_EDGE_TYPE_ALLOWLIST")
    fi
    /usr/bin/time -v -o "${logdir}/time.log" \
      "$BIN" --io-backend "$IO_BACKEND" import \
        --input "$DATA" \
        --data-dir "$store" \
        --relation snb-full \
        --memgraph-bytes "$MEMGRAPH_BYTES" \
        --l0-layout semantic-budgeted \
        --semantic-budget-min-edge-type-bytes "$SCORED_EDGE_TYPE_BYTES" \
        --semantic-budget-min-edge-type-score "$SCORED_EDGE_TYPE_SCORE" \
        --semantic-budget-core-edge-weight "$SCORED_CORE_WEIGHT" \
        --semantic-budget-reverse-core-edge-weight "$SCORED_REVERSE_CORE_WEIGHT" \
        --semantic-budget-other-edge-weight "$SCORED_OTHER_WEIGHT" \
        "${budget_args[@]}" \
        "${allowlist_args[@]}" \
        --semantic-budget-min-exact-bytes "$SCORED_EXACT_BYTES" \
        --semantic-budget-min-benefit-score "$SCORED_DEGREE_SCORE" \
        --semantic-budget-degree-weight "$SCORED_DEGREE_WEIGHT" \
        > "${logdir}/import.stdout" 2> "${logdir}/import.stderr"
  fi

  "$BIN" --io-backend "$IO_BACKEND" stats \
    --data-dir "$store" \
    > "${logdir}/stats.log" 2> "${logdir}/stats.err"
  write_file_summary "$store" "$logdir"
  summarize_candidates "$store" "$logdir"

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

summarize_comparison() {
  local scored_logdir="remote-logs/p1-c1-sf1-budgeted-${SCORED_NAME}-${DATE_TAG}"
  local outdir="remote-logs/p1-c1-sf1-benefit-scored-${DATE_TAG}"
  mkdir -p "$outdir"
  python3 - "$outdir/summary.tsv" "$scored_logdir" "$BASE_DATE_TAG" "$THRESHOLD_DATE_TAG" "$INCLUDE_THRESHOLD_ROWS" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
scored_logdir = Path(sys.argv[2])
base_date_tag = sys.argv[3]
threshold_date_tag = sys.argv[4]
include_threshold_rows = sys.argv[5].lower() == "true"

variants = [
    ("schema", Path(f"remote-logs/p1-c1-sf1-schema-{base_date_tag}"), "fair-core-s200-r1.json", "fair-alltypes-s50-r1.json"),
    ("full_semantic", Path(f"remote-logs/p1-c1-sf1-semantic-{base_date_tag}"), "fair-core-s200-r1.json", "fair-alltypes-s50-r1.json"),
    ("benefit_scored", scored_logdir, "fair-core-s200.json", "fair-alltypes-s50.json"),
]
if include_threshold_rows:
    variants.insert(
        2,
        ("fine512k", Path(f"remote-logs/p1-c1-sf1-budgeted-fine512k_exact0_score0_weight1-{threshold_date_tag}"), "fair-core-s200.json", "fair-alltypes-s50.json"),
    )
    variants.insert(
        3,
        ("high640k", Path(f"remote-logs/p1-c1-sf1-budgeted-high640k_exact0_score0_weight1-{threshold_date_tag}"), "fair-core-s200.json", "fair-alltypes-s50.json"),
    )

def file_summary(logdir):
    values = {}
    path = logdir / "file-summary.tsv"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "\t" in line:
                key, value = line.split("\t", 1)
                values[key] = value
    return values

def bench_sum(path):
    if not path.exists():
        return ("NA", "NA", "NA", "NA")
    data = json.loads(path.read_text(encoding="utf-8"))
    read_bytes = 0
    body_reads = 0
    candidate_l0 = 0
    matched_l0 = 0
    for bench in data.get("benchmarks", []):
        summary = bench.get("neighbor_summary") or {}
        read_bytes += int(summary.get("read_bytes", 0))
        body_reads += int(summary.get("body_reads", 0))
        candidate_l0 += int(summary.get("candidate_l0_segments", 0))
        matched_l0 += int(summary.get("matched_l0_segments", 0))
    return (read_bytes, body_reads, candidate_l0, matched_l0)

def mismatches(path):
    if not path.exists():
        return "NA"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("mismatches", "NA")

with out.open("w", encoding="utf-8") as f:
    f.write(
        "variant\tstore_bytes\tl0_files\tmanifest_bytes\tedge_exact_files\tmixed_edge_files\t"
        "core_read_bytes\tcore_body_reads\tcore_candidate_l0\tcore_matched_l0\t"
        "alltypes_read_bytes\talltypes_body_reads\talltypes_candidate_l0\talltypes_matched_l0\t"
        "core_mismatches\talltypes_mismatches\n"
    )
    for name, logdir, core_name, all_name in variants:
        fs = file_summary(logdir)
        core = bench_sum(logdir / core_name)
        alltypes = bench_sum(logdir / all_name)
        f.write(
            f"{name}\t{fs.get('bytes','NA')}\t{fs.get('l0_files','NA')}\t{fs.get('manifest_bytes','NA')}\t"
            f"{fs.get('edge_exact_files','NA')}\t{fs.get('mixed_edge_files','NA')}\t"
            f"{core[0]}\t{core[1]}\t{core[2]}\t{core[3]}\t"
            f"{alltypes[0]}\t{alltypes[1]}\t{alltypes[2]}\t{alltypes[3]}\t"
            f"{mismatches(logdir / 'neighbor-compare-core.json')}\t{mismatches(logdir / 'neighbor-compare-alltypes.json')}\n"
        )
PY
}

main() {
  require_base
  run_scored_variant
  summarize_comparison
}

main "$@"
