#!/usr/bin/env python3
"""Calculate the quantization-aware P02B stability admission result."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from p02b_common import GateError, atomic_write_json, read_json, sha256_file


METRICS_SCHEMA = "p02b-sentinel-run-metrics-v2"
STABILITY_SCHEMA = "p02b-sentinel-stability-v2"
OVERFLOW_BOUND_US = 2**64 - 1


def finite_positive(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateError("{} must be numeric".format(context))
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise GateError("{} must be finite and positive".format(context))
    return numeric


def nonnegative_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GateError("{} must be a non-negative integer".format(context))
    return value


def sample_cv(values: Sequence[float], context: str) -> float:
    if len(values) < 2:
        raise GateError("{} needs at least two independent runs".format(context))
    mean = statistics.mean(values)
    if mean <= 0:
        raise GateError("{} mean must be positive".format(context))
    return statistics.stdev(values) / mean


def metric_summary(
    values: Sequence[float], maximum_cv: float, admission_role: str = "hard_gate"
) -> Dict[str, Any]:
    mean = statistics.mean(values)
    sample_stdev = statistics.stdev(values)
    cv = sample_stdev / mean
    result: Dict[str, Any] = {
        "values": list(values),
        "mean": mean,
        "sample_stdev": sample_stdev,
        "cv": cv,
        "admission_role": admission_role,
    }
    if admission_role == "hard_gate":
        result.update({"maximum_cv": maximum_cv, "pass": cv <= maximum_cv})
    return result


def validate_histogram(record: Dict[str, Any], context: str) -> Dict[str, Any]:
    histogram = record.get("histogram")
    if not isinstance(histogram, dict):
        raise GateError("{} has no merged histogram".format(context))
    bounds = histogram.get("bounds_us")
    counts = histogram.get("counts")
    if (
        not isinstance(bounds, list)
        or not bounds
        or not isinstance(counts, list)
        or len(counts) != len(bounds)
    ):
        raise GateError("{} has malformed histogram bounds/counts".format(context))
    parsed_bounds = [nonnegative_int(value, "{} histogram bound".format(context)) for value in bounds]
    parsed_counts = [nonnegative_int(value, "{} histogram count".format(context)) for value in counts]
    if any(right <= left for left, right in zip(parsed_bounds, parsed_bounds[1:])):
        raise GateError("{} histogram bounds are not strictly increasing".format(context))
    if parsed_bounds[-1] != OVERFLOW_BOUND_US:
        raise GateError("{} histogram has no canonical overflow bucket".format(context))
    count = nonnegative_int(histogram.get("count"), "{} histogram count".format(context))
    if count < 1:
        raise GateError("{} histogram count must be positive".format(context))
    if sum(parsed_counts) != count:
        raise GateError("{} histogram bucket sum differs from count".format(context))
    expected_boundary_sha = hashlib.sha256(
        json.dumps(parsed_bounds, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    if histogram.get("boundary_sha256") != expected_boundary_sha:
        raise GateError("{} histogram boundary SHA-256 is invalid".format(context))
    sum_us = nonnegative_int(histogram.get("sum_us"), "{} histogram sum_us".format(context))
    mean_us = finite_positive(histogram.get("mean_us"), "{} histogram mean_us".format(context))
    if not math.isclose(mean_us, sum_us / count, rel_tol=1e-12, abs_tol=1e-12):
        raise GateError("{} histogram mean_us was not derived from sum/count".format(context))
    max_us = nonnegative_int(histogram.get("max_us"), "{} histogram max_us".format(context))
    overflow_count = nonnegative_int(
        histogram.get("overflow_count"), "{} histogram overflow_count".format(context)
    )
    if overflow_count != parsed_counts[-1]:
        raise GateError("{} histogram overflow count drift".format(context))
    return {
        "bounds": parsed_bounds,
        "counts": parsed_counts,
        "count": count,
        "sum_us": sum_us,
        "mean_us": mean_us,
        "max_us": max_us,
        "overflow_count": overflow_count,
        "boundary_sha256": expected_boundary_sha,
    }


def percentile_upper_bound(bounds: Sequence[int], counts: Sequence[int], rank: int) -> int:
    cumulative = 0
    for bound, count in zip(bounds, counts):
        cumulative += count
        if cumulative >= rank:
            return bound
    raise GateError("histogram cannot locate the requested percentile rank")


def tail_count_above(bounds: Sequence[int], counts: Sequence[int], threshold: int) -> int:
    if threshold not in bounds:
        raise GateError("tail threshold {} is not an exact histogram boundary".format(threshold))
    return sum(count for bound, count in zip(bounds, counts) if bound > threshold)


def calculate_stability(
    metric_paths: Sequence[Path],
    expected_runs: int,
    qps_cv_max: float,
    mean_latency_cv_max: float,
    quantile_numerator: int,
    quantile_denominator: int,
    lower_tail_bound_us: int,
    upper_tail_bound_us: int,
    sigma_multiplier: int,
    require_zero_overflow: bool,
) -> Dict[str, Any]:
    if isinstance(expected_runs, bool) or not isinstance(expected_runs, int):
        raise GateError("expected_runs must be an integer")
    if expected_runs < 3:
        raise GateError("sentinel requires at least three independent runs")
    if len(metric_paths) != expected_runs:
        raise GateError(
            "expected {} metric files, found {}".format(expected_runs, len(metric_paths))
        )
    qps_cv_max = finite_positive(qps_cv_max, "qps_cv_max")
    mean_latency_cv_max = finite_positive(
        mean_latency_cv_max, "mean_storage_latency_cv_max"
    )
    if qps_cv_max > 0.07:
        raise GateError("qps_cv_max must be in (0, 0.07]")
    if mean_latency_cv_max > 0.07:
        raise GateError("mean_storage_latency_cv_max must be in (0, 0.07]")
    if (
        isinstance(quantile_numerator, bool)
        or isinstance(quantile_denominator, bool)
        or not isinstance(quantile_numerator, int)
        or not isinstance(quantile_denominator, int)
        or quantile_numerator != 99
        or quantile_denominator != 100
    ):
        raise GateError("quantile must be frozen at 99/100")
    if (
        isinstance(lower_tail_bound_us, bool)
        or isinstance(upper_tail_bound_us, bool)
        or not isinstance(lower_tail_bound_us, int)
        or not isinstance(upper_tail_bound_us, int)
        or lower_tail_bound_us != 150_000
        or upper_tail_bound_us != 250_000
    ):
        raise GateError("tail boundaries must be frozen at 150000us and 250000us")
    if (
        isinstance(sigma_multiplier, bool)
        or not isinstance(sigma_multiplier, int)
        or sigma_multiplier != 3
    ):
        raise GateError("sigma_multiplier must be frozen at 3")
    if require_zero_overflow is not True:
        raise GateError("require_zero_overflow must be true")

    records: List[Dict[str, Any]] = []
    seen_indices = set()
    seen_ids = set()
    common_bounds: Optional[List[int]] = None
    common_boundary_sha: Optional[str] = None
    query_count: Optional[int] = None
    for path in metric_paths:
        record = read_json(path)
        context = str(path)
        if record.get("schema_version") != METRICS_SCHEMA or record.get("state") != "PASS":
            raise GateError("{} is not a PASS {} document".format(path, METRICS_SCHEMA))
        run_index = nonnegative_int(record.get("run_index"), "{} run_index".format(context))
        run_id = record.get("run_id")
        if run_index < 1 or not isinstance(run_id, str) or not run_id:
            raise GateError("{} has invalid run identity".format(path))
        if run_index in seen_indices or run_id in seen_ids:
            raise GateError("duplicate independent run identity in {}".format(path))
        seen_indices.add(run_index)
        seen_ids.add(run_id)
        run_query_count = nonnegative_int(
            record.get("query_count"), "{} query_count".format(context)
        )
        if run_query_count < 1:
            raise GateError("{} has no measured queries".format(path))
        histogram = validate_histogram(record, context)
        if histogram["count"] != run_query_count:
            raise GateError("{} histogram count differs from query_count".format(path))
        if query_count is None:
            query_count = run_query_count
            common_bounds = list(histogram["bounds"])
            common_boundary_sha = str(histogram["boundary_sha256"])
        elif (
            run_query_count != query_count
            or histogram["bounds"] != common_bounds
            or histogram["boundary_sha256"] != common_boundary_sha
        ):
            raise GateError("independent runs have different query counts or histogram boundaries")

        assert common_bounds is not None
        rank = (quantile_numerator * run_query_count + quantile_denominator - 1) // quantile_denominator
        p99_upper = percentile_upper_bound(histogram["bounds"], histogram["counts"], rank)
        declared_p99 = finite_positive(
            record.get("p99_upper_bound_us"), "{} p99_upper_bound_us".format(context)
        )
        if declared_p99 != float(p99_upper):
            raise GateError("{} p99 upper bound does not match histogram".format(path))
        records.append(
            {
                "path": str(path.resolve()),
                "sha256": sha256_file(path),
                "run_index": run_index,
                "run_id": run_id,
                "query_count": run_query_count,
                "measured_seconds": finite_positive(
                    record.get("measured_seconds"), "{} measured_seconds".format(context)
                ),
                "qps": finite_positive(record.get("qps"), "{} qps".format(context)),
                "mean_storage_latency_us": histogram["mean_us"],
                "p99_upper_bound_us": float(p99_upper),
                "histogram_boundary_sha256": histogram["boundary_sha256"],
                "overflow_count": histogram["overflow_count"],
                "tail_gt_150000_count": tail_count_above(
                    histogram["bounds"], histogram["counts"], lower_tail_bound_us
                ),
                "tail_gt_250000_count": tail_count_above(
                    histogram["bounds"], histogram["counts"], upper_tail_bound_us
                ),
            }
        )

    if seen_indices != set(range(1, expected_runs + 1)):
        raise GateError("run_index values must be exactly 1..{}".format(expected_runs))
    records.sort(key=lambda item: int(item["run_index"]))
    assert query_count is not None and common_bounds is not None and common_boundary_sha is not None

    rank = (quantile_numerator * query_count + quantile_denominator - 1) // quantile_denominator
    tail_budget = query_count - rank
    tail_probability = (quantile_denominator - quantile_numerator) / quantile_denominator
    sigma_count = math.sqrt(query_count * tail_probability * (1.0 - tail_probability))
    within_run_jitter_budget = int(math.ceil(sigma_multiplier * sigma_count))
    cross_run_range_budget = int(
        math.ceil(sigma_multiplier * math.sqrt(2.0) * sigma_count)
    )
    lower_max = tail_budget + within_run_jitter_budget
    upper_max = tail_budget

    qps_values = [float(record["qps"]) for record in records]
    mean_latency_values = [float(record["mean_storage_latency_us"]) for record in records]
    p99_values = [float(record["p99_upper_bound_us"]) for record in records]
    lower_values = [int(record["tail_gt_150000_count"]) for record in records]
    upper_values = [int(record["tail_gt_250000_count"]) for record in records]
    overflow_values = [int(record["overflow_count"]) for record in records]

    qps = metric_summary(qps_values, qps_cv_max)
    mean_latency = metric_summary(mean_latency_values, mean_latency_cv_max)
    legacy_p99 = metric_summary(p99_values, 0.0, admission_role="diagnostic_only")
    lower_range = max(lower_values) - min(lower_values)
    upper_range = max(upper_values) - min(upper_values)
    lower_pass = max(lower_values) <= lower_max and lower_range <= cross_run_range_budget
    upper_pass = max(upper_values) <= upper_max and upper_range <= cross_run_range_budget
    overflow_pass = all(value == 0 for value in overflow_values)
    tail_pass = lower_pass and upper_pass and overflow_pass
    passed = bool(qps["pass"] and mean_latency["pass"] and tail_pass)

    return {
        "schema_version": STABILITY_SCHEMA,
        "state": "PASS" if passed else "HOLD",
        "method": "quantization-aware-tail-v1",
        "independent_process_runs": expected_runs,
        "query_count_per_run": query_count,
        "histogram_boundary_sha256": common_boundary_sha,
        "histogram_bounds_us": common_bounds,
        "policy": {
            "qps_cv_max": qps_cv_max,
            "mean_storage_latency_cv_max": mean_latency_cv_max,
            "quantile_numerator": quantile_numerator,
            "quantile_denominator": quantile_denominator,
            "lower_tail_bound_us": lower_tail_bound_us,
            "upper_tail_bound_us": upper_tail_bound_us,
            "sigma_multiplier": sigma_multiplier,
            "require_zero_overflow": require_zero_overflow,
        },
        "derived_budgets": {
            "rank": rank,
            "tail_budget_count": tail_budget,
            "sigma_count": sigma_count,
            "within_run_jitter_budget_count": within_run_jitter_budget,
            "cross_run_range_budget_count": cross_run_range_budget,
        },
        "qps": qps,
        "mean_storage_latency_us": mean_latency,
        "p99_upper_bound_us": legacy_p99,
        "tail": {
            "lower_boundary": {
                "bound_us": lower_tail_bound_us,
                "values": lower_values,
                "maximum_per_run_count": lower_max,
                "range_count": lower_range,
                "maximum_range_count": cross_run_range_budget,
                "pass": lower_pass,
            },
            "upper_boundary": {
                "bound_us": upper_tail_bound_us,
                "values": upper_values,
                "maximum_per_run_count": upper_max,
                "range_count": upper_range,
                "maximum_range_count": cross_run_range_budget,
                "pass": upper_pass,
            },
            "overflow": {
                "bound_us": OVERFLOW_BOUND_US,
                "values": overflow_values,
                "required_count": 0,
                "pass": overflow_pass,
            },
            "pass": tail_pass,
        },
        "runs": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", action="append", required=True, type=Path)
    parser.add_argument("--expected-runs", required=True, type=int)
    parser.add_argument("--qps-cv-max", required=True, type=float)
    parser.add_argument("--mean-latency-cv-max", required=True, type=float)
    parser.add_argument("--quantile-numerator", required=True, type=int)
    parser.add_argument("--quantile-denominator", required=True, type=int)
    parser.add_argument("--lower-tail-bound-us", required=True, type=int)
    parser.add_argument("--upper-tail-bound-us", required=True, type=int)
    parser.add_argument("--sigma-multiplier", required=True, type=int)
    parser.add_argument("--require-zero-overflow", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = calculate_stability(
            args.metrics,
            args.expected_runs,
            args.qps_cv_max,
            args.mean_latency_cv_max,
            args.quantile_numerator,
            args.quantile_denominator,
            args.lower_tail_bound_us,
            args.upper_tail_bound_us,
            args.sigma_multiplier,
            args.require_zero_overflow,
        )
        if args.output:
            atomic_write_json(args.output, result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["state"] == "PASS" else 3
    except GateError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
