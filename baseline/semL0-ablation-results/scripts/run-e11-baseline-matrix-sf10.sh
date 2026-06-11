#!/usr/bin/env bash
# SF10 dry-run wrapper for the E11 SemL0 baseline/ablation matrix.
# Small (~10x SF1) — used to de-risk the full pipeline before SF30/SF100.
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
export SCALE=sf10
export DATA="${DATA:-/data/WorkSpace/ldbc-sf10/social_network}"
export MEMGRAPH_BYTES="${MEMGRAPH_BYTES:-1048576}"        # 1 MB (faithful SF1 operating point)
export DELETE_CANDIDATES_AFTER_CAPTURE="${DELETE_CANDIDATES_AFTER_CAPTURE:-true}"
export DELETE_SCHEMA_AFTER_ALL="${DELETE_SCHEMA_AFTER_ALL:-true}"
exec bash scripts/experiments/active/run-e11-baseline-matrix.sh "$@"
