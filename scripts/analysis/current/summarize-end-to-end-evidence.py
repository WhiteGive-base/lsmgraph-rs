#!/usr/bin/env python3
"""Summarize end-to-end and maintenance-cost evidence for SemL0.

This script is intentionally evidence-oriented. It does not run benchmarks; it
normalizes already-captured artifacts into TSV/Markdown tables that can be used
by the paper draft.

Supported inputs:

* LDBC driver case directories from deps/ldbc_snb_interactive_impls/lsmgraph/
  run_high_thread_benchmarks.sh. Each case should contain driver.log,
  server-metrics.json, driver.time.txt, and case.json.
* storage-bench JSON artifacts produced by lsmgraph storage-bench.
* file-summary.tsv artifacts produced by the SF1/SF30 controlled scripts.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any


INTERESTING_OPS = [
    "LdbcQuery1",
    "LdbcQuery2",
    "LdbcQuery3",
    "LdbcQuery4",
    "LdbcQuery5",
    "LdbcQuery6",
    "LdbcQuery7",
    "LdbcQuery8",
    "LdbcQuery9",
    "LdbcQuery10",
    "LdbcQuery11",
    "LdbcQuery12",
    "LdbcQuery13",
    "LdbcQuery14",
    "LdbcShortQuery1PersonProfile",
    "LdbcShortQuery2PersonPosts",
    "LdbcShortQuery3PersonFriends",
    "LdbcShortQuery4MessageContent",
    "LdbcShortQuery5MessageCreator",
    "LdbcShortQuery6MessageForum",
    "LdbcShortQuery7MessageReplies",
    "LdbcUpdate1AddPerson",
    "LdbcUpdate2AddPostLike",
    "LdbcUpdate3AddCommentLike",
    "LdbcUpdate4AddForum",
    "LdbcUpdate5AddForumMembership",
    "LdbcUpdate6AddPost",
    "LdbcUpdate7AddComment",
    "LdbcUpdate8AddFriendship",
]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse_number(raw: str) -> str:
    return raw.replace(",", "").strip()


def latest_throughput(driver_log: str) -> str:
    matches = re.findall(r"Throughput:\s*([0-9]+(?:\.[0-9]+)?)\s*\(op/s\)", driver_log)
    return matches[-1] if matches else ""


def final_operation_count(driver_log: str) -> str:
    matches = re.findall(r"Operation Count:\s*([0-9,]+)", driver_log)
    return parse_number(matches[-1]) if matches else ""


def parse_driver_ops(driver_log: str) -> dict[str, dict[str, str]]:
    lines = driver_log.splitlines()
    out: dict[str, dict[str, str]] = {}
    for idx, line in enumerate(lines):
        op = line.strip()
        if op not in INTERESTING_OPS:
            continue
        block = "\n".join(lines[idx : idx + 12])
        stats = {
            "count": "",
            "min_ms": "",
            "max_ms": "",
            "mean_ms": "",
            "p50_ms": "",
            "p90_ms": "",
            "p95_ms": "",
            "p99_ms": "",
        }
        patterns = {
            "count": r"Count:\s*([0-9,]+)",
            "min_ms": r"Min:\s*([0-9,]+(?:\.[0-9]+)?)",
            "max_ms": r"Max:\s*([0-9,]+(?:\.[0-9]+)?)",
            "mean_ms": r"Mean:\s*([0-9,]+(?:\.[0-9]+)?)",
            "p50_ms": r"50th Percentile:\s*([0-9,]+(?:\.[0-9]+)?)",
            "p90_ms": r"90th Percentile:\s*([0-9,]+(?:\.[0-9]+)?)",
            "p95_ms": r"95th Percentile:\s*([0-9,]+(?:\.[0-9]+)?)",
            "p99_ms": r"99th Percentile:\s*([0-9,]+(?:\.[0-9]+)?)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, block)
            if match:
                stats[key] = parse_number(match.group(1))
        if stats["count"]:
            out[op] = stats
    return out


def read_case_metadata(case_dir: Path) -> dict[str, str]:
    case = read_json(case_dir / "case.json")
    if case:
        return {
            "scale": str(case.get("scale", "")),
            "threads": str(case.get("thread_count", "")),
            "driver_status": str(case.get("driver_status", "")),
        }
    match = re.search(r"(sf[0-9]+)-tc([0-9]+)", case_dir.name)
    if match:
        return {
            "scale": match.group(1),
            "threads": match.group(2),
            "driver_status": "",
        }
    return {"scale": "", "threads": "", "driver_status": ""}


def summarize_ldbc_case(case_dir: Path) -> tuple[dict[str, str], list[dict[str, str]]]:
    meta = read_case_metadata(case_dir)
    driver_log = read_text(case_dir / "driver.log")
    metrics = read_json(case_dir / "server-metrics.json")
    ops = parse_driver_ops(driver_log)
    summary = {
        "case": case_dir.name,
        "scale": meta["scale"],
        "threads": meta["threads"],
        "driver_status": meta["driver_status"],
        "operation_count": final_operation_count(driver_log),
        "qps": latest_throughput(driver_log),
        "requests": str(metrics.get("requests", "")),
        "queries": str(metrics.get("queries", "")),
        "updates": str(metrics.get("updates", "")),
        "errors": str(metrics.get("errors", "")),
        "slow_requests": str(metrics.get("slow_requests", "")),
        "read_lock_wait_avg_us": str(metrics.get("read_lock_wait_us_avg", "")),
        "read_lock_wait_max_us": str(metrics.get("read_lock_wait_us_max", "")),
        "write_lock_wait_avg_us": str(metrics.get("write_lock_wait_us_avg", "")),
        "write_lock_wait_max_us": str(metrics.get("write_lock_wait_us_max", "")),
        "query_exec_avg_us": str(metrics.get("query_exec_us_avg", "")),
        "query_exec_max_us": str(metrics.get("query_exec_us_max", "")),
        "update_exec_avg_us": str(metrics.get("update_exec_us_avg", "")),
        "update_exec_max_us": str(metrics.get("update_exec_us_max", "")),
    }
    op_rows = []
    for op, stats in sorted(ops.items()):
        row = {
            "case": case_dir.name,
            "scale": meta["scale"],
            "threads": meta["threads"],
            "operation": op,
        }
        row.update(stats)
        op_rows.append(row)
    return summary, op_rows


def find_ldbc_cases(root: Path) -> list[Path]:
    cases = []
    for driver in root.rglob("driver.log"):
        case_dir = driver.parent
        if (case_dir / "server-metrics.json").exists() or (case_dir / "case.json").exists():
            cases.append(case_dir)
    return sorted(set(cases), key=lambda p: str(p))


def storage_summary_rows(json_path: Path, label: str) -> list[dict[str, str]]:
    data = read_json(json_path)
    rows: list[dict[str, str]] = []
    for idx, bench in enumerate(data.get("benchmarks", [])):
        summary = bench.get("neighbor_summary", {})
        rows.append(
            {
                "artifact": str(json_path),
                "label": label or json_path.stem,
                "bench_index": str(idx),
                "edge_type": str(bench.get("edge_type", "")),
                "sampled_vertices": str(bench.get("sampled_vertices", "")),
                "neighbor_edges": str(bench.get("neighbor_edges", "")),
                "elapsed_ms": str(bench.get("get_neighbors_elapsed_ms", "")),
                "candidate_l0_segments": str(summary.get("candidate_l0_segments", "")),
                "matched_l0_segments": str(summary.get("matched_l0_segments", "")),
                "body_reads": str(summary.get("body_reads", "")),
                "body_bytes": str(summary.get("body_bytes", "")),
                "read_bytes": str(summary.get("read_bytes", "")),
                "get_neighbors_ops": str(summary.get("get_neighbors_ops", "")),
                "get_neighbors_avg_us": str(summary.get("get_neighbors_avg_us", "")),
                "get_neighbors_p50_us": str(summary.get("get_neighbors_p50_us", "")),
                "get_neighbors_p90_us": str(summary.get("get_neighbors_p90_us", "")),
                "get_neighbors_p99_us": str(summary.get("get_neighbors_p99_us", "")),
                "csr_get_neighbors_avg_us": str(summary.get("csr_get_neighbors_avg_us", "")),
                "csr_get_neighbors_p50_us": str(summary.get("csr_get_neighbors_p50_us", "")),
                "csr_get_neighbors_p90_us": str(summary.get("csr_get_neighbors_p90_us", "")),
                "csr_get_neighbors_p99_us": str(summary.get("csr_get_neighbors_p99_us", "")),
            }
        )
    return rows


def parse_file_summary(path: Path, label: str) -> dict[str, str]:
    row = {"artifact": str(path), "label": label or path.parent.name}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            parts = raw.rstrip("\n").split("\t")
            if len(parts) >= 2:
                row[parts[0]] = parts[1]
    return row


def write_tsv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_markdown(
    path: Path,
    ldbc_rows: list[dict[str, str]],
    storage_rows: list[dict[str, str]],
    fanout_rows: list[dict[str, str]],
) -> None:
    with path.open("w", encoding="utf-8") as out:
        out.write("# SemL0 End-to-End Evidence Summary\n\n")
        if ldbc_rows:
            out.write("## LDBC End-to-End Driver\n\n")
            out.write(
                "| Case | Scale | Threads | Ops | QPS | Queries | Updates | Slow | "
                "Read lock avg/max us | Write lock avg/max us | Query exec avg/max us | "
                "Update exec avg/max us |\n"
            )
            out.write("|---|---|---:|---:|---:|---:|---:|---:|---|---|---|---|\n")
            for row in ldbc_rows:
                out.write(
                    f"| {row['case']} | {row['scale']} | {row['threads']} | "
                    f"{row['operation_count']} | {row['qps']} | {row['queries']} | "
                    f"{row['updates']} | {row['slow_requests']} | "
                    f"{row['read_lock_wait_avg_us']}/{row['read_lock_wait_max_us']} | "
                    f"{row['write_lock_wait_avg_us']}/{row['write_lock_wait_max_us']} | "
                    f"{row['query_exec_avg_us']}/{row['query_exec_max_us']} | "
                    f"{row['update_exec_avg_us']}/{row['update_exec_max_us']} |\n"
                )
            out.write("\n")
        if storage_rows:
            out.write("## Storage-Bench Latency and Read Amplification\n\n")
            out.write(
                "| Label | Edge type | Samples | Elapsed ms | Read bytes | Candidate L0 | "
                "get_neighbors avg/p50/p90/p99 us |\n"
            )
            out.write("|---|---:|---:|---:|---:|---:|---|\n")
            for row in storage_rows:
                out.write(
                    f"| {row['label']} | {row['edge_type']} | {row['sampled_vertices']} | "
                    f"{row['elapsed_ms']} | {row['read_bytes']} | {row['candidate_l0_segments']} | "
                    f"{row['get_neighbors_avg_us']}/{row['get_neighbors_p50_us']}/"
                    f"{row['get_neighbors_p90_us']}/{row['get_neighbors_p99_us']} |\n"
                )
            out.write("\n")
        if fanout_rows:
            out.write("## Metadata and Fanout\n\n")
            out.write("| Label | Files | Bytes | L0 files | L0 bytes | Manifest bytes | Degree exact files | Mixed edge files |\n")
            out.write("|---|---:|---:|---:|---:|---:|---:|---:|\n")
            for row in fanout_rows:
                out.write(
                    f"| {row.get('label', '')} | {row.get('files', '')} | {row.get('bytes', '')} | "
                    f"{row.get('l0_files', '')} | {row.get('l0_bytes', '')} | "
                    f"{row.get('manifest_bytes', '')} | {row.get('degree_exact_files', '')} | "
                    f"{row.get('mixed_edge_files', '')} |\n"
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--ldbc-dir", action="append", type=Path, default=[])
    parser.add_argument(
        "--storage-json",
        action="append",
        nargs=2,
        metavar=("LABEL", "PATH"),
        default=[],
        help="Storage-bench JSON artifact with a paper-facing label.",
    )
    parser.add_argument(
        "--file-summary",
        action="append",
        nargs=2,
        metavar=("LABEL", "PATH"),
        default=[],
        help="file-summary.tsv artifact with a paper-facing label.",
    )
    args = parser.parse_args()

    ldbc_rows: list[dict[str, str]] = []
    op_rows: list[dict[str, str]] = []
    for root in args.ldbc_dir:
        for case_dir in find_ldbc_cases(root):
            summary, ops = summarize_ldbc_case(case_dir)
            ldbc_rows.append(summary)
            op_rows.extend(ops)

    storage_rows: list[dict[str, str]] = []
    for label, raw_path in args.storage_json:
        storage_rows.extend(storage_summary_rows(Path(raw_path), label))

    fanout_rows = [
        parse_file_summary(Path(raw_path), label) for label, raw_path in args.file_summary
    ]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_tsv(
        args.out_dir / "ldbc-end-to-end-summary.tsv",
        ldbc_rows,
        [
            "case",
            "scale",
            "threads",
            "driver_status",
            "operation_count",
            "qps",
            "requests",
            "queries",
            "updates",
            "errors",
            "slow_requests",
            "read_lock_wait_avg_us",
            "read_lock_wait_max_us",
            "write_lock_wait_avg_us",
            "write_lock_wait_max_us",
            "query_exec_avg_us",
            "query_exec_max_us",
            "update_exec_avg_us",
            "update_exec_max_us",
        ],
    )
    write_tsv(
        args.out_dir / "ldbc-operation-latency-summary.tsv",
        op_rows,
        [
            "case",
            "scale",
            "threads",
            "operation",
            "count",
            "min_ms",
            "max_ms",
            "mean_ms",
            "p50_ms",
            "p90_ms",
            "p95_ms",
            "p99_ms",
        ],
    )
    write_tsv(
        args.out_dir / "storage-latency-summary.tsv",
        storage_rows,
        [
            "artifact",
            "label",
            "bench_index",
            "edge_type",
            "sampled_vertices",
            "neighbor_edges",
            "elapsed_ms",
            "candidate_l0_segments",
            "matched_l0_segments",
            "body_reads",
            "body_bytes",
            "read_bytes",
            "get_neighbors_ops",
            "get_neighbors_avg_us",
            "get_neighbors_p50_us",
            "get_neighbors_p90_us",
            "get_neighbors_p99_us",
            "csr_get_neighbors_avg_us",
            "csr_get_neighbors_p50_us",
            "csr_get_neighbors_p90_us",
            "csr_get_neighbors_p99_us",
        ],
    )
    write_tsv(
        args.out_dir / "metadata-fanout-summary.tsv",
        fanout_rows,
        [
            "artifact",
            "label",
            "store",
            "files",
            "bytes",
            "l0_files",
            "l0_bytes",
            "manifest_bytes",
            "manifest_records",
            "degree_exact_files",
            "mixed_edge_files",
        ],
    )
    write_markdown(
        args.out_dir / "end-to-end-evidence-summary.md",
        ldbc_rows,
        storage_rows,
        fanout_rows,
    )
    print(args.out_dir / "end-to-end-evidence-summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
