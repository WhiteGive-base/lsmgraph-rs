#!/usr/bin/env bash
set -Eeuo pipefail

# Read-only readiness monitor for formal CIDR measurements.  This script never
# stops a process, drops caches, changes CPU policy, or starts a benchmark.

umask 027

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
RUN_ID="${RUN_ID:-P03-CLEAN-WINDOW-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_DIR="${OUT_DIR:-$SCRIPT_DIR/raw/$RUN_ID}"

GATE_MODE="${GATE_MODE:-seml0}"
SAMPLE_INTERVAL_SECONDS="${SAMPLE_INTERVAL_SECONDS:-60}"
MEASURE_SECONDS="${MEASURE_SECONDS:-1}"
READY_SAMPLES="${READY_SAMPLES:-15}"
DEVICE="${DEVICE:-nvme1n1}"
ZCL_USER="${ZCL_USER:-zcl}"
ZCL_BIG_RSS_KIB="${ZCL_BIG_RSS_KIB:-1048576}"

LOAD_MAX="${LOAD_MAX:-5}"
CPU_IDLE_MIN_PCT="${CPU_IDLE_MIN_PCT:-95}"
MEM_AVAILABLE_MIN_KIB="${MEM_AVAILABLE_MIN_KIB:-419430400}" # 400 GiB
DATA_FREE_MIN_KIB="${DATA_FREE_MIN_KIB:-891289600}"         # 850 GiB
DISK_UTIL_MAX_PCT="${DISK_UTIL_MAX_PCT:-5}"
DISK_AWAIT_MAX_MS="${DISK_AWAIT_MAX_MS:-5}"

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 2
}

is_positive_integer() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

float_lt() {
    awk -v lhs="$1" -v rhs="$2" 'BEGIN { exit !(lhs < rhs) }'
}

float_gt() {
    awk -v lhs="$1" -v rhs="$2" 'BEGIN { exit !(lhs > rhs) }'
}

read_cpu_counters() {
    local label user nice system idle iowait irq softirq steal guest guest_nice
    read -r label user nice system idle iowait irq softirq steal guest guest_nice < /proc/stat
    [[ "$label" == "cpu" ]] || die 'cannot read aggregate CPU counters'
    CPU_TOTAL=$((user + nice + system + idle + iowait + irq + softirq + steal))
    CPU_IDLE="$idle"
}

read_disk_counters() {
    local major minor name reads_completed reads_merged sectors_read read_ms
    local writes_completed writes_merged sectors_written write_ms in_flight io_ms
    local weighted_io_ms remainder

    while read -r major minor name reads_completed reads_merged sectors_read read_ms \
        writes_completed writes_merged sectors_written write_ms in_flight io_ms \
        weighted_io_ms remainder; do
        if [[ "$name" == "$DEVICE" ]]; then
            DISK_READS="$reads_completed"
            DISK_READ_MS="$read_ms"
            DISK_WRITES="$writes_completed"
            DISK_WRITE_MS="$write_ms"
            DISK_IO_MS="$io_ms"
            return 0
        fi
    done < /proc/diskstats

    die "device $DEVICE is absent from /proc/diskstats"
}

numeric_metrics() {
    local start_ns end_ns elapsed_ms
    local cpu_total_before cpu_idle_before disk_reads_before disk_read_ms_before
    local disk_writes_before disk_write_ms_before disk_io_ms_before
    local delta_total delta_idle delta_reads delta_read_ms delta_writes
    local delta_write_ms delta_io_ms delta_ops

    read_cpu_counters
    cpu_total_before="$CPU_TOTAL"
    cpu_idle_before="$CPU_IDLE"
    read_disk_counters
    disk_reads_before="$DISK_READS"
    disk_read_ms_before="$DISK_READ_MS"
    disk_writes_before="$DISK_WRITES"
    disk_write_ms_before="$DISK_WRITE_MS"
    disk_io_ms_before="$DISK_IO_MS"
    start_ns="$(date +%s%N)"

    sleep "$MEASURE_SECONDS"

    end_ns="$(date +%s%N)"
    read_cpu_counters
    read_disk_counters

    elapsed_ms=$(((end_ns - start_ns) / 1000000))
    ((elapsed_ms > 0)) || elapsed_ms=1
    delta_total=$((CPU_TOTAL - cpu_total_before))
    delta_idle=$((CPU_IDLE - cpu_idle_before))
    delta_reads=$((DISK_READS - disk_reads_before))
    delta_read_ms=$((DISK_READ_MS - disk_read_ms_before))
    delta_writes=$((DISK_WRITES - disk_writes_before))
    delta_write_ms=$((DISK_WRITE_MS - disk_write_ms_before))
    delta_io_ms=$((DISK_IO_MS - disk_io_ms_before))
    delta_ops=$((delta_reads + delta_writes))

    CPU_IDLE_PCT="$(awk -v idle="$delta_idle" -v total="$delta_total" \
        'BEGIN { if (total <= 0) print "0.000000"; else printf "%.6f", 100 * idle / total }')"
    DISK_UTIL_PCT="$(awk -v busy="$delta_io_ms" -v elapsed="$elapsed_ms" \
        'BEGIN { printf "%.6f", 100 * busy / elapsed }')"
    DISK_READ_AWAIT_MS="$(awk -v ticks="$delta_read_ms" -v ops="$delta_reads" \
        'BEGIN { if (ops <= 0) print "0.000000"; else printf "%.6f", ticks / ops }')"
    DISK_WRITE_AWAIT_MS="$(awk -v ticks="$delta_write_ms" -v ops="$delta_writes" \
        'BEGIN { if (ops <= 0) print "0.000000"; else printf "%.6f", ticks / ops }')"
    DISK_AWAIT_MS="$(awk -v rt="$delta_read_ms" -v wt="$delta_write_ms" -v ops="$delta_ops" \
        'BEGIN { if (ops <= 0) print "0.000000"; else printf "%.6f", (rt + wt) / ops }')"
}

process_metrics() {
    local zcl_snapshot service_snapshot

    zcl_snapshot="$(ps -u "$ZCL_USER" -o pid=,rss=,comm=,args= 2>/dev/null || true)"
    ZCL_BIG_COUNT="$(printf '%s\n' "$zcl_snapshot" | awk -v minimum="$ZCL_BIG_RSS_KIB" \
        '$2 + 0 >= minimum { count++ } END { print count + 0 }')"
    ZCL_RSYNC_COUNT="$(printf '%s\n' "$zcl_snapshot" | awk \
        '$3 == "rsync" || ($3 == "ssh" && index($0, "rsync --server")) { count++ } END { print count + 0 }')"

    if ((ZCL_BIG_COUNT > 0 || ZCL_RSYNC_COUNT > 0)); then
        printf '%s\n' "$zcl_snapshot" | awk -v timestamp="$SAMPLE_TS" -v minimum="$ZCL_BIG_RSS_KIB" \
            '$2 + 0 >= minimum || $3 == "rsync" || ($3 == "ssh" && index($0, "rsync --server")) {
                printf "%s\t%s\n", timestamp, $0
            }' >> "$OUT_DIR/process-watch.tsv"
    fi

    service_snapshot="$(ps -eo comm=,args= 2>/dev/null || true)"
    GPSTORE_COUNT="$(printf '%s\n' "$service_snapshot" | awk \
        '$1 == "grpc" && index($0, "/gpstore/") { count++ } END { print count + 0 }')"
    TUGRAPH_COUNT="$(printf '%s\n' "$service_snapshot" | awk \
        '$1 == "lgraph" || index($0, "lgraph_server") { count++ } END { print count + 0 }')"
}

write_atomic() {
    local destination="$1"
    local temporary="${destination}.tmp.$$"
    shift
    printf '%s\n' "$@" > "$temporary"
    mv -f "$temporary" "$destination"
}

case "$GATE_MODE" in
    observe|seml0) ;;
    *) die "GATE_MODE must be observe or seml0, got: $GATE_MODE" ;;
esac

is_positive_integer "$SAMPLE_INTERVAL_SECONDS" || die 'SAMPLE_INTERVAL_SECONDS must be a positive integer'
is_positive_integer "$MEASURE_SECONDS" || die 'MEASURE_SECONDS must be a positive integer'
is_positive_integer "$READY_SAMPLES" || die 'READY_SAMPLES must be a positive integer'
((MEASURE_SECONDS < SAMPLE_INTERVAL_SECONDS)) || die 'MEASURE_SECONDS must be smaller than SAMPLE_INTERVAL_SECONDS'
[[ "$OUT_DIR" == /* ]] || die 'OUT_DIR must be an absolute path'
[[ -r /proc/stat && -r /proc/diskstats && -r /proc/meminfo ]] || die 'required /proc counters are unavailable'
[[ -d /data ]] || die '/data is unavailable'
command -v awk >/dev/null || die 'awk is required'
command -v ps >/dev/null || die 'ps is required'
command -v sha256sum >/dev/null || die 'sha256sum is required'

mkdir -p "$OUT_DIR"
if [[ -e "$OUT_DIR/samples.tsv" || -e "$OUT_DIR/READY" || -e "$OUT_DIR/RUNNING" ]]; then
    die "refusing to reuse non-empty monitor run directory: $OUT_DIR"
fi

printf 'timestamp\tpid\trss_kib\tcomm_and_args\n' > "$OUT_DIR/process-watch.tsv"
printf 'timestamp\tsample\tload1\tcpu_idle_pct\tmem_available_kib\tmem_available_gib\tdata_free_kib\tdata_free_gib\tdevice\tdisk_util_pct\tdisk_read_await_ms\tdisk_write_await_ms\tdisk_await_ms\tzcl_big_count\tzcl_rsync_count\tgpstore_count\ttugraph_count\tgate_mode\tmetric_pass\tservice_pass\tsample_pass\tstreak\treasons\n' \
    > "$OUT_DIR/samples.tsv"

SCRIPT_SHA256="$(sha256sum "$0" | awk '{print $1}')"
GIT_HEAD="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || printf 'UNKNOWN')"
HOSTNAME_VALUE="$(hostname)"
START_TS="$(date -Is)"

write_atomic "$OUT_DIR/classification.env" \
    'performance_eligible=false' \
    'purpose=clean_window_readiness_only' \
    "gate_mode=$GATE_MODE" \
    "run_id=$RUN_ID" \
    "script_sha256=$SCRIPT_SHA256" \
    "git_head=$GIT_HEAD" \
    "host=$HOSTNAME_VALUE" \
    "start_time=$START_TS" \
    "sample_interval_seconds=$SAMPLE_INTERVAL_SECONDS" \
    "measure_seconds=$MEASURE_SECONDS" \
    "ready_samples=$READY_SAMPLES" \
    "load_max_exclusive=$LOAD_MAX" \
    "cpu_idle_min_exclusive_pct=$CPU_IDLE_MIN_PCT" \
    "mem_available_min_kib=$MEM_AVAILABLE_MIN_KIB" \
    "data_free_min_kib=$DATA_FREE_MIN_KIB" \
    "disk_util_max_exclusive_pct=$DISK_UTIL_MAX_PCT" \
    "disk_await_max_exclusive_ms=$DISK_AWAIT_MAX_MS" \
    "zcl_big_rss_kib=$ZCL_BIG_RSS_KIB" \
    'disk_counter_source=/proc/diskstats'

write_atomic "$OUT_DIR/RUNNING" \
    "run_id=$RUN_ID" \
    "pid=$$" \
    "start_time=$START_TS" \
    "out_dir=$OUT_DIR"

streak=0
sample=0
finished=0

on_signal() {
    local signal="$1"
    write_atomic "$OUT_DIR/STOPPED" \
        "run_id=$RUN_ID" \
        "pid=$$" \
        "signal=$signal" \
        "stop_time=$(date -Is)" \
        "samples=$sample" \
        "streak=$streak"
    exit 128
}

trap 'on_signal TERM' TERM
trap 'on_signal INT' INT

while :; do
    cycle_start_epoch="$(date +%s)"
    sample=$((sample + 1))
    SAMPLE_TS="$(date -Is)"
    LOAD1="$(awk '{print $1}' /proc/loadavg)"
    MEM_AVAILABLE_KIB="$(awk '$1 == "MemAvailable:" {print $2}' /proc/meminfo)"
    DATA_FREE_KIB="$(df -Pk /data | awk 'NR == 2 {print $4}')"

    numeric_metrics
    process_metrics

    MEM_AVAILABLE_GIB="$(awk -v kib="$MEM_AVAILABLE_KIB" 'BEGIN {printf "%.3f", kib / 1048576}')"
    DATA_FREE_GIB="$(awk -v kib="$DATA_FREE_KIB" 'BEGIN {printf "%.3f", kib / 1048576}')"

    reasons=()
    metric_pass=1
    service_pass=1

    if ! float_lt "$LOAD1" "$LOAD_MAX"; then
        metric_pass=0
        reasons+=(load)
    fi
    if ! float_gt "$CPU_IDLE_PCT" "$CPU_IDLE_MIN_PCT"; then
        metric_pass=0
        reasons+=(cpu_idle)
    fi
    if ((MEM_AVAILABLE_KIB < MEM_AVAILABLE_MIN_KIB)); then
        metric_pass=0
        reasons+=(mem_available)
    fi
    if ((DATA_FREE_KIB < DATA_FREE_MIN_KIB)); then
        metric_pass=0
        reasons+=(data_free)
    fi
    if ! float_lt "$DISK_UTIL_PCT" "$DISK_UTIL_MAX_PCT"; then
        metric_pass=0
        reasons+=(disk_util)
    fi
    if ! float_lt "$DISK_AWAIT_MS" "$DISK_AWAIT_MAX_MS"; then
        metric_pass=0
        reasons+=(disk_await)
    fi
    if ((ZCL_BIG_COUNT != 0)); then
        metric_pass=0
        reasons+=(zcl_big)
    fi
    if ((ZCL_RSYNC_COUNT != 0)); then
        metric_pass=0
        reasons+=(zcl_rsync)
    fi

    if [[ "$GATE_MODE" == "seml0" ]] && ((GPSTORE_COUNT != 0 || TUGRAPH_COUNT != 0)); then
        service_pass=0
        reasons+=(external_services)
    fi

    sample_pass=0
    if ((metric_pass == 1 && service_pass == 1)); then
        sample_pass=1
        streak=$((streak + 1))
    else
        streak=0
    fi

    if ((${#reasons[@]} == 0)); then
        reason_text=none
    else
        reason_text="$(IFS=,; printf '%s' "${reasons[*]}")"
    fi

    sample_line="${SAMPLE_TS}\t${sample}\t${LOAD1}\t${CPU_IDLE_PCT}\t${MEM_AVAILABLE_KIB}\t${MEM_AVAILABLE_GIB}\t${DATA_FREE_KIB}\t${DATA_FREE_GIB}\t${DEVICE}\t${DISK_UTIL_PCT}\t${DISK_READ_AWAIT_MS}\t${DISK_WRITE_AWAIT_MS}\t${DISK_AWAIT_MS}\t${ZCL_BIG_COUNT}\t${ZCL_RSYNC_COUNT}\t${GPSTORE_COUNT}\t${TUGRAPH_COUNT}\t${GATE_MODE}\t${metric_pass}\t${service_pass}\t${sample_pass}\t${streak}\t${reason_text}"
    printf '%b\n' "$sample_line" >> "$OUT_DIR/samples.tsv"
    write_atomic "$OUT_DIR/latest.tsv" \
        "$(printf '%b' 'timestamp\tsample\tload1\tcpu_idle_pct\tmem_available_kib\tmem_available_gib\tdata_free_kib\tdata_free_gib\tdevice\tdisk_util_pct\tdisk_read_await_ms\tdisk_write_await_ms\tdisk_await_ms\tzcl_big_count\tzcl_rsync_count\tgpstore_count\ttugraph_count\tgate_mode\tmetric_pass\tservice_pass\tsample_pass\tstreak\treasons')" \
        "$(printf '%b' "$sample_line")"
    write_atomic "$OUT_DIR/STATE" \
        'performance_eligible=false' \
        "timestamp=$SAMPLE_TS" \
        "sample=$sample" \
        "sample_pass=$sample_pass" \
        "streak=$streak" \
        "required_streak=$READY_SAMPLES" \
        "reasons=$reason_text"

    printf '[%s] sample=%d pass=%d streak=%d/%d load=%s idle=%s memGiB=%s dataGiB=%s util=%s await=%s zcl_big=%s zcl_rsync=%s gpstore=%s tugraph=%s reasons=%s\n' \
        "$SAMPLE_TS" "$sample" "$sample_pass" "$streak" "$READY_SAMPLES" \
        "$LOAD1" "$CPU_IDLE_PCT" "$MEM_AVAILABLE_GIB" "$DATA_FREE_GIB" \
        "$DISK_UTIL_PCT" "$DISK_AWAIT_MS" "$ZCL_BIG_COUNT" "$ZCL_RSYNC_COUNT" \
        "$GPSTORE_COUNT" "$TUGRAPH_COUNT" "$reason_text"

    if ((streak >= READY_SAMPLES)); then
        READY_TS="$(date -Is)"
        write_atomic "$OUT_DIR/READY" \
            'performance_eligible=false' \
            'readiness_gate=PASS' \
            "gate_mode=$GATE_MODE" \
            "run_id=$RUN_ID" \
            "ready_time=$READY_TS" \
            "samples=$sample" \
            "consecutive_passes=$streak" \
            "latest_sample=$OUT_DIR/latest.tsv"
        write_atomic "$OUT_DIR/COMPLETE" \
            "run_id=$RUN_ID" \
            "pid=$$" \
            "start_time=$START_TS" \
            "complete_time=$READY_TS"
        finished=1
        printf '[%s] READY after %d consecutive passing samples\n' "$READY_TS" "$streak"
        exit 0
    fi

    cycle_elapsed=$(( $(date +%s) - cycle_start_epoch ))
    sleep_for=$((SAMPLE_INTERVAL_SECONDS - cycle_elapsed))
    if ((sleep_for > 0)); then
        sleep "$sleep_for"
    fi
done
