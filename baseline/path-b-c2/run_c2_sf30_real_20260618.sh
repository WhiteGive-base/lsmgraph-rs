#!/usr/bin/env bash
# C2 real LDBC SF30 retention run (v2: build-l1-from-l0).
# Import SF30 WITHOUT compaction (keep many live L0 semantic segments), copy x2,
# then on each copy drain L0 -> per-partition exact L1 and run one policy's
# L1->L2 merge, measuring surface + cost. Validated on SF1 first.
set -uo pipefail
cd /data/WorkSpace/lsmgraph-rs

RUN_ID="c2-sf30-real-20260619"
LOG_DIR="remote-logs/${RUN_ID}"
mkdir -p "$LOG_DIR"
INPUT="/data/WorkSpace/ldbc-sf30/social_network"
BASE="store/c2-sf30-base"
NAIVE="store/c2-sf30-naive"
SEM="store/c2-sf30-semantic"
LAYOUT="semantic"
MEMGRAPH_BYTES=$((256 * 1024 * 1024))
IMPORT_TIMEOUT=7200   # 2h hard cap on import

rm -f "$LOG_DIR/DONE" "$LOG_DIR/FAILED"
fail() { echo "FAILED: $*" | tee "$LOG_DIR/FAILED"; exit 1; }

# --- resources ---
avail_g=$(df -PBG /data | awk 'NR==2{gsub(/G/,"",$4); print $4}')
mem_g=$(free -g | awk '/Mem:/{print $7}')
echo "resources: /data avail=${avail_g}G MemAvailable=${mem_g}G" | tee "$LOG_DIR/resources.txt"
[ "${avail_g:-0}" -lt 200 ] && fail "disk < 200G"
[ "${mem_g:-0}" -lt 80 ] && fail "MemAvailable < 80G"

# --- release build ---
nice -n 10 cargo build --release -j 16 --bin lsmgraph --bin c2-merge-retention \
  > "$LOG_DIR/build.log" 2>&1 || fail "release build (see build.log)"
BIN=target/release/lsmgraph
C2=target/release/c2-merge-retention

# --- fresh import WITHOUT compaction (keep many live L0) ---
rm -rf "$BASE"   # discard any prior FullCompact'd base
echo "[import] SF30 -> $BASE (layout=$LAYOUT, NO compact) ..."
timeout "$IMPORT_TIMEOUT" nice -n 10 "$BIN" import \
  --input "$INPUT" --data-dir "$BASE" --relation snb-full \
  --memgraph-bytes "$MEMGRAPH_BYTES" --l0-layout "$LAYOUT" \
  > "$LOG_DIR/import.log" 2>&1 || fail "import (see import.log)"
echo "[import] base ready: $(du -sh "$BASE" | cut -f1); live L0 on disk: $(ls "$BASE"/levels/L0/*.edge 2>/dev/null | wc -l)" | tee -a "$LOG_DIR/resources.txt"

# --- two copies (same L0 state) ---
rm -rf "$NAIVE" "$SEM"
cp -r "$BASE" "$NAIVE" || fail "copy naive"
cp -r "$BASE" "$SEM"   || fail "copy semantic"

# --- build exact L1 from L0, then L1->L2 under each policy ---
nice -n 10 "$C2" --open "$NAIVE" --policy naive    --build-l1-from-l0 \
  --output "$LOG_DIR/naive.json"    > "$LOG_DIR/naive.log" 2>&1    || fail "naive run (see naive.log)"
nice -n 10 "$C2" --open "$SEM"  --policy semantic --build-l1-from-l0 \
  --output "$LOG_DIR/semantic.json" > "$LOG_DIR/semantic.log" 2>&1 || fail "semantic run (see semantic.log)"

echo "DONE" | tee "$LOG_DIR/DONE"
echo "=== naive ===";    cat "$LOG_DIR/naive.json"
echo "=== semantic ==="; cat "$LOG_DIR/semantic.json"

# --- cleanup big copies (keep base + jsons + logs) ---
rm -rf "$NAIVE" "$SEM"
echo "kept: $LOG_DIR/{naive,semantic}.json + logs; base at $BASE"
