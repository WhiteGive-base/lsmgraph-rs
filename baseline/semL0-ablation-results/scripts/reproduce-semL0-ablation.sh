#!/usr/bin/env bash
# One-click reproduction of the SemL0 SF30/SF100 ablation + cost evidence.
#
# Usage:
#   bash reproduce-semL0-ablation.sh [sf10|sf30|sf100|all]
#     all   = sf30 + sf100   (default)
#     sf10  = quick pipeline smoke (~reduced)
#
# Env:
#   DATE_TAG     shared tag for all artifacts (default: YYYYMMDD-<rand>)
#   SKIP_BUILD   set to 1 to reuse the existing release binary
#   ONLY_VARIANTS  restrict the matrix to a subset (comma list of vids)
#
# Produces, under remote-logs/:
#   e11-{vid}-${DATE_TAG}/                 raw per-variant traces
#   e11-normalized-${DATE_TAG}/            extracted CSV + P1/P2 markdown tables
#   p3-feedback-vs-no-feedback-${DATE_TAG}.json   feedback-compaction comparison
#   semL0-ablation-summary-${DATE_TAG}-cn.md      human-readable Chinese summary
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
export PATH="$HOME/.cargo/bin:$PATH"

MODE="${1:-all}"
case "$MODE" in
  all)   SCALES=(sf30 sf100) ;;
  sf10)  SCALES=(sf10) ;;
  sf30)  SCALES=(sf30) ;;
  sf100) SCALES=(sf100) ;;
  *) echo "usage: $0 [sf10|sf30|sf100|all]" >&2; exit 1 ;;
esac

DATE_TAG="${DATE_TAG:-$(date +%Y%m%d)-$(openssl rand -hex 3 2>/dev/null || echo manual)}"
export DATE_TAG
SUMMARY="remote-logs/semL0-ablation-summary-${DATE_TAG}-cn.md"
EXTRACT="scripts/analysis/current/extract-e11-tables.py"
RENDER="scripts/analysis/current/render-e11-baseline-tables.py"
NORM="remote-logs/e11-normalized-${DATE_TAG}"

echo "=== SemL0 ablation reproduction: mode=${MODE} date_tag=${DATE_TAG} scales=${SCALES[*]} ==="

# 1) build
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  echo "[build] cargo build --release"
  mkdir -p remote-logs
  cargo build --release > "remote-logs/reproduce-build-${DATE_TAG}.stdout" \
                        2> "remote-logs/reproduce-build-${DATE_TAG}.stderr"
fi

# 2) matrices
for scale in "${SCALES[@]}"; do
  echo "[matrix] ${scale}"
  bash "scripts/experiments/active/run-e11-baseline-matrix-${scale}.sh"
done

# 3) feedback vs no-feedback (deterministic microbench; fast)
echo "[feedback] p3-feedback-bench"
FB_JSON="remote-logs/p3-feedback-vs-no-feedback-${DATE_TAG}.json"
./target/release/p3-feedback-bench \
  --reset-store --compare-no-feedback \
  --store-dir "target/p3-feedback-store-${DATE_TAG}" \
  --no-feedback-store-dir "target/p3-no-feedback-store-${DATE_TAG}" \
  --output "$FB_JSON" --segments-per-phase 6 --repeats 5 --range-bucket-size 256 \
  > "remote-logs/p3-feedback-${DATE_TAG}.stdout" 2>&1 || echo "[feedback] WARN: p3 bench failed (non-fatal)"

# 4) extract tables (per scale) + best-effort latex render
for scale in "${SCALES[@]}"; do
  echo "[extract] ${scale}"
  python3 "$EXTRACT" --root . --scale "$scale" --date-tag "$DATE_TAG" || true
  python3 "$RENDER" --root . --date-tag "$DATE_TAG" --out-dir "${NORM}/latex-${scale}" 2>/dev/null \
    && echo "[render] latex -> ${NORM}/latex-${scale}" || echo "[render] skipped (optional)"
done

# 5) assemble Chinese summary doc
echo "[summary] -> ${SUMMARY}"
python3 scripts/analysis/current/assemble-semL0-summary.py \
  --root . --date-tag "$DATE_TAG" --scales "$(IFS=,; echo "${SCALES[*]}")" \
  --feedback "$FB_JSON" --out "$SUMMARY" || echo "[summary] WARN: assembler failed"

echo "=== done. tables: ${NORM}/  summary: ${SUMMARY} ==="
