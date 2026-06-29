#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

OUT="baseline/external-baselines-20260626/3plus3-baselines/systems/tugraph/sf10-main-r1"
mkdir -p "$OUT"
date -Is > "$OUT/STARTED"

docker run --rm -u 1006:1006 \
  -v /data/WorkSpace/lsmgraph-rs:/work \
  -w /work \
  tugraph/tugraph-runtime-ubuntu18.04:4.0.0-finbench \
  baseline/external-baselines-20260626/3plus3-baselines/scripts/tugraph_dense_driver \
  --edges baseline/external-baselines-20260624/livegraph/sf10-typed-neighbor/edges-dense.txt \
  --truth baseline/external-baselines-20260626/3plus3-baselines/systems/neo4j/sf10-main/workload/truth-s50-seed42.tsv \
  --db-dir "$OUT/db" \
  --output "$OUT/result.json" \
  --batch 10000

date -Is > "$OUT/FINISHED"
