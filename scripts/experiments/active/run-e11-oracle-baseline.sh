#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

DATE_TAG="${DATE_TAG:-$(date +%Y%m%d)-$(openssl rand -hex 3)}"
BIN="${BIN:-target/release/lsmgraph}"
IO_BACKEND="${IO_BACKEND:-blocking}"
BUILD_RELEASE="${BUILD_RELEASE:-true}"

ORACLE_STORE="store/e11-oracle-${DATE_TAG}"
ORACLE_LOG="remote-logs/e11-oracle-${DATE_TAG}"
FULL_SEMANTIC_STORE=""
FULL_SEMANTIC_LOG=""

for d in store/e11-full-semantic-*; do
  if [[ -d "$d" ]]; then
    FULL_SEMANTIC_STORE="$d"
    break
  fi
done

if [[ -z "$FULL_SEMANTIC_STORE" ]]; then
  echo "full-semantic store not found. Run run-e11-baseline-matrix-sf1.sh first." >&2
  exit 2
fi

ORACLE_TAG="$(basename "$FULL_SEMANTIC_STORE" | sed 's/store\/e11-full-semantic-//')"
if [[ -z "$ORACLE_TAG" ]]; then
  echo "could not derive oracle store tag from full-semantic store" >&2
  exit 3
fi

mkdir -p "$ORACLE_LOG"

record_meta() {
  {
    echo "date=$(date -Is)"
    echo "pwd=$(pwd)"
    echo "binary=${BIN}"
    echo "io_backend=${IO_BACKEND}"
    echo "date_tag=${DATE_TAG}"
    echo "full_semantic_store=${FULL_SEMANTIC_STORE}"
    echo "oracle_store=${ORACLE_STORE}"
    "${BIN}" --version || true
  } > "${ORACLE_LOG}/run.meta" 2>&1
  git rev-parse HEAD > "${ORACLE_LOG}/git-head.txt" 2> "${ORACLE_LOG}/git-head.err" || true
  git status --short > "${ORACLE_LOG}/git-status.txt" 2> "${ORACLE_LOG}/git-status.err" || true
}

build_binary() {
  if [[ "$BUILD_RELEASE" == "true" ]]; then
    mkdir -p "remote-logs/e11-oracle-build-${DATE_TAG}"
    cargo build --release \
      > "remote-logs/e11-oracle-build-${DATE_TAG}/build.stdout" \
      2> "remote-logs/e11-oracle-build-${DATE_TAG}/build.stderr"
  fi
  if [[ ! -x "$BIN" ]]; then
    echo "binary not found or not executable: ${BIN}" >&2
    exit 3
  fi
}

if "${BIN}" import --help | grep -q 'build-oracle-index'; then
  ORACLE_BUILD_CMD="build-oracle-index"
elif "${BIN}" import --help | grep -q 'build-perfect-index'; then
  ORACLE_BUILD_CMD="build-perfect-index"
else
  echo "binary does not support oracle/perfect index build" >&2
  exit 4
fi

record_meta
build_binary

echo "build oracle index from full-semantic store: ${FULL_SEMANTIC_STORE}"

mkdir -p "$(dirname "$ORACLE_STORE")"
/usr/bin/time -v -o "${ORACLE_LOG}/time.log" \
  "${BIN}" --io-backend "$IO_BACKEND" "$ORACLE_BUILD_CMD" \
    --data-dir "$FULL_SEMANTIC_STORE" \
    --output-dir "$ORACLE_STORE" \
    > "${ORACLE_LOG}/build-oracle.stdout" 2> "${ORACLE_LOG}/build-oracle.stderr"

"${BIN}" --io-backend "$IO_BACKEND" stats \
  --data-dir "$ORACLE_STORE" \
  > "${ORACLE_LOG}/stats.log" 2> "${ORACLE_LOG}/stats.err"

{
  printf "store\t%s\n" "$ORACLE_STORE"
  find "$ORACLE_STORE" -type f | wc -l | awk '{printf "files\t%s\n", $1}'
  find "$ORACLE_STORE" -type f -printf '%s\n' | awk '{s+=$1} END{printf "bytes\t%.0f\n", s}'
} > "${ORACLE_LOG}/file-summary.tsv"

SAMPLE_PLANS=(
  "remote-logs/e11-sample-plans-${ORACLE_TAG}/sf1-core-s200.plan.json"
  "remote-logs/e11-sample-plans-${ORACLE_TAG}/sf1-alltypes-s50.plan.json"
)

for plan in "${SAMPLE_PLANS[@]}"; do
  if [[ ! -s "$plan" ]]; then
    echo "missing sample plan: ${plan}" >&2
    exit 5
  fi
done

CORE_PLAN="${SAMPLE_PLANS[0]}"
ALLTYPES_PLAN="${SAMPLE_PLANS[1]}"

for repeat in 1 2 3; do
  if [[ ! -s "${ORACLE_LOG}/oracle-core-s200-r${repeat}.json" ]]; then
    "${BIN}" --io-backend "$IO_BACKEND" storage-bench \
      --data-dir "$ORACLE_STORE" \
      --edge-types="1,2,3,7,8,9,10,11,12" \
      --sample-plan-in "$CORE_PLAN" \
      > "${ORACLE_LOG}/oracle-core-s200-r${repeat}.json" \
      2> "${ORACLE_LOG}/oracle-core-s200-r${repeat}.err"
  fi

  if [[ ! -s "${ORACLE_LOG}/oracle-alltypes-s50-r${repeat}.json" ]]; then
    "${BIN}" --io-backend "$IO_BACKEND" storage-bench \
      --data-dir "$ORACLE_STORE" \
      --edge-types="-17,-16,-15,-14,-13,-12,-11,-10,-9,-8,-7,-6,-5,-4,-3,-2,-1,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17" \
      --sample-plan-in "$ALLTYPES_PLAN" \
      > "${ORACLE_LOG}/oracle-alltypes-s50-r${repeat}.json" \
      2> "${ORACLE_LOG}/oracle-alltypes-s50-r${repeat}.err"
  fi
done

echo "E11 oracle baseline complete"
echo "oracle_store=${ORACLE_STORE}"
echo "oracle_log=${ORACLE_LOG}"
echo "date_tag=${DATE_TAG}"
