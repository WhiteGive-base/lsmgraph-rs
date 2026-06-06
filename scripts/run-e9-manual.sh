#!/usr/bin/env bash
# Standalone E9 LDBC benchmark runner

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR"
DGS_DIR="$ROOT/deps/ldbc_snb_interactive_impls/dgs"
BIN="$ROOT/target/release/lsmgraph"
HOST="127.0.0.1"
PORT="9090"
IO_BACKEND="${IO_BACKEND:-direct}"
SF_BASE_STORE="${1:-/data/WorkSpace/lsmgraph-rs/store/sf10-base-graph}"
THREADS="${2:-4}"
WARMUP="${3:-200}"
OP_COUNT="${4:-1000}"
TAG="${5:-manual-test}"
OUT_DIR="/tmp/e9-${TAG}"

echo "=== E9 LDBC Benchmark ==="
echo "SF Base Store: $SF_BASE_STORE"
echo "Threads: $THREADS"
echo "Warmup: $WARMUP"
echo "Operations: $OP_COUNT"
echo "Output: $OUT_DIR"
echo ""

# Create output directory
mkdir -p "$OUT_DIR"

# Copy base store to run store
RUN_STORE="$OUT_DIR/run-store"
echo "Preparing run store..."
rm -rf "$RUN_STORE"
mkdir -p "$RUN_STORE"
cp -r "$SF_BASE_STORE"/* "$RUN_STORE/"

# Create properties file
PROPS="$OUT_DIR/benchmark.properties"
echo "Creating properties file..."
cd "$DGS_DIR"
sed "s/thread_count=1/thread_count=$THREADS/" interactive-benchmark-sf10.properties > "$PROPS"
sed -i "s/warmup=100/warmup=$WARMUP/" "$PROPS"
sed -i "s/operation_count=250/operation_count=$OP_COUNT/" "$PROPS"

# Start server
echo "Starting server..."
SERVER_LOG="$OUT_DIR/server.log"
DATA_DIR="$RUN_STORE" IO_BACKEND="$IO_BACKEND" HOST="$HOST" PORT="$PORT" \
  bash "$ROOT/deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh" \
  > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!
echo "Server PID: $SERVER_PID"

# Wait for server
echo "Waiting for server..."
for i in $(seq 1 60); do
  if grep -q '"server":"lsmgraph-snb"' "$SERVER_LOG" 2>/dev/null; then
    echo "Server ready after ${i}s"
    break
  fi
  if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo "Server died during startup"
    cat "$SERVER_LOG"
    exit 1
  fi
  sleep 1
done

# Reset metrics
curl -s -X POST "http://$HOST:$PORT/metrics/reset" > "$OUT_DIR/metrics-reset.json" || true
echo "Metrics reset"

# Run driver
echo "Running LDBC driver..."
DRIVER_LOG="$OUT_DIR/driver.log"
cd "$DGS_DIR"
timeout 600 ./run.sh "$PROPS" > "$DRIVER_LOG" 2>&1
DRIVER_STATUS=$?
echo "Driver exit status: $DRIVER_STATUS"

# Get server metrics
curl -s "http://$HOST:$PORT/metrics" > "$OUT_DIR/server-metrics.json" || true
echo "Server metrics captured"

# Kill server
kill $SERVER_PID 2>/dev/null || true
wait $SERVER_PID 2>/dev/null || true
echo "Server stopped"

# Check results
echo ""
echo "=== Results ==="
echo "Driver status: $DRIVER_STATUS"
echo "Driver log: $DRIVER_LOG"
if [[ -f "$DRIVER_LOG" ]]; then
  echo "Last 20 lines of driver log:"
  tail -20 "$DRIVER_LOG"
fi

echo ""
echo "Server metrics:"
if [[ -f "$OUT_DIR/server-metrics.json" ]]; then
  cat "$OUT_DIR/server-metrics.json"
fi

# Copy results to remote-logs
FINAL_DIR="remote-logs/e9-ldbc-end-to-end-${TAG}"
mkdir -p "$FINAL_DIR"
cp -r "$OUT_DIR"/* "$FINAL_DIR/"
echo ""
echo "Results copied to: $FINAL_DIR"
