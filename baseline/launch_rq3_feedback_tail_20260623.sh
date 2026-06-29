#!/usr/bin/env bash
# Thin launch wrapper for the RQ3 feedback-tail experiment (mirrors launch_w14_*).
# Writes a self-contained run.sh into LOG_ROOT, nohups it, records PID + COMMAND.txt.
#
# Usage:
#   MODE=smoke  bash baseline/launch_rq3_feedback_tail_20260623.sh
#   RUN_FORMAL=1 MODE=formal DURATION=1800 bash baseline/launch_rq3_feedback_tail_20260623.sh
set -euo pipefail

ROOT="/data/WorkSpace/lsmgraph-rs"
MODE="${MODE:-smoke}"
RUN_ID="${RUN_ID:-rq3-feedback-tail-${MODE}-$(date +%Y%m%d-%H%M%S)}"
LOG_ROOT="$ROOT/remote-logs/$RUN_ID"

mkdir -p "$LOG_ROOT"
cd "$ROOT"

cat > "$LOG_ROOT/run.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd ${ROOT}
env \\
  RUN_ID=${RUN_ID} \\
  LOG_ROOT=${LOG_ROOT} \\
  MODE=${MODE} \\
  RUN_FORMAL=${RUN_FORMAL:-0} \\
  ARMS="${ARMS:-feedback full none}" \\
  DURATION=${DURATION:-} \\
  CHECKPOINT_SECS=${CHECKPOINT_SECS:-} \\
  COMPACT_SECS=${COMPACT_SECS:-} \\
  QUERY_RATE=${QUERY_RATE:-200} \\
  WRITE_RATE=${WRITE_RATE:-200} \\
  BASE_STORE=${BASE_STORE:-${ROOT}/store/sf30-base-graph} \\
  DATA_ROOT=${DATA_ROOT:-/data/WorkSpace/ldbc-sf30/social_network} \\
  RQ3_FORCE_RESET=${RQ3_FORCE_RESET:-0} \\
  bash baseline/run_rq3_feedback_tail_20260623.sh
EOF

chmod +x "$LOG_ROOT/run.sh"
cp "$LOG_ROOT/run.sh" "$LOG_ROOT/COMMAND.txt"

nohup "$LOG_ROOT/run.sh" > "$LOG_ROOT/nohup.out" 2>&1 &
pid=$!
echo "$pid" > "$LOG_ROOT/PID"
printf 'RUN_ID=%s\nPID=%s\nLOG_ROOT=%s\nMODE=%s\n' "$RUN_ID" "$pid" "$LOG_ROOT" "$MODE"
echo "tail -f $LOG_ROOT/nohup.out   # progress"
