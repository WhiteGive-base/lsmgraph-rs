#!/usr/bin/env bash
# Read-only machine monitor for the seml0-gap-analysis-20260612 session.
# Samples every INTERVAL seconds; appends one line per sample to machine-monitor.log.
# It must NEVER signal/kill any process. It only reads /proc and runs ps/df.
set -u
INTERVAL="${INTERVAL:-90}"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="${OUT_DIR}/machine-monitor.log"

echo "# machine monitor started pid=$$ interval=${INTERVAL}s $(date '+%F %T')" >> "$LOG"
echo "# columns: time | load1 | mem_avail_gib | data_free_gib | lsmgraph_release_procs(pid:rss_gib:state) | correctness_script_alive" >> "$LOG"

while true; do
  ts=$(date '+%F %T')
  load1=$(cut -d' ' -f1 /proc/loadavg)
  mem_avail_gib=$(awk '/MemAvailable/ {printf "%.1f", $2/1048576}' /proc/meminfo)
  data_free_gib=$(df -BG --output=avail /data 2>/dev/null | tail -1 | tr -dc '0-9')
  procs=$(ps -eo pid,rss,state,args --no-headers 2>/dev/null \
    | awk '/target\/release\/lsmgraph/ && !/awk/ {printf "%s%s:%.1f:%s", sep, $1, $2/1048576, $3; sep=","} END {if (sep=="") printf "none"}')
  script_alive=$(pgrep -f 'run_sf100_correctness_csr_variant_20260611.sh' >/dev/null 2>&1 && echo yes || echo no)
  echo "${ts} | ${load1} | ${mem_avail_gib} | ${data_free_gib} | ${procs} | ${script_alive}" >> "$LOG"
  sleep "$INTERVAL"
done
