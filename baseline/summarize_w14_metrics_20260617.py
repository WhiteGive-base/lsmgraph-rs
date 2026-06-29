import json
import os
import sys


ROOT = sys.argv[1] if len(sys.argv) > 1 else "/data/WorkSpace/lsmgraph-rs/remote-logs/w14-sf100-minimal-reuse-20260617-codex1"
LABELS = ["property-required", "degree-class"]
VARIANTS = ["schema", "edge-type-only", "budg-b64", "semantic"]
KEYS = [
    "candidate_l0_segments",
    "filter_passed_segments",
    "body_reads",
    "body_bytes",
    "full_scan_reads",
    "bloom_filtered_segments",
]


def summarize(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    rows = data.get("benchmarks", [])
    sums = {key: 0 for key in KEYS}
    elapsed = 0
    neighbor_edges = 0
    result_entries = 0
    for row in rows:
        elapsed += row.get("get_neighbors_elapsed_ms", 0) or 0
        neighbor_edges += row.get("neighbor_edges", 0) or 0
        result_entries += row.get("result_count", 0) or 0
        metrics = (row.get("neighbor_metrics") or {}).get("csr") or {}
        for key in KEYS:
            sums[key] += metrics.get(key, 0) or 0
    return rows, elapsed, neighbor_edges, result_entries, sums


print(
    "\t".join(
        [
            "label",
            "variant",
            "n",
            "elapsed_ms_sum",
            "neighbor_edges_sum",
            "result_count_sum",
            *KEYS,
        ]
    )
)
for label in LABELS:
    for variant in VARIANTS:
        path = os.path.join(ROOT, label, f"{variant}.json")
        rows, elapsed, neighbor_edges, result_entries, sums = summarize(path)
        print(
            "\t".join(
                str(value)
                for value in [
                    label,
                    variant,
                    len(rows),
                    elapsed,
                    neighbor_edges,
                    result_entries,
                    *[sums[key] for key in KEYS],
                ]
            )
        )
