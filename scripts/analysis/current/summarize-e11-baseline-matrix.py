#!/usr/bin/env python3
"""Summarize E11 baseline-strengthening experiment logs into TSV and Markdown summaries.

This script aggregates experiment output from `remote-logs/e11-*-{DATE_TAG}/`
directories into summary CSV/TSV files suitable for paper table rendering
(`render-e11-baseline-tables.py`).

Supported input artifacts:
- `fair-*-*.json`         — storage-bench repeats (core / alltypes / full)
- `neighbor-compare-*.json` — correctness verification per variant
- `ldbc-*.json`            — LDBC query latency summaries
- `import.stdout`         — import wall time
- `file-summary.tsv`       — store / manifest size
- `time.log`              — wall time and RSS from /usr/bin/time
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

E11_VARIANTS = [
    "naive",
    "lsmgraph_style",
    "schema_only",
    "label_only",
    "edge_type_only",
    "degree_only",
    "label_edge_type",
    "label_edge_degree",
    "full_semantic",
    "benefit_scored",
    "oracle",
    "full_compact",
    "materialized",
]

DATE_TAG_DEFAULT = "20260606"


# ---------------------------------------------------------------------------
# Low-level readers
# ---------------------------------------------------------------------------

def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def number(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def parse_tsv(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    text = read_text(path)
    if text is None:
        return out
    for line in text.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def parse_time_log(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    text = read_text(path)
    if text is None:
        return out
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Elapsed (wall clock) time"):
            out["wall_time"] = line.rsplit("):", 1)[-1].strip()
        elif line.startswith("Maximum resident set size"):
            out["max_rss_kb"] = line.split(":", 1)[1].strip()
        elif line.startswith("wall_s="):
            out["wall_time"] = line.split("=", 1)[1].strip() + "s"
        elif line.startswith("maxrss_kb="):
            out["max_rss_kb"] = line.split("=", 1)[1].strip()
    return out


# ---------------------------------------------------------------------------
# Per-variant aggregation
# ---------------------------------------------------------------------------

STORAGE_BENCH_KEYS = [
    "candidate_l0_segments",
    "matched_l0_segments",
    "body_reads",
    "body_bytes",
    "read_bytes",
    "get_neighbors_ops",
    "get_neighbors_avg_us",
    "get_neighbors_p50_us",
    "get_neighbors_p90_us",
    "get_neighbors_p95_us",
    "get_neighbors_p99_us",
    "filter_passed_segments",
]


def aggregate_storage_bench(logdir: Path, variant: str) -> Dict[str, float]:
    """Read fair-*-{variant}-*.json files (3 repeats) and average them."""
    runs: List[Dict[str, Any]] = []
    for repeat in (1, 2, 3):
        for kind in ("core", "alltypes"):
            path = logdir / f"fair-{kind}-{variant}-s200-r{repeat}.json"
            data = read_json(path)
            if not isinstance(data, dict):
                continue
            # Handle list-of-benchmarks format (like summarize-p1)
            for bench in data.get("benchmarks", []):
                if not isinstance(bench, dict):
                    continue
                summary = bench.get("neighbor_summary", {})
                if not isinstance(summary, dict):
                    continue
                run: Dict[str, Any] = {"kind": kind}
                for key in STORAGE_BENCH_KEYS:
                    run[key] = number(summary.get(key))
                run["elapsed_ms"] = number(bench.get("get_neighbors_elapsed_ms"))
                run["neighbor_edges"] = number(bench.get("neighbor_edges"))
                runs.append(run)
    if not runs:
        return {}

    # Average by kind
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for run in runs:
        by_kind.setdefault(run["kind"], []).append(run)

    out: Dict[str, float] = {}
    for kind, kruns in by_kind.items():
        prefix = f"{kind}_"
        n = len(kruns)
        for key in STORAGE_BENCH_KEYS + ["elapsed_ms", "neighbor_edges"]:
            values = [r[key] for r in kruns]
            out[prefix + key] = round(sum(values) / n, 3)
    return out


def aggregate_neighbor_compare(logdir: Path, variant: str) -> Dict[str, Any]:
    """Read neighbor-compare-{variant}-r*.json files (3 repeats) and summarise mismatches."""
    reports: List[Dict[str, Any]] = []
    for repeat in (1, 2, 3):
        path = logdir / f"neighbor-compare-{variant}-r{repeat}.json"
        data = read_json(path)
        if isinstance(data, dict):
            reports.append(data)
    if not reports:
        return {}
    return {
        "checked": sum(number(r.get("checked")) for r in reports),
        "passed": sum(number(r.get("passed")) for r in reports),
        "mismatches": sum(number(r.get("mismatches")) for r in reports),
        "left_edges_total": sum(number(r.get("left_edges_total")) for r in reports),
        "right_edges_total": sum(number(r.get("right_edges_total")) for r in reports),
        "runs": len(reports),
    }


def aggregate_ldbc(logdir: Path) -> Dict[str, Any]:
    """Read ldbc-*.json files and aggregate query latency."""
    reports: List[Dict[str, Any]] = []
    for repeat in (1, 2, 3):
        data = read_json(logdir / f"ldbc-passing-s3-r{repeat}.json")
        if isinstance(data, dict):
            reports.append(data)
    if not reports:
        return {}

    total_checked = sum(number(r.get("checked")) for r in reports)
    total_passed = sum(number(r.get("passed")) for r in reports)
    total_failed = sum(number(r.get("failed")) for r in reports)

    latency_sum = 0.0
    latency_count = 0
    for report in reports:
        for item in report.get("reports", []):
            if not isinstance(item, dict):
                continue
            lat = item.get("query_latency_us", {})
            if isinstance(lat, dict):
                latency_sum += number(lat.get("sum_us"))
                latency_count += number(lat.get("count"))

    avg_us = int(latency_sum / latency_count) if latency_count else 0
    return {
        "checked": total_checked,
        "passed": total_passed,
        "failed": total_failed,
        "avg_query_us": avg_us,
        "runs": len(reports),
    }


# ---------------------------------------------------------------------------
# Write summary TSV
# ---------------------------------------------------------------------------

def variant_display_name(vid: str) -> str:
    mapping = {
        "naive": "Naive L0 scan",
        "lsmgraph_style": "LSMGraph-style",
        "schema_only": "Schema-only",
        "label_only": "Label-only",
        "edge_type_only": "Edge-type-only",
        "degree_only": "Degree-only",
        "label_edge_type": "Label+edge-type (indep. model)",
        "label_edge_degree": "Label+edge-type+degree (indep. model)",
        "full_semantic": "Full semantic",
        "benefit_scored": "Benefit-scored SemL0",
        "oracle": "Oracle",
        "full_compact": "Full L0->L1 compact",
        "materialized": "Materialized cache",
    }
    return mapping.get(vid, vid)


def variant_layout_signal(vid: str) -> str:
    mapping = {
        "naive": "L0 scan baseline",
        "lsmgraph_style": "key/range only",
        "schema_only": "src_label",
        "label_only": "src_label only",
        "edge_type_only": "edge_type only",
        "degree_only": "degree_class only",
        "label_edge_type": "computed (indep.)",
        "label_edge_degree": "computed (indep.)",
        "full_semantic": "upper bound",
        "benefit_scored": "candidate policy",
        "oracle": "theoretical upper bound",
        "full_compact": "L0 eliminated",
        "materialized": "read-optimized extreme",
    }
    return mapping.get(vid, "")


def write_summary_tsv(
    output_path: Path,
    variants: List[str],
    date_tag: str,
    root: Path,
) -> None:
    rows: List[Dict[str, str]] = []
    for vid in variants:
        logdir = root / f"remote-logs/e11-{vid}-{date_tag}"

        import_data = read_json(logdir / "import.stdout") or {}
        file_data = parse_tsv(logdir / "file-summary.tsv")
        time_data = parse_time_log(logdir / "time.log")
        storage = aggregate_storage_bench(logdir, vid)
        compare = aggregate_neighbor_compare(logdir, vid)
        ldbc = aggregate_ldbc(logdir)

        def f(key: str, default: str = "N/A") -> str:
            val = storage.get(key)
            if val is not None:
                return str(int(round(val)))
            return default

        def fs(key: str, default: str = "N/A") -> str:
            val = storage.get(key)
            if val is not None:
                return f"{val:.3f}"
            return default

        rows.append({
            "variant_id": vid,
            "variant_name": variant_display_name(vid),
            "layout_signal": variant_layout_signal(vid),
            "import_wall_time": time_data.get("wall_time", "N/A"),
            "import_max_rss_kb": time_data.get("max_rss_kb", "N/A"),
            "store_bytes": file_data.get("bytes", "N/A"),
            "manifest_bytes": file_data.get("manifest_bytes", "N/A"),
            "l0_files": file_data.get("l0_files", "N/A"),
            "l0_bytes": file_data.get("l0_bytes", "N/A"),
            "directed_edges": str(import_data.get("directed_edges", "N/A")),
            "snapshot": str(import_data.get("snapshot", "N/A")),
            # core storage bench
            "core_read_bytes": f("core_read_bytes"),
            "core_body_bytes": f("core_body_bytes"),
            "core_candidate_l0_segments": f("core_candidate_l0_segments"),
            "core_matched_l0_segments": f("core_matched_l0_segments"),
            "core_elapsed_ms": fs("core_elapsed_ms"),
            "core_get_neighbors_avg_us": fs("core_get_neighbors_avg_us"),
            "core_get_neighbors_p50_us": fs("core_get_neighbors_p50_us"),
            "core_get_neighbors_p95_us": fs("core_get_neighbors_p95_us"),
            "core_get_neighbors_p99_us": fs("core_get_neighbors_p99_us"),
            # alltypes storage bench
            "alltypes_read_bytes": f("alltypes_read_bytes"),
            "alltypes_body_bytes": f("alltypes_body_bytes"),
            "alltypes_candidate_l0_segments": f("alltypes_candidate_l0_segments"),
            "alltypes_elapsed_ms": fs("alltypes_elapsed_ms"),
            "alltypes_get_neighbors_avg_us": fs("alltypes_get_neighbors_avg_us"),
            "alltypes_get_neighbors_p50_us": fs("alltypes_get_neighbors_p50_us"),
            "alltypes_get_neighbors_p95_us": fs("alltypes_get_neighbors_p95_us"),
            "alltypes_get_neighbors_p99_us": fs("alltypes_get_neighbors_p99_us"),
            # correctness
            "neighbor_checked": str(int(compare.get("checked", 0))),
            "neighbor_passed": str(int(compare.get("passed", 0))),
            "neighbor_mismatches": str(int(compare.get("mismatches", -1))),
            "neighbor_runs": str(compare.get("runs", 0)),
            # ldbc
            "ldbc_checked": str(int(ldbc.get("checked", 0))),
            "ldbc_passed": str(int(ldbc.get("passed", 0))),
            "ldbc_failed": str(int(ldbc.get("failed", -1))),
            "ldbc_avg_query_us": str(int(ldbc.get("avg_query_us", 0))),
            "ldbc_runs": str(ldbc.get("runs", 0)),
        })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        output_path.write_text("# no data found\n", encoding="utf-8")
        return

    fieldnames = list(rows[0].keys())
    with output_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Markdown summary
# ---------------------------------------------------------------------------

def write_markdown_summary(output_path: Path, variants: List[str], date_tag: str, root: Path) -> None:
    rows: List[Dict[str, str]] = []
    for vid in variants:
        logdir = root / f"remote-logs/e11-{vid}-{date_tag}"
        file_data = parse_tsv(logdir / "file-summary.tsv")
        storage = aggregate_storage_bench(logdir, vid)
        rows.append({
            "variant": variant_display_name(vid),
            "l0_files": file_data.get("l0_files", "N/A"),
            "store_bytes": file_data.get("bytes", "N/A"),
            "core_read_bytes": str(int(round(storage.get("core_read_bytes", 0)))),
            "alltypes_read_bytes": str(int(round(storage.get("alltypes_read_bytes", 0)))),
        })

    lines: List[str] = [
        "# E11 Baseline-Strengthening Summary",
        "",
        f"Date tag: `{date_tag}`",
        "",
        "## Storage Cost",
        "",
        "| Variant | L0 files | Store bytes | Core read bytes | All-types read bytes |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['variant']} | {row['l0_files']} | {row['store_bytes']} | "
            f"{row['core_read_bytes']} | {row['alltypes_read_bytes']} |"
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize E11 baseline-strengthening experiment logs into TSV/Markdown.",
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--date-tag", default=DATE_TAG_DEFAULT)
    parser.add_argument(
        "--variants",
        nargs="+",
        default=E11_VARIANTS,
        help="List of variant IDs to process.",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Output directory. Defaults to remote-logs/e11-paper-tables-{DATE_TAG}/.",
    )
    args = parser.parse_args()

    root = Path(args.root)
    date = args.date_tag
    out_dir = root / (
        str(args.out_dir) if args.out_dir
        else f"remote-logs/e11-paper-tables-{date}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    # Write main TSV
    tsv_path = out_dir / "e11-mechanism-isolation.tsv"
    write_summary_tsv(tsv_path, args.variants, date, root)
    print(tsv_path)

    # Write markdown summary
    md_path = out_dir / "e11-summary.md"
    write_markdown_summary(md_path, args.variants, date, root)
    print(md_path)

    # Copy TSV as the normalized CSV that render-e11-baseline-tables.py expects
    normalized_dir = root / f"remote-logs/e11-normalized-{date}"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy(tsv_path, normalized_dir / "e11-mechanism-isolation.csv")
    print(normalized_dir / "e11-mechanism-isolation.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
