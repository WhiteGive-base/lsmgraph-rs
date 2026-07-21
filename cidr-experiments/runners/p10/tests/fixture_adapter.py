#!/usr/bin/env python3
"""Synthetic adapter used only to test the P10/P11 contract."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))

from p10_contract import (  # noqa: E402
    CONTRACT_VERSION,
    INTERFACE_SCOPE,
    OBSERVATION_COLUMNS,
    RESULT_SCHEMA_VERSION,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    CLOCK_NAME,
    phase_digest,
    read_truth,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("ok", "mismatch", "timeout", "bad-order"), default="ok")
    parser.add_argument("--latency-base-ns", type=int, default=100_000)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def phase_rows(
    *,
    request: dict,
    truth_rows: list[dict[str, int]],
    phase: str,
    passes: int,
    profile: str,
    latency_base_ns: int,
) -> tuple[list[dict[str, str]], dict, list[dict]]:
    events: list[dict] = []
    start = time.monotonic_ns()
    events.append(
        {"contract_version": CONTRACT_VERSION, "phase": phase, "event": "start", "monotonic_ns": start}
    )
    rows: list[dict[str, str]] = []
    completed = 0
    timeouts = 0
    mismatches = 0
    timeout_ns = request["timing"]["per_query_timeout_ms"] * 1_000_000
    for pass_index in range(passes):
        for truth in truth_rows:
            actual = {
                "count": truth["count"],
                "sum_hash": truth["sum_hash"],
                "xor_hash": truth["xor_hash"],
            }
            status = "ok"
            latency_ns = latency_base_ns + truth["query_index"] * 1_000 + pass_index * 10_000
            if profile == "mismatch" and phase == "measured" and pass_index == 0 and truth["query_index"] == 0:
                actual["count"] += 1
                mismatches += 1
            if profile == "timeout" and phase == "measured" and pass_index == 0 and truth["query_index"] == 0:
                status = "timeout"
                latency_ns = timeout_ns
                timeouts += 1
            else:
                completed += 1
            rows.append(
                {
                    "contract_version": CONTRACT_VERSION,
                    "system_id": request["system_id"],
                    "group": request["group"],
                    "repeat_index": str(request["repeat_index"]),
                    "phase": phase,
                    "pass_index": str(pass_index),
                    "query_index": str(truth["query_index"]),
                    "edge_type": str(truth["edge_type"]),
                    "src": str(truth["src"]),
                    "expected_count": str(truth["count"]),
                    "actual_count": "" if status == "timeout" else str(actual["count"]),
                    "expected_sum_hash": str(truth["sum_hash"]),
                    "actual_sum_hash": "" if status == "timeout" else str(actual["sum_hash"]),
                    "expected_xor_hash": str(truth["xor_hash"]),
                    "actual_xor_hash": "" if status == "timeout" else str(actual["xor_hash"]),
                    "status": status,
                    "latency_ns": str(latency_ns),
                }
            )
    if profile == "bad-order" and phase == "measured" and len(rows) >= 2:
        rows[0], rows[1] = rows[1], rows[0]
    # The contract freezes concurrency=1, so the enclosing phase interval must
    # cover the sum of its per-query intervals.  Sleeping keeps the synthetic
    # CLOCK_MONOTONIC boundary honest without running a real benchmark.
    total_latency_ns = sum(int(row["latency_ns"]) for row in rows)
    remaining_ns = start + total_latency_ns - time.monotonic_ns()
    if remaining_ns > 0:
        time.sleep(remaining_ns / 1_000_000_000)
    end = max(time.monotonic_ns(), start + total_latency_ns)
    events.append(
        {"contract_version": CONTRACT_VERSION, "phase": phase, "event": "end", "monotonic_ns": end}
    )
    summary = {
        "passes": passes,
        "requested_queries": passes * len(truth_rows),
        "completed_queries": completed,
        "timeout_queries": timeouts,
        "mismatch_queries": mismatches,
        "started_monotonic_ns": start,
        "ended_monotonic_ns": end,
        "elapsed_ns": end - start,
        "expected_digest_sha256": phase_digest(phase, passes, truth_rows),
        "actual_digest_sha256": phase_digest(phase, passes, truth_rows, rows),
    }
    return rows, summary, events


def main() -> int:
    args = parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    truth_rows = read_truth(Path(request["truth"]["path"]), request["truth"]["query_count"])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    warmup_rows, warmup, warmup_events = phase_rows(
        request=request,
        truth_rows=truth_rows,
        phase="warmup",
        passes=request["timing"]["warmup_passes"],
        profile=args.profile,
        latency_base_ns=args.latency_base_ns,
    )
    measured_rows, measured, measured_events = phase_rows(
        request=request,
        truth_rows=truth_rows,
        phase="measured",
        passes=request["timing"]["measured_passes"],
        profile=args.profile,
        latency_base_ns=args.latency_base_ns,
    )
    with (args.output_dir / "query-observations.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OBSERVATION_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(warmup_rows + measured_rows)
    with (args.output_dir / "phase-events.jsonl").open("w", encoding="utf-8") as handle:
        for event in warmup_events + measured_events:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "system_id": request["system_id"],
        "group": request["group"],
        "system_version": request["system_version"],
        "interface_scope": INTERFACE_SCOPE,
        "repeat_index": request["repeat_index"],
        "process_lifetime": request["process_lifetime"],
        "truth_sha256": request["truth"]["sha256"],
        "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "concurrency": request["timing"]["concurrency"],
        "per_query_timeout_ms": request["timing"]["per_query_timeout_ms"],
        "warmup": warmup,
        "measured": measured,
    }
    (args.output_dir / "adapter-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
