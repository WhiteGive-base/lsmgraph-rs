#!/usr/bin/env python3
"""Extract one run-level QPS/P99 record from lsmgraph storage-bench JSON."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from p02b_common import GateError, atomic_write_json, read_json, same_resolved_path, sha256_file


def as_nonnegative_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateError("{} must be numeric".format(context))
    numeric = int(value)
    if float(value) != float(numeric) or numeric < 0:
        raise GateError("{} must be a non-negative integer".format(context))
    return numeric


def nested(mapping: Dict[str, Any], keys: List[str], context: str) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise GateError("{} is missing {}".format(context, ".".join(keys)))
        value = value[key]
    return value


def validate_query_plan(path: Path, expected_queries: int) -> Dict[str, Any]:
    plan = read_json(path)
    if plan.get("version") != 1 or plan.get("source") != "shared-truth-tsv":
        raise GateError("query plan is not a version-1 shared-truth plan")
    entries = plan.get("entries")
    if not isinstance(entries, list) or not entries:
        raise GateError("query plan entries must be a non-empty array")
    total = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("samples"), list):
            raise GateError("query plan entry {} has invalid samples".format(index))
        if not entry["samples"]:
            raise GateError("query plan entry {} has no samples".format(index))
        for sample_index, sample in enumerate(entry["samples"]):
            if not isinstance(sample, dict):
                raise GateError("query plan entry {} sample {} is invalid".format(index, sample_index))
            src = as_nonnegative_int(sample.get("src"), "query-plan src")
            degree = as_nonnegative_int(sample.get("degree"), "query-plan degree")
            if src < 0 or degree < 0:
                raise GateError("query-plan sample values must be non-negative")
        total += len(entry["samples"])
    if total != expected_queries:
        raise GateError(
            "query plan has {} queries, expected {}".format(total, expected_queries)
        )
    return {
        "version": 1,
        "source": "shared-truth-tsv",
        "entries": len(entries),
        "queries": total,
        "semantic_degree_hint": plan.get("semantic_degree_hint"),
        "force_signature": plan.get("force_signature"),
        "sha256": sha256_file(path),
    }


def merge_histogram(
    merged: Dict[int, int], buckets: Any, expected_count: int, context: str
) -> None:
    if not isinstance(buckets, list) or not buckets:
        raise GateError("{} has no latency buckets".format(context))
    prior_bound = -1
    observed = 0
    for index, bucket in enumerate(buckets):
        if not isinstance(bucket, dict):
            raise GateError("{} bucket {} is not an object".format(context, index))
        bound = as_nonnegative_int(bucket.get("upper_bound_us"), "{} upper bound".format(context))
        count = as_nonnegative_int(bucket.get("count"), "{} bucket count".format(context))
        if bound <= prior_bound:
            raise GateError("{} latency bucket bounds are not strictly increasing".format(context))
        prior_bound = bound
        observed += count
        merged[bound] = merged.get(bound, 0) + count
    if observed != expected_count:
        raise GateError(
            "{} bucket count {} differs from op count {}".format(context, observed, expected_count)
        )


def histogram_percentile(merged: Dict[int, int], percentile: float) -> float:
    total = sum(merged.values())
    if total <= 0:
        raise GateError("merged latency histogram is empty")
    rank = max(1, int(math.ceil(percentile * total)))
    cumulative = 0
    for bound, count in sorted(merged.items()):
        cumulative += count
        if cumulative >= rank:
            if bound >= 10**12:
                raise GateError("P99 fell in the unbounded overflow latency bucket")
            return float(bound)
    raise GateError("cannot locate percentile in merged histogram")


def extract_run_metrics(
    bench_path: Path,
    query_plan: Path,
    run_index: int,
    run_id: str,
    expected_queries: int,
    warmup_runs: int,
    measured_repeats: int,
    minimum_measured_seconds: float,
) -> Dict[str, Any]:
    if run_index < 1 or not run_id:
        raise GateError("run identity is invalid")
    if expected_queries < 1 or warmup_runs < 0 or measured_repeats < 1:
        raise GateError("metric extraction protocol values are invalid")
    if minimum_measured_seconds <= 0:
        raise GateError("minimum_measured_seconds must be positive")
    plan_summary = validate_query_plan(query_plan, expected_queries)
    raw = read_json(bench_path)
    if not same_resolved_path(raw.get("sample_plan_in"), query_plan):
        raise GateError("storage-bench did not report the frozen query-plan path")
    if raw.get("sample_plan_version") != 1:
        raise GateError("storage-bench sample plan version is not 1")
    if raw.get("warmup_runs") != warmup_runs:
        raise GateError("storage-bench warmup_runs differs from frozen config")
    if raw.get("repeats") != measured_repeats:
        raise GateError("storage-bench repeats differs from frozen config")
    benchmarks = raw.get("benchmarks")
    if not isinstance(benchmarks, list) or not benchmarks:
        raise GateError("storage-bench JSON has no benchmark entries")

    total_ops = 0
    total_elapsed_ms = 0
    merged_histogram: Dict[int, int] = {}
    entry_summaries: List[Dict[str, Any]] = []
    for entry_index, entry in enumerate(benchmarks):
        if not isinstance(entry, dict):
            raise GateError("benchmark entry {} is not an object".format(entry_index))
        if entry.get("warmup_runs") != warmup_runs or entry.get("repeats") != measured_repeats:
            raise GateError("benchmark entry {} does not match warmup/repeat config".format(entry_index))
        warmup_rounds = entry.get("warmup_rounds")
        measured_rounds = entry.get("rounds")
        if not isinstance(warmup_rounds, list) or len(warmup_rounds) != warmup_runs:
            raise GateError("benchmark entry {} has wrong warmup round count".format(entry_index))
        if not isinstance(measured_rounds, list) or len(measured_rounds) != measured_repeats:
            raise GateError("benchmark entry {} has wrong measured round count".format(entry_index))
        entry_ops = 0
        entry_elapsed_ms = 0
        for round_index, round_value in enumerate(measured_rounds):
            context = "benchmark {} round {}".format(entry_index, round_index + 1)
            if not isinstance(round_value, dict) or round_value.get("kind") != "measured":
                raise GateError("{} is not a measured round".format(context))
            if round_value.get("round") != round_index + 1:
                raise GateError("{} has non-contiguous round index".format(context))
            elapsed_ms = as_nonnegative_int(
                round_value.get("get_neighbors_elapsed_ms"), "{} elapsed_ms".format(context)
            )
            if elapsed_ms <= 0:
                raise GateError("{} has zero elapsed_ms".format(context))
            storage = nested(round_value, ["neighbor_metrics", "storage"], context)
            if not isinstance(storage, dict):
                raise GateError("{} storage metrics is not an object".format(context))
            ops = as_nonnegative_int(storage.get("get_neighbors_ops"), "{} ops".format(context))
            if ops <= 0:
                raise GateError("{} has no operations".format(context))
            latency = storage.get("get_neighbors_latency")
            if not isinstance(latency, dict):
                raise GateError("{} has no latency histogram".format(context))
            latency_count = as_nonnegative_int(latency.get("count"), "{} latency count".format(context))
            if latency_count != ops:
                raise GateError("{} latency count differs from operations".format(context))
            merge_histogram(merged_histogram, latency.get("buckets"), ops, context)
            entry_ops += ops
            entry_elapsed_ms += elapsed_ms
        total_ops += entry_ops
        total_elapsed_ms += entry_elapsed_ms
        entry_summaries.append(
            {
                "entry_index": entry_index,
                "edge_type": entry.get("edge_type"),
                "ops": entry_ops,
                "elapsed_ms": entry_elapsed_ms,
            }
        )

    required_ops = expected_queries * measured_repeats
    if total_ops != required_ops:
        raise GateError(
            "storage-bench executed {} operations, expected {}".format(total_ops, required_ops)
        )
    measured_seconds = total_elapsed_ms / 1000.0
    if measured_seconds < minimum_measured_seconds:
        raise GateError(
            "measured query time {:.3f}s is shorter than frozen minimum {:.3f}s".format(
                measured_seconds, minimum_measured_seconds
            )
        )
    qps = total_ops / measured_seconds
    p99_us = histogram_percentile(merged_histogram, 0.99)
    return {
        "schema_version": "p02b-sentinel-run-metrics-v1",
        "state": "PASS",
        "run_index": run_index,
        "run_id": run_id,
        "query_count": total_ops,
        "logical_trace_queries": expected_queries,
        "measured_repeats": measured_repeats,
        "warmup_runs": warmup_runs,
        "measured_seconds": measured_seconds,
        "qps": qps,
        "p99_us": p99_us,
        "p99_source": "merged_storage_latency_histogram_upper_bound",
        "bench_json": {
            "path": str(bench_path.resolve()),
            "sha256": sha256_file(bench_path),
        },
        "query_plan": plan_summary,
        "cache_state_before": raw.get("cache_state_before"),
        "cache_state_after": raw.get("cache_state_after"),
        "entries": entry_summaries,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench-json", required=True, type=Path)
    parser.add_argument("--query-plan", required=True, type=Path)
    parser.add_argument("--run-index", required=True, type=int)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-queries", required=True, type=int)
    parser.add_argument("--warmup-runs", required=True, type=int)
    parser.add_argument("--measured-repeats", required=True, type=int)
    parser.add_argument("--minimum-measured-seconds", required=True, type=float)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = extract_run_metrics(
            args.bench_json,
            args.query_plan,
            args.run_index,
            args.run_id,
            args.expected_queries,
            args.warmup_runs,
            args.measured_repeats,
            args.minimum_measured_seconds,
        )
        if args.output:
            atomic_write_json(args.output, result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
