#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: $0 LABEL BINARY STORE [extra import args...]" >&2
  exit 2
fi

LABEL="$1"
BIN="$2"
STORE="$3"
shift 3

INPUT="/data/WorkSpace/ldbc-sf30/social_network"
ARTIFACT_ROOT="/data/WorkSpace/try-aster-artifacts"
LOG_DIR="${ARTIFACT_ROOT}/logs/${LABEL}"

[[ -x "$BIN" ]] || { echo "binary is not executable: $BIN" >&2; exit 2; }
[[ -d "$INPUT/dynamic" ]] || { echo "missing SF30 input: $INPUT" >&2; exit 2; }
if [[ -e "$STORE" ]]; then
  echo "refusing to overwrite existing store: $STORE" >&2
  exit 2
fi

mkdir -p "$LOG_DIR" "$(dirname "$STORE")"
printf 'timestamp\tpid\trss_kb\tvmsize_kb\tthreads\n' > "${LOG_DIR}/rss.tsv"
printf '%q ' "$BIN" --io-backend blocking import \
  --input "$INPUT" \
  --data-dir "$STORE" \
  --relation snb-full \
  --memgraph-bytes 67108864 \
  --l0-layout semantic-budgeted \
  --semantic-budget-min-edge-type-bytes 0 \
  --semantic-budget-max-extra-l0-files 64 \
  "$@" > "${LOG_DIR}/command.txt"
printf '\n' >> "${LOG_DIR}/command.txt"

set +e
SNB_SKIP_ADJ_CACHE=1 /usr/bin/time -v -o "${LOG_DIR}/time.log" \
  "$BIN" --io-backend blocking import \
    --input "$INPUT" \
    --data-dir "$STORE" \
    --relation snb-full \
    --memgraph-bytes 67108864 \
    --l0-layout semantic-budgeted \
    --semantic-budget-min-edge-type-bytes 0 \
    --semantic-budget-max-extra-l0-files 64 \
    "$@" \
    > "${LOG_DIR}/import.stdout" \
    2> "${LOG_DIR}/import.stderr" &
time_pid=$!

while kill -0 "$time_pid" 2>/dev/null; do
  child_pid="$(pgrep -P "$time_pid" | head -n 1)"
  target_pid="${child_pid:-$time_pid}"
  if [[ -r "/proc/${target_pid}/status" ]]; then
    rss="$(awk '/^VmRSS:/ {print $2}' "/proc/${target_pid}/status")"
    vmsize="$(awk '/^VmSize:/ {print $2}' "/proc/${target_pid}/status")"
    threads="$(awk '/^Threads:/ {print $2}' "/proc/${target_pid}/status")"
    printf '%s\t%s\t%s\t%s\t%s\n' \
      "$(date -Is)" "$target_pid" "${rss:-0}" "${vmsize:-0}" "${threads:-0}" \
      >> "${LOG_DIR}/rss.tsv"
  fi
  sleep 1
done

wait "$time_pid"
status=$?
set -e
printf '%s\n' "$status" > "${LOG_DIR}/exit.status"
du -sb "$STORE" > "${LOG_DIR}/store-size.tsv" 2>/dev/null || true
find "$STORE" -maxdepth 1 -type f -printf '%f\t%s\n' \
  | sort > "${LOG_DIR}/top-level-files.tsv" 2>/dev/null || true
exit "$status"
