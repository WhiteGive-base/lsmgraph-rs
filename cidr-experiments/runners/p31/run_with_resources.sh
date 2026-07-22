#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECTOR="${SCRIPT_DIR}/resource_collector.py"
MANIFEST_TOOL="${SCRIPT_DIR}/run_manifest.py"
VALIDATOR="${SCRIPT_DIR}/validate_resource_run.py"

usage() {
  cat <<'EOF'
Usage: run_with_resources.sh [options] -- command [args...]

Required:
  --run-dir ABS_PATH       New/empty artifact directory.
  --task-id ID             Frozen experiment task id.
  --store LABEL=PATH       Store root to sample (repeatable).

Core options:
  --run-id ID              Defaults to basename(run-dir).
  --performance-eligible true|false   Defaults to false.
  --repo-root PATH         Defaults to enclosing git worktree.
  --device NAME            Defaults to nvme1n1.
  --data-mount PATH        Defaults to /data.
  --interval SECONDS       Process/host sample interval (default 1).
  --disk-interval SECONDS  Store scan interval (default 15).
  --collector-ready-timeout SECONDS  Bound before adapter release (default 60).
  --min-samples N          Validator minimum (default 2).
  --temp LABEL=PATH        Temporary-disk root (repeatable).
  --container NAME         Add a Docker container process tree (repeatable).
  --extra-pid PID          Add an external process tree (repeatable).
  --allow-missing-aux-tools  Permit smoke without pidstat/iostat; never use formally.

Short clean-window v2 (all four options are required together):
  --batch-lease PATH
  --batch-gate-tool PATH
  --batch-consumer P10|P20
  --batch-anchor-binary PATH

Provenance inputs (paths and optional precomputed directory hashes):
  --binary PATH [--binary-sha256 HEX]
  --dataset PATH [--dataset-sha256 HEX]
  --dataset-sha256-mode verify|declared-no-read-v1
  --truth PATH [--truth-sha256 HEX]
  --query-or-trace PATH [--query-or-trace-sha256 HEX]
  --config PATH [--config-sha256 HEX]
  --input LABEL=PATH=SHA256   Additional immutable formal input (repeatable).

Only the validator creates DONE. Any command/collector/validation failure creates
FAILED and the wrapper exits non-zero.
EOF
}

RUN_DIR=""
RUN_ID=""
TASK_ID=""
PERFORMANCE_ELIGIBLE="false"
REPO_ROOT=""
DEVICE="nvme1n1"
DATA_MOUNT="/data"
INTERVAL="1"
DISK_INTERVAL="15"
COLLECTOR_READY_TIMEOUT="60"
MIN_SAMPLES="2"
REQUIRE_AUX_TOOLS="true"
declare -a STORES=()
declare -a TEMPS=()
declare -a CONTAINERS=()
declare -a EXTRA_PIDS=()
BATCH_LEASE=""
BATCH_GATE_TOOL=""
BATCH_CONSUMER=""
BATCH_ANCHOR_BINARY=""
BATCH_MODE=0
declare -a INPUTS=()
BINARY=""
BINARY_SHA256=""
DATASET=""
DATASET_SHA256=""
DATASET_SHA256_MODE="verify"
TRUTH=""
TRUTH_SHA256=""
QUERY_OR_TRACE=""
QUERY_OR_TRACE_SHA256=""
CONFIG=""
CONFIG_SHA256=""
declare -a COMMAND=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir) RUN_DIR="${2:?}"; shift 2 ;;
    --run-id) RUN_ID="${2:?}"; shift 2 ;;
    --task-id) TASK_ID="${2:?}"; shift 2 ;;
    --performance-eligible) PERFORMANCE_ELIGIBLE="${2:?}"; shift 2 ;;
    --repo-root) REPO_ROOT="${2:?}"; shift 2 ;;
    --device) DEVICE="${2:?}"; shift 2 ;;
    --data-mount) DATA_MOUNT="${2:?}"; shift 2 ;;
    --interval) INTERVAL="${2:?}"; shift 2 ;;
    --disk-interval) DISK_INTERVAL="${2:?}"; shift 2 ;;
    --collector-ready-timeout) COLLECTOR_READY_TIMEOUT="${2:?}"; shift 2 ;;
    --min-samples) MIN_SAMPLES="${2:?}"; shift 2 ;;
    --store) STORES+=("${2:?}"); shift 2 ;;
    --temp) TEMPS+=("${2:?}"); shift 2 ;;
    --container) CONTAINERS+=("${2:?}"); shift 2 ;;
    --extra-pid) EXTRA_PIDS+=("${2:?}"); shift 2 ;;
    --batch-lease) BATCH_LEASE="${2:?}"; shift 2 ;;
    --batch-gate-tool) BATCH_GATE_TOOL="${2:?}"; shift 2 ;;
    --batch-consumer) BATCH_CONSUMER="${2:?}"; shift 2 ;;
    --batch-anchor-binary) BATCH_ANCHOR_BINARY="${2:?}"; shift 2 ;;
    --allow-missing-aux-tools) REQUIRE_AUX_TOOLS="false"; shift ;;
    --binary) BINARY="${2:?}"; shift 2 ;;
    --binary-sha256) BINARY_SHA256="${2:?}"; shift 2 ;;
    --dataset) DATASET="${2:?}"; shift 2 ;;
    --dataset-sha256) DATASET_SHA256="${2:?}"; shift 2 ;;
    --dataset-sha256-mode) DATASET_SHA256_MODE="${2:?}"; shift 2 ;;
    --input) INPUTS+=("${2:?}"); shift 2 ;;
    --truth) TRUTH="${2:?}"; shift 2 ;;
    --truth-sha256) TRUTH_SHA256="${2:?}"; shift 2 ;;
    --query-or-trace) QUERY_OR_TRACE="${2:?}"; shift 2 ;;
    --query-or-trace-sha256) QUERY_OR_TRACE_SHA256="${2:?}"; shift 2 ;;
    --config) CONFIG="${2:?}"; shift 2 ;;
    --config-sha256) CONFIG_SHA256="${2:?}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    --) shift; COMMAND=("$@"); break ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

[[ -n "$RUN_DIR" ]] || { echo "--run-dir is required" >&2; exit 64; }
[[ "$RUN_DIR" == /* ]] || { echo "--run-dir must be absolute" >&2; exit 64; }
[[ -n "$TASK_ID" ]] || { echo "--task-id is required" >&2; exit 64; }
[[ ${#STORES[@]} -gt 0 ]] || { echo "at least one --store is required" >&2; exit 64; }
[[ ${#COMMAND[@]} -gt 0 ]] || { echo "command after -- is required" >&2; exit 64; }
[[ "$PERFORMANCE_ELIGIBLE" == "true" || "$PERFORMANCE_ELIGIBLE" == "false" ]] || {
  echo "--performance-eligible must be true or false" >&2; exit 64;
}
[[ "$COLLECTOR_READY_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || {
  echo "--collector-ready-timeout must be a positive integer" >&2; exit 64;
}
[[ "$DATASET_SHA256_MODE" == "verify" || "$DATASET_SHA256_MODE" == "declared-no-read-v1" ]] || {
  echo "--dataset-sha256-mode is invalid" >&2; exit 64;
}
if [[ "$DATASET_SHA256_MODE" == "declared-no-read-v1" && "$PERFORMANCE_ELIGIBLE" != "true" ]]; then
  echo "declared-no-read dataset SHA mode is formal-only" >&2
  exit 64
fi
batch_option_count=0
for value in "$BATCH_LEASE" "$BATCH_GATE_TOOL" "$BATCH_CONSUMER" "$BATCH_ANCHOR_BINARY"; do
  [[ -n "$value" ]] && batch_option_count=$((batch_option_count + 1))
done
if [[ "$batch_option_count" -ne 0 && "$batch_option_count" -ne 4 ]]; then
  echo "all short-clean-window v2 batch options are required together" >&2
  exit 64
fi
if [[ "$batch_option_count" -eq 4 ]]; then
  BATCH_MODE=1
  [[ "$BATCH_LEASE" == /* && -f "$BATCH_LEASE" ]] || {
    echo "--batch-lease must be an absolute existing file" >&2; exit 66;
  }
  [[ "$BATCH_GATE_TOOL" == /* && -f "$BATCH_GATE_TOOL" ]] || {
    echo "--batch-gate-tool must be an absolute existing file" >&2; exit 66;
  }
  [[ "$BATCH_ANCHOR_BINARY" == /* && -x "$BATCH_ANCHOR_BINARY" ]] || {
    echo "--batch-anchor-binary must be an absolute executable file" >&2; exit 66;
  }
  [[ "$BATCH_CONSUMER" == "P10" || "$BATCH_CONSUMER" == "P20" ]] || {
    echo "--batch-consumer must be P10 or P20" >&2; exit 64;
  }
fi
if [[ -e "$RUN_DIR" && -n "$(find "$RUN_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "refusing non-empty run directory: $RUN_DIR" >&2
  exit 73
fi

mkdir -p "$RUN_DIR"
RUN_DIR="$(cd "$RUN_DIR" && pwd)"
RUN_ID="${RUN_ID:-$(basename "$RUN_DIR")}"
REPO_ROOT="${REPO_ROOT:-$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)}"
STOP_FILE="${RUN_DIR}/COLLECTOR_STOP"
READY_FILE="${RUN_DIR}/collector-ready.json"
COLLECTOR_STATUS_FILE="${RUN_DIR}/collector-status.json"
COMMAND_FILE="${RUN_DIR}/command.txt"
GUARD_DIR="${RUN_DIR}/integrity-guard"
GUARD_READY_FILE="${GUARD_DIR}/READY.json"
GUARD_STATUS_FILE="${GUARD_DIR}/status.json"
COMMAND_RELEASE_FILE="${RUN_DIR}/command-release.json"
printf '%q ' "${COMMAND[@]}" > "$COMMAND_FILE"
printf '\n' >> "$COMMAND_FILE"

declare -a MANIFEST_ARGS=(
  python3 -B "$MANIFEST_TOOL" create
  --run-dir "$RUN_DIR"
  --run-id "$RUN_ID"
  --task-id "$TASK_ID"
  --performance-eligible "$PERFORMANCE_ELIGIBLE"
  --repo-root "$REPO_ROOT"
  --command-file "$COMMAND_FILE"
  --wrapper "${SCRIPT_DIR}/run_with_resources.sh"
  --manifest-tool "$MANIFEST_TOOL"
  --collector "$COLLECTOR"
  --validator "$VALIDATOR"
  --device "$DEVICE"
  --data-mount "$DATA_MOUNT"
  --interval "$INTERVAL"
  --disk-interval "$DISK_INTERVAL"
  --ready-timeout "$COLLECTOR_READY_TIMEOUT"
  --min-samples "$MIN_SAMPLES"
  --require-aux-tools "$REQUIRE_AUX_TOOLS"
)
for value in "${STORES[@]}"; do MANIFEST_ARGS+=(--store "$value"); done
for value in "${TEMPS[@]}"; do MANIFEST_ARGS+=(--temp "$value"); done
for value in "${CONTAINERS[@]}"; do MANIFEST_ARGS+=(--container "$value"); done
for value in "${EXTRA_PIDS[@]}"; do MANIFEST_ARGS+=(--extra-pid "$value"); done
for value in "${INPUTS[@]}"; do MANIFEST_ARGS+=(--input "$value"); done
[[ -n "$BINARY" ]] && MANIFEST_ARGS+=(--binary "$BINARY")
[[ -n "$BINARY_SHA256" ]] && MANIFEST_ARGS+=(--binary-sha256 "$BINARY_SHA256")
[[ -n "$DATASET" ]] && MANIFEST_ARGS+=(--dataset "$DATASET")
[[ -n "$DATASET_SHA256" ]] && MANIFEST_ARGS+=(--dataset-sha256 "$DATASET_SHA256")
MANIFEST_ARGS+=(--dataset-sha256-mode "$DATASET_SHA256_MODE")
[[ -n "$TRUTH" ]] && MANIFEST_ARGS+=(--truth "$TRUTH")
[[ -n "$TRUTH_SHA256" ]] && MANIFEST_ARGS+=(--truth-sha256 "$TRUTH_SHA256")
[[ -n "$QUERY_OR_TRACE" ]] && MANIFEST_ARGS+=(--query-or-trace "$QUERY_OR_TRACE")
[[ -n "$QUERY_OR_TRACE_SHA256" ]] && MANIFEST_ARGS+=(--query-or-trace-sha256 "$QUERY_OR_TRACE_SHA256")
[[ -n "$CONFIG" ]] && MANIFEST_ARGS+=(--config "$CONFIG")
[[ -n "$CONFIG_SHA256" ]] && MANIFEST_ARGS+=(--config-sha256 "$CONFIG_SHA256")
if [[ "$BATCH_MODE" == "1" ]]; then
  MANIFEST_ARGS+=(
    --batch-lease "$BATCH_LEASE"
    --batch-gate-tool "$BATCH_GATE_TOOL"
    --batch-consumer "$BATCH_CONSUMER"
    --batch-anchor-binary "$BATCH_ANCHOR_BINARY"
  )
fi
set +e
"${MANIFEST_ARGS[@]}"
MANIFEST_RC=$?
set -e
if [[ "$MANIFEST_RC" -ne 0 ]]; then
  python3 -B "$MANIFEST_TOOL" abort --run-dir "$RUN_DIR" \
    --reason "manifest creation failed (rc=${MANIFEST_RC})" || true
  exit "$MANIFEST_RC"
fi

COMMAND_PID=""
COLLECTOR_PID=""
GUARD_PID=""
COMMAND_RC=255
COLLECTOR_RC=255
GUARD_RC=255
WRAPPER_SIGNAL=""
COMMAND_RELEASE_AT_UTC=""
COMMAND_ENDED_AT_UTC=""
FINALIZED=0

terminate_command_group() {
  [[ -n "$COMMAND_PID" ]] || return 0
  if kill -0 -- "-${COMMAND_PID}" 2>/dev/null || kill -0 "$COMMAND_PID" 2>/dev/null; then
    kill -TERM -- "-${COMMAND_PID}" 2>/dev/null || kill -TERM "$COMMAND_PID" 2>/dev/null || true
    for _ in $(seq 1 50); do
      if ! kill -0 -- "-${COMMAND_PID}" 2>/dev/null && ! kill -0 "$COMMAND_PID" 2>/dev/null; then
        return 0
      fi
      sleep 0.1
    done
    kill -KILL -- "-${COMMAND_PID}" 2>/dev/null || kill -KILL "$COMMAND_PID" 2>/dev/null || true
  fi
}

on_signal() {
  WRAPPER_SIGNAL="$1"
  terminate_command_group
}

cleanup() {
  local prior_rc=$?
  if [[ "$FINALIZED" != "1" ]]; then
    terminate_command_group
    : > "$STOP_FILE"
    if [[ -n "$COLLECTOR_PID" ]] && kill -0 "$COLLECTOR_PID" 2>/dev/null; then
      kill -TERM "$COLLECTOR_PID" 2>/dev/null || true
      wait "$COLLECTOR_PID" 2>/dev/null || true
    fi
    if [[ -n "$GUARD_PID" ]] && kill -0 "$GUARD_PID" 2>/dev/null; then
      kill -TERM "$GUARD_PID" 2>/dev/null || true
      wait "$GUARD_PID" 2>/dev/null || true
    fi
    python3 -B "$MANIFEST_TOOL" abort --run-dir "$RUN_DIR" \
      --reason "wrapper exited before validation (rc=${prior_rc}, signal=${WRAPPER_SIGNAL:-none})" || true
  fi
}

trap cleanup EXIT
trap 'on_signal 1' HUP
trap 'on_signal 2' INT
trap 'on_signal 15' TERM

STARTED_AT_UTC="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
if [[ "$BATCH_MODE" == "1" ]]; then
  setsid -- bash -c '
    collector_ready="$1"
    collector_status="$2"
    guard_ready="$3"
    guard_status="$4"
    release_file="$5"
    timeout_s="$6"
    shift 6
    deadline=$((SECONDS + timeout_s))
    while [[ ! -f "$collector_ready" || ! -f "$guard_ready" ]]; do
      if [[ -f "$collector_status" || -f "$guard_status" ]]; then
        echo "collector/guard exited before dual readiness; command was not released" >&2
        exit 125
      fi
      if (( SECONDS >= deadline )); then
        echo "collector/guard readiness timed out after ${timeout_s}s; command was not released" >&2
        exit 125
      fi
      sleep 0.05
    done
    if [[ -f "$collector_status" || -f "$guard_status" ]]; then
      echo "collector/guard failed at the release boundary" >&2
      exit 125
    fi
    released_at="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
    temporary="${release_file}.tmp.$$"
    printf "{\"schema_version\":\"cidr-command-release-v2\",\"state\":\"RELEASED\",\"released_at_utc\":\"%s\"}\n" "$released_at" > "$temporary"
    mv "$temporary" "$release_file"
    exec "$@"
  ' cidr-dual-ready-gate "$READY_FILE" "$COLLECTOR_STATUS_FILE" "$GUARD_READY_FILE" "$GUARD_STATUS_FILE" "$COMMAND_RELEASE_FILE" "$COLLECTOR_READY_TIMEOUT" "${COMMAND[@]}" \
    > "${RUN_DIR}/command.stdout.log" 2> "${RUN_DIR}/command.stderr.log" &
else
  setsid -- bash -c '
    ready_file="$1"
    status_file="$2"
    timeout_s="$3"
    shift 3
    deadline=$((SECONDS + timeout_s))
    while [[ ! -f "$ready_file" ]]; do
      if [[ -f "$status_file" ]]; then
        echo "collector exited before readiness; adapter command was not released" >&2
        exit 125
      fi
      if (( SECONDS >= deadline )); then
        echo "collector readiness timed out after ${timeout_s}s; adapter command was not released" >&2
        exit 125
      fi
      sleep 0.05
    done
    exec "$@"
  ' cidr-collector-gate "$READY_FILE" "$COLLECTOR_STATUS_FILE" "$COLLECTOR_READY_TIMEOUT" "${COMMAND[@]}" \
    > "${RUN_DIR}/command.stdout.log" 2> "${RUN_DIR}/command.stderr.log" &
fi
COMMAND_PID=$!

declare -a COLLECTOR_ARGS=(
  python3 -B "$COLLECTOR"
  --run-dir "$RUN_DIR"
  --root-pid "$COMMAND_PID"
  --root-pgid "$COMMAND_PID"
  --stop-file "$STOP_FILE"
  --ready-file "$READY_FILE"
  --interval "$INTERVAL"
  --disk-interval "$DISK_INTERVAL"
  --device "$DEVICE"
  --data-mount "$DATA_MOUNT"
)
for value in "${STORES[@]}"; do COLLECTOR_ARGS+=(--store "$value"); done
for value in "${TEMPS[@]}"; do COLLECTOR_ARGS+=(--temp "$value"); done
for value in "${CONTAINERS[@]}"; do COLLECTOR_ARGS+=(--container "$value"); done
for value in "${EXTRA_PIDS[@]}"; do COLLECTOR_ARGS+=(--extra-pid "$value"); done
"${COLLECTOR_ARGS[@]}" > "${RUN_DIR}/collector.stdout.log" 2> "${RUN_DIR}/collector.stderr.log" &
COLLECTOR_PID=$!

if [[ "$BATCH_MODE" == "1" ]]; then
  declare -a GUARD_ARGS=(
    python3 -B "$BATCH_GATE_TOOL" guard
    --lease "$BATCH_LEASE"
    --consumer "$BATCH_CONSUMER"
    --repo-root "$REPO_ROOT"
    --binary "$BATCH_ANCHOR_BINARY"
    --root-pid "$COMMAND_PID"
    --stop-file "$STOP_FILE"
    --output-dir "$GUARD_DIR"
    --interval-seconds 1
    --max-gap-seconds 3
    --terminate-pgid-on-failure
  )
  for value in "${EXTRA_PIDS[@]}"; do GUARD_ARGS+=(--allowed-pid "$value"); done
  for value in "${CONTAINERS[@]}"; do GUARD_ARGS+=(--allowed-container "$value"); done
  "${GUARD_ARGS[@]}" > "${RUN_DIR}/integrity-guard.stdout.log" 2> "${RUN_DIR}/integrity-guard.stderr.log" &
  GUARD_PID=$!
fi

set +e
wait "$COMMAND_PID"
COMMAND_RC=$?
set -e
COMMAND_ENDED_AT_UTC="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
terminate_command_group
: > "$STOP_FILE"
set +e
wait "$COLLECTOR_PID"
COLLECTOR_RC=$?
set -e
if [[ "$BATCH_MODE" == "1" ]]; then
  set +e
  wait "$GUARD_PID"
  GUARD_RC=$?
  set -e
  if [[ -f "$COMMAND_RELEASE_FILE" ]]; then
    COMMAND_RELEASE_AT_UTC="$(python3 -B -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["released_at_utc"])' "$COMMAND_RELEASE_FILE")"
  else
    COMMAND_RELEASE_AT_UTC="NOT_RELEASED"
  fi
fi
ENDED_AT_UTC="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"

declare -a EXECUTION_ARGS=(
  python3 -B "$MANIFEST_TOOL" execution
  --run-dir "$RUN_DIR"
  --root-pid "$COMMAND_PID"
  --started-at-utc "$STARTED_AT_UTC"
  --ended-at-utc "$ENDED_AT_UTC"
  --command-exit-code "$COMMAND_RC"
  --collector-exit-code "$COLLECTOR_RC"
)
[[ -n "$WRAPPER_SIGNAL" ]] && EXECUTION_ARGS+=(--wrapper-signal "$WRAPPER_SIGNAL")
if [[ "$BATCH_MODE" == "1" ]]; then
  EXECUTION_ARGS+=(
    --command-release-at-utc "$COMMAND_RELEASE_AT_UTC"
    --command-ended-at-utc "$COMMAND_ENDED_AT_UTC"
    --guard-exit-code "$GUARD_RC"
  )
fi
"${EXECUTION_ARGS[@]}"

set +e
python3 -B "$VALIDATOR" --run-dir "$RUN_DIR" --min-samples "$MIN_SAMPLES" > "${RUN_DIR}/validator.stdout.log" 2> "${RUN_DIR}/validator.stderr.log"
VALIDATOR_RC=$?
set -e
if [[ "$VALIDATOR_RC" -ne 0 && ! -f "${RUN_DIR}/FAILED" ]]; then
  python3 -B "$MANIFEST_TOOL" abort --run-dir "$RUN_DIR" \
    --reason "validator failed without a FAILED marker (rc=${VALIDATOR_RC})" || true
fi

FINALIZED=1
trap - EXIT
if [[ "$COMMAND_RC" -ne 0 ]]; then
  exit "$COMMAND_RC"
fi
if [[ "$COLLECTOR_RC" -ne 0 ]]; then
  exit 90
fi
if [[ "$BATCH_MODE" == "1" && "$GUARD_RC" -ne 0 ]]; then
  exit 92
fi
if [[ "$VALIDATOR_RC" -ne 0 ]]; then
  exit 91
fi
exit 0
