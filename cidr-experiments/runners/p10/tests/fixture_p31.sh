#!/usr/bin/env bash
set -Eeuo pipefail

# Contract fixture only.  It executes the synthetic adapter and emits the
# minimum P31 success surface consumed by the orchestrator.  Formal mode is
# rejected and no resource measurement is performed.

args=("$@")
run_dir=""
performance_eligible=""
allow_missing_aux=0
declare -A seen=()
declare -a command=()
declare -a stores=()
declare -a containers=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir|--run-id|--task-id|--performance-eligible|--repo-root|--device|--data-mount|--interval|--disk-interval|--min-samples|--binary|--binary-sha256|--dataset|--dataset-sha256|--truth|--truth-sha256|--query-or-trace|--query-or-trace-sha256|--config|--config-sha256)
      [[ $# -ge 2 ]] || { echo "missing fixture value for $1" >&2; exit 64; }
      seen["$1"]="$2"
      [[ "$1" == "--run-dir" ]] && run_dir="$2"
      [[ "$1" == "--performance-eligible" ]] && performance_eligible="$2"
      shift 2
      ;;
    --store)
      stores+=("${2:?}")
      shift 2
      ;;
    --temp|--extra-pid)
      [[ $# -ge 2 ]] || { echo "missing fixture value for $1" >&2; exit 64; }
      shift 2
      ;;
    --container)
      containers+=("${2:?}")
      shift 2
      ;;
    --allow-missing-aux-tools)
      allow_missing_aux=1
      shift
      ;;
    --)
      shift
      command=("$@")
      break
      ;;
    *) echo "unexpected fixture P31 option: $1" >&2; exit 64 ;;
  esac
done

required=(--run-dir --run-id --task-id --performance-eligible --repo-root --device --data-mount --interval --disk-interval --min-samples --binary --binary-sha256 --dataset --dataset-sha256 --truth --truth-sha256 --query-or-trace --query-or-trace-sha256 --config --config-sha256)
for option in "${required[@]}"; do
  [[ -n "${seen[$option]:-}" ]] || { echo "fixture P31 missing $option" >&2; exit 64; }
done
[[ "$run_dir" == /* ]] || { echo 'fixture P31 run-dir must be absolute' >&2; exit 64; }
[[ "$performance_eligible" == "false" ]] || { echo 'fixture P31 rejects performance-eligible runs' >&2; exit 64; }
[[ "$allow_missing_aux" == "1" ]] || { echo 'fixture P31 requires --allow-missing-aux-tools' >&2; exit 64; }
[[ ${#stores[@]} -gt 0 ]] || { echo 'fixture P31 requires a store' >&2; exit 64; }
[[ ${#command[@]} -gt 0 ]] || { echo 'fixture P31 missing command' >&2; exit 64; }
if [[ -e "$run_dir" && -n "$(find "$run_dir" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "fixture P31 refusing non-empty run directory: $run_dir" >&2
  exit 73
fi
mkdir -p "$run_dir"
printf '%s\n' "${args[@]}" > "$run_dir/argv.txt"

set +e
"${command[@]}" > "$run_dir/command.stdout.log" 2> "$run_dir/command.stderr.log"
command_rc=$?
set -e
if [[ "$command_rc" -ne 0 ]]; then
  printf '{"state":"FAILED","command_exit_code":%d}\n' "$command_rc" > "$run_dir/FAILED"
  exit "$command_rc"
fi

python3 -B "$(dirname "$0")/fixture_p31_finalize.py" "$run_dir"
