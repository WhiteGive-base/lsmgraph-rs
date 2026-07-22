#!/usr/bin/env bash
set -Eeuo pipefail

# Narrow, auditable bridge from the P10/P11 orchestrator to P31.  It accepts
# no benchmark-specific options and never starts or stops a database service.

usage() {
  cat <<'EOF'
Usage: run_adapter_with_p31.sh [options] -- adapter-command [args...]

Required scalar options:
  --p31-wrapper PATH
  --run-dir ABS_PATH
  --run-id ID
  --task-id ID
  --performance-eligible true|false
  --repo-root PATH
  --device NAME
  --data-mount PATH
  --interval SECONDS
  --disk-interval SECONDS
  --min-samples N
  --binary PATH --binary-sha256 HEX
  --dataset PATH --dataset-sha256 HEX
  --dataset-sha256-mode verify|declared-no-read-v1
  --truth PATH --truth-sha256 HEX
  --query-or-trace PATH --query-or-trace-sha256 HEX
  --config PATH --config-sha256 HEX
  [--batch-lease PATH --batch-gate-tool PATH --batch-consumer P10|P20
   --batch-anchor-binary PATH]

Repeatable:
  --store LABEL=PATH (at least one), --temp LABEL=PATH,
  --container NAME, --extra-pid PID
  --input LABEL=PATH=SHA256

Fixture only:
  --allow-missing-aux-tools
EOF
}

P31=""
RUN_DIR=""
RUN_ID=""
TASK_ID=""
PERFORMANCE_ELIGIBLE=""
REPO_ROOT=""
DEVICE=""
DATA_MOUNT=""
INTERVAL=""
DISK_INTERVAL=""
MIN_SAMPLES=""
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
BATCH_LEASE=""
BATCH_GATE_TOOL=""
BATCH_CONSUMER=""
BATCH_ANCHOR_BINARY=""
ALLOW_MISSING_AUX=0
declare -a STORES=()
declare -a TEMPS=()
declare -a CONTAINERS=()
declare -a EXTRA_PIDS=()
declare -a INPUTS=()
declare -a COMMAND=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --p31-wrapper) P31="${2:?}"; shift 2 ;;
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
    --batch-lease) BATCH_LEASE="${2:?}"; shift 2 ;;
    --batch-gate-tool) BATCH_GATE_TOOL="${2:?}"; shift 2 ;;
    --batch-consumer) BATCH_CONSUMER="${2:?}"; shift 2 ;;
    --batch-anchor-binary) BATCH_ANCHOR_BINARY="${2:?}"; shift 2 ;;
    --allow-missing-aux-tools) ALLOW_MISSING_AUX=1; shift ;;
    --) shift; COMMAND=("$@"); break ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown P10/P11 P31 bridge option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

for pair in \
  "--p31-wrapper=$P31" \
  "--run-dir=$RUN_DIR" \
  "--run-id=$RUN_ID" \
  "--task-id=$TASK_ID" \
  "--performance-eligible=$PERFORMANCE_ELIGIBLE" \
  "--repo-root=$REPO_ROOT" \
  "--device=$DEVICE" \
  "--data-mount=$DATA_MOUNT" \
  "--interval=$INTERVAL" \
  "--disk-interval=$DISK_INTERVAL" \
  "--min-samples=$MIN_SAMPLES" \
  "--binary=$BINARY" \
  "--binary-sha256=$BINARY_SHA256" \
  "--dataset=$DATASET" \
  "--dataset-sha256=$DATASET_SHA256" \
  "--truth=$TRUTH" \
  "--truth-sha256=$TRUTH_SHA256" \
  "--query-or-trace=$QUERY_OR_TRACE" \
  "--query-or-trace-sha256=$QUERY_OR_TRACE_SHA256" \
  "--config=$CONFIG" \
  "--config-sha256=$CONFIG_SHA256"; do
  option="${pair%%=*}"
  value="${pair#*=}"
  [[ -n "$value" ]] || { echo "$option is required" >&2; exit 64; }
done
[[ "$RUN_DIR" == /* ]] || { echo '--run-dir must be absolute' >&2; exit 64; }
[[ "$PERFORMANCE_ELIGIBLE" == "true" || "$PERFORMANCE_ELIGIBLE" == "false" ]] \
  || { echo '--performance-eligible must be true or false' >&2; exit 64; }
[[ "$DATASET_SHA256_MODE" == "verify" || "$DATASET_SHA256_MODE" == "declared-no-read-v1" ]] \
  || { echo '--dataset-sha256-mode is invalid' >&2; exit 64; }
if [[ "$DATASET_SHA256_MODE" == "declared-no-read-v1" && "$PERFORMANCE_ELIGIBLE" != "true" ]]; then
  echo 'declared-no-read dataset SHA mode is formal-only' >&2
  exit 64
fi
[[ ${#STORES[@]} -gt 0 ]] || { echo 'at least one --store is required' >&2; exit 64; }
[[ ${#COMMAND[@]} -gt 0 ]] || { echo 'adapter command after -- is required' >&2; exit 64; }
[[ -x "$P31" ]] || { echo "P31 wrapper is not executable: $P31" >&2; exit 66; }
batch_option_count=0
for value in "$BATCH_LEASE" "$BATCH_GATE_TOOL" "$BATCH_CONSUMER" "$BATCH_ANCHOR_BINARY"; do
  [[ -n "$value" ]] && batch_option_count=$((batch_option_count + 1))
done
if [[ "$batch_option_count" -ne 0 && "$batch_option_count" -ne 4 ]]; then
  echo 'batch gate options must be supplied together' >&2
  exit 64
fi
if [[ "$batch_option_count" -eq 4 ]]; then
  [[ "$BATCH_LEASE" == /* && -f "$BATCH_LEASE" ]] || { echo 'invalid batch lease' >&2; exit 66; }
  [[ "$BATCH_GATE_TOOL" == /* && -f "$BATCH_GATE_TOOL" ]] || { echo 'invalid batch gate tool' >&2; exit 66; }
  [[ "$BATCH_ANCHOR_BINARY" == /* && -x "$BATCH_ANCHOR_BINARY" ]] || { echo 'invalid batch anchor binary' >&2; exit 66; }
  [[ "$BATCH_CONSUMER" == "P10" || "$BATCH_CONSUMER" == "P20" ]] || { echo 'invalid batch consumer' >&2; exit 64; }
fi
if [[ "$PERFORMANCE_ELIGIBLE" == "true" && "$ALLOW_MISSING_AUX" == "1" ]]; then
  echo 'formal mode forbids --allow-missing-aux-tools' >&2
  exit 64
fi

declare -a P31_ARGS=(
  "$P31"
  --run-dir "$RUN_DIR"
  --run-id "$RUN_ID"
  --task-id "$TASK_ID"
  --performance-eligible "$PERFORMANCE_ELIGIBLE"
  --repo-root "$REPO_ROOT"
  --device "$DEVICE"
  --data-mount "$DATA_MOUNT"
  --interval "$INTERVAL"
  --disk-interval "$DISK_INTERVAL"
  --min-samples "$MIN_SAMPLES"
)
for value in "${STORES[@]}"; do P31_ARGS+=(--store "$value"); done
for value in "${TEMPS[@]}"; do P31_ARGS+=(--temp "$value"); done
for value in "${CONTAINERS[@]}"; do P31_ARGS+=(--container "$value"); done
for value in "${EXTRA_PIDS[@]}"; do P31_ARGS+=(--extra-pid "$value"); done
for value in "${INPUTS[@]}"; do P31_ARGS+=(--input "$value"); done
if [[ "$batch_option_count" -eq 4 ]]; then
  P31_ARGS+=(
    --batch-lease "$BATCH_LEASE"
    --batch-gate-tool "$BATCH_GATE_TOOL"
    --batch-consumer "$BATCH_CONSUMER"
    --batch-anchor-binary "$BATCH_ANCHOR_BINARY"
  )
fi
P31_ARGS+=(
  --binary "$BINARY" --binary-sha256 "$BINARY_SHA256"
  --dataset "$DATASET" --dataset-sha256 "$DATASET_SHA256"
  --truth "$TRUTH" --truth-sha256 "$TRUTH_SHA256"
  --query-or-trace "$QUERY_OR_TRACE" --query-or-trace-sha256 "$QUERY_OR_TRACE_SHA256"
  --config "$CONFIG" --config-sha256 "$CONFIG_SHA256"
)
[[ "$DATASET_SHA256_MODE" != "verify" ]] && P31_ARGS+=(--dataset-sha256-mode "$DATASET_SHA256_MODE")
[[ "$ALLOW_MISSING_AUX" == "1" ]] && P31_ARGS+=(--allow-missing-aux-tools)
P31_ARGS+=(-- "${COMMAND[@]}")

exec "${P31_ARGS[@]}"
