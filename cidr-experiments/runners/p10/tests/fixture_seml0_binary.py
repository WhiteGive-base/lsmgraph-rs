#!/usr/bin/env python3
"""Tiny storage-bench stand-in used only by SemL0 adapter contract tests."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def option(name: str) -> str:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError) as exc:
        raise SystemExit(f"missing {name}") from exc


def main() -> int:
    if "--help" in sys.argv:
        print("fixture SemL0 storage-bench")
        return 0
    raw_dir = Path(option("--p10-raw-output-dir"))
    raw_dir.mkdir(parents=True)
    invocation_file = raw_dir.parent / "fixture-invocations.txt"
    count = int(invocation_file.read_text(encoding="utf-8")) + 1 if invocation_file.exists() else 1
    invocation_file.write_text(str(count), encoding="utf-8")
    with Path(option("--p10-truth-tsv")).open("r", encoding="utf-8", newline="") as handle:
        truth = list(csv.DictReader(handle, delimiter="\t"))
    warmup_passes = int(option("--warmup-runs"))
    measured_passes = int(option("--repeats"))
    observations: list[dict[str, str]] = []
    summaries: dict[str, dict[str, int]] = {}
    events: list[dict[str, object]] = []
    clock = 1_000_000_000
    for phase, passes in (("warmup", warmup_passes), ("measured", measured_passes)):
        start = clock
        events.append({"phase": phase, "event": "start", "monotonic_ns": start})
        for pass_index in range(passes):
            for row in truth:
                observations.append(
                    {
                        "phase": phase,
                        "pass_index": str(pass_index),
                        "query_index": row["query_index"],
                        "edge_type": row["edge_type"],
                        "src": row["src"],
                        "expected_count": row["count"],
                        "actual_count": row["count"],
                        "expected_sum_hash": row["sum_hash"],
                        "actual_sum_hash": row["sum_hash"],
                        "expected_xor_hash": row["xor_hash"],
                        "actual_xor_hash": row["xor_hash"],
                        "status": "ok",
                        "latency_ns": "1000",
                    }
                )
                clock += 1_000
        end = clock + 1_000
        clock = end + 1_000
        events.append({"phase": phase, "event": "end", "monotonic_ns": end})
        summaries[phase] = {
            "passes": passes,
            "requested_queries": passes * len(truth),
            "completed_queries": passes * len(truth),
            "timeout_queries": 0,
            "mismatch_queries": 0,
            "started_monotonic_ns": start,
            "ended_monotonic_ns": end,
            "elapsed_ns": end - start,
        }
    columns = [
        "phase", "pass_index", "query_index", "edge_type", "src",
        "expected_count", "actual_count", "expected_sum_hash", "actual_sum_hash",
        "expected_xor_hash", "actual_xor_hash", "status", "latency_ns",
    ]
    with (raw_dir / "p10-raw-observations.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(observations)
    (raw_dir / "p10-raw-phase-events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events), encoding="utf-8"
    )
    result = {
        "schema_version": "p10-seml0-storage-bench-raw-v1",
        "clock": "CLOCK_MONOTONIC",
        "timing_boundary": "typed-neighbor-call-plus-result-materialization-and-digest-v1",
        "snapshot": 1,
        "query_count": len(truth),
        "semantic_degree_hint": "--semantic-degree-hint" in sys.argv,
        "force_signature": False,
        "per_query_timeout_ms": int(option("--p10-per-query-timeout-ms")),
        "mapping_hash": "fixture",
        "id_map_vertex_count": 3,
        **summaries,
    }
    (raw_dir / "p10-raw-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
