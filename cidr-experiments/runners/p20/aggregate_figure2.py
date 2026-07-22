#!/usr/bin/env python3
"""Join paired P20 modes and aggregate three independent Figure 2 runs.

Input ``summary.tsv`` files are entry-level.  This tool first merges their
latency histograms and additive counters into one run-stage observation, then
joins the uninstrumented ``latency`` run to the matching ``cpu-phase`` run by
scale/stage/workload/property/repeat.  Benchmark entries are never treated as
independent repetitions and per-entry P99 values are never averaged.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import sys
from pathlib import Path


CANONICAL_STAGES = {
    "sf10": ["A0", "A1", "A2", "A3", "A4", "A5", "A6"],
    "sf30": ["A0", "A2", "A4", "A6"],
}
PAIR_KEY = (
    "scale",
    "stage",
    "workload",
    "property_predicate_mode",
    "property_id",
    "repeat_index",
)
RUN_CONSTANTS = (
    "experiment_id",
    "task_id",
    "run_id",
    "repeat_index",
    "scale",
    "stage",
    "mode",
    "workload",
    "property_predicate_mode",
    "property_id",
    "performance_eligible",
    "concurrency",
    "worker_threads",
    "cpuset",
    "host_fingerprint_sha256",
    "git_sha",
    "feature_switches_json",
    "pre_measurement_json",
    "warmup_runs",
    "training_runs",
    "training_feedback_compactions",
    "cache_state_before_json",
    "cache_state_after_json",
    "binary_sha256",
    "dataset_sha256",
    "sample_plan_sha256",
    "truth_sha256",
    "correctness_pass_sha256",
    "profiles_sha256",
    "p20_runner_sha256",
    "p20_summarizer_sha256",
    "p31_wrapper_sha256",
    "pristine_store_sha256",
    "pristine_store_manifest_sha256",
    "admission_protocol",
    "p02b_sentinel_result_sha256",
    "p02b_pass_marker_sha256",
    "p02b_provenance_sha256",
    "p02b_validator_sha256",
    "p02b_admission_sha256",
    "batch_lease_sha256",
    "batch_gate_tool_sha256",
    "batch_lease_admission_sha256",
    "batch_lease_pre_p31_sha256",
    "p31_integrity_guard_status_sha256",
    "p31_integrity_guard_samples_sha256",
    "p31_integrity_guard_ready_sha256",
    "p31_command_release_sha256",
    "current_digest_pass",
    "current_digest_mismatches",
)
ADDITIVE = (
    "measured_operations",
    "neighbor_edges",
    "candidate_segments_total",
    "routed_segments_total",
    "body_read_segments_total",
    "logical_read_bytes_total",
    "logical_body_bytes_total",
    "query_cpu_query_setup_ns",
    "query_cpu_metadata_admission_ns",
    "query_cpu_routing_index_ns",
    "query_cpu_body_decode_filter_ns",
    "query_cpu_mvcc_result_ns",
    "query_cpu_phase_sum_ns",
    "query_cpu_clock_failures",
)
PAIR_SHARED = (
    "scale",
    "stage",
    "workload",
    "property_predicate_mode",
    "property_id",
    "repeat_index",
    "concurrency",
    "worker_threads",
    "cpuset",
    "host_fingerprint_sha256",
    "git_sha",
    "feature_switches_json",
    "pre_measurement_json",
    "warmup_runs",
    "training_runs",
    "training_feedback_compactions",
    "binary_sha256",
    "dataset_sha256",
    "sample_plan_sha256",
    "truth_sha256",
    "correctness_pass_sha256",
    "profiles_sha256",
    "p20_runner_sha256",
    "p20_summarizer_sha256",
    "p31_wrapper_sha256",
    "pristine_store_sha256",
    "pristine_store_manifest_sha256",
    "admission_protocol",
    "p02b_sentinel_result_sha256",
    "p02b_pass_marker_sha256",
    "p02b_provenance_sha256",
    "p02b_validator_sha256",
    "p02b_admission_sha256",
    "batch_lease_sha256",
    "batch_gate_tool_sha256",
    "batch_lease_admission_sha256",
    "batch_lease_pre_p31_sha256",
    "p31_integrity_guard_status_sha256",
    "p31_integrity_guard_samples_sha256",
    "p31_integrity_guard_ready_sha256",
    "p31_command_release_sha256",
)
RUN_COLUMNS = [
    "figure2_schema_version",
    "scale",
    "ablation_stage",
    "workload",
    "property_predicate_mode",
    "property_id",
    "repeat_index",
    "latency_run_id",
    "cpu_phase_run_id",
    "feature_switches",
    "pre_measurement_json",
    "latency_p99_us",
    "candidate_segments_total",
    "body_read_segments_total",
    "measured_operations",
    "body_read_bytes_total",
    "device_read_bytes_total",
    "cpu_query_setup_ns",
    "cpu_admission_ns",
    "cpu_routing_ns",
    "cpu_body_decode_filter_ns",
    "cpu_mvcc_result_ns",
    "cpu_total_ns",
    "latency_histogram_json",
    "binary_sha256",
    "dataset_sha256",
    "sample_plan_sha256",
    "truth_sha256",
    "correctness_pass_sha256",
    "profiles_sha256",
    "pristine_store_sha256",
    "pristine_store_manifest_sha256",
    "admission_protocol",
    "p02b_sentinel_result_sha256",
    "p02b_pass_marker_sha256",
    "p02b_provenance_sha256",
    "p02b_validator_sha256",
    "p02b_admission_sha256",
    "batch_lease_sha256",
    "batch_gate_tool_sha256",
    "batch_lease_admission_sha256",
    "batch_lease_pre_p31_sha256",
    "p31_integrity_guard_status_sha256",
    "p31_integrity_guard_samples_sha256",
    "p31_integrity_guard_ready_sha256",
    "p31_command_release_sha256",
    "latency_summary_sha256",
    "cpu_phase_summary_sha256",
]
AGGREGATE_METRICS = [
    "latency_p99_us",
    "candidate_segments_per_op",
    "body_read_segments_per_op",
    "body_read_bytes_per_op",
    "cpu_query_setup_ns_per_op",
    "cpu_admission_ns_per_op",
    "cpu_routing_ns_per_op",
    "cpu_body_decode_filter_ns_per_op",
    "cpu_mvcc_result_ns_per_op",
    "cpu_total_ns_per_op",
]
AGGREGATE_COLUMNS = [
    "figure2_aggregate_schema_version",
    "scale",
    "ablation_stage",
    "workload",
    "property_predicate_mode",
    "property_id",
    "independent_runs",
] + [suffix for metric in AGGREGATE_METRICS for suffix in (metric + "_mean", metric + "_ci95")]


class AggregateError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise AggregateError(message)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def integer(value, label):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise AggregateError("{} must be an integer".format(label))
    require(str(parsed) == str(value), "{} must be a canonical integer".format(label))
    require(parsed >= 0, "{} must be non-negative".format(label))
    return parsed


def load_rows(path):
    path = Path(path).resolve()
    require(path.is_file(), "summary does not exist: {}".format(path))
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        rows = list(reader)
    require(rows, "summary is empty: {}".format(path))
    required = set(RUN_CONSTANTS) | set(ADDITIVE) | {
        "benchmark_entry_index",
        "get_neighbors_latency_histogram_json",
        "query_cpu_total_ns",
        "admission_protocol",
    }
    require(required <= set(reader.fieldnames or []), "summary schema is missing Figure 2 fields")
    return path, rows


def merge_histograms(rows):
    histograms = []
    for row in rows:
        try:
            value = json.loads(row["get_neighbors_latency_histogram_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise AggregateError("invalid latency histogram JSON: {}".format(exc))
        require(isinstance(value, dict) and isinstance(value.get("buckets"), list), "latency histogram invalid")
        histograms.append(value)
    bounds = [bucket.get("upper_bound_us") for bucket in histograms[0]["buckets"]]
    counts = [0] * len(bounds)
    total_count = 0
    total_sum = 0
    for value in histograms:
        require(
            [bucket.get("upper_bound_us") for bucket in value["buckets"]] == bounds,
            "latency histogram bucket boundaries differ across entries",
        )
        current_counts = [bucket.get("count") for bucket in value["buckets"]]
        require(all(isinstance(item, int) and item >= 0 for item in current_counts), "histogram count invalid")
        count = integer(value.get("count"), "histogram count")
        require(sum(current_counts) == count, "histogram bucket coverage drift")
        total_count += count
        total_sum += integer(value.get("sum_us"), "histogram sum_us")
        counts = [left + right for left, right in zip(counts, current_counts)]
    require(total_count > 0, "merged latency histogram is empty")
    target = max(1, int(math.ceil(total_count * 0.99)))
    seen = 0
    p99 = bounds[-1]
    for bound, count in zip(bounds, counts):
        seen += count
        if seen >= target:
            p99 = bound
            break
    merged = {
        "count": total_count,
        "sum_us": total_sum,
        "avg_us": total_sum // total_count,
        "p99_us": p99,
        "buckets": [
            {"upper_bound_us": bound, "count": count}
            for bound, count in zip(bounds, counts)
        ],
    }
    return merged


def collapse_summary(path, rows):
    first = rows[0]
    for row in rows[1:]:
        for field in RUN_CONSTANTS:
            require(row[field] == first[field], "entry-level {} drift within one run".format(field))
    indices = [integer(row["benchmark_entry_index"], "benchmark entry index") for row in rows]
    require(indices == list(range(len(rows))), "benchmark entries are missing/reordered")
    merged = {field: first[field] for field in RUN_CONSTANTS}
    for field in ADDITIVE:
        merged[field] = sum(integer(row[field], field) for row in rows)
    query_totals = [row["query_cpu_total_ns"] for row in rows]
    if first["mode"] == "cpu-phase":
        require(all(value != "" for value in query_totals), "CPU-phase run lacks query CPU total")
        merged["query_cpu_total_ns"] = sum(integer(value, "query CPU total") for value in query_totals)
    else:
        require(all(value == "" for value in query_totals), "latency run contains instrumented CPU total")
        merged["query_cpu_total_ns"] = ""
    histogram = merge_histograms(rows)
    require(histogram["count"] == merged["measured_operations"], "run histogram/operation coverage drift")
    merged["latency_p99_us"] = histogram["p99_us"]
    merged["latency_histogram_json"] = json.dumps(histogram, sort_keys=True, separators=(",", ":"))
    merged["summary_path"] = str(path)
    merged["summary_sha256"] = sha256_file(path)
    return merged


def validate_adaptation_pair(rows_by_key):
    for base_key in sorted({key[:1] + key[2:] for key in rows_by_key}):
        # key[:1] + key[2:] removes stage while preserving scale/workload/property/repeat.
        related = {
            key[1]: value
            for key, value in rows_by_key.items()
            if key[:1] + key[2:] == base_key and key[1] in {"A3", "A4"}
        }
        if not related:
            continue
        require(set(related) == {"A3", "A4"}, "A3/A4 adaptation control pair is incomplete")
        a3 = json.loads(related["A3"]["pre_measurement_json"])
        a4 = json.loads(related["A4"]["pre_measurement_json"])
        for field in ("trace", "training_runs", "training_start_cache_state", "adaptation_prestate"):
            require(a3.get(field) == a4.get(field), "A3/A4 {} prestate drift".format(field))
        for field in (
            "worker_threads",
            "cpuset",
            "cache_state_before_json",
            "binary_sha256",
            "dataset_sha256",
            "sample_plan_sha256",
            "truth_sha256",
            "pristine_store_sha256",
            "pristine_store_manifest_sha256",
        ):
            require(
                related["A3"][field] == related["A4"][field],
                "A3/A4 runtime prestate drift: {}".format(field),
            )
        require(
            a3.get("feedback") == "disabled"
            and a3.get("feedback_compactions") == 0
            and a4.get("feedback") == "enabled"
            and a4.get("feedback_compactions") == 1,
            "A3/A4 adaptation treatment contract drift",
        )
        require(
            integer(related["A3"]["training_runs"], "A3 training runs") == 1
            and integer(related["A4"]["training_runs"], "A4 training runs") == 1
            and integer(
                related["A3"]["training_feedback_compactions"],
                "A3 feedback compactions",
            )
            == 0
            and integer(
                related["A4"]["training_feedback_compactions"],
                "A4 feedback compactions",
            )
            == 1,
            "A3/A4 observed training treatment drift",
        )


def require_admission_fields(row, label):
    protocol = row.get("admission_protocol")
    legacy_fields = (
        "p02b_sentinel_result_sha256",
        "p02b_pass_marker_sha256",
        "p02b_provenance_sha256",
        "p02b_validator_sha256",
        "p02b_admission_sha256",
    )
    batch_fields = (
        "batch_lease_sha256",
        "batch_gate_tool_sha256",
        "batch_lease_admission_sha256",
        "batch_lease_pre_p31_sha256",
        "p31_integrity_guard_status_sha256",
        "p31_integrity_guard_samples_sha256",
        "p31_integrity_guard_ready_sha256",
        "p31_command_release_sha256",
    )
    if protocol == "legacy-p02b-admission-v1":
        require(all(row.get(field) for field in legacy_fields), "{} lacks legacy P02B admission".format(label))
        require(not any(row.get(field) for field in batch_fields), "{} mixes v2 batch evidence into legacy admission".format(label))
    elif protocol == "short-clean-window-v2":
        require(all(row.get(field) for field in batch_fields), "{} lacks v2 batch/guard admission".format(label))
        require(not any(row.get(field) for field in legacy_fields), "{} mixes legacy P02B evidence into v2 admission".format(label))
    else:
        raise AggregateError("{} has unsupported admission protocol".format(label))


def build_run_rows(collapsed, expected_repeats):
    protocols = {item.get("admission_protocol") for item in collapsed}
    require(
        len(protocols) == 1,
        "one Figure 2 aggregation batch may not mix admission protocols",
    )
    by_pair = {}
    for item in collapsed:
        key = tuple(item[field] for field in PAIR_KEY)
        mode = item["mode"]
        require(mode in {"latency", "cpu-phase"}, "Figure 2 accepts only latency/cpu-phase summaries")
        require(mode not in by_pair.setdefault(key, {}), "duplicate paired mode for {}".format(key))
        by_pair[key][mode] = item

    run_rows = []
    latency_by_key = {}
    for key, modes in sorted(by_pair.items()):
        stage = key[1]
        required_modes = {"latency"} if stage == "A6" else {"latency", "cpu-phase"}
        require(set(modes) == required_modes, "paired-mode coverage drift for {}".format(key))
        latency = modes["latency"]
        cpu = modes.get("cpu-phase")
        require(latency["performance_eligible"] == "true", "latency half is not formal")
        require_admission_fields(latency, "latency half")
        require(latency["current_digest_pass"] == "1", "latency digest gate failed")
        if cpu is not None:
            require(cpu["performance_eligible"] == "false", "CPU half must remain diagnostic")
            require_admission_fields(cpu, "CPU half")
            require(cpu["current_digest_pass"] == "1", "CPU digest gate failed")
            for field in PAIR_SHARED:
                require(latency[field] == cpu[field], "paired-mode {} drift".format(field))
            require(
                latency["measured_operations"] == cpu["measured_operations"],
                "paired-mode operation coverage drift",
            )
        row = {
            "figure2_schema_version": 1,
            "scale": latency["scale"],
            "ablation_stage": stage,
            "workload": latency["workload"],
            "property_predicate_mode": latency["property_predicate_mode"],
            "property_id": latency["property_id"],
            "repeat_index": latency["repeat_index"],
            "latency_run_id": latency["run_id"],
            "cpu_phase_run_id": "" if cpu is None else cpu["run_id"],
            "feature_switches": latency["feature_switches_json"],
            "pre_measurement_json": latency["pre_measurement_json"],
            "latency_p99_us": latency["latency_p99_us"],
            "candidate_segments_total": latency["candidate_segments_total"],
            "body_read_segments_total": latency["body_read_segments_total"],
            "measured_operations": latency["measured_operations"],
            "body_read_bytes_total": latency["logical_body_bytes_total"],
            "device_read_bytes_total": "",
            "cpu_query_setup_ns": "" if cpu is None else cpu["query_cpu_query_setup_ns"],
            "cpu_admission_ns": "" if cpu is None else cpu["query_cpu_metadata_admission_ns"],
            "cpu_routing_ns": "" if cpu is None else cpu["query_cpu_routing_index_ns"],
            "cpu_body_decode_filter_ns": "" if cpu is None else cpu["query_cpu_body_decode_filter_ns"],
            "cpu_mvcc_result_ns": "" if cpu is None else cpu["query_cpu_mvcc_result_ns"],
            "cpu_total_ns": "" if cpu is None else cpu["query_cpu_total_ns"],
            "latency_histogram_json": latency["latency_histogram_json"],
            "binary_sha256": latency["binary_sha256"],
            "dataset_sha256": latency["dataset_sha256"],
            "sample_plan_sha256": latency["sample_plan_sha256"],
            "truth_sha256": latency["truth_sha256"],
            "correctness_pass_sha256": latency["correctness_pass_sha256"],
            "profiles_sha256": latency["profiles_sha256"],
            "pristine_store_sha256": latency["pristine_store_sha256"],
            "pristine_store_manifest_sha256": latency["pristine_store_manifest_sha256"],
            "admission_protocol": latency["admission_protocol"],
            "p02b_sentinel_result_sha256": latency["p02b_sentinel_result_sha256"],
            "p02b_pass_marker_sha256": latency["p02b_pass_marker_sha256"],
            "p02b_provenance_sha256": latency["p02b_provenance_sha256"],
            "p02b_validator_sha256": latency["p02b_validator_sha256"],
            "p02b_admission_sha256": latency["p02b_admission_sha256"],
            "batch_lease_sha256": latency["batch_lease_sha256"],
            "batch_gate_tool_sha256": latency["batch_gate_tool_sha256"],
            "batch_lease_admission_sha256": latency["batch_lease_admission_sha256"],
            "batch_lease_pre_p31_sha256": latency["batch_lease_pre_p31_sha256"],
            "p31_integrity_guard_status_sha256": latency["p31_integrity_guard_status_sha256"],
            "p31_integrity_guard_samples_sha256": latency["p31_integrity_guard_samples_sha256"],
            "p31_integrity_guard_ready_sha256": latency["p31_integrity_guard_ready_sha256"],
            "p31_command_release_sha256": latency["p31_command_release_sha256"],
            "latency_summary_sha256": latency["summary_sha256"],
            "cpu_phase_summary_sha256": "" if cpu is None else cpu["summary_sha256"],
        }
        require(set(row) == set(RUN_COLUMNS), "internal Figure 2 run schema mismatch")
        run_rows.append(row)
        latency_by_key[key] = latency

    groups = {}
    for row in run_rows:
        group_key = tuple(row[field] for field in (
            "scale", "workload", "property_predicate_mode", "property_id"
        ))
        groups.setdefault(group_key, {}).setdefault(row["ablation_stage"], []).append(row)
    for group_key, stages in groups.items():
        expected_stages = CANONICAL_STAGES[group_key[0]]
        require(list(sorted(stages, key=expected_stages.index)) == expected_stages, "canonical stage coverage drift")
        for stage, rows in stages.items():
            repeats = sorted(integer(row["repeat_index"], "repeat index") for row in rows)
            require(repeats == list(range(1, expected_repeats + 1)), "independent repeat coverage drift")
    validate_adaptation_pair(latency_by_key)
    return run_rows


def mean_ci95(values):
    require(len(values) == 3, "current Figure 2 contract requires exactly three runs")
    mean = statistics.fmean(values)
    ci = 4.302652729911275 * statistics.stdev(values) / math.sqrt(len(values))
    return mean, ci


def build_aggregate_rows(run_rows):
    groups = {}
    for row in run_rows:
        key = tuple(row[field] for field in (
            "scale", "ablation_stage", "workload", "property_predicate_mode", "property_id"
        ))
        groups.setdefault(key, []).append(row)
    output = []
    for key, rows in sorted(groups.items()):
        values = {metric: [] for metric in AGGREGATE_METRICS}
        for row in rows:
            operations = integer(row["measured_operations"], "measured operations")
            require(operations > 0, "measured operations must be positive")
            values["latency_p99_us"].append(float(row["latency_p99_us"]))
            values["candidate_segments_per_op"].append(integer(row["candidate_segments_total"], "candidates") / operations)
            values["body_read_segments_per_op"].append(integer(row["body_read_segments_total"], "body reads") / operations)
            values["body_read_bytes_per_op"].append(integer(row["body_read_bytes_total"], "body bytes") / operations)
            cpu_mapping = {
                "cpu_query_setup_ns_per_op": "cpu_query_setup_ns",
                "cpu_admission_ns_per_op": "cpu_admission_ns",
                "cpu_routing_ns_per_op": "cpu_routing_ns",
                "cpu_body_decode_filter_ns_per_op": "cpu_body_decode_filter_ns",
                "cpu_mvcc_result_ns_per_op": "cpu_mvcc_result_ns",
                "cpu_total_ns_per_op": "cpu_total_ns",
            }
            for metric, field in cpu_mapping.items():
                if row[field] != "":
                    values[metric].append(integer(row[field], field) / operations)
        aggregate = {
            "figure2_aggregate_schema_version": 1,
            "scale": key[0],
            "ablation_stage": key[1],
            "workload": key[2],
            "property_predicate_mode": key[3],
            "property_id": key[4],
            "independent_runs": len(rows),
        }
        for metric in AGGREGATE_METRICS:
            if not values[metric]:
                aggregate[metric + "_mean"] = ""
                aggregate[metric + "_ci95"] = ""
            else:
                mean, ci = mean_ci95(values[metric])
                aggregate[metric + "_mean"] = "{:.12g}".format(mean)
                aggregate[metric + "_ci95"] = "{:.12g}".format(ci)
        require(set(aggregate) == set(AGGREGATE_COLUMNS), "internal aggregate schema mismatch")
        output.append(aggregate)
    return output


def write_tsv(path, columns, rows):
    path = Path(path)
    require(path.is_absolute(), "output path must be absolute")
    require(not os.path.lexists(str(path)), "output path already exists: {}".format(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(str(temporary), str(path))


def run(args):
    require(args.expected_repeats == 3, "Figure 2 CI contract currently requires exactly three repeats")
    collapsed = [collapse_summary(*load_rows(path)) for path in args.summary]
    run_rows = build_run_rows(collapsed, args.expected_repeats)
    aggregate_rows = build_aggregate_rows(run_rows)
    write_tsv(args.run_output.resolve(), RUN_COLUMNS, run_rows)
    write_tsv(args.aggregate_output.resolve(), AGGREGATE_COLUMNS, aggregate_rows)
    print(str(args.run_output.resolve()))
    print(str(args.aggregate_output.resolve()))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", action="append", required=True, type=Path)
    parser.add_argument("--run-output", required=True, type=Path)
    parser.add_argument("--aggregate-output", required=True, type=Path)
    parser.add_argument("--expected-repeats", type=int, default=3)
    args = parser.parse_args()
    try:
        return run(args)
    except (AggregateError, OSError) as exc:
        print("FAIL: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
