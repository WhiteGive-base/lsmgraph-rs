#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


LAYOUTS = [
    ("schema", "Schema"),
    ("semantic", "Full semantic"),
    ("budgeted", "Budgeted semantic"),
]


def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def read_json(path: Path) -> Optional[Any]:
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


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


def import_summary(path: Path) -> Dict[str, Any]:
    data = read_json(path)
    if isinstance(data, dict):
        return data
    return {}


def number(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def aggregate_storage(path: Path) -> Dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, dict):
        return {"missing": True}
    summary_keys = [
        "candidate_l0_segments",
        "body_reads",
        "body_bytes",
        "read_bytes",
        "filter_passed_segments",
        "matched_l0_segments",
        "get_neighbors_ops",
    ]
    agg: Dict[str, Any] = {
        "missing": False,
        "snapshot": data.get("snapshot"),
        "scan_elapsed_ms": number(data.get("scan_elapsed_ms")),
        "scan_edges": number(data.get("scan_edges")),
        "get_neighbors_elapsed_ms": 0,
        "neighbor_edges": 0,
        "benchmarks": len(data.get("benchmarks", [])) if isinstance(data.get("benchmarks"), list) else 0,
    }
    for key in summary_keys:
        agg[key] = 0
    for bench in data.get("benchmarks", []):
        if not isinstance(bench, dict):
            continue
        agg["get_neighbors_elapsed_ms"] += number(bench.get("get_neighbors_elapsed_ms"))
        agg["neighbor_edges"] += number(bench.get("neighbor_edges"))
        summary = bench.get("neighbor_summary", {})
        if isinstance(summary, dict):
            for key in summary_keys:
                agg[key] += number(summary.get(key))
    return agg


def aggregate_storage_repeats(logdir: Path, kind: str, samples: int) -> List[Dict[str, Any]]:
    return [
        aggregate_storage(logdir / f"fair-{kind}-s{samples}-r{repeat}.json")
        for repeat in (1, 2, 3)
    ]


def mean(values: Iterable[int]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def summarize_repeats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    present = [row for row in rows if not row.get("missing")]
    if not present:
        return {"missing": True}
    keys = [
        "get_neighbors_elapsed_ms",
        "body_reads",
        "body_bytes",
        "candidate_l0_segments",
        "read_bytes",
        "neighbor_edges",
    ]
    out: Dict[str, Any] = {"missing": False, "runs": len(present)}
    for key in keys:
        out[key] = round(mean(number(row.get(key)) for row in present), 3)
    return out


def summarize_ldbc(logdir: Path) -> Dict[str, Any]:
    reports = []
    for repeat in (1, 2, 3):
        data = read_json(logdir / f"ldbc-passing-s3-r{repeat}.json")
        if isinstance(data, dict):
            reports.append(data)
    if not reports:
        return {"missing": True}
    total_checked = sum(number(r.get("checked")) for r in reports)
    total_passed = sum(number(r.get("passed")) for r in reports)
    total_failed = sum(number(r.get("failed")) for r in reports)
    latency_sum = 0
    latency_count = 0
    for report in reports:
        for item in report.get("reports", []):
            if not isinstance(item, dict):
                continue
            latency = item.get("query_latency_us", {})
            if isinstance(latency, dict):
                latency_sum += number(latency.get("sum_us"))
                latency_count += number(latency.get("count"))
    avg_us = int(latency_sum / latency_count) if latency_count else 0
    return {
        "missing": False,
        "runs": len(reports),
        "checked": total_checked,
        "passed": total_passed,
        "failed": total_failed,
        "avg_query_us": avg_us,
    }


def summarize_compare(path: Path) -> Dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, dict):
        return {"missing": True}
    return {
        "missing": False,
        "checked": number(data.get("checked")),
        "passed": number(data.get("passed")),
        "mismatches": number(data.get("mismatches")),
        "left_edges_total": number(data.get("left_edges_total")),
        "right_edges_total": number(data.get("right_edges_total")),
    }


def fmt(value: Any) -> str:
    if value is None:
        return "missing"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def row(cells: List[Any]) -> str:
    return "| " + " | ".join(fmt(cell) for cell in cells) + " |"


def reduction(base: Any, value: Any) -> str:
    b = float(base or 0)
    v = float(value or 0)
    if b <= 0:
        return "n/a"
    return f"{(b - v) / b * 100:.2f}%"


def reduction_value(base: Any, value: Any) -> float:
    b = float(base or 0)
    v = float(value or 0)
    if b <= 0:
        return 0.0
    return (b - v) / b * 100


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date-tag", default="20260603")
    parser.add_argument("--root", default=".")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    root = Path(args.root)
    date = args.date_tag
    out_path = Path(args.out) if args.out else root / f"seml0-stage-p1-results-sf1-controlled-{date}.md"

    layout_data = {}
    for key, label in LAYOUTS:
        logdir = root / f"remote-logs/p1-c1-sf1-{key}-{date}"
        layout_data[key] = {
            "label": label,
            "logdir": logdir,
            "import": import_summary(logdir / "import.stdout"),
            "time": parse_time_log(logdir / "time.log"),
            "files": parse_tsv(logdir / "file-summary.tsv"),
            "core": summarize_repeats(aggregate_storage_repeats(logdir, "core", 200)),
            "alltypes": summarize_repeats(aggregate_storage_repeats(logdir, "alltypes", 50)),
            "ldbc": summarize_ldbc(logdir),
        }

    compare_names = [
        ("semantic-core", "Schema vs full semantic core"),
        ("semantic-alltypes", "Schema vs full semantic all types"),
        ("budgeted-core", "Schema vs budgeted core"),
        ("budgeted-alltypes", "Schema vs budgeted all types"),
    ]
    compares = [
        (
            label,
            summarize_compare(root / f"remote-logs/p1-c1-sf1-neighbor-compare-{name}-{date}.json"),
        )
        for name, label in compare_names
    ]

    lines: List[str] = []
    lines.append("# SemL0 Stage P1 Results: SF1 Controlled")
    lines.append("")
    lines.append(f"Date tag: `{date}`")
    lines.append("")
    lines.append("Generated by:")
    lines.append("")
    lines.append("```text")
    lines.append("summarize-p1-c1-sf1-controlled.py")
    lines.append("```")
    lines.append("")
    lines.append("## Metadata")
    lines.append("")
    lines.append(row(["layout", "logdir", "store", "snapshot", "directed edges"]))
    lines.append(row(["---", "---", "---", "---:", "---:"]))
    for key, data in layout_data.items():
        imp = data["import"]
        lines.append(row([
            data["label"],
            data["logdir"],
            data["files"].get("store", "missing"),
            imp.get("snapshot", "missing"),
            imp.get("directed_edges", "missing"),
        ]))
    lines.append("")
    lines.append("## Import And Store Cost")
    lines.append("")
    lines.append(row(["layout", "wall time", "max RSS KB", "store bytes", "L0 files", "L0 bytes", "MANIFEST bytes"]))
    lines.append(row(["---", "---:", "---:", "---:", "---:", "---:", "---:"]))
    for key, data in layout_data.items():
        lines.append(row([
            data["label"],
            data["time"].get("wall_time", "missing"),
            data["time"].get("max_rss_kb", "missing"),
            data["files"].get("bytes", "missing"),
            data["files"].get("l0_files", "missing"),
            data["files"].get("l0_bytes", "missing"),
            data["files"].get("manifest_bytes", "missing"),
        ]))
    lines.append("")
    lines.append("## Core Storage Bench Mean")
    lines.append("")
    lines.append(row(["layout", "runs", "elapsed ms", "body reads", "body bytes", "candidate L0", "IO read bytes", "reduction vs schema IO"]))
    lines.append(row(["---", "---:", "---:", "---:", "---:", "---:", "---:", "---:"]))
    schema_core = layout_data["schema"]["core"]
    schema_core_io = schema_core.get("read_bytes", 0) if not schema_core.get("missing") else 0
    for key, data in layout_data.items():
        core = data["core"]
        lines.append(row([
            data["label"],
            core.get("runs", "missing"),
            core.get("get_neighbors_elapsed_ms", "missing"),
            core.get("body_reads", "missing"),
            core.get("body_bytes", "missing"),
            core.get("candidate_l0_segments", "missing"),
            core.get("read_bytes", "missing"),
            reduction(schema_core_io, core.get("read_bytes")) if not core.get("missing") else "missing",
        ]))
    lines.append("")
    lines.append("## All-Types Storage Bench Mean")
    lines.append("")
    lines.append(row(["layout", "runs", "elapsed ms", "body reads", "body bytes", "candidate L0", "IO read bytes", "reduction vs schema IO"]))
    lines.append(row(["---", "---:", "---:", "---:", "---:", "---:", "---:", "---:"]))
    schema_all = layout_data["schema"]["alltypes"]
    schema_all_io = schema_all.get("read_bytes", 0) if not schema_all.get("missing") else 0
    for key, data in layout_data.items():
        alltypes = data["alltypes"]
        lines.append(row([
            data["label"],
            alltypes.get("runs", "missing"),
            alltypes.get("get_neighbors_elapsed_ms", "missing"),
            alltypes.get("body_reads", "missing"),
            alltypes.get("body_bytes", "missing"),
            alltypes.get("candidate_l0_segments", "missing"),
            alltypes.get("read_bytes", "missing"),
            reduction(schema_all_io, alltypes.get("read_bytes")) if not alltypes.get("missing") else "missing",
        ]))
    lines.append("")
    lines.append("## P1 Target Check")
    lines.append("")
    budgeted_core = layout_data["budgeted"]["core"]
    budgeted_all = layout_data["budgeted"]["alltypes"]
    full_files = number(layout_data["semantic"]["files"].get("l0_files"))
    budgeted_files = number(layout_data["budgeted"]["files"].get("l0_files"))
    schema_files = number(layout_data["schema"]["files"].get("l0_files"))
    budgeted_core_reduction = reduction_value(schema_core_io, budgeted_core.get("read_bytes"))
    budgeted_all_reduction = reduction_value(schema_all_io, budgeted_all.get("read_bytes"))
    file_fanout_control = full_files > 0 and budgeted_files < full_files
    budgeted_has_read_benefit = budgeted_core_reduction > 0 or budgeted_all_reduction > 0
    lines.append(row(["check", "result", "evidence"]))
    lines.append(row(["---", "---", "---"]))
    lines.append(row([
        "budgeted read benefit",
        "pass" if budgeted_has_read_benefit else "fail",
        f"core IO reduction={budgeted_core_reduction:.2f}%, all-types IO reduction={budgeted_all_reduction:.2f}%",
    ]))
    lines.append(row([
        "budgeted file fanout control",
        "pass" if file_fanout_control else "fail",
        f"schema L0={schema_files}, full semantic L0={full_files}, budgeted L0={budgeted_files}",
    ]))
    lines.append("")
    lines.append("## LDBC Passing Batch")
    lines.append("")
    lines.append(row(["layout", "runs", "checked", "passed", "failed", "avg query us"]))
    lines.append(row(["---", "---:", "---:", "---:", "---:", "---:"]))
    for key, data in layout_data.items():
        ldbc = data["ldbc"]
        lines.append(row([
            data["label"],
            ldbc.get("runs", "missing"),
            ldbc.get("checked", "missing"),
            ldbc.get("passed", "missing"),
            ldbc.get("failed", "missing"),
            ldbc.get("avg_query_us", "missing"),
        ]))
    lines.append("")
    lines.append("## Neighbor Compare Correctness")
    lines.append("")
    lines.append(row(["gate", "checked", "passed", "mismatches", "left edges", "right edges"]))
    lines.append(row(["---", "---:", "---:", "---:", "---:", "---:"]))
    for label, data in compares:
        lines.append(row([
            label,
            data.get("checked", "missing"),
            data.get("passed", "missing"),
            data.get("mismatches", "missing"),
            data.get("left_edges_total", "missing"),
            data.get("right_edges_total", "missing"),
        ]))
    lines.append("")
    lines.append("## Completion Status")
    lines.append("")
    missing_sections = []
    for key, data in layout_data.items():
        for section in ("core", "alltypes", "ldbc"):
            if data[section].get("missing"):
                missing_sections.append(f"{data['label']} {section}")
            elif section in ("core", "alltypes") and data[section].get("runs", 0) < 3:
                missing_sections.append(f"{data['label']} {section} has only {data[section].get('runs')} runs")
            elif section == "ldbc" and data[section].get("runs", 0) < 3:
                missing_sections.append(f"{data['label']} ldbc has only {data[section].get('runs')} runs")
        if not data["import"]:
            missing_sections.append(f"{data['label']} import")
    for label, data in compares:
        if data.get("missing"):
            missing_sections.append(label)
    if missing_sections:
        lines.append("Incomplete. Missing evidence:")
        lines.append("")
        for item in missing_sections:
            lines.append(f"- {item}")
    else:
        mismatches = [label for label, data in compares if data.get("mismatches", 1) != 0]
        failures = [data["label"] for data in layout_data.values() if data["ldbc"].get("failed", 1) != 0]
        if mismatches or failures:
            lines.append("Complete logs found, but correctness gates are not all clean.")
        elif not budgeted_has_read_benefit:
            lines.append("All expected logs were found and correctness gates are clean, but the P1 budgeted-read-benefit target failed.")
        else:
            lines.append("All expected logs were found and correctness gates are clean.")
    lines.append("")
    lines.append("## Next Step")
    lines.append("")
    lines.append("Use this result document to update the P1 evidence ledger and decide whether to proceed to SF30 scale rerun.")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(out_path)


if __name__ == "__main__":
    main()
