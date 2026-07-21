#!/usr/bin/env python3
"""Emit a deterministic storage-bench-shaped JSON fixture."""

import json
import os
import sys
import time


def value(argv, name, default=None):
    if name not in argv:
        return default
    index = argv.index(name)
    return argv[index + 1]


def main():
    argv = sys.argv[1:]
    fixture_sleep = float(os.environ.get("P20_FIXTURE_SLEEP", "0"))
    if fixture_sleep > 0:
        time.sleep(fixture_sleep)
    required = ["storage-bench", "--data-dir", "--sample-plan-in", "--query-control-stage"]
    if any(token not in argv for token in required) or "--auto-compact" in argv:
        return 64
    if value(argv, "--repeats") != "1":
        return 65
    stage = value(argv, "--query-control-stage").upper()
    stage_index = int(stage[1:])
    query_cpu = "--query-cpu-phases" in argv
    automatic = "--automatic-maintenance" in argv
    switches = {
        "exact_evidence_admission": stage_index >= 1,
        "semantic_routing": stage_index >= 2,
        "budgeted_degree_promotion": stage_index >= 3,
        "feedback_priority": stage_index >= 4,
        "semantic_compaction": stage_index >= 5,
        "automatic_maintenance": automatic,
        "query_cpu_phase_instrumentation": query_cpu,
        "post_round_feedback_compact": False,
    }
    phase = {
        "query_cpu_query_setup_ns": 101 if query_cpu else 0,
        "query_cpu_metadata_admission_ns": 202 if query_cpu else 0,
        "query_cpu_routing_index_ns": 303 if query_cpu else 0,
        "query_cpu_body_decode_filter_ns": 404 if query_cpu else 0,
        "query_cpu_mvcc_result_ns": 505 if query_cpu else 0,
        "query_cpu_clock_failures": 0,
    }
    neighbor_summary = {
        "get_neighbors_ops": 2,
        "get_neighbors_avg_us": 50,
        "get_neighbors_p50_us": 40,
        "get_neighbors_p90_us": 70,
        "get_neighbors_p99_us": 90,
        "candidate_l0_segments": 22,
        "routed_l0_segments": 7,
        "body_reads": 5,
        "read_bytes": 8192,
        "body_bytes": 4096,
    }
    neighbor_summary.update(phase)
    measured = {
        "kind": "measured",
        "round": 1,
        "get_neighbors_elapsed_ms": 2,
        "neighbor_edges": 9,
        "query_cpu_total_ns": 2000 if query_cpu else None,
        "neighbor_summary": neighbor_summary,
        "neighbor_metrics": {
            "storage": {
                "get_neighbors_latency": {
                    "count": 2,
                    "sum_us": 100,
                    "min_us": 40,
                    "max_us": 60,
                    "avg_us": 50,
                    "p50_us": 50,
                    "p90_us": 100,
                    "p99_us": 100,
                    "buckets": [
                        {"upper_bound_us": 50, "count": 1},
                        {"upper_bound_us": 100, "count": 1},
                    ],
                }
            }
        },
    }
    measured.update(phase)
    output = {
        "data_dir": value(argv, "--data-dir"),
        "query_control_stage": stage,
        "l0_layout": "SemanticBudgeted",
        "sample_plan_version": 1,
        "scan_requested": False,
        "emit_result_digests": "--emit-result-digests" in argv,
        "repeats": 1,
        "warmup_runs": int(value(argv, "--warmup-runs", "0")),
        "training_runs": int(value(argv, "--training-runs", "0")),
        "training_feedback_compactions": int(value(argv, "--training-feedback-compactions", "0")),
        "workload_mode": value(argv, "--workload-mode", "one-hop").replace("-", "_"),
        "property_predicate_mode": value(argv, "--property-predicate-mode", "none").replace("-", "_"),
        "property_id": int(value(argv, "--property-id", "0")),
        "feature_switches": switches,
        "cache_state_before": {"fixture": "cold"},
        "cache_state_after": {"fixture": "warm"},
        "benchmarks": [
            {
                "edge_type": 1,
                "src_label": 1,
                "dst_label": 2,
                "sampled_vertices": 2,
                "warmup_runs": int(value(argv, "--warmup-runs", "0")),
                "repeats": 1,
                "rounds": [measured],
                "emit_result_digests": True,
                "entry_result_digest": "0123456789abcdef",
                "result_digests": [
                    {"src": 1, "result_count": 4, "result_digest": "1111111111111111"},
                    {"src": 2, "result_count": 5, "result_digest": "2222222222222222"},
                ],
            }
        ],
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
