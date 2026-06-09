#!/usr/bin/env bash
set -u

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
DATE_TAG="${DATE_TAG:-e2linux-$(date +%Y%m%d-%H%M%S)}"
BUILD_RELEASE="${BUILD_RELEASE:-true}"
E2_DIR="${E2_DIR:-remote-logs/e2-c1-sf1-refresh-${DATE_TAG}}"

cd "$ROOT"

if [[ -e "$E2_DIR" ]]; then
  echo "refusing to overwrite existing E2 directory: ${E2_DIR}" >&2
  exit 2
fi

mkdir -p "$E2_DIR"

{
  echo "# E2 C1 SF1 Refresh"
  echo
  echo "start_time=$(date -Iseconds)"
  echo "workdir=$(pwd)"
  echo "date_tag=${DATE_TAG}"
  echo "build_release=${BUILD_RELEASE}"
  echo "command=DATE_TAG=${DATE_TAG} BUILD_RELEASE=${BUILD_RELEASE} bash scripts/experiments/active/run-p1-c1-latest-sf1-controlled.sh"
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
    scripts/experiments/active/run-p1-c1-latest-sf1-controlled.sh \
    scripts/experiments/active/run-e2-linux-driver.sh \
    2>&1
  echo
  echo "## Expected Output Paths"
  echo "schema_store=store/p1-c1-sf1-schema-${DATE_TAG}"
  echo "semantic_store=store/p1-c1-sf1-semantic-${DATE_TAG}"
  echo "budgeted_store=store/p1-c1-sf1-budgeted-${DATE_TAG}"
  echo "plan_dir=remote-logs/p1-c1-sample-plans-${DATE_TAG}"
  echo "schema_log=remote-logs/p1-c1-sf1-schema-${DATE_TAG}"
  echo "semantic_log=remote-logs/p1-c1-sf1-semantic-${DATE_TAG}"
  echo "budgeted_log=remote-logs/p1-c1-sf1-budgeted-${DATE_TAG}"
  echo
  echo "## Driver"
} > "${E2_DIR}/summary.md"

set +e
DATE_TAG="$DATE_TAG" BUILD_RELEASE="$BUILD_RELEASE" \
  bash scripts/experiments/active/run-p1-c1-latest-sf1-controlled.sh \
  > "${E2_DIR}/driver.stdout.log" \
  2> "${E2_DIR}/driver.stderr.log"
code=$?
set -e

{
  echo "driver_exit_code=${code}"
  echo "end_time=$(date -Iseconds)"
} >> "${E2_DIR}/summary.md"

if [[ "$code" -ne 0 ]]; then
  echo "FAIL" > "${E2_DIR}/FAIL"
  exit "$code"
fi

python3 - "$DATE_TAG" "$E2_DIR" <<'PY'
import json
import sys
from pathlib import Path

tag = sys.argv[1]
e2 = Path(sys.argv[2])
checks = {
    "semantic_core": Path(f"remote-logs/p1-c1-sf1-neighbor-compare-semantic-core-{tag}.json"),
    "semantic_alltypes": Path(f"remote-logs/p1-c1-sf1-neighbor-compare-semantic-alltypes-{tag}.json"),
    "budgeted_core": Path(f"remote-logs/p1-c1-sf1-neighbor-compare-budgeted-core-{tag}.json"),
    "budgeted_alltypes": Path(f"remote-logs/p1-c1-sf1-neighbor-compare-budgeted-alltypes-{tag}.json"),
}
lines = ["", "## Correctness Checks", "name\tchecked\tpassed\tmismatches\tpath"]
failed = False
for name, path in checks.items():
    if not path.exists():
        lines.append(f"{name}\tmissing\tmissing\tmissing\t{path}")
        failed = True
        continue
    data = json.loads(path.read_text(encoding="utf-8"))
    checked = data.get("checked")
    passed = data.get("passed")
    mismatches = data.get("mismatches")
    lines.append(f"{name}\t{checked}\t{passed}\t{mismatches}\t{path}")
    if mismatches != 0 or checked != passed:
        failed = True
(e2 / "correctness-summary.tsv").write_text("\n".join(lines[2:]) + "\n", encoding="utf-8")
with (e2 / "summary.md").open("a", encoding="utf-8") as out:
    out.write("\n".join(lines) + "\n")
if failed:
    raise SystemExit(1)
PY
summary_code=$?

if [[ "$summary_code" -eq 0 ]]; then
  echo "PASS" > "${E2_DIR}/PASS"
else
  echo "FAIL" > "${E2_DIR}/FAIL"
  exit "$summary_code"
fi

echo "E2_PASS_DIR=${E2_DIR}"
