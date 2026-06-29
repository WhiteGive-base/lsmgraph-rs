#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/data/WorkSpace/lsmgraph-rs}"
RUN_ID="${RUN_ID:?RUN_ID is required}"
INTERVAL_SECS="${INTERVAL_SECS:-300}"
OUT="${ROOT}/remote-logs/${RUN_ID}/resource-monitor.tsv"
PROGRESS="${ROOT}/remote-logs/${RUN_ID}/progress.log"
STORE="${ROOT}/store/${RUN_ID}"

mkdir -p "$(dirname "$OUT")"
if [[ ! -s "$OUT" ]]; then
  printf 'timestamp\tmem_available_gib\tdata_free_gib\tstore_size\tlast_progress\n' > "$OUT"
fi

while true; do
  ts="$(date -Is)"
  mem="$(awk '/MemAvailable/ { printf "%d", $2 / 1024 / 1024 }' /proc/meminfo)"
  disk="$(df -BG /data | awk 'NR == 2 { gsub(/G/, "", $4); print $4 }')"
  size="$(du -sh "$STORE" 2>/dev/null | awk '{ print $1 }')"
  phase="$(tail -1 "$PROGRESS" 2>/dev/null | tr '\t' ' ')"
  printf '%s\t%s\t%s\t%s\t%s\n' "$ts" "$mem" "$disk" "${size:-NA}" "$phase" >> "$OUT"

  [[ -e "${ROOT}/remote-logs/${RUN_ID}/DONE" ]] && break
  [[ -e "${ROOT}/remote-logs/${RUN_ID}/FAILED" ]] && break
  sleep "$INTERVAL_SECS"
done
