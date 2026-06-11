#!/usr/bin/env bash
# SF100 wrapper for the E11 SemL0 baseline/ablation matrix.
# memgraph is enlarged to 8 MB to keep L0 file count (~1.5-2 万) and per-variant
# import time (~1.5-2.5h) tractable. This is a different operating point from
# SF1/SF30 (1MB): SF100 is reported as a within-scale, cross-variant comparison.
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
export SCALE=sf100
export DATA="${DATA:-/data/WorkSpace/ldbc-sf100/social_network}"
export MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-67108864}"       # 64 MB (same operating point as SF30)
export REPEATS="${REPEATS:-1}"                            # exact counters; single pass
export RUN_STATS="${RUN_STATS:-0}"                        # skip extra open
export RUN_ALLTYPES="${RUN_ALLTYPES:-0}"                  # core only (each open is O(edges) ~46min at SF100)
export RUN_NEIGHBOR_COMPARE="${RUN_NEIGHBOR_COMPARE:-none}"  # correctness anchored at SF1/SF30
export DELETE_CANDIDATES_AFTER_CAPTURE="${DELETE_CANDIDATES_AFTER_CAPTURE:-true}"
export DELETE_SCHEMA_AFTER_ALL="${DELETE_SCHEMA_AFTER_ALL:-true}"
exec bash scripts/experiments/active/run-e11-baseline-matrix.sh "$@"
