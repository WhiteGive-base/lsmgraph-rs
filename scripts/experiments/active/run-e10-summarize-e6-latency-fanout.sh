#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

TAG="${TAG:-e10-e6-latency-fanout-$(date +%Y%m%d-%H%M%S)}"
OUT_DIR="${OUT_DIR:-remote-logs/${TAG}}"

SCHEMA_STORAGE_JSON="${SCHEMA_STORAGE_JSON:-}"
FULL_STORAGE_JSON="${FULL_STORAGE_JSON:-}"
BUDGETED_STORAGE_JSON="${BUDGETED_STORAGE_JSON:-}"
SCHEMA_ALLTYPES_STORAGE_JSON="${SCHEMA_ALLTYPES_STORAGE_JSON:-}"
FULL_ALLTYPES_STORAGE_JSON="${FULL_ALLTYPES_STORAGE_JSON:-}"
BUDGETED_ALLTYPES_STORAGE_JSON="${BUDGETED_ALLTYPES_STORAGE_JSON:-}"

SCHEMA_FILE_SUMMARY="${SCHEMA_FILE_SUMMARY:-}"
FULL_FILE_SUMMARY="${FULL_FILE_SUMMARY:-}"
BUDGETED_FILE_SUMMARY="${BUDGETED_FILE_SUMMARY:-}"

mkdir -p "$OUT_DIR"

{
  echo "date=$(date -Is)"
  echo "pwd=$(pwd)"
  echo "tag=${TAG}"
  echo "schema_storage_json=${SCHEMA_STORAGE_JSON}"
  echo "full_storage_json=${FULL_STORAGE_JSON}"
  echo "budgeted_storage_json=${BUDGETED_STORAGE_JSON}"
  echo "schema_alltypes_storage_json=${SCHEMA_ALLTYPES_STORAGE_JSON}"
  echo "full_alltypes_storage_json=${FULL_ALLTYPES_STORAGE_JSON}"
  echo "budgeted_alltypes_storage_json=${BUDGETED_ALLTYPES_STORAGE_JSON}"
  echo "schema_file_summary=${SCHEMA_FILE_SUMMARY}"
  echo "full_file_summary=${FULL_FILE_SUMMARY}"
  echo "budgeted_file_summary=${BUDGETED_FILE_SUMMARY}"
  git rev-parse HEAD || true
  git status --short || true
  rustc --version || true
  cargo --version || true
  df -h /data || true
} > "${OUT_DIR}/run.meta"

args=(--out-dir "$OUT_DIR")

if [[ -n "$SCHEMA_STORAGE_JSON" ]]; then
  [[ -s "$SCHEMA_STORAGE_JSON" ]] || { echo "missing SCHEMA_STORAGE_JSON=$SCHEMA_STORAGE_JSON" >&2; exit 2; }
  args+=(--storage-json sf30-schema-core "$SCHEMA_STORAGE_JSON")
fi
if [[ -n "$FULL_STORAGE_JSON" ]]; then
  [[ -s "$FULL_STORAGE_JSON" ]] || { echo "missing FULL_STORAGE_JSON=$FULL_STORAGE_JSON" >&2; exit 2; }
  args+=(--storage-json sf30-full-semantic-core "$FULL_STORAGE_JSON")
fi
if [[ -n "$BUDGETED_STORAGE_JSON" ]]; then
  [[ -s "$BUDGETED_STORAGE_JSON" ]] || { echo "missing BUDGETED_STORAGE_JSON=$BUDGETED_STORAGE_JSON" >&2; exit 2; }
  args+=(--storage-json sf30-budgeted-core "$BUDGETED_STORAGE_JSON")
fi
if [[ -n "$SCHEMA_ALLTYPES_STORAGE_JSON" ]]; then
  [[ -s "$SCHEMA_ALLTYPES_STORAGE_JSON" ]] || { echo "missing SCHEMA_ALLTYPES_STORAGE_JSON=$SCHEMA_ALLTYPES_STORAGE_JSON" >&2; exit 2; }
  args+=(--storage-json sf30-schema-alltypes "$SCHEMA_ALLTYPES_STORAGE_JSON")
fi
if [[ -n "$FULL_ALLTYPES_STORAGE_JSON" ]]; then
  [[ -s "$FULL_ALLTYPES_STORAGE_JSON" ]] || { echo "missing FULL_ALLTYPES_STORAGE_JSON=$FULL_ALLTYPES_STORAGE_JSON" >&2; exit 2; }
  args+=(--storage-json sf30-full-semantic-alltypes "$FULL_ALLTYPES_STORAGE_JSON")
fi
if [[ -n "$BUDGETED_ALLTYPES_STORAGE_JSON" ]]; then
  [[ -s "$BUDGETED_ALLTYPES_STORAGE_JSON" ]] || { echo "missing BUDGETED_ALLTYPES_STORAGE_JSON=$BUDGETED_ALLTYPES_STORAGE_JSON" >&2; exit 2; }
  args+=(--storage-json sf30-budgeted-alltypes "$BUDGETED_ALLTYPES_STORAGE_JSON")
fi

if [[ -n "$SCHEMA_FILE_SUMMARY" ]]; then
  [[ -s "$SCHEMA_FILE_SUMMARY" ]] || { echo "missing SCHEMA_FILE_SUMMARY=$SCHEMA_FILE_SUMMARY" >&2; exit 3; }
  args+=(--file-summary sf30-schema "$SCHEMA_FILE_SUMMARY")
fi
if [[ -n "$FULL_FILE_SUMMARY" ]]; then
  [[ -s "$FULL_FILE_SUMMARY" ]] || { echo "missing FULL_FILE_SUMMARY=$FULL_FILE_SUMMARY" >&2; exit 3; }
  args+=(--file-summary sf30-full-semantic "$FULL_FILE_SUMMARY")
fi
if [[ -n "$BUDGETED_FILE_SUMMARY" ]]; then
  [[ -s "$BUDGETED_FILE_SUMMARY" ]] || { echo "missing BUDGETED_FILE_SUMMARY=$BUDGETED_FILE_SUMMARY" >&2; exit 3; }
  args+=(--file-summary sf30-budgeted "$BUDGETED_FILE_SUMMARY")
fi

python3 scripts/analysis/current/summarize-end-to-end-evidence.py "${args[@]}" \
  > "${OUT_DIR}/summary-path.txt"

if [[ ! -s "${OUT_DIR}/storage-latency-summary.tsv" && ! -s "${OUT_DIR}/metadata-fanout-summary.tsv" ]]; then
  echo "no E6 latency/fanout summary was generated; provide storage JSON and/or file-summary inputs" >&2
  exit 4
fi

touch "${OUT_DIR}/PASS"
echo "PASS ${OUT_DIR}"
