#!/usr/bin/env bash
# SF30 wrapper for the E11 SemL0 baseline/ablation matrix.
# memgraph=1MB keeps the SF1 operating point (faithful ~10x continuation).
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
export SCALE=sf30
export DATA="${DATA:-/data/WorkSpace/ldbc-sf30/social_network}"
export MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-67108864}"       # 64 MB (~hundreds of L0 segs -> cheap per-open reindex + fast reads)
export REPEATS="${REPEATS:-1}"                            # exact counters; single pass
export RUN_STATS="${RUN_STATS:-0}"                        # skip extra open; cost from file-summary
export RUN_ALLTYPES="${RUN_ALLTYPES:-1}"                  # core + alltypes read passes
export RUN_NEIGHBOR_COMPARE="${RUN_NEIGHBOR_COMPARE:-benefit_scored,full_semantic}"  # scale spot-check; full correctness at SF1
export DELETE_CANDIDATES_AFTER_CAPTURE="${DELETE_CANDIDATES_AFTER_CAPTURE:-true}"
export DELETE_SCHEMA_AFTER_ALL="${DELETE_SCHEMA_AFTER_ALL:-true}"
exec bash scripts/experiments/active/run-e11-baseline-matrix.sh "$@"
