#!/usr/bin/env python3
import argparse
import csv
import json
import re
from pathlib import Path


DEFAULT_IMPORT_ROWS = [
    ("schema", "20260604-p57", "remote-logs/p1-c1-sf1-schema-20260604-p57"),
    ("full_semantic", "20260604-p57", "remote-logs/p1-c1-sf1-semantic-20260604-p57"),
    ("default_budgeted", "20260604-p57", "remote-logs/p1-c1-sf1-budgeted-20260604-p57"),
    (
        "benefit_scored",
        "20260604-p58",
        "remote-logs/p1-c1-sf1-budgeted-score1024_edge1t_core4_rev2_other01_exact0-20260604-p58",
    ),
]

DEFAULT_COMPACTION_SOURCES = [
    ("workload_shift", "remote-logs/p3-feedback-workload-shift-p32-20260604.json"),
    ("vs_no_feedback", "remote-logs/p3-feedback-vs-no-feedback-p33-20260604.json"),
]


def parse_elapsed_seconds(value: str):
    value = value.strip()
    parts = value.split(":")
    try:
        if len(parts) == 3:
            hours = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes = float(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds
        return float(value)
    except ValueError:
        return None


def parse_time_log(path: Path):
    result = {
        "time_log_present": path.exists(),
        "wall_time": "",
        "wall_time_seconds": "",
        "user_seconds": "",
        "system_seconds": "",
        "cpu_percent": "",
        "max_rss_kb": "",
        "fs_inputs": "",
        "fs_outputs": "",
        "exit_status": "",
    }
    if not path.exists():
        return result
    patterns = {
        "user_seconds": re.compile(r"User time \(seconds\):\s*(.+)$"),
        "system_seconds": re.compile(r"System time \(seconds\):\s*(.+)$"),
        "cpu_percent": re.compile(r"Percent of CPU this job got:\s*(.+)$"),
        "wall_time": re.compile(r"Elapsed \(wall clock\) time.*?\):\s*(.+)$"),
        "max_rss_kb": re.compile(r"Maximum resident set size \(kbytes\):\s*(.+)$"),
        "fs_inputs": re.compile(r"File system inputs:\s*(.+)$"),
        "fs_outputs": re.compile(r"File system outputs:\s*(.+)$"),
        "exit_status": re.compile(r"Exit status:\s*(.+)$"),
    }
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        for key, pattern in patterns.items():
            match = pattern.search(line)
            if match:
                result[key] = match.group(1).strip()
    if result["wall_time"]:
        seconds = parse_elapsed_seconds(result["wall_time"])
        if seconds is not None:
            result["wall_time_seconds"] = f"{seconds:.3f}"
    return result


def parse_file_summary(path: Path):
    result = {
        "file_summary_present": path.exists(),
        "store": "",
        "files": "",
        "store_bytes": "",
        "l0_files": "",
        "l0_bytes": "",
        "manifest_bytes": "",
    }
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 2:
                continue
            key, value = parts
            if key == "bytes":
                key = "store_bytes"
            if key in result:
                result[key] = value
    return result


def pct_delta(value, baseline):
    try:
        value_f = float(value)
        baseline_f = float(baseline)
        if baseline_f == 0:
            return ""
        return f"{((value_f - baseline_f) / baseline_f) * 100:.2f}%"
    except (TypeError, ValueError):
        return ""


def collect_import_rows(root: Path):
    rows = []
    for layout, tag, rel_dir in DEFAULT_IMPORT_ROWS:
        log_dir = root / rel_dir
        row = {
            "layout": layout,
            "tag": tag,
            "log_dir": rel_dir,
        }
        row.update(parse_time_log(log_dir / "time.log"))
        row.update(parse_file_summary(log_dir / "file-summary.tsv"))
        rows.append(row)
    schema = next((row for row in rows if row["layout"] == "schema"), None)
    if schema:
        for row in rows:
            row["wall_time_overhead_vs_schema"] = pct_delta(
                row.get("wall_time_seconds"), schema.get("wall_time_seconds")
            )
            row["store_bytes_overhead_vs_schema"] = pct_delta(
                row.get("store_bytes"), schema.get("store_bytes")
            )
            row["l0_files_overhead_vs_schema"] = pct_delta(
                row.get("l0_files"), schema.get("l0_files")
            )
            row["manifest_bytes_overhead_vs_schema"] = pct_delta(
                row.get("manifest_bytes"), schema.get("manifest_bytes")
            )
    return rows


def safe_get(obj, path, default=""):
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def collect_compaction_rows(sources):
    rows = []
    for source_name, path in sources:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for phase_key in ["phase_a", "phase_b"]:
            phase = data.get(phase_key)
            if not isinstance(phase, dict) or "decision" not in phase:
                continue
            decision = phase.get("decision", {})
            before = phase.get("before", {})
            after = phase.get("after", {})
            before_read = before.get("io_read_bytes", "")
            after_read = after.get("io_read_bytes", "")
            rewrite_mib = decision.get("rewrite_mib", "")
            benefit = ""
            try:
                benefit = f"{(float(before_read) - float(after_read)) / max(float(rewrite_mib), 1.0):.3f}"
            except (TypeError, ValueError):
                pass
            compaction_summary = safe_get(phase, ["compaction", "summary"], {})
            compaction_metrics_present = isinstance(compaction_summary, dict) and bool(compaction_summary)
            compaction_count = safe_get(compaction_summary, ["compaction_count"])
            compaction_latency_sum_us = safe_get(compaction_summary, ["compaction_latency_sum_us"])
            compaction_latency_max_us = safe_get(compaction_summary, ["compaction_latency_max_us"])
            compaction_input_bytes = safe_get(compaction_summary, ["compaction_input_bytes"])
            compaction_output_bytes = safe_get(compaction_summary, ["compaction_output_bytes"])
            io_read_bytes_compaction = safe_get(compaction_summary, ["io_read_bytes"])
            io_write_bytes_compaction = safe_get(compaction_summary, ["io_write_bytes"])
            io_write_syscalls_compaction = safe_get(compaction_summary, ["io_write_syscalls"])
            write_blocking_latency_sum_us = safe_get(
                compaction_summary, ["write_blocking_latency_sum_us"]
            )
            sync_blocking_latency_sum_us = safe_get(compaction_summary, ["sync_blocking_latency_sum_us"])
            rebuild_index_latency_sum_us = safe_get(
                compaction_summary, ["rebuild_index_latency_sum_us"]
            )
            actual_delta = pct_delta(compaction_input_bytes, decision.get("estimated_rewrite_bytes", ""))
            benefit_per_actual_input = ""
            try:
                input_mib = max(float(compaction_input_bytes) / 1_048_576.0, 1.0)
                benefit_per_actual_input = f"{(float(before_read) - float(after_read)) / input_mib:.3f}"
            except (TypeError, ValueError):
                pass
            rows.append(
                {
                    "source": source_name,
                    "phase": phase.get("name", phase_key),
                    "selected_l0_segments": decision.get("selected_l0_segments", ""),
                    "output_segments": decision.get("output_segments", ""),
                    "output_edges": decision.get("output_edges", ""),
                    "estimated_rewrite_bytes": decision.get("estimated_rewrite_bytes", ""),
                    "rewrite_mib": rewrite_mib,
                    "score": decision.get("score", ""),
                    "query_count": decision.get("query_count", ""),
                    "candidate_l0_before": before.get("candidate_l0_segments", ""),
                    "candidate_l0_after": after.get("candidate_l0_segments", ""),
                    "read_bytes_before": before_read,
                    "read_bytes_after": after_read,
                    "benefit_bytes_per_rewrite_mib": benefit,
                    "compaction_metrics_present": "true" if compaction_metrics_present else "false",
                    "compaction_count": compaction_count,
                    "compaction_latency_sum_us": compaction_latency_sum_us,
                    "compaction_latency_max_us": compaction_latency_max_us,
                    "compaction_input_bytes": compaction_input_bytes,
                    "compaction_output_bytes": compaction_output_bytes,
                    "io_read_bytes_compaction": io_read_bytes_compaction,
                    "io_write_bytes_compaction": io_write_bytes_compaction,
                    "io_write_syscalls_compaction": io_write_syscalls_compaction,
                    "write_blocking_latency_sum_us": write_blocking_latency_sum_us,
                    "sync_blocking_latency_sum_us": sync_blocking_latency_sum_us,
                    "rebuild_index_latency_sum_us": rebuild_index_latency_sum_us,
                    "actual_input_vs_estimated_rewrite_delta_pct": actual_delta,
                    "read_bytes_saved_per_compaction_input_mib": benefit_per_actual_input,
                    "compaction_latency_note": ""
                    if compaction_metrics_present
                    else "not captured in current P3 JSON",
                }
            )
    return rows


def write_csv(path: Path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def markdown_table(headers, rows):
    lines = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")
    return "\n".join(lines)


def write_markdown(path: Path, import_rows, compaction_rows):
    import_headers = [
        "layout",
        "wall_time",
        "wall_time_seconds",
        "max_rss_kb",
        "fs_outputs",
        "store_bytes",
        "l0_files",
        "manifest_bytes",
        "wall_time_overhead_vs_schema",
        "store_bytes_overhead_vs_schema",
    ]
    compaction_headers = [
        "source",
        "phase",
        "selected_l0_segments",
        "output_segments",
        "estimated_rewrite_bytes",
        "rewrite_mib",
        "candidate_l0_before",
        "candidate_l0_after",
        "read_bytes_before",
        "read_bytes_after",
        "benefit_bytes_per_rewrite_mib",
        "compaction_metrics_present",
        "compaction_count",
        "compaction_latency_sum_us",
        "compaction_input_bytes",
        "compaction_output_bytes",
        "io_write_bytes_compaction",
        "sync_blocking_latency_sum_us",
        "read_bytes_saved_per_compaction_input_mib",
    ]
    text = []
    text.append("# P6.3 Write-Cost Extraction Tables")
    text.append("")
    text.append("Generated from existing logs. No new experiment was run.")
    text.append("")
    text.append("## Import and Store Cost")
    text.append("")
    text.append(markdown_table(import_headers, import_rows))
    text.append("")
    text.append("## Feedback Compaction Rewrite Cost")
    text.append("")
    text.append(markdown_table(compaction_headers, compaction_rows))
    text.append("")
    text.append("## Known Gaps")
    text.append("")
    has_compaction_metrics = any(
        str(row.get("compaction_metrics_present", "")).lower() == "true"
        for row in compaction_rows
    )
    if has_compaction_metrics:
        text.append(
            "- P6.5 compaction rows include metrics snapshots; older P3 JSON artifacts still lack these fields."
        )
        text.append(
            "- The current feedback compaction evidence is still microbenchmark-scale, not a large-scale SF30/SF100 write-overhead result."
        )
    else:
        text.append("- Current P3 JSON records estimated rewrite bytes but not a metrics snapshot around compaction.")
    text.append("- True write-stall counters are not implemented; use write/sync latency as proxy only after metrics extraction is added.")
    path.write_text("\n".join(text) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Summarize SemL0 P6 write-cost evidence from existing logs.")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument(
        "--out-dir",
        default="remote-logs/p6-3-write-cost-extraction-20260604",
        help="Output directory",
    )
    parser.add_argument(
        "--workload-shift-json",
        default=DEFAULT_COMPACTION_SOURCES[0][1],
        help="Feedback workload-shift JSON path, relative to --root unless absolute",
    )
    parser.add_argument(
        "--vs-no-feedback-json",
        default=DEFAULT_COMPACTION_SOURCES[1][1],
        help="Feedback vs no-feedback JSON path, relative to --root unless absolute",
    )
    args = parser.parse_args()

    root = Path(args.root)
    out_dir = root / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    import_rows = collect_import_rows(root)
    compaction_inputs = [
        ("workload_shift", Path(args.workload_shift_json)),
        ("vs_no_feedback", Path(args.vs_no_feedback_json)),
    ]
    compaction_sources = [
        (name, path if path.is_absolute() else root / path) for name, path in compaction_inputs
    ]
    compaction_rows = collect_compaction_rows(compaction_sources)

    write_csv(out_dir / "write_cost_import_sf1.csv", import_rows)
    write_csv(out_dir / "write_cost_compaction_microbench.csv", compaction_rows)
    write_markdown(out_dir / "write_cost_tables.md", import_rows, compaction_rows)

    has_compaction_metrics = any(
        str(row.get("compaction_metrics_present", "")).lower() == "true"
        for row in compaction_rows
    )
    manifest_notes = [
        "No new import experiment was run.",
        "Import/store rows are extracted from existing P5.7/P5.8 logs.",
        "Compaction rewrite rows are extracted from the configured feedback JSON artifacts.",
    ]
    if has_compaction_metrics:
        manifest_notes.append(
            "At least one compaction row includes metrics snapshots and measured compaction-window counters."
        )
        manifest_notes.append(
            "True write-stall counters are not implemented; write/sync latency remains a proxy."
        )
    else:
        manifest_notes.append(
            "Compaction latency and write/sync latency need metrics snapshot enhancement before final paper claims."
        )

    manifest = {
        "output_dir": args.out_dir,
        "inputs": {
            "import_rows": [row["log_dir"] for row in import_rows],
            "compaction_json": [
                args.workload_shift_json,
                args.vs_no_feedback_json,
            ],
        },
        "outputs": [
            "write_cost_import_sf1.csv",
            "write_cost_compaction_microbench.csv",
            "write_cost_tables.md",
        ],
        "notes": manifest_notes,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_dir}")


if __name__ == "__main__":
    main()
