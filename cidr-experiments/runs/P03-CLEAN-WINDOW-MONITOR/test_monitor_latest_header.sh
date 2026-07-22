#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TMP_ROOT"' EXIT

env \
  ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)" \
  RUN_ID=P03-LATEST-HEADER-FIXTURE \
  OUT_DIR="$TMP_ROOT/run" \
  GATE_MODE=observe \
  SAMPLE_INTERVAL_SECONDS=2 \
  MEASURE_SECONDS=1 \
  READY_SAMPLES=1 \
  LOAD_MAX=100000 \
  CPU_IDLE_MIN_PCT=-1 \
  MEM_AVAILABLE_MIN_KIB=1 \
  DATA_FREE_MIN_KIB=1 \
  DISK_UTIL_MAX_PCT=101 \
  DISK_AWAIT_MAX_MS=100000 \
  ZCL_BIG_RSS_KIB=999999999 \
  /bin/bash "$SCRIPT_DIR/monitor_clean_window.sh" >/dev/null

expected_header=$'timestamp\tsample\tload1\tcpu_idle_pct\tmem_available_kib\tmem_available_gib\tdata_free_kib\tdata_free_gib\tdevice\tdisk_util_pct\tdisk_read_await_ms\tdisk_write_await_ms\tdisk_await_ms\tzcl_big_count\tzcl_rsync_count\tgpstore_count\ttugraph_count\tgate_mode\tmetric_pass\tservice_pass\tsample_pass\tstreak\treasons'
actual_header="$(head -n 1 "$TMP_ROOT/run/latest.tsv")"
[[ "$actual_header" == "$expected_header" ]]
[[ "$(awk -F '\t' 'NR == 1 { print NF }' "$TMP_ROOT/run/latest.tsv")" == 23 ]]
[[ "$(wc -l < "$TMP_ROOT/run/latest.tsv")" == 2 ]]
[[ -f "$TMP_ROOT/run/READY" ]]

printf 'P03 latest.tsv header fixture PASS\n'
