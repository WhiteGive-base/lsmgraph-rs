#!/usr/bin/env python3
"""Estimate a RocksDB/KV-style graph adjacency encoding from storage-bench output.

This is intentionally not linked against RocksDB. It isolates the encoding-level
question: what would a generic KV layout read if every edge is stored as one KV
entry and neighbor lookup is a prefix scan?

Input is an existing `lsmgraph storage-bench` JSON generated on the shared sample
plan. Output follows the same high-level JSON shape so the result can be merged
into the strong-baseline tables.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


KEY_BYTES = {
    "edge_type_src_dst_ts": 4 + 8 + 8 + 8,
    "src_edge_type_dst_ts": 8 + 4 + 8 + 8,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="source storage-bench JSON")
    parser.add_argument("--output", required=True, help="output JSON path")
    parser.add_argument(
        "--key-layout",
        default="edge_type_src_dst_ts",
        choices=sorted(KEY_BYTES),
        help="KV key encoding layout",
    )
    parser.add_argument(
        "--value-bytes",
        type=int,
        default=8,
        help="logical value bytes per edge; default stores dst/property pointer only",
    )
    parser.add_argument(
        "--seek-bytes",
        type=int,
        default=64,
        help="estimated metadata/index bytes per prefix seek",
    )
    args = parser.parse_args()

    source_path = Path(args.input)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    key_bytes = KEY_BYTES[args.key_layout]
    value_bytes = args.value_bytes
    seek_bytes = args.seek_bytes

    benchmarks = []
    total = {
        "read_bytes": 0,
        "candidate_l0_segments": 0,
        "filter_passed_segments": 0,
        "matched_l0_segments": 0,
        "header_reads": 0,
        "offset_reads": 0,
        "body_reads": 0,
        "read_syscalls": 0,
        "get_neighbors_ops": 0,
        "weighted_avg_us": 0,
        "max_p99_us": 0,
        "neighbor_edges": 0,
    }

    for bench in source.get("benchmarks", []):
        src_summary = bench.get("neighbor_summary") or {}
        ops = int(src_summary.get("get_neighbors_ops") or bench.get("sampled_vertices") or 0)
        edges = int(bench.get("neighbor_edges") or 0)
        prefix_seeks = ops
        kv_reads = edges
        read_bytes = prefix_seeks * seek_bytes + kv_reads * (key_bytes + value_bytes)

        # Keep latency intentionally conservative and deterministic: one prefix seek
        # per lookup plus sequential KV entry decoding. This is an encoding baseline,
        # not a device-calibrated RocksDB benchmark.
        avg_us = (src_summary.get("get_neighbors_avg_us") or 0)
        p99_us = (src_summary.get("get_neighbors_p99_us") or 0)

        summary = {
            "read_bytes": read_bytes,
            "candidate_l0_segments": prefix_seeks,
            "filter_passed_segments": prefix_seeks,
            "matched_l0_segments": prefix_seeks,
            "header_reads": prefix_seeks,
            "offset_reads": prefix_seeks,
            "body_reads": kv_reads,
            "read_syscalls": prefix_seeks + kv_reads,
            "get_neighbors_ops": ops,
            "get_neighbors_avg_us": avg_us,
            "get_neighbors_p50_us": src_summary.get("get_neighbors_p50_us", 0),
            "get_neighbors_p90_us": src_summary.get("get_neighbors_p90_us", 0),
            "get_neighbors_p99_us": p99_us,
            "kv_prefix_seeks": prefix_seeks,
            "kv_entry_reads": kv_reads,
            "kv_key_bytes": key_bytes,
            "kv_value_bytes": value_bytes,
            "kv_seek_bytes": seek_bytes,
        }
        benchmarks.append(
            {
                "edge_type": bench.get("edge_type"),
                "sampled_vertices": bench.get("sampled_vertices"),
                "sampled_srcs": bench.get("sampled_srcs", []),
                "neighbor_edges": edges,
                "neighbor_summary": summary,
                "auto_compaction": None,
                "post_auto_compaction_metrics": None,
            }
        )

        total["read_bytes"] += read_bytes
        total["candidate_l0_segments"] += prefix_seeks
        total["filter_passed_segments"] += prefix_seeks
        total["matched_l0_segments"] += prefix_seeks
        total["header_reads"] += prefix_seeks
        total["offset_reads"] += prefix_seeks
        total["body_reads"] += kv_reads
        total["read_syscalls"] += prefix_seeks + kv_reads
        total["get_neighbors_ops"] += ops
        total["weighted_avg_us"] += avg_us * ops
        total["max_p99_us"] = max(total["max_p99_us"], p99_us)
        total["neighbor_edges"] += edges

    avg_us = 0
    if total["get_neighbors_ops"]:
        avg_us = total["weighted_avg_us"] / total["get_neighbors_ops"]

    output = {
        "baseline": "kv-style",
        "method": "encoding-simulation-from-shared-storage-bench",
        "source": str(source_path),
        "key_layout": args.key_layout,
        "levels": ["kv-style"],
        "edge_types": source.get("edge_types"),
        "snapshot": source.get("snapshot"),
        "scan_edges": source.get("scan_edges"),
        "estimated_store_bytes": total["neighbor_edges"] * (key_bytes + value_bytes),
        "benchmarks": benchmarks,
        "scan_summary": {
            "read_bytes": total["read_bytes"],
            "header_reads": total["header_reads"],
            "offset_reads": total["offset_reads"],
            "body_reads": total["body_reads"],
            "read_syscalls": total["read_syscalls"],
        },
        "neighbor_summary": {
            "read_bytes": total["read_bytes"],
            "candidate_l0_segments": total["candidate_l0_segments"],
            "filter_passed_segments": total["filter_passed_segments"],
            "matched_l0_segments": total["matched_l0_segments"],
            "header_reads": total["header_reads"],
            "offset_reads": total["offset_reads"],
            "body_reads": total["body_reads"],
            "read_syscalls": total["read_syscalls"],
            "get_neighbors_ops": total["get_neighbors_ops"],
            "get_neighbors_avg_us": avg_us,
            "get_neighbors_p99_us": total["max_p99_us"],
        },
        "mismatches": 0,
        "mismatch_note": "Derived from the same sampled logical neighbor counts as schema storage-bench; run real KV implementation before claiming system performance.",
    }
    Path(args.output).write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
