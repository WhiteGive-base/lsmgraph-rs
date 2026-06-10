#!/usr/bin/env bash
# One-glance status of the SF100 strong-baseline run. Run anytime:
#   bash /data/WorkSpace/lsmgraph-rs/baseline/run-status.sh
# (or in this Claude session type:  ! bash baseline/run-status.sh )
set -u
ROOT=/data/WorkSpace/lsmgraph-rs
RUN_ID="${1:-qslsm-sf100-strong-baseline-20260610}"
OUT="$ROOT/remote-logs/$RUN_ID"
PROG="$OUT/progress.log"

echo "==================== $(date '+%Y-%m-%d %H:%M:%S') ===================="

# 1) is the harness orchestrator + a worker alive?
ORCH=$(pgrep -f "codex_qslsm_sf100_strong_baseline.sh" | head -1)
WORKER=$(pgrep -f "target/release/lsmgraph .*$RUN_ID" | head -1)
if [ -n "$ORCH" ]; then echo "ORCHESTRATOR: RUNNING (pid $ORCH)"; else echo "ORCHESTRATOR: not running"; fi
if [ -n "$WORKER" ]; then
  RSS=$(awk '/VmRSS/{printf "%.0fG",$2/1048576}' /proc/$WORKER/status 2>/dev/null)
  CMD=$(tr '\0' ' ' < /proc/$WORKER/cmdline 2>/dev/null | grep -oE '(import|storage-bench|neighbor-compare)[^/]*' | head -1)
  echo "WORKER:       RUNNING (pid $WORKER, rss=$RSS) -> $CMD"
else
  echo "WORKER:       none active right now (between steps, or finished)"
fi

# 2) finished / failed?
if grep -q 'done qslsm' "$PROG" 2>/dev/null; then echo "STATE:        *** DONE ***"; fi
if grep -q 'FATAL' "$PROG" 2>/dev/null; then echo "STATE:        *** FAILED ***  -> $(grep FATAL "$PROG" | tail -1)"; fi

# 3) latest milestones
echo "--- last milestones (progress.log) ---"
grep -E 'import |start .*-bench|finish |generate sample|delete |done|FATAL' "$PROG" 2>/dev/null | tail -6 || echo "(no progress yet)"

# 4) live progress of the current import/bench (is it actually moving?)
LATEST=$(ls -t "$OUT"/*-import.stderr "$OUT"/*-bench.err 2>/dev/null | head -1)
if [ -n "${LATEST:-}" ]; then
  echo "--- current step ($(basename "$LATEST"), updated $(stat -c '%y' "$LATEST" | cut -d. -f1)) ---"
  grep -oE 'directed_edges=[0-9]+ elapsed_s=[0-9.]+|get_neighbors|flush complete|rebuild_semantic' "$LATEST" 2>/dev/null | tail -1 || tail -1 "$LATEST"
fi

# 5) which variants already have results
echo "--- variants benched so far ---"
ls "$OUT"/*-bench.json 2>/dev/null | sed 's#.*/##;s/-bench.json//' | tr '\n' ' '; echo

# 6) resources
echo "--- resources ---"
free -g | awk '/Mem:/{print "  mem: "$7"G available of "$2"G (swap=0)"}'
df -h /data | awk 'NR==2{print "  disk: "$4" free ("$5" used)"}'
echo "============================================================"
