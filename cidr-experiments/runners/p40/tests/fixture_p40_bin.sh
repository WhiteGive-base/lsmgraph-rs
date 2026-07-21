#!/usr/bin/env bash
set -Eeuo pipefail

output=""
data_dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output) output="$2"; shift 2 ;;
    --data-dir) data_dir="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[[ -n "$output" ]] || { echo 'fake binary did not receive --output' >&2; exit 64; }
[[ -n "$data_dir" ]] || { echo 'fake binary did not receive --data-dir' >&2; exit 64; }
mkdir -p "$data_dir"
printf 'fixture-store\n' > "$data_dir/fixture.data"
sleep "${P40_FIXTURE_SLEEP_SECONDS:-0}"
printf '{"event":"done"}\n' > "$output"
