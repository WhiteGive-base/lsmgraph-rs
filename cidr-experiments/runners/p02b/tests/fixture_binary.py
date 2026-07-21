#!/usr/bin/env python3
"""Small deterministic fake binary used only by the P02B fixture smoke."""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List


def option(args: List[str], name: str) -> str:
    try:
        index = args.index(name)
    except ValueError as exc:
        raise SystemExit("missing option {}".format(name)) from exc
    if index + 1 >= len(args):
        raise SystemExit("missing value for {}".format(name))
    return args[index + 1]


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_dense_to_original(id_map_dir: Path) -> Dict[int, int]:
    result: Dict[int, int] = {}
    with (id_map_dir / "dense-to-original.tsv").open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            result[int(row["dense_id"])] = int(row["original_id"])
    return result


def read_truth(path: Path) -> List[Dict[str, int]]:
    rows: List[Dict[str, int]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            rows.append({key: int(value) for key, value in row.items()})
    return rows


def build_plan(truth: List[Dict[str, int]], ids: Dict[int, int], semantic: bool, force: bool) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    for row in truth:
        edge_type = row["edge_type"]
        if not entries or entries[-1]["edge_type"] != edge_type:
            entries.append(
                {
                    "candidate_edges_for_sampling": 0,
                    "candidate_sources_for_sampling": 0,
                    "dst_label": None,
                    "edge_type": edge_type,
                    "samples": [],
                    "src_label": None,
                }
            )
        entry = entries[-1]
        entry["candidate_edges_for_sampling"] += row["count"]
        entry["candidate_sources_for_sampling"] += 1
        entry["samples"].append({"degree": row["count"], "src": ids[row["src"]]})
    return {
        "dst_label": None,
        "entries": entries,
        "force_signature": force,
        "samples_per_edge_type": max(len(entry["samples"]) for entry in entries),
        "semantic_degree_hint": semantic,
        "source": "shared-truth-tsv",
        "src_label": None,
        "version": 1,
    }


def shared_truth_verify(args: List[str]) -> int:
    truth_path = Path(option(args, "--truth-tsv")).resolve()
    id_map_dir = Path(option(args, "--id-map-dir")).resolve()
    output_path = Path(option(args, "--output")).resolve()
    plan_path = Path(option(args, "--sample-plan-out")).resolve()
    store_path = Path(option(args, "--data-dir")).resolve()
    expected = int(option(args, "--expected-queries"))
    rows = read_truth(truth_path)
    id_manifest = json.loads((id_map_dir / "id-map-manifest.json").read_text(encoding="utf-8"))
    if len(rows) != expected:
        return 3
    plan = build_plan(rows, read_dense_to_original(id_map_dir), "--semantic-degree-hint" in args, "--force-signature" in args)
    write_json(plan_path, plan)
    write_json(
        output_path,
        {
            "consumer": "seml0-shared-truth-v1",
            "correctness_only": True,
            "data_dir": str(store_path),
            "force_signature": "--force-signature" in args,
            "id_map_dir": str(id_map_dir),
            "performance_eligible": False,
            "sample_plan_out": str(plan_path),
            "semantic_degree_hint": "--semantic-degree-hint" in args,
            "truth_rows": len(rows),
            "truth_tsv": str(truth_path),
            "verification": {
                "checked": len(rows),
                "mapping_hash": id_manifest["mapping_hash"],
                "mismatches": 0,
                "sample_mismatches": [],
                "status": "PASS",
                "total_neighbors": sum(row["count"] for row in rows),
            },
        },
    )
    time.sleep(0.15)
    return 0


def latency_metrics(ops: int) -> Dict[str, Any]:
    return {
        "get_neighbors_latency": {
            "buckets": [
                {"count": 0, "upper_bound_us": 50},
                {"count": ops, "upper_bound_us": 100},
                {"count": 0, "upper_bound_us": 200},
            ],
            "count": ops,
            "max_us": 90,
            "min_us": 60,
            "p50_us": 100,
            "p90_us": 100,
            "p99_us": 100,
            "sum_us": ops * 75,
        },
        "get_neighbors_ops": ops,
    }


def storage_bench(args: List[str]) -> int:
    plan_path = Path(option(args, "--sample-plan-in")).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    warmup_runs = int(option(args, "--warmup-runs"))
    repeats = int(option(args, "--repeats"))
    run_index = int(os.environ.get("P02B_SENTINEL_RUN_INDEX", "1"))
    # QPS varies by less than 2%; P99 stays in the same frozen histogram bucket.
    elapsed_base = {1: 100, 2: 101, 3: 99}.get(run_index, 100)
    benchmarks: List[Dict[str, Any]] = []
    for entry in plan["entries"]:
        ops = len(entry["samples"])
        warmups = [
            {"kind": "warmup", "round": index + 1}
            for index in range(warmup_runs)
        ]
        rounds = []
        for index in range(repeats):
            rounds.append(
                {
                    "get_neighbors_elapsed_ms": elapsed_base,
                    "kind": "measured",
                    "neighbor_metrics": {"storage": latency_metrics(ops)},
                    "round": index + 1,
                }
            )
        benchmarks.append(
            {
                "edge_type": entry["edge_type"],
                "repeats": repeats,
                "rounds": rounds,
                "warmup_rounds": warmups,
                "warmup_runs": warmup_runs,
            }
        )
    value = {
        "benchmarks": benchmarks,
        "cache_state_after": {"fixture": "warm"},
        "cache_state_before": {"fixture": "as-is"},
        "repeats": repeats,
        "sample_plan_in": str(plan_path),
        "sample_plan_version": 1,
        "warmup_runs": warmup_runs,
    }
    # Keep the fake process alive long enough for at least two P31 /proc samples.
    time.sleep(2.2)
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "shared-truth-verify" in args:
        return shared_truth_verify(args)
    if "storage-bench" in args:
        return storage_bench(args)
    print("fixture binary only supports shared-truth-verify and storage-bench", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
