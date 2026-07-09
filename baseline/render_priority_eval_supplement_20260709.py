#!/usr/bin/env python3
"""Render the priority evaluation supplement tables.

Inputs are measured artifacts only:
- W6 SF10 priority run generated on 2026-07-09/10.
- W6 SF100 matrix already frozen in the repository.
- External baseline metrics table, when present.

The script intentionally labels scope boundaries rather than promoting these
rows to a full DBMS benchmark.
"""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


VARIANTS = ("schema", "naive", "kv-lsm", "edge-type-only", "semantic", "budg-b64", "budg-b256", "budg-b1024", "oracle")
MAIN_SCALE_VARIANTS = ("schema", "budg-b64", "semantic", "naive", "kv-lsm")


@dataclass
class W6Row:
    variant: str
    layout: str
    store_gib: str
    l0_files: str
    ops: str
    candidate_l0: str
    read_bytes: str
    avg_us: str
    pct: str
    compare: str


def parse_md_table_after(text: str, heading: str) -> list[dict[str, str]]:
    start = text.find(heading)
    if start < 0:
        return []
    lines = text[start:].splitlines()
    table_lines: list[str] = []
    in_table = False
    for line in lines:
        if line.startswith("| "):
            table_lines.append(line)
            in_table = True
        elif in_table:
            break
    if len(table_lines) < 3:
        return []
    headers = [part.strip() for part in table_lines[0].strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in table_lines[2:]:
        parts = [part.strip() for part in line.strip("|").split("|")]
        if len(parts) != len(headers):
            continue
        rows.append(dict(zip(headers, parts)))
    return rows


def load_w6_rows(path: Path) -> dict[str, W6Row]:
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = parse_md_table_after(text, "## Aggregate Matrix")
    out: dict[str, W6Row] = {}
    for row in rows:
        variant = row.get("variant", "")
        if not variant or variant == "PENDING":
            continue
        out[variant] = W6Row(
            variant=variant,
            layout=row.get("layout", ""),
            store_gib=row.get("store GiB", ""),
            l0_files=row.get("L0 files", ""),
            ops=row.get("ops/repeat", ""),
            candidate_l0=row.get("candidate L0 mean+/-std", ""),
            read_bytes=row.get("read bytes mean+/-std", ""),
            avg_us=row.get("avg us mean+/-std", ""),
            pct=row.get("p50/p90/p99 edge-mean us", ""),
            compare=row.get("compare vs naive", ""),
        )
    return out


def number_prefix(text: str) -> str:
    return text.split("+/-", 1)[0].split("plusminus", 1)[0].strip()


def parse_float(text: str) -> float | None:
    cleaned = number_prefix(text).replace(",", "").strip()
    if cleaned in {"", "PENDING", "-"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_int(text: str) -> int | None:
    value = parse_float(text)
    return int(round(value)) if value is not None else None


def read_external_metrics(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def parse_time_v(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    patterns = {
        "user_cpu_s": r"User time \(seconds\):\s*([0-9.]+)",
        "sys_cpu_s": r"System time \(seconds\):\s*([0-9.]+)",
        "cpu_pct": r"Percent of CPU this job got:\s*([0-9%]+)",
        "elapsed": r"Elapsed \(wall clock\) time.*:\s*([0-9:.]+)",
        "max_rss_kb": r"Maximum resident set size \(kbytes\):\s*(\d+)",
        "fs_inputs": r"File system inputs:\s*(\d+)",
        "fs_outputs": r"File system outputs:\s*(\d+)",
        "exit_status": r"Exit status:\s*(\d+)",
    }
    out: dict[str, str] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            out[key] = match.group(1)
    return out


def write_tsv(path: Path, headers: Iterable[str], rows: Iterable[Iterable[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(list(headers))
        writer.writerows(rows)


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |"]
    out.append("| " + " | ".join("---" for _ in headers) + " |")
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(out)


def build_external_table(sf10: dict[str, W6Row], external: list[dict[str, str]]) -> tuple[list[str], list[list[str]]]:
    headers = [
        "system",
        "scale",
        "scope",
        "ops",
        "avg_us",
        "p50_us",
        "p90_us",
        "p99_us",
        "load_s",
        "peak_rss_kb",
        "disk_bytes",
        "correctness",
        "claim_scope",
    ]
    rows: list[list[str]] = []
    for variant in ("schema", "budg-b64", "semantic", "naive", "kv-lsm"):
        row = sf10.get(variant)
        if row is None:
            continue
        p50, p90, p99 = split_percentiles(row.pct)
        rows.append(
            [
                f"SemL0-{variant}",
                "SF10",
                "W6 core typed-neighbor; 9 core edge types; 1000 samples/type; 3 repeats",
                row.ops,
                number_prefix(row.avg_us),
                p50,
                p90,
                p99,
                "import measured separately",
                "see resource table",
                f"{row.store_gib} GiB",
                row.compare,
                "internal storage-layout row, not external DBMS",
            ]
        )
    for row in external:
        if row.get("scale") != "SF10" or row.get("scope") not in {"all-types-summary", "positive-edge-types-summary"}:
            continue
        rows.append(
            [
                row.get("system", ""),
                row.get("scale", ""),
                row.get("scope", ""),
                row.get("ops", ""),
                row.get("avg_us", ""),
                row.get("p50_us", ""),
                row.get("p90_us", ""),
                row.get("p99_us", ""),
                row.get("load_s", ""),
                row.get("peak_rss_kb", ""),
                row.get("disk_total_bytes", ""),
                row.get("correctness", ""),
                row.get("claim_level", ""),
            ]
        )
    return headers, rows


def split_percentiles(text: str) -> tuple[str, str, str]:
    normalized = text.replace("+/-", " plusminus ")
    parts = normalized.split("/")
    vals = [number_prefix(part) for part in parts]
    vals += [""] * (3 - len(vals))
    return vals[0], vals[1], vals[2]


def build_resource_table(w6_log: Path, sf10: dict[str, W6Row]) -> tuple[list[str], list[list[str]]]:
    headers = [
        "variant",
        "store_gib",
        "l0_files",
        "import_elapsed",
        "user_cpu_s",
        "sys_cpu_s",
        "cpu_pct",
        "max_rss_kb",
        "fs_inputs",
        "fs_outputs",
        "exit_status",
    ]
    rows: list[list[str]] = []
    for variant in VARIANTS:
        row = sf10.get(variant)
        timev = parse_time_v(w6_log / f"{variant}-import.stderr")
        if row is None and not timev:
            continue
        rows.append(
            [
                variant,
                row.store_gib if row else "",
                row.l0_files if row else "",
                timev.get("elapsed", ""),
                timev.get("user_cpu_s", ""),
                timev.get("sys_cpu_s", ""),
                timev.get("cpu_pct", ""),
                timev.get("max_rss_kb", ""),
                timev.get("fs_inputs", ""),
                timev.get("fs_outputs", ""),
                timev.get("exit_status", ""),
            ]
        )
    return headers, rows


def build_scale_table(sf10: dict[str, W6Row], sf100: dict[str, W6Row]) -> tuple[list[str], list[list[str]]]:
    headers = [
        "variant",
        "scale",
        "ops_per_repeat",
        "candidate_l0",
        "read_bytes",
        "avg_us",
        "p99_us",
        "store_gib",
        "l0_files",
        "candidate_per_op",
    ]
    rows: list[list[str]] = []
    for scale, data in (("SF10", sf10), ("SF100", sf100)):
        for variant in MAIN_SCALE_VARIANTS:
            row = data.get(variant)
            if row is None:
                continue
            _, _, p99 = split_percentiles(row.pct)
            cand = parse_float(row.candidate_l0)
            ops = parse_float(row.ops)
            cand_per_op = f"{cand / ops:.2f}" if cand is not None and ops else ""
            rows.append(
                [
                    variant,
                    scale,
                    row.ops,
                    number_prefix(row.candidate_l0),
                    number_prefix(row.read_bytes),
                    number_prefix(row.avg_us),
                    p99,
                    row.store_gib,
                    row.l0_files,
                    cand_per_op,
                ]
            )
    return headers, rows


def render_report(
    out: Path,
    external_table: tuple[list[str], list[list[str]]],
    resource_table: tuple[list[str], list[list[str]]],
    scale_table: tuple[list[str], list[list[str]]],
    sf10_summary: Path,
    sf100_summary: Path,
    external_metrics: Path,
) -> None:
    e_headers, e_rows = external_table
    r_headers, r_rows = resource_table
    s_headers, s_rows = scale_table
    lines = [
        "# Priority Evaluation Supplement 20260709",
        "",
        "This supplement closes the three highest-priority evaluation gaps with measured Linux-side artifacts.",
        "",
        "## Inputs",
        "",
        f"- SF10 SemL0 matrix: `{sf10_summary}`",
        f"- SF100 SemL0 matrix: `{sf100_summary}`",
        f"- External baseline metrics: `{external_metrics}`",
        "",
        "## 1. SF10 SemL0 Rows With Digest-Gated External Baselines",
        "",
        "SemL0 rows are internal storage-layout rows from the W6 core typed-neighbor workload. External rows are scoped to their original digest-gated typed-neighbor workload. This is not a full DBMS benchmark.",
        "",
        markdown_table(e_headers, e_rows),
        "",
        "## 2. SF10 Import Resource Overhead",
        "",
        "Resource numbers are parsed from `/usr/bin/time -v` logs emitted by the W6 runner during import.",
        "",
        markdown_table(r_headers, r_rows),
        "",
        "## 3. SF10 to SF100 Scale Trend",
        "",
        "The trend uses the same W6 core typed-neighbor protocol. SF10 uses 1,000 sampled sources per core edge type; SF100 uses the frozen 5,000 sampled sources per core edge type.",
        "",
        markdown_table(s_headers, s_rows),
        "",
        "## Claim Boundary",
        "",
        "- Use the external table as scope-limited typed-neighbor positioning, not a full graph database head-to-head.",
        "- CPU/resource overhead is import-side `/usr/bin/time -v`; it is not a perf-counter CPU-cycle study.",
        "- The scale table is a two-point SF10/SF100 trend for the W6 protocol. SF30 remains covered by separate dynamic/property workloads.",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sf10-summary", type=Path, default=Path("baseline/w6-sf10-priority-20260709-summary.md"))
    parser.add_argument("--sf10-log", type=Path, default=Path("remote-logs/w6-sf10-priority-20260709"))
    parser.add_argument("--sf100-summary", type=Path, default=Path("baseline/sf100-matrix-20260613-cn.md"))
    parser.add_argument("--external-metrics", type=Path, default=Path("baseline/external-baselines-20260626/3plus3-baselines/metrics.tsv"))
    parser.add_argument("--out-md", type=Path, default=Path("baseline/priority-eval-supplement-20260709.md"))
    parser.add_argument("--out-dir", type=Path, default=Path("baseline"))
    args = parser.parse_args()

    sf10 = load_w6_rows(args.sf10_summary)
    sf100 = load_w6_rows(args.sf100_summary)
    external = read_external_metrics(args.external_metrics)

    external_table = build_external_table(sf10, external)
    resource_table = build_resource_table(args.sf10_log, sf10)
    scale_table = build_scale_table(sf10, sf100)

    write_tsv(args.out_dir / "priority-external-seml0-sf10-20260709.tsv", *external_table)
    write_tsv(args.out_dir / "priority-resource-overhead-20260709.tsv", *resource_table)
    write_tsv(args.out_dir / "priority-scale-trend-20260709.tsv", *scale_table)
    render_report(
        args.out_md,
        external_table,
        resource_table,
        scale_table,
        args.sf10_summary,
        args.sf100_summary,
        args.external_metrics,
    )
    print(args.out_md)


if __name__ == "__main__":
    main()
