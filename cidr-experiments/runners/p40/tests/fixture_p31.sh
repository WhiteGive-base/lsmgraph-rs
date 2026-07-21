#!/usr/bin/env bash
set -Eeuo pipefail

# Contract fixture for the real P31 run_with_resources.sh argv shape. It is not
# a resource collector and must never be used for a performance-eligible run.

args=("$@")
run_dir=""
performance_eligible=""
declare -a command=()
declare -A seen=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir|--run-id|--task-id|--performance-eligible|--repo-root|--device|--data-mount|--interval|--disk-interval|--min-samples|--store|--binary|--dataset|--query-or-trace|--config)
      [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 64; }
      seen["$1"]="$2"
      [[ "$1" == "--run-dir" ]] && run_dir="$2"
      [[ "$1" == "--performance-eligible" ]] && performance_eligible="$2"
      shift 2
      ;;
    --allow-missing-aux-tools)
      seen["$1"]="true"
      shift
      ;;
    --repeat|--arm|--raw-dir)
      echo "legacy P40 collector option is forbidden: $1" >&2
      exit 64
      ;;
    --)
      shift
      command=("$@")
      break
      ;;
    *)
      echo "unexpected fixture option: $1" >&2
      exit 64
      ;;
  esac
done

required=(--run-dir --run-id --task-id --performance-eligible --repo-root --device --data-mount --interval --disk-interval --min-samples --store --binary --dataset --query-or-trace --config)
for option in "${required[@]}"; do
  [[ -n "${seen[$option]:-}" ]] || { echo "missing required P31 option: $option" >&2; exit 64; }
done
[[ "$run_dir" == /* ]] || { echo 'fixture --run-dir must be absolute' >&2; exit 64; }
[[ "$performance_eligible" == "false" ]] || { echo 'fixture must be performance-ineligible' >&2; exit 64; }
[[ "${seen[--allow-missing-aux-tools]:-}" == "true" ]] \
  || { echo 'fixture expected --allow-missing-aux-tools' >&2; exit 64; }
[[ ${#command[@]} -gt 0 ]] || { echo 'missing command after --' >&2; exit 64; }

mkdir -p "$run_dir"
printf '%s\n' "${args[@]}" > "$run_dir/argv.txt"
"${command[@]}"
printf 'fixture-only\n' > "$run_dir/DONE"
