#!/usr/bin/env bash
# C2 real LDBC SF30 read-amplification proxy replay.
#
# Reuses store/c2-sf30-base, copies it into two policy-specific stores, then
# builds exact L1 partitions and runs the C2 merge-retention runner. The runner
# now emits a metadata-level typed-neighbor partition replay over the real SF30
# post-merge output segments.
set -uo pipefail

cd /data/WorkSpace/lsmgraph-rs

RUN_ID="c2-sf30-readamp-proxy-20260621"
LOG_DIR="remote-logs/${RUN_ID}"
BASE="store/c2-sf30-base"
NAIVE="store/c2-sf30-proxy-naive-20260621"
SEMANTIC="store/c2-sf30-proxy-semantic-20260621"
C2="target/release/c2-merge-retention"

mkdir -p "$LOG_DIR"

fail() {
  echo "FAILED: $*" | tee "$LOG_DIR/FAILED"
  exit 1
}

echo "[start] $(date -Is)" | tee "$LOG_DIR/progress.log"
df -h /data | tee "$LOG_DIR/df-start.txt"

[ -d "$BASE" ] || fail "missing base store: $BASE"

if [ ! -x "$C2" ]; then
  echo "[build] c2-merge-retention" | tee -a "$LOG_DIR/progress.log"
  cargo build --release --bin c2-merge-retention > "$LOG_DIR/build.log" 2>&1 \
    || fail "cargo build --release --bin c2-merge-retention"
fi

if [ -e "$NAIVE" ] || [ -e "$SEMANTIC" ]; then
  fail "target store already exists: $NAIVE or $SEMANTIC"
fi

echo "[copy] $BASE -> $NAIVE" | tee -a "$LOG_DIR/progress.log"
cp -a --reflink=auto "$BASE" "$NAIVE" > "$LOG_DIR/copy-naive.log" 2>&1 \
  || fail "copy naive"

echo "[copy] $BASE -> $SEMANTIC" | tee -a "$LOG_DIR/progress.log"
cp -a --reflink=auto "$BASE" "$SEMANTIC" > "$LOG_DIR/copy-semantic.log" 2>&1 \
  || fail "copy semantic"

echo "[run] naive proxy" | tee -a "$LOG_DIR/progress.log"
nice -n 10 "$C2" --open "$NAIVE" --policy naive --build-l1-from-l0 \
  --output "$LOG_DIR/naive-proxy.json" > "$LOG_DIR/naive-proxy.log" 2>&1 \
  || fail "naive proxy"

echo "[run] semantic proxy" | tee -a "$LOG_DIR/progress.log"
nice -n 10 "$C2" --open "$SEMANTIC" --policy semantic --build-l1-from-l0 \
  --output "$LOG_DIR/semantic-proxy.json" > "$LOG_DIR/semantic-proxy.log" 2>&1 \
  || fail "semantic proxy"

df -h /data | tee "$LOG_DIR/df-end.txt"
echo "DONE" | tee "$LOG_DIR/DONE"
echo "[done] $(date -Is)" | tee -a "$LOG_DIR/progress.log"
