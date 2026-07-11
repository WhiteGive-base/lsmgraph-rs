#!/usr/bin/env python3
"""Render a W6 scale matrix report from raw runner artifacts.

The script is intentionally conservative: it reads only JSON/TSV/stderr
artifacts produced by baseline/run_w6_sf100_matrix_20260613.sh and labels
missing variants as PENDING. It does not infer measured values from models.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


VARIANT_ORDER = [
    "schema",
    "naive",
    "kv-lsm",
    "edge-type-only",
    "semantic",
    "budg-b64",
    "budg-b256",
    "budg-b1024",
    "oracle",
]

SUM_METRICS = [
    "read_bytes",
    "body_reads",
    "body_bytes",
    "candidate_l0_segments",
    "matched_l0_segments",
    "filter_passed_segments",
    "bloom_filtered_segments",
    "range_filtered_segments",
    "header_reads",
    "get_neighbors_latency_sum_us",
    "get_neighbors_elapsed_ms",
    "neighbor_edges",
]

PERCENTILE_METRICS = [
    "get_neighbors_p50_us",
    "get_neighbors_p90_us",
    "get_neighbors_p99_us",
]


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def mean(values: Sequence[float]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not vals:
        return None
    return sum(vals) / len(vals)


def stddev(values: Sequence[float]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if len(vals) < 2:
        return 0.0 if vals else None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


def fmt_num(value: Optional[float], digits: int = 1) -> str:
    if value is None:
        return "PENDING"
    if abs(value) >= 1000:
        return f"{value:,.{digits}f}"
    return f"{value:.{digits}f}"


def fmt_int(value: Optional[float]) -> str:
    if value is None:
        return "PENDING"
    return f"{int(round(value)):,}"


def fmt_mean_std(avg: Optional[float], sd: Optional[float], digits: int = 1) -> str:
    if avg is None:
        return "PENDING"
    return f"{fmt_num(avg, digits)} +/- {fmt_num(sd or 0.0, digits)}"


def fmt_bytes_gib(value: Optional[float]) -> str:
    if value is None:
        return "PENDING"
    return f"{value / (1024 ** 3):.1f}"


def fmt_mib(value: Optional[float]) -> str:
    if value is None:
        return "PENDING"
    return f"{value / (1024 ** 2):,.1f}"


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |"]
    out.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(out)


def infer_scale(log_dir: Path) -> str:
    match = re.search(r"(?:^|[-_/])sf(\d+)(?:[-_/]|$)", str(log_dir), re.IGNORECASE)
    return f"SF{match.group(1)}" if match else "unknown-scale"


def read_manifest(log_dir: Path) -> Dict[Tuple[str, str], Dict[str, str]]:
    path = log_dir / "manifest.tsv"
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    return {(row.get("variant", ""), row.get("kind", "")): row for row in rows}


def max_store_bytes(manifest: Dict[Tuple[str, str], Dict[str, str]], variant: str) -> Optional[float]:
    vals: List[float] = []
    for kind in ("import", "bench", "compare"):
        text = manifest.get((variant, kind), {}).get("store_size_bytes", "")
        try:
            vals.append(float(text))
        except ValueError:
            pass
    return max(vals) if vals else None


def parse_import_rss_kib(log_dir: Path, variant: str) -> Optional[int]:
    path = log_dir / f"{variant}-import.stderr"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", text)
    return int(match.group(1)) if match else None


def round_ops(round_obj: Dict[str, Any], bench_obj: Dict[str, Any]) -> int:
    summary = round_obj.get("neighbor_summary") or {}
    if "get_neighbors_ops" in summary:
        return int(summary["get_neighbors_ops"])
    summary = bench_obj.get("neighbor_summary") or {}
    if "get_neighbors_ops" in summary:
        return int(summary["get_neighbors_ops"])
    return len(bench_obj.get("sampled_srcs") or [])


def round_summary_value(round_obj: Dict[str, Any], key: str) -> Optional[float]:
    summary = round_obj.get("neighbor_summary") or {}
    if key in summary:
        return float(summary[key])
    if key in round_obj:
        return float(round_obj[key])
    return None


def aggregate_bench(path: Path) -> Optional[Dict[str, Any]]:
    data = read_json(path)
    if data is None:
        return None
    benchmarks = data.get("benchmarks") or []
    if not benchmarks:
        return None

    round_totals: Dict[int, Dict[str, float]] = {}
    round_percentiles: Dict[int, Dict[str, List[float]]] = {}
    for bench in benchmarks:
        for round_obj in bench.get("rounds") or []:
            idx = int(round_obj.get("round", len(round_totals) + 1))
            totals = round_totals.setdefault(idx, {"ops": 0.0})
            totals["ops"] += round_ops(round_obj, bench)
            for metric in SUM_METRICS:
                value = round_summary_value(round_obj, metric)
                if value is not None:
                    totals[metric] = totals.get(metric, 0.0) + value
            percentile_lists = round_percentiles.setdefault(idx, {m: [] for m in PERCENTILE_METRICS})
            for metric in PERCENTILE_METRICS:
                value = round_summary_value(round_obj, metric)
                if value is not None:
                    percentile_lists[metric].append(value)

    rounds = [round_totals[k] for k in sorted(round_totals)]
    if not rounds:
        return None

    ops_values = [r.get("ops", 0.0) for r in rounds]
    latency_avg_us_values = []
    for r in rounds:
        ops = r.get("ops", 0.0)
        latency_sum = r.get("get_neighbors_latency_sum_us")
        elapsed_ms = r.get("get_neighbors_elapsed_ms")
        if ops > 0 and latency_sum is not None:
            latency_avg_us_values.append(latency_sum / ops)
        elif ops > 0 and elapsed_ms is not None:
            latency_avg_us_values.append(elapsed_ms * 1000.0 / ops)

    percentile_means: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
    for metric in PERCENTILE_METRICS:
        per_round = []
        for idx in sorted(round_percentiles):
            per_edge = round_percentiles[idx][metric]
            avg = mean(per_edge)
            if avg is not None:
                per_round.append(avg)
        percentile_means[metric] = (mean(per_round), stddev(per_round))

    levels = data.get("levels") or []
    l0_files = float(levels[0]) if levels else None

    out: Dict[str, Any] = {
        "path": str(path),
        "l0_layout": data.get("l0_layout", ""),
        "edge_types": ",".join(str(x) for x in data.get("edge_types", [])),
        "repeat_count": len(rounds),
        "ops_mean": mean(ops_values),
        "ops_stddev": stddev(ops_values),
        "avg_us_mean": mean(latency_avg_us_values),
        "avg_us_stddev": stddev(latency_avg_us_values),
        "l0_files": l0_files,
        "levels": ",".join(str(x) for x in levels),
        "cache_state_before": data.get("cache_state_before"),
        "cache_state_after": data.get("cache_state_after"),
    }
    for metric in SUM_METRICS:
        values = [r.get(metric) for r in rounds if metric in r]
        out[f"{metric}_mean"] = mean(values)
        out[f"{metric}_stddev"] = stddev(values)
    for metric, (avg, sd) in percentile_means.items():
        out[f"{metric}_edge_mean"] = avg
        out[f"{metric}_edge_stddev"] = sd
    return out


def read_compare(log_dir: Path, variant: str) -> Optional[Dict[str, Any]]:
    path = log_dir / f"compare-naive-vs-{variant}.json"
    data = read_json(path)
    if data is None:
        return None
    return data


def parse_resource_monitor(log_dir: Path) -> Dict[str, Any]:
    path = log_dir / "resource-monitor.tsv"
    if not path.exists():
        return {}
    mem: List[float] = []
    disk: List[float] = []
    store: List[str] = []
    first_ts = last_ts = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or not parts[0].startswith("20"):
            continue
        first_ts = first_ts or parts[0]
        last_ts = parts[0]
        try:
            mem.append(float(parts[1]))
            disk.append(float(parts[2]))
        except ValueError:
            pass
        store.append(parts[3])
    return {
        "first_ts": first_ts,
        "last_ts": last_ts,
        "mem_min_gib": min(mem) if mem else None,
        "disk_min_gib": min(disk) if disk else None,
        "store_last": store[-1] if store else None,
        "samples": len(mem),
    }


def compare_status(compare: Optional[Dict[str, Any]]) -> str:
    if compare is None:
        return "PENDING"
    mismatches = compare.get("mismatches")
    checked = compare.get("checked")
    if mismatches == 0 and checked and checked > 0:
        return f"PASS ({checked:,})"
    return f"FAIL mismatches={mismatches} checked={checked}"


def build_variant_rows(
    log_dir: Path,
    manifest: Dict[Tuple[str, str], Dict[str, str]],
) -> Tuple[List[List[str]], Dict[str, Dict[str, Any]]]:
    metrics: Dict[str, Dict[str, Any]] = {}
    rows: List[List[str]] = []
    for variant in VARIANT_ORDER:
        bench = aggregate_bench(log_dir / f"{variant}-bench.json")
        if bench:
            metrics[variant] = bench
        compare = None if variant == "naive" else read_compare(log_dir, variant)
        rss_kib = parse_import_rss_kib(log_dir, variant)
        store_bytes = max_store_bytes(manifest, variant)
        if bench is None:
            rows.append([variant, "PENDING", fmt_bytes_gib(store_bytes), "PENDING", "PENDING", "PENDING", "PENDING", "PENDING", "PENDING", compare_status(compare)])
            continue
        rows.append(
            [
                variant,
                bench.get("l0_layout") or "",
                fmt_bytes_gib(store_bytes),
                fmt_int(bench.get("l0_files")),
                fmt_int(bench.get("ops_mean")),
                fmt_mean_std(
                    bench.get("candidate_l0_segments_mean"),
                    bench.get("candidate_l0_segments_stddev"),
                    0,
                ),
                fmt_mean_std(
                    bench.get("read_bytes_mean"),
                    bench.get("read_bytes_stddev"),
                    0,
                ),
                fmt_mean_std(bench.get("avg_us_mean"), bench.get("avg_us_stddev"), 1),
                "/".join(
                    [
                        fmt_mean_std(bench.get("get_neighbors_p50_us_edge_mean"), bench.get("get_neighbors_p50_us_edge_stddev"), 0),
                        fmt_mean_std(bench.get("get_neighbors_p90_us_edge_mean"), bench.get("get_neighbors_p90_us_edge_stddev"), 0),
                        fmt_mean_std(bench.get("get_neighbors_p99_us_edge_mean"), bench.get("get_neighbors_p99_us_edge_stddev"), 0),
                    ]
                ),
                compare_status(compare) if variant != "naive" else "anchor",
            ]
        )
        if rss_kib is not None:
            metrics[variant]["import_rss_kib"] = rss_kib
    return rows, metrics


def build_compare_rows(log_dir: Path) -> List[List[str]]:
    rows: List[List[str]] = []
    for variant in VARIANT_ORDER:
        if variant == "naive":
            continue
        data = read_compare(log_dir, variant)
        if data is None:
            rows.append([variant, "PENDING", "PENDING", "PENDING", "PENDING", "PENDING"])
            continue
        rows.append(
            [
                variant,
                fmt_int(float(data.get("checked", 0))),
                fmt_int(float(data.get("passed", 0))),
                str(data.get("mismatches")),
                fmt_num(float(data.get("elapsed_ms", 0)) / 1000.0, 1),
                "yes" if data.get("left_snapshot") == data.get("right_snapshot") else "no",
            ]
        )
    return rows


def gate_1(metrics: Dict[str, Dict[str, Any]]) -> str:
    schema = metrics.get("schema")
    budg = metrics.get("budg-b64")
    if not schema or not budg:
        return "PENDING: requires schema and budg-b64 bench results."
    s_mean = schema.get("avg_us_mean")
    b_mean = budg.get("avg_us_mean")
    s_sd = schema.get("avg_us_stddev") or 0.0
    b_sd = budg.get("avg_us_stddev") or 0.0
    if s_mean is None or b_mean is None:
        return "PENDING: latency fields missing."
    delta = (s_mean - b_mean) / s_mean * 100.0 if s_mean > 0 else 0.0
    stable = (b_mean + b_sd) < (s_mean - s_sd)
    verdict = "GO" if stable else "FALLBACK"
    return (
        f"{verdict}: budg-b64 avg_us={b_mean:.1f}+/-{b_sd:.1f}, "
        f"schema avg_us={s_mean:.1f}+/-{s_sd:.1f}, delta={delta:.2f}%. "
        "GO requires the 1-stddev intervals not to overlap."
    )


def gate_2(metrics: Dict[str, Dict[str, Any]]) -> str:
    naive = metrics.get("naive", {}).get("import_rss_kib")
    schema = metrics.get("schema", {}).get("import_rss_kib")
    budg = metrics.get("budg-b64", {}).get("import_rss_kib")
    if not naive or not schema or not budg:
        return "PENDING: requires import RSS for naive, schema, and budg-b64."
    schema_ratio = schema / naive
    budg_ratio = budg / naive
    verdict = "GO" if schema_ratio <= 2.0 and budg_ratio <= 2.0 else "FALLBACK"
    return (
        f"{verdict}: schema/naive RSS={schema_ratio:.2f}x, "
        f"budg-b64/naive RSS={budg_ratio:.2f}x. "
        "Heuristic threshold is <=2.0x; final text should describe measured RSS exactly."
    )


def render_report(log_dir: Path, out_path: Path) -> str:
    manifest = read_manifest(log_dir)
    variant_rows, metrics = build_variant_rows(log_dir, manifest)
    compare_rows = build_compare_rows(log_dir)
    resource = parse_resource_monitor(log_dir)
    done = (log_dir / "DONE").exists()
    failed = (log_dir / "FAILED").exists()
    status = "DONE" if done else "FAILED" if failed else "RUNNING/PARTIAL"
    generated = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    scale = infer_scale(log_dir)

    if resource:
        resource_block = [
            "## Resource Envelope",
            "",
            markdown_table(
                ["first sample", "last sample", "samples", "min MemAvailable GiB", "min /data free GiB", "last W6 store"],
                [
                    [
                        resource.get("first_ts", "PENDING"),
                        resource.get("last_ts", "PENDING"),
                        resource.get("samples", "PENDING"),
                        fmt_num(resource.get("mem_min_gib"), 0),
                        fmt_num(resource.get("disk_min_gib"), 0),
                        resource.get("store_last", "PENDING"),
                    ]
                ],
            ),
            "",
        ]
    else:
        resource_block = [
            "## Resource Envelope",
            "",
            "No `resource-monitor.tsv` was recorded for this run. Per-variant import RSS below remains measured from `/usr/bin/time -v`; no all-PENDING envelope is emitted.",
            "",
        ]

    lines = [
        f"# W6 {scale} Matrix Report",
        "",
        f"Generated: {generated}",
        f"Run dir: `{log_dir}`",
        f"Status: `{status}`",
        "",
        "This report is regenerated from raw W6 JSON/TSV artifacts. Missing variants are labeled `PENDING`; no simulated/model-derived rows are promoted to measured results.",
        "",
        *resource_block,
        "## Aggregate Matrix",
        "",
        "The p50/p90/p99 column is the mean of per-edge-type percentiles across repeats; it is not a raw global percentile over all operations.",
        "",
        markdown_table(
            [
                "variant",
                "layout",
                "store GiB",
                "L0 files",
                "ops/repeat",
                "candidate L0 mean+/-std",
                "read bytes mean+/-std",
                "avg us mean+/-std",
                "p50/p90/p99 edge-mean us",
                "compare vs naive",
            ],
            variant_rows,
        ),
        "",
        "## Correctness Compares",
        "",
        markdown_table(
            ["variant", "checked", "passed", "mismatches", "elapsed s", "same snapshot"],
            compare_rows,
        ),
        "",
        "## Import RSS",
        "",
        markdown_table(
            ["variant", "max RSS GiB"],
            [
                [
                    variant,
                    fmt_num((parse_import_rss_kib(log_dir, variant) or 0) / (1024 ** 2), 2)
                    if parse_import_rss_kib(log_dir, variant)
                    else "PENDING",
                ]
                for variant in VARIANT_ORDER
            ],
        ),
        "",
        "## Gate Candidates",
        "",
        f"- Gate 1 latency: {gate_1(metrics)}",
        f"- Gate 2 RSS: {gate_2(metrics)}",
        "",
        "## Required Manual Checks Before Paper Use",
        "",
        "- Confirm `DONE` exists before treating this as final.",
        "- Keep `kv-lsm` as measured only if its import, bench, compare, and manifest rows are present.",
        "- If Gate 1 is FALLBACK, write latency claims as preliminary or omit latency advantage.",
        "- If Gate 2 is FALLBACK, move RSS to caveat/limitation rather than a main claim.",
        "- Reconcile any reader over-read caveat before making read_bytes a primary claim.",
        "",
    ]
    text = "\n".join(lines)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    render_report(args.log_dir, args.out)
    print(args.out)


if __name__ == "__main__":
    main()
