#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
RUNNER="$ROOT/baseline/run_p40_fixed_trace.sh"
FIXTURE_P31="$ROOT/cidr-experiments/runners/p40/tests/fixture_p31.sh"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/p40-p31-contract.XXXXXX")"
trap 'rm -rf -- "$TMP_ROOT"' EXIT

TRACE="$TMP_ROOT/trace.jsonl"
BIN="$ROOT/cidr-experiments/runners/p40/tests/fixture_p40_bin.sh"
LOG_ROOT="$TMP_ROOT/log"
STORE_PARENT="$TMP_ROOT/store"

printf '{"seq":0,"op":"query","src":1,"edge_type":7}\n' > "$TRACE"
[[ -x "$BIN" && -x "$FIXTURE_P31" ]] \
  || { echo 'P40/P31 fixtures must be executable' >&2; exit 1; }

READY="$TMP_ROOT/READY"
printf 'readiness_gate=PASS\n' > "$READY"
set +e
MODE=formal \
RUN_FORMAL=1 \
PERFORMANCE_ELIGIBLE=1 \
REPEATS=1 \
BUILD=0 \
TRACE_IN="$TRACE" \
BIN="$BIN" \
LOG_ROOT="$TMP_ROOT/formal-blocked-log" \
STORE_PARENT="$TMP_ROOT/formal-blocked-store" \
P31_COLLECTOR="$FIXTURE_P31" \
CLEAN_READY_FILE="$READY" \
P31_ALLOW_MISSING_AUX_TOOLS=1 \
bash "$RUNNER" > "$TMP_ROOT/formal.stdout" 2> "$TMP_ROOT/formal.stderr"
formal_rc=$?
set -e
[[ "$formal_rc" -ne 0 ]] || { echo 'formal mode accepted missing auxiliary tools' >&2; exit 1; }
grep -Fq 'formal mode forbids P31_ALLOW_MISSING_AUX_TOOLS=1' "$TMP_ROOT/formal.stderr" \
  || { echo 'formal missing-aux rejection was not fail closed' >&2; exit 1; }
[[ ! -e "$TMP_ROOT/formal-blocked-log" && ! -e "$TMP_ROOT/formal-blocked-store" ]] \
  || { echo 'formal rejection created run artifacts' >&2; exit 1; }

set +e
MODE=fixture \
REPEATS=1 \
BUILD=0 \
TRACE_IN="$TRACE" \
BIN="$BIN" \
LOG_ROOT="$LOG_ROOT" \
STORE_PARENT="$STORE_PARENT" \
P31_COLLECTOR="$FIXTURE_P31" \
P31_ALLOW_MISSING_AUX_TOOLS=1 \
bash "$RUNNER" > "$TMP_ROOT/runner.stdout" 2> "$TMP_ROOT/runner.stderr"
runner_rc=$?
set -e

# The synthetic output intentionally lacks query records, so the real P40
# correctness validator must reject it after all four collector invocations.
[[ "$runner_rc" -ne 0 ]] || { echo 'contract fixture unexpectedly passed correctness validation' >&2; exit 1; }
[[ ! -e "$LOG_ROOT/DONE" ]] || { echo 'contract fixture must not publish P40 DONE' >&2; exit 1; }

for arm in none capacity-naive semantic-static semantic-feedback; do
  argv="$LOG_ROOT/repeat-01/$arm.p31/argv.txt"
  [[ -f "$argv" ]] || { echo "missing captured P31 argv for $arm" >&2; exit 1; }
  for option in --run-dir --task-id --performance-eligible --repo-root --store --binary --dataset --query-or-trace --config --allow-missing-aux-tools; do
    grep -Fxq -- "$option" "$argv" || { echo "$arm missing $option" >&2; exit 1; }
  done
  if grep -Exq -- '--repeat|--arm|--raw-dir' "$argv"; then
    echo "$arm used a legacy P31 option" >&2
    exit 1
  fi
done

printf 'P40/P31 argv contract fixture passed (4 arms; formal timing not run).\n'
