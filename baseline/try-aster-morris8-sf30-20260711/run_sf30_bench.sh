#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 LABEL STORE exact|morris8" >&2
  exit 2
fi

LABEL="$1"
STORE="$2"
ESTIMATOR="$3"
BIN="/data/WorkSpace/try-aster-artifacts/bin/lsmgraph-after-morris8"
PLAN="/data/WorkSpace/lsmgraph-rs/remote-logs/w2-c10-20260613/sample-plans/sf30-core-s200.plan.json"
OUT_DIR="/data/WorkSpace/try-aster-artifacts/logs/${LABEL}"
EDGE_TYPES="1,2,3,7,8,9,10,11,12"

[[ -x "$BIN" ]] || { echo "missing binary: $BIN" >&2; exit 2; }
[[ -d "$STORE" ]] || { echo "missing store: $STORE" >&2; exit 2; }
[[ -s "$PLAN" ]] || { echo "missing sample plan: $PLAN" >&2; exit 2; }
[[ "$ESTIMATOR" == "exact" || "$ESTIMATOR" == "morris8" ]] || {
  echo "invalid estimator: $ESTIMATOR" >&2
  exit 2
}

mkdir -p "$OUT_DIR"
/usr/bin/time -v -o "${OUT_DIR}/bench.time" \
  "$BIN" --io-backend blocking storage-bench \
    --data-dir "$STORE" \
    --edge-types "$EDGE_TYPES" \
    --semantic-degree-hint \
    --semantic-degree-estimator "$ESTIMATOR" \
    --sample-plan-in "$PLAN" \
    --warmup-runs 1 \
    --repeats 3 \
    --emit-result-digests \
    --l0-layout semantic-budgeted \
    > "${OUT_DIR}/bench.json" \
    2> "${OUT_DIR}/bench.err"

python3 -m json.tool "${OUT_DIR}/bench.json" >/dev/null
printf '0\n' > "${OUT_DIR}/bench.exit.status"
