#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

DATE_TAG="${DATE_TAG:-20260604}"
CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-target-codex-p33feedback}"
STORE_DIR="${STORE_DIR:-target/p3-feedback-vs-no-feedback-store-${DATE_TAG}}"
NO_FEEDBACK_STORE_DIR="${NO_FEEDBACK_STORE_DIR:-target/p3-no-feedback-baseline-store-${DATE_TAG}}"
OUT_DIR="${OUT_DIR:-remote-logs}"
OUT_JSON="${OUT_JSON:-${OUT_DIR}/p3-feedback-vs-no-feedback-p33-${DATE_TAG}.json}"
SEGMENTS_PER_PHASE="${SEGMENTS_PER_PHASE:-3}"
REPEATS="${REPEATS:-3}"
RANGE_BUCKET_SIZE="${RANGE_BUCKET_SIZE:-256}"

mkdir -p "$OUT_DIR"

CARGO_TARGET_DIR="$CARGO_TARGET_DIR" ~/.cargo/bin/cargo run --bin p3-feedback-bench -- \
  --reset-store \
  --compare-no-feedback \
  --store-dir "$STORE_DIR" \
  --no-feedback-store-dir "$NO_FEEDBACK_STORE_DIR" \
  --output "$OUT_JSON" \
  --segments-per-phase "$SEGMENTS_PER_PHASE" \
  --repeats "$REPEATS" \
  --range-bucket-size "$RANGE_BUCKET_SIZE" \
  "$@"

echo "wrote ${OUT_JSON}"
