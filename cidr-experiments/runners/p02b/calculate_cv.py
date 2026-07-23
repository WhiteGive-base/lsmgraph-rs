#!/usr/bin/env python3
"""Calculate fail-closed run-level QPS/P99 coefficients of variation."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

from p02b_common import GateError, atomic_write_json, read_json, sha256_file


METRICS_SCHEMA = "p02b-sentinel-run-metrics-v1"


def finite_positive(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateError("{} must be numeric".format(context))
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise GateError("{} must be finite and positive".format(context))
    return numeric


def sample_cv(values: Sequence[float], context: str) -> float:
    if len(values) < 2:
        raise GateError("{} needs at least two independent runs".format(context))
    mean = statistics.mean(values)
    if mean <= 0:
        raise GateError("{} mean must be positive".format(context))
    return statistics.stdev(values) / mean


def calculate_cv(
    metric_paths: Sequence[Path],
    expected_runs: int,
    qps_cv_max: float,
    p99_cv_max: float,
) -> Dict[str, Any]:
    if expected_runs < 3:
        raise GateError("sentinel requires at least three independent runs")
    if len(metric_paths) != expected_runs:
        raise GateError(
            "expected {} metric files, found {}".format(expected_runs, len(metric_paths))
        )
    if not 0 < qps_cv_max <= 0.07:
        raise GateError("qps_cv_max must be in (0, 0.07]")
    if not 0 < p99_cv_max <= 0.05:
        raise GateError("p99_cv_max must be in (0, 0.05]")

    records: List[Dict[str, Any]] = []
    seen_indices = set()
    seen_ids = set()
    for path in metric_paths:
        record = read_json(path)
        if record.get("schema_version") != METRICS_SCHEMA:
            raise GateError("{} has wrong metric schema".format(path))
        if record.get("state") != "PASS":
            raise GateError("{} is not PASS".format(path))
        try:
            run_index = int(record["run_index"])
            query_count = int(record["query_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GateError("{} has invalid run_index/query_count".format(path)) from exc
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise GateError("{} has missing run_id".format(path))
        if run_index in seen_indices or run_id in seen_ids:
            raise GateError("duplicate independent run identity in {}".format(path))
        seen_indices.add(run_index)
        seen_ids.add(run_id)
        if query_count <= 0:
            raise GateError("{} has non-positive query_count".format(path))
        records.append(
            {
                "path": str(path.resolve()),
                "sha256": sha256_file(path),
                "run_index": run_index,
                "run_id": run_id,
                "query_count": query_count,
                "qps": finite_positive(record.get("qps"), "{} qps".format(path)),
                "p99_us": finite_positive(record.get("p99_us"), "{} p99_us".format(path)),
                "measured_seconds": finite_positive(
                    record.get("measured_seconds"), "{} measured_seconds".format(path)
                ),
            }
        )

    if seen_indices != set(range(1, expected_runs + 1)):
        raise GateError("run_index values must be exactly 1..{}".format(expected_runs))
    query_counts = {record["query_count"] for record in records}
    if len(query_counts) != 1:
        raise GateError("independent runs have different query counts")
    records.sort(key=lambda item: int(item["run_index"]))

    qps_values = [float(record["qps"]) for record in records]
    p99_values = [float(record["p99_us"]) for record in records]
    qps_cv = sample_cv(qps_values, "QPS")
    p99_cv = sample_cv(p99_values, "P99")
    passed = qps_cv <= qps_cv_max and p99_cv <= p99_cv_max
    return {
        "schema_version": "p02b-sentinel-cv-v1",
        "state": "PASS" if passed else "HOLD",
        "method": "sample_standard_deviation_over_arithmetic_mean",
        "independent_process_runs": expected_runs,
        "query_count_per_run": next(iter(query_counts)),
        "qps": {
            "values": qps_values,
            "mean": statistics.mean(qps_values),
            "sample_stdev": statistics.stdev(qps_values),
            "cv": qps_cv,
            "maximum_cv": qps_cv_max,
            "pass": qps_cv <= qps_cv_max,
        },
        "p99_us": {
            "values": p99_values,
            "mean": statistics.mean(p99_values),
            "sample_stdev": statistics.stdev(p99_values),
            "cv": p99_cv,
            "maximum_cv": p99_cv_max,
            "pass": p99_cv <= p99_cv_max,
        },
        "runs": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", action="append", required=True, type=Path)
    parser.add_argument("--expected-runs", required=True, type=int)
    parser.add_argument("--qps-cv-max", required=True, type=float)
    parser.add_argument("--p99-cv-max", required=True, type=float)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = calculate_cv(
            args.metrics,
            args.expected_runs,
            args.qps_cv_max,
            args.p99_cv_max,
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
