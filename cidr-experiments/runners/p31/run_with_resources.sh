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
  --min-samples N          Validator minimum (default 2).
  --temp LABEL=PATH        Temporary-disk root (repeatable).
  --container NAME         Add a Docker container process tree (repeatable).
  --extra-pid PID          Add an external process tree (repeatable).
  --allow-missing-aux-tools  Permit smoke without pidstat/iostat; never use formally.

Provenance inputs (paths and optional precomputed directory hashes):
  --binary PATH [--binary-sha256 HEX]
  --dataset PATH [--dataset-sha256 HEX]
  --truth PATH [--truth-sha256 HEX]
  --query-or-trace PATH [--query-or-trace-sha256 HEX]
  --config PATH [--config-sha256 HEX]

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
MIN_SAMPLES="2"
REQUIRE_AUX_TOOLS="true"
declare -a STORES=()
declare -a TEMPS=()
declare -a CONTAINERS=()
declare -a EXTRA_PIDS=()
BINARY=""
BINARY_SHA256=""
DATASET=""
DATASET_SHA256=""
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
    --min-samples) MIN_SAMPLES="${2:?}"; shift 2 ;;
    --store) STORES+=("${2:?}"); shift 2 ;;
    --temp) TEMPS+=("${2:?}"); shift 2 ;;
    --container) CONTAINERS+=("${2:?}"); shift 2 ;;
    --extra-pid) EXTRA_PIDS+=("${2:?}"); shift 2 ;;
    --allow-missing-aux-tools) REQUIRE_AUX_TOOLS="false"; shift ;;
    --binary) BINARY="${2:?}"; shift 2 ;;
    --binary-sha256) BINARY_SHA256="${2:?}"; shift 2 ;;
    --dataset) DATASET="${2:?}"; shift 2 ;;
    --dataset-sha256) DATASET_SHA256="${2:?}"; shift 2 ;;
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
if [[ -e "$RUN_DIR" && -n "$(find "$RUN_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "refusing non-empty run directory: $RUN_DIR" >&2
  exit 73
fi

mkdir -p "$RUN_DIR"
RUN_DIR="$(cd "$RUN_DIR" && pwd)"
RUN_ID="${RUN_ID:-$(basename "$RUN_DIR")}"
REPO_ROOT="${REPO_ROOT:-$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)}"
STOP_FILE="${RUN_DIR}/COLLECTOR_STOP"
COMMAND_FILE="${RUN_DIR}/command.txt"
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
  --min-samples "$MIN_SAMPLES"
  --require-aux-tools "$REQUIRE_AUX_TOOLS"
)
for value in "${STORES[@]}"; do MANIFEST_ARGS+=(--store "$value"); done
for value in "${TEMPS[@]}"; do MANIFEST_ARGS+=(--temp "$value"); done
for value in "${CONTAINERS[@]}"; do MANIFEST_ARGS+=(--container "$value"); done
for value in "${EXTRA_PIDS[@]}"; do MANIFEST_ARGS+=(--extra-pid "$value"); done
[[ -n "$BINARY" ]] && MANIFEST_ARGS+=(--binary "$BINARY")
[[ -n "$BINARY_SHA256" ]] && MANIFEST_ARGS+=(--binary-sha256 "$BINARY_SHA256")
[[ -n "$DATASET" ]] && MANIFEST_ARGS+=(--dataset "$DATASET")
[[ -n "$DATASET_SHA256" ]] && MANIFEST_ARGS+=(--dataset-sha256 "$DATASET_SHA256")
[[ -n "$TRUTH" ]] && MANIFEST_ARGS+=(--truth "$TRUTH")
[[ -n "$TRUTH_SHA256" ]] && MANIFEST_ARGS+=(--truth-sha256 "$TRUTH_SHA256")
[[ -n "$QUERY_OR_TRACE" ]] && MANIFEST_ARGS+=(--query-or-trace "$QUERY_OR_TRACE")
[[ -n "$QUERY_OR_TRACE_SHA256" ]] && MANIFEST_ARGS+=(--query-or-trace-sha256 "$QUERY_OR_TRACE_SHA256")
[[ -n "$CONFIG" ]] && MANIFEST_ARGS+=(--config "$CONFIG")
[[ -n "$CONFIG_SHA256" ]] && MANIFEST_ARGS+=(--config-sha256 "$CONFIG_SHA256")
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
COMMAND_RC=255
COLLECTOR_RC=255
WRAPPER_SIGNAL=""
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
    python3 -B "$MANIFEST_TOOL" abort --run-dir "$RUN_DIR" \
      --reason "wrapper exited before validation (rc=${prior_rc}, signal=${WRAPPER_SIGNAL:-none})" || true
  fi
}

trap cleanup EXIT
trap 'on_signal 1' HUP
trap 'on_signal 2' INT
trap 'on_signal 15' TERM

STARTED_AT_UTC="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
setsid -- "${COMMAND[@]}" > "${RUN_DIR}/command.stdout.log" 2> "${RUN_DIR}/command.stderr.log" &
COMMAND_PID=$!

declare -a COLLECTOR_ARGS=(
  python3 -B "$COLLECTOR"
  --run-dir "$RUN_DIR"
  --root-pid "$COMMAND_PID"
  --root-pgid "$COMMAND_PID"
  --stop-file "$STOP_FILE"
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

set +e
wait "$COMMAND_PID"
COMMAND_RC=$?
set -e
terminate_command_group
: > "$STOP_FILE"
set +e
wait "$COLLECTOR_PID"
COLLECTOR_RC=$?
set -e
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
if [[ "$VALIDATOR_RC" -ne 0 ]]; then
  exit 91
fi
exit 0
