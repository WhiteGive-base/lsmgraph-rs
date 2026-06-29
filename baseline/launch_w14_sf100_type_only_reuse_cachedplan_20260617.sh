#!/usr/bin/env bash
set -euo pipefail

ROOT="/data/WorkSpace/lsmgraph-rs"
RUN_ID="w14-sf100-type-only-reuse-cachedplan-20260617-codex2"
LOG_ROOT="$ROOT/remote-logs/$RUN_ID"

mkdir -p "$LOG_ROOT"
cd "$ROOT"

cat > "$LOG_ROOT/run.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd /data/WorkSpace/lsmgraph-rs
timeout 7200 env \
  W14_ALLOW_SF100=1 \
  SCALE=sf100 \
  RUN_ID=w14-sf100-type-only-reuse-cachedplan-20260617-codex2 \
  LOG_ROOT=/data/WorkSpace/lsmgraph-rs/remote-logs/w14-sf100-type-only-reuse-cachedplan-20260617-codex2 \
  STORE_ROOT=/data/WorkSpace/lsmgraph-rs/store/w14-sf100-import-only-tuned-20260616-codex2 \
  SF100_SCHEMA_STORE=/data/WorkSpace/lsmgraph-rs/store/qslsm-sf100-strong-baseline-20260610/schema \
  W14_PLAN_CACHE_ROOT=/data/WorkSpace/lsmgraph-rs/remote-logs/w14-sf100-scenarios-min-tuned-20260616-codex1 \
  VARIANTS="schema edge-type-only budg-b64 semantic" \
  SCENARIOS=type-only \
  SAMPLES=5000 \
  WARMUP_RUNS=1 \
  REPEATS=3 \
  IMPORT_TIMEOUT_SECONDS=60 \
  BENCH_TIMEOUT_SECONDS=7200 \
  COMPARE_TIMEOUT_SECONDS=600 \
  W14_EMIT_DIGESTS=1 \
  W14_NEIGHBOR_COMPARE=0 \
  bash baseline/run_w14_semantic_necessity_20260615.sh
EOF

chmod +x "$LOG_ROOT/run.sh"
cp "$LOG_ROOT/run.sh" "$LOG_ROOT/COMMAND.txt"

nohup "$LOG_ROOT/run.sh" > "$LOG_ROOT/nohup.out" 2>&1 &
pid=$!
echo "$pid" > "$LOG_ROOT/PID"
printf 'RUN_ID=%s\nPID=%s\nLOG_ROOT=%s\n' "$RUN_ID" "$pid" "$LOG_ROOT"
