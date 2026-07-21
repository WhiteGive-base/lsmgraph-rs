#!/usr/bin/env bash
set -Eeuo pipefail

# P40 fixed-trace runner.  Fixture mode is correctness-only and never claims
# performance eligibility.  No existing store is removed or overwritten.

ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MODE="${MODE:-fixture}"
RUN_ID="${RUN_ID:-P40-FIXED-TRACE-${MODE}-$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$ROOT" rev-parse --short=12 HEAD)}"
LOG_ROOT="${LOG_ROOT:-$ROOT/cidr-experiments/runs/P40-FIXED-TRACE/raw/$RUN_ID}"
STORE_PARENT="${STORE_PARENT:-$ROOT/store/p40-fixed-trace/$RUN_ID}"
CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$ROOT/target}"
BIN="${BIN:-$CARGO_TARGET_DIR/release/p40_fixed_trace}"

REPEATS="${REPEATS:-3}"
ARMS=(none capacity-naive semantic-static semantic-feedback)
SOURCE_CSV="${SOURCE_CSV:-/data/WorkSpace/ldbc-sf1/social_network/dynamic/person_knows_person_0_0.csv}"
TRACE_IN="${TRACE_IN:-}"
TRACE_SEED="${TRACE_SEED:-20260722}"
SOURCE_EDGE_LIMIT="${SOURCE_EDGE_LIMIT:-48}"
FLUSH_EVERY="${FLUSH_EVERY:-8}"
HOT_QUERY_REPEATS="${HOT_QUERY_REPEATS:-2}"
DELETE_COUNT="${DELETE_COUNT:-6}"
CAPACITY_L0_FILES="${CAPACITY_L0_FILES:-4}"
STATIC_RANGE_BUCKET_SIZE="${STATIC_RANGE_BUCKET_SIZE:-1048576}"
ARM_TIMEOUT="${ARM_TIMEOUT:-10m}"
NICE_LEVEL="${NICE_LEVEL:-15}"
BUILD="${BUILD:-1}"
CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-4}"

# Optional P31 wrapper. This must be the actual P31 run_with_resources.sh;
# every arm receives an independent P31 run directory and store root.
P31_COLLECTOR="${P31_COLLECTOR:-}"
P31_DEVICE="${P31_DEVICE:-nvme1n1}"
P31_DATA_MOUNT="${P31_DATA_MOUNT:-/data}"
P31_INTERVAL="${P31_INTERVAL:-1}"
P31_DISK_INTERVAL="${P31_DISK_INTERVAL:-15}"
P31_MIN_SAMPLES="${P31_MIN_SAMPLES:-2}"
P31_ALLOW_MISSING_AUX_TOOLS="${P31_ALLOW_MISSING_AUX_TOOLS:-0}"
CLEAN_READY_FILE="${CLEAN_READY_FILE:-}"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

[[ "$REPEATS" =~ ^[1-9][0-9]*$ ]] || die 'REPEATS must be a positive integer'
[[ "$LOG_ROOT" == /* && "$STORE_PARENT" == /* ]] || die 'LOG_ROOT and STORE_PARENT must be absolute'
[[ -f "$SOURCE_CSV" || -n "$TRACE_IN" ]] || die "missing source CSV: $SOURCE_CSV"
[[ -z "$TRACE_IN" || -f "$TRACE_IN" ]] || die "TRACE_IN does not exist: $TRACE_IN"
[[ -z "$P31_COLLECTOR" || -x "$P31_COLLECTOR" ]] || die "P31_COLLECTOR is not executable: $P31_COLLECTOR"
[[ "$P31_ALLOW_MISSING_AUX_TOOLS" == "0" || "$P31_ALLOW_MISSING_AUX_TOOLS" == "1" ]] \
  || die 'P31_ALLOW_MISSING_AUX_TOOLS must be 0 or 1'

case "$MODE" in
  fixture)
    PERFORMANCE_ELIGIBLE=false
    ;;
  formal)
    [[ "${RUN_FORMAL:-0}" == "1" ]] || die 'formal mode requires RUN_FORMAL=1'
    [[ -n "$P31_COLLECTOR" ]] || die 'formal mode requires P31_COLLECTOR'
    [[ -f "$CLEAN_READY_FILE" ]] || die 'formal mode requires CLEAN_READY_FILE'
    grep -q '^readiness_gate=PASS$' "$CLEAN_READY_FILE" || die 'clean-window readiness gate is not PASS'
    [[ "${PERFORMANCE_ELIGIBLE:-0}" == "1" ]] || die 'formal mode requires PERFORMANCE_ELIGIBLE=1'
    [[ "$P31_ALLOW_MISSING_AUX_TOOLS" == "0" ]] \
      || die 'formal mode forbids P31_ALLOW_MISSING_AUX_TOOLS=1'
    PERFORMANCE_ELIGIBLE=true
    ;;
  *) die "unknown MODE=$MODE; use fixture or formal" ;;
esac

[[ ! -e "$LOG_ROOT" ]] || die "refusing existing LOG_ROOT: $LOG_ROOT"
[[ ! -e "$STORE_PARENT" ]] || die "refusing existing STORE_PARENT: $STORE_PARENT"
mkdir -p "$LOG_ROOT" "$STORE_PARENT"

printf 'performance_eligible=%s\nmode=%s\nrun_id=%s\n' \
  "$PERFORMANCE_ELIGIBLE" "$MODE" "$RUN_ID" > "$LOG_ROOT/classification.env"
git -C "$ROOT" rev-parse HEAD > "$LOG_ROOT/git-head.txt"
git -C "$ROOT" status --porcelain > "$LOG_ROOT/git-status.txt"
uname -a > "$LOG_ROOT/uname.txt"

if [[ "$BUILD" == "1" ]]; then
  (
    cd "$ROOT"
    CARGO_TARGET_DIR="$CARGO_TARGET_DIR" CARGO_BUILD_JOBS="$CARGO_BUILD_JOBS" \
      nice -n "$NICE_LEVEL" cargo build --release --bin p40_fixed_trace
  ) > "$LOG_ROOT/build.stdout.log" 2> "$LOG_ROOT/build.stderr.log"
fi
[[ -x "$BIN" ]] || die "missing binary: $BIN"

TRACE_FILE="$LOG_ROOT/trace.jsonl"
TRACE_META="$LOG_ROOT/trace-meta.json"
if [[ -n "$TRACE_IN" ]]; then
  cp -- "$TRACE_IN" "$TRACE_FILE"
  printf '{"trace_in":%s}\n' "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$TRACE_IN")" > "$TRACE_META"
else
  python3 "$ROOT/baseline/make_p40_fixed_trace.py" \
    --source-csv "$SOURCE_CSV" \
    --trace-out "$TRACE_FILE" \
    --meta-out "$TRACE_META" \
    --seed "$TRACE_SEED" \
    --edge-limit "$SOURCE_EDGE_LIMIT" \
    --flush-every "$FLUSH_EVERY" \
    --hot-query-repeats "$HOT_QUERY_REPEATS" \
    --delete-count "$DELETE_COUNT" \
    > "$LOG_ROOT/trace-generation.json"
fi
TRACE_SHA256="$(sha256sum "$TRACE_FILE" | awk '{print $1}')"
BINARY_SHA256="$(sha256sum "$BIN" | awk '{print $1}')"
GIT_HEAD="$(git -C "$ROOT" rev-parse HEAD)"

{
  printf 'trace_sha256=%s\n' "$TRACE_SHA256"
  printf 'binary_sha256=%s\n' "$BINARY_SHA256"
  printf 'trace_seed=%s\n' "$TRACE_SEED"
  printf 'repeats=%s\n' "$REPEATS"
  printf 'arms=%s\n' "${ARMS[*]}"
  printf 'p31_collector=%s\n' "${P31_COLLECTOR:-none}"
  printf 'p31_device=%s\n' "$P31_DEVICE"
  printf 'p31_data_mount=%s\n' "$P31_DATA_MOUNT"
  printf 'p31_interval=%s\n' "$P31_INTERVAL"
  printf 'p31_disk_interval=%s\n' "$P31_DISK_INTERVAL"
  printf 'p31_min_samples=%s\n' "$P31_MIN_SAMPLES"
} >> "$LOG_ROOT/classification.env"

for repeat in $(seq 1 "$REPEATS"); do
  repeat_dir="$LOG_ROOT/repeat-$(printf '%02d' "$repeat")"
  mkdir -p "$repeat_dir"
  for arm in "${ARMS[@]}"; do
    store_dir="$STORE_PARENT/repeat-$(printf '%02d' "$repeat")-$arm"
    output="$repeat_dir/$arm.jsonl"
    [[ ! -e "$store_dir" && ! -e "$output" ]] || die "refusing existing arm artifact: $store_dir or $output"
    command=(
      "$BIN"
      --data-dir "$store_dir"
      --trace-in "$TRACE_FILE"
      --output "$output"
      --policy "$arm"
      --store-mode create
      --capacity-l0-files "$CAPACITY_L0_FILES"
      --static-range-bucket-size "$STATIC_RANGE_BUCKET_SIZE"
    )
    printf '%q ' "${command[@]}" > "$repeat_dir/$arm.command.txt"
    printf '\n' >> "$repeat_dir/$arm.command.txt"

    if [[ -n "$P31_COLLECTOR" ]]; then
      repeat_label="$(printf '%02d' "$repeat")"
      p31_run_dir="$repeat_dir/$arm.p31"
      arm_config="$repeat_dir/$arm.config.json"
      printf '{"schema_version":"p40-arm-config-v1","policy":"%s","capacity_l0_files":%s,"static_range_bucket_size":%s,"store_mode":"create","trace_sha256":"%s"}\n' \
        "$arm" "$CAPACITY_L0_FILES" "$STATIC_RANGE_BUCKET_SIZE" "$TRACE_SHA256" \
        > "$arm_config"
      p31_command=(
        "$P31_COLLECTOR"
        --run-dir "$p31_run_dir"
        --run-id "${RUN_ID}-r${repeat_label}-${arm}"
        --task-id "P40-${RUN_ID}-r${repeat_label}-${arm}"
        --performance-eligible "$PERFORMANCE_ELIGIBLE"
        --repo-root "$ROOT"
        --device "$P31_DEVICE"
        --data-mount "$P31_DATA_MOUNT"
        --interval "$P31_INTERVAL"
        --disk-interval "$P31_DISK_INTERVAL"
        --min-samples "$P31_MIN_SAMPLES"
        --store "lsm=$store_dir"
        --binary "$BIN"
        --dataset "$TRACE_FILE"
        --query-or-trace "$TRACE_FILE"
        --config "$arm_config"
      )
      if [[ "$P31_ALLOW_MISSING_AUX_TOOLS" == "1" ]]; then
        p31_command+=(--allow-missing-aux-tools)
      fi
      p31_command+=(-- "${command[@]}")
      timeout "$ARM_TIMEOUT" nice -n "$NICE_LEVEL" "${p31_command[@]}" \
        > "$repeat_dir/$arm.stdout.log" 2> "$repeat_dir/$arm.stderr.log"
    else
      timeout "$ARM_TIMEOUT" nice -n "$NICE_LEVEL" "${command[@]}" \
        > "$repeat_dir/$arm.stdout.log" 2> "$repeat_dir/$arm.stderr.log"
    fi
    grep -q '"event":"done"' "$output" || die "arm has no done event: repeat=$repeat arm=$arm"
  done
done

python3 "$ROOT/baseline/validate_p40_fixed_trace.py" \
  --run-root "$LOG_ROOT" \
  --trace "$TRACE_FILE" \
  --trace-sha256 "$TRACE_SHA256" \
  --repeats "$REPEATS" \
  --arms "${ARMS[@]}" \
  --output "$LOG_ROOT/correctness.json" \
  --manifest-output "$LOG_ROOT/run-manifest.json" \
  --run-id "$RUN_ID" \
  --git-head "$GIT_HEAD" \
  --binary-sha256 "$BINARY_SHA256" \
  --performance-eligible "$PERFORMANCE_ELIGIBLE" \
  --p31-collector "$P31_COLLECTOR" \
  > "$LOG_ROOT/validation.stdout.log" 2> "$LOG_ROOT/validation.stderr.log"

printf 'gate=PASS\ntrace_sha256=%s\nrepeats=%s\narms=%s\nperformance_eligible=%s\n' \
  "$TRACE_SHA256" "$REPEATS" "${#ARMS[@]}" "$PERFORMANCE_ELIGIBLE" > "$LOG_ROOT/.DONE.tmp"
(
  cd "$LOG_ROOT"
  find . -type f ! -name 'files.sha256*' ! -name '.DONE.tmp' -print0 \
    | sort -z | xargs -0 sha256sum > files.sha256.tmp
  done_sha256="$(sha256sum .DONE.tmp | awk '{print $1}')"
  printf '%s  ./DONE\n' "$done_sha256" >> files.sha256.tmp
  mv .DONE.tmp DONE
  mv files.sha256.tmp files.sha256
)
printf 'P40 PASS run_id=%s log_root=%s trace_sha256=%s\n' "$RUN_ID" "$LOG_ROOT" "$TRACE_SHA256"
