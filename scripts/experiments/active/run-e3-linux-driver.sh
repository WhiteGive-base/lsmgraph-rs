#!/usr/bin/env bash
set -u

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
BASE_DATE_TAG="${BASE_DATE_TAG:?BASE_DATE_TAG is required and should point to a passing E2 run}"
DATE_TAG="${DATE_TAG:-e3linux-$(date +%Y%m%d-%H%M%S)}"
E3_DIR="${E3_DIR:-remote-logs/e3-c1-ablation-${DATE_TAG}}"

cd "$ROOT"

if [[ -e "$E3_DIR" ]]; then
  echo "refusing to overwrite existing E3 directory: ${E3_DIR}" >&2
  exit 2
fi

mkdir -p "$E3_DIR"

{
  echo "# E3 C1 Ablation Refresh"
  echo
  echo "start_time=$(date -Iseconds)"
  echo "workdir=$(pwd)"
  echo "base_date_tag=${BASE_DATE_TAG}"
  echo "date_tag=${DATE_TAG}"
  echo "command_1=BASE_DATE_TAG=${BASE_DATE_TAG} DATE_TAG=${DATE_TAG} bash scripts/experiments/active/run-p1-c1-fine-threshold-sweep-sf1.sh"
  echo "command_2=BASE_DATE_TAG=${BASE_DATE_TAG} DATE_TAG=${DATE_TAG} bash scripts/experiments/active/run-p1-c1-micro-threshold-sweep-sf1.sh"
  echo "command_3=BASE_DATE_TAG=${BASE_DATE_TAG} DATE_TAG=${DATE_TAG} THRESHOLD_DATE_TAG=${DATE_TAG} INCLUDE_THRESHOLD_ROWS=false bash scripts/experiments/active/run-p1-c1-benefit-scored-sf1.sh"
  echo
  echo "## Toolchain"
  cargo --version 2>&1
  rustc --version 2>&1
  echo
  echo "## Source Status"
  git rev-parse HEAD 2>&1 | sed 's/^/git_head=/'
  echo 'git_status_short<<EOF'
  git status --short 2>&1
  echo EOF
  echo
  echo "## Source Hashes"
  sha256sum \
    Cargo.toml \
    Cargo.lock \
    src/graph.rs \
    src/schema.rs \
    src/csr/format.rs \
    src/csr/writer.rs \
    src/csr/reader.rs \
    src/property_encoding.rs \
    src/bin/lsmgraph.rs \
    tests/engine_tests.rs \
    scripts/experiments/active/run-p1-c1-fine-threshold-sweep-sf1.sh \
    scripts/experiments/active/run-p1-c1-micro-threshold-sweep-sf1.sh \
    scripts/experiments/active/run-p1-c1-benefit-scored-sf1.sh \
    scripts/experiments/active/run-e3-linux-driver.sh \
    2>&1
  echo
  echo "## Driver"
} > "${E3_DIR}/summary.md"

run_step() {
  local name="$1"
  shift
  {
    echo "- $*"
    echo "  start=$(date -Iseconds)"
  } >> "${E3_DIR}/summary.md"
  set +e
  "$@" > "${E3_DIR}/${name}.stdout.log" 2> "${E3_DIR}/${name}.stderr.log"
  local code=$?
  set -e
  {
    echo "  exit_code=${code}"
    echo "  end=$(date -Iseconds)"
    echo "  stdout=${E3_DIR}/${name}.stdout.log"
    echo "  stderr=${E3_DIR}/${name}.stderr.log"
  } >> "${E3_DIR}/summary.md"
  return "$code"
}

failed=0
run_step fine-threshold env BASE_DATE_TAG="$BASE_DATE_TAG" DATE_TAG="$DATE_TAG" \
  bash scripts/experiments/active/run-p1-c1-fine-threshold-sweep-sf1.sh || failed=1
run_step micro-threshold env BASE_DATE_TAG="$BASE_DATE_TAG" DATE_TAG="$DATE_TAG" \
  bash scripts/experiments/active/run-p1-c1-micro-threshold-sweep-sf1.sh || failed=1
run_step benefit-scored env BASE_DATE_TAG="$BASE_DATE_TAG" DATE_TAG="$DATE_TAG" THRESHOLD_DATE_TAG="$DATE_TAG" INCLUDE_THRESHOLD_ROWS=false \
  bash scripts/experiments/active/run-p1-c1-benefit-scored-sf1.sh || failed=1

if [[ "$failed" -eq 0 ]]; then
  python3 - "$BASE_DATE_TAG" "$DATE_TAG" "$E3_DIR" <<'PY'
import json
import sys
from pathlib import Path

base_tag, tag, e3_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3])

variants = [
    ("schema", "baseline", Path(f"remote-logs/p1-c1-sf1-schema-{base_tag}"), "fair-core-s200-r1.json", "fair-alltypes-s50-r1.json", None, None),
    ("full_semantic", "upper_bound", Path(f"remote-logs/p1-c1-sf1-semantic-{base_tag}"), "fair-core-s200-r1.json", "fair-alltypes-s50-r1.json", None, None),
    ("e2_budgeted", "e2_candidate", Path(f"remote-logs/p1-c1-sf1-budgeted-{base_tag}"), "fair-core-s200-r1.json", "fair-alltypes-s50-r1.json", None, None),
]

threshold_names = [
    "fine64k_exact0_score0_weight1",
    "fine128k_exact0_score0_weight1",
    "fine256k_exact0_score0_weight1",
    "fine512k_exact0_score0_weight1",
    "micro544k_exact0_score0_weight1",
    "micro576k_exact0_score0_weight1",
    "micro608k_exact0_score0_weight1",
    "score1024_edge1t_core4_rev2_other01_exact0",
]
for name in threshold_names:
    variants.append((
        name,
        "latest_code_ablation",
        Path(f"remote-logs/p1-c1-sf1-budgeted-{name}-{tag}"),
        "fair-core-s200.json",
        "fair-alltypes-s50.json",
        "neighbor-compare-core.json",
        "neighbor-compare-alltypes.json",
    ))

def parse_tsv(path: Path):
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if "\t" in line:
            key, value = line.split("\t", 1)
            out[key] = value
    return out

def bench(path: Path):
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    read_bytes = body_reads = candidate_l0 = matched_l0 = 0
    for item in data.get("benchmarks", []):
        summary = item.get("neighbor_summary") or {}
        read_bytes += int(summary.get("read_bytes", 0))
        body_reads += int(summary.get("body_reads", 0))
        candidate_l0 += int(summary.get("candidate_l0_segments", 0))
        matched_l0 += int(summary.get("matched_l0_segments", 0))
    return {
        "read_bytes": read_bytes,
        "body_reads": body_reads,
        "candidate_l0": candidate_l0,
        "matched_l0": matched_l0,
    }

def compare(path):
    if path is None:
        return {"checked": "NA", "passed": "NA", "mismatches": "NA"}
    p = Path(path)
    if not p.exists():
        return {"checked": "missing", "passed": "missing", "mismatches": "missing"}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {
        "checked": data.get("checked", "NA"),
        "passed": data.get("passed", "NA"),
        "mismatches": data.get("mismatches", "NA"),
    }

def reduction(base, value):
    if not base:
        return "NA"
    return f"{(base - value) / base * 100:.2f}"

rows = []
schema_core = bench(Path(f"remote-logs/p1-c1-sf1-schema-{base_tag}") / "fair-core-s200-r1.json")
schema_all = bench(Path(f"remote-logs/p1-c1-sf1-schema-{base_tag}") / "fair-alltypes-s50-r1.json")
base_core = schema_core["read_bytes"] if schema_core else 0
base_all = schema_all["read_bytes"] if schema_all else 0

failed = False
for name, role, logdir, core_name, all_name, core_cmp_name, all_cmp_name in variants:
    fs = parse_tsv(logdir / "file-summary.tsv")
    core = bench(logdir / core_name)
    alltypes = bench(logdir / all_name)
    core_cmp = compare(logdir / core_cmp_name if core_cmp_name else None)
    all_cmp = compare(logdir / all_cmp_name if all_cmp_name else None)
    if core_cmp_name and (core_cmp["mismatches"] != 0 or core_cmp["checked"] != core_cmp["passed"]):
        failed = True
    if all_cmp_name and (all_cmp["mismatches"] != 0 or all_cmp["checked"] != all_cmp["passed"]):
        failed = True
    rows.append({
        "variant": name,
        "role": role,
        "store_bytes": fs.get("bytes", "NA"),
        "l0_files": fs.get("l0_files", "NA"),
        "manifest_bytes": fs.get("manifest_bytes", "NA"),
        "core_read_bytes": core["read_bytes"] if core else "missing",
        "core_reduction_pct": reduction(base_core, core["read_bytes"]) if core else "missing",
        "core_candidate_l0": core["candidate_l0"] if core else "missing",
        "alltypes_read_bytes": alltypes["read_bytes"] if alltypes else "missing",
        "alltypes_reduction_pct": reduction(base_all, alltypes["read_bytes"]) if alltypes else "missing",
        "alltypes_candidate_l0": alltypes["candidate_l0"] if alltypes else "missing",
        "core_checked": core_cmp["checked"],
        "core_mismatches": core_cmp["mismatches"],
        "alltypes_checked": all_cmp["checked"],
        "alltypes_mismatches": all_cmp["mismatches"],
        "logdir": str(logdir),
    })

headers = [
    "variant", "role", "store_bytes", "l0_files", "manifest_bytes",
    "core_read_bytes", "core_reduction_pct", "core_candidate_l0",
    "alltypes_read_bytes", "alltypes_reduction_pct", "alltypes_candidate_l0",
    "core_checked", "core_mismatches", "alltypes_checked", "alltypes_mismatches",
    "logdir",
]
with (e3_dir / "ablation-summary.tsv").open("w", encoding="utf-8") as out:
    out.write("\t".join(headers) + "\n")
    for row in rows:
        out.write("\t".join(str(row[h]) for h in headers) + "\n")

with (e3_dir / "summary.md").open("a", encoding="utf-8") as out:
    out.write("\n## Ablation Summary\n")
    out.write("ablation_summary_tsv=" + str(e3_dir / "ablation-summary.tsv") + "\n")
    out.write("ablation_failed=" + str(failed).lower() + "\n")

if failed:
    raise SystemExit(1)
PY
  summary_code=$?
  if [[ "$summary_code" -ne 0 ]]; then
    failed=1
  fi
fi

{
  echo
  echo "end_time=$(date -Iseconds)"
  echo "failed=${failed}"
} >> "${E3_DIR}/summary.md"

if [[ "$failed" -eq 0 ]]; then
  echo "PASS" > "${E3_DIR}/PASS"
else
  echo "FAIL" > "${E3_DIR}/FAIL"
  exit 1
fi

echo "E3_PASS_DIR=${E3_DIR}"
