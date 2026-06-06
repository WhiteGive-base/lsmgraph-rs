#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path


IMPORT_INPUT = "remote-logs/p6-5-write-cost-extraction-20260604/write_cost_import_sf1.csv"
COMPACTION_INPUT = "remote-logs/p6-5-write-cost-extraction-20260604/write_cost_compaction_microbench.csv"


LAYOUT_LABELS = {
    "schema": "Schema",
    "full_semantic": "Full semantic",
    "default_budgeted": "Default budgeted",
    "benefit_scored": "Benefit scored",
}

SOURCE_LABELS = {
    "workload_shift": "workload shift",
    "vs_no_feedback": "vs no-feedback",
}


def read_csv(path: Path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def fmt_int(value):
    if value in ("", None):
        return ""
    try:
        return f"{int(float(value)):,}"
    except ValueError:
        return str(value)


def fmt_float(value, digits=3):
    if value in ("", None):
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except ValueError:
        return str(value)


def rss_mib(kb):
    if kb in ("", None):
        return ""
    try:
        return f"{float(kb) / 1024.0:.1f}"
    except ValueError:
        return str(kb)


def markdown_table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(item) for item in row) + " |")
    return "\n".join(lines)


def latex_escape(value):
    return str(value).replace("_", r"\_").replace("%", r"\%")


def latex_table(caption, label, colspec, headers, rows):
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        rf"\caption{{{caption}}}",
        rf"\label{{{label}}}",
        rf"\begin{{tabular}}{{{colspec}}}",
        r"\toprule",
        " & ".join(latex_escape(header) for header in headers) + r" \\",
        r"\midrule",
    ]
    for row in rows:
        lines.append(" & ".join(latex_escape(item) for item in row) + r" \\")
    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}",
            r"\end{table}",
        ]
    )
    return "\n".join(lines) + "\n"


def render_import_table(rows):
    headers = [
        "Layout",
        "Import s",
        "RSS MiB",
        "Store bytes",
        "Store ovh",
        "L0 files",
        "Manifest bytes",
    ]
    table_rows = []
    for row in rows:
        table_rows.append(
            [
                LAYOUT_LABELS.get(row["layout"], row["layout"]),
                fmt_float(row["wall_time_seconds"], 2),
                rss_mib(row["max_rss_kb"]),
                fmt_int(row["store_bytes"]),
                row["store_bytes_overhead_vs_schema"],
                fmt_int(row["l0_files"]),
                fmt_int(row["manifest_bytes"]),
            ]
        )
    md = "# Table 6: SF1 Import and Store Write-Cost Proxies\n\n"
    md += markdown_table(headers, table_rows) + "\n"
    tex = latex_table(
        "SF1 import and store write-cost proxies.",
        "tab:seml0-write-import",
        "lrrrrrr",
        headers,
        table_rows,
    )
    return md, tex


def render_compaction_table(rows):
    headers = [
        "Source",
        "Phase",
        "Sel L0",
        "Out segs",
        "Est bytes",
        "Input bytes",
        "Output bytes",
        "Lat us",
        "Write bytes",
        "Read before",
        "Read after",
    ]
    table_rows = []
    for row in rows:
        table_rows.append(
            [
                SOURCE_LABELS.get(row["source"], row["source"]),
                row["phase"],
                fmt_int(row["selected_l0_segments"]),
                fmt_int(row["output_segments"]),
                fmt_int(row["estimated_rewrite_bytes"]),
                fmt_int(row["compaction_input_bytes"]),
                fmt_int(row["compaction_output_bytes"]),
                fmt_int(row["compaction_latency_sum_us"]),
                fmt_int(row["io_write_bytes_compaction"]),
                fmt_int(row["read_bytes_before"]),
                fmt_int(row["read_bytes_after"]),
            ]
        )
    md = "# Table 7: Feedback Compaction Cost Microbenchmark\n\n"
    md += markdown_table(headers, table_rows) + "\n"
    tex = latex_table(
        "Feedback compaction rewrite-cost accounting in the microbenchmark.",
        "tab:seml0-feedback-write-cost",
        "llrrrrrrrrr",
        headers,
        table_rows,
    )
    return md, tex


def main():
    parser = argparse.ArgumentParser(description="Render SemL0 P6.6 write-cost paper tables.")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument(
        "--out-dir",
        default="remote-logs/p6-6-paper-write-cost-tables-20260604",
        help="Output directory",
    )
    parser.add_argument("--import-csv", default=IMPORT_INPUT)
    parser.add_argument("--compaction-csv", default=COMPACTION_INPUT)
    args = parser.parse_args()

    root = Path(args.root)
    out_dir = root / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    import_path = root / args.import_csv
    compaction_path = root / args.compaction_csv
    import_rows = read_csv(import_path)
    compaction_rows = read_csv(compaction_path)

    import_md, import_tex = render_import_table(import_rows)
    compaction_md, compaction_tex = render_compaction_table(compaction_rows)

    (out_dir / "table6-write-cost-import-sf1.md").write_text(import_md, encoding="utf-8")
    (out_dir / "table6-write-cost-import-sf1.tex").write_text(import_tex, encoding="utf-8")
    (out_dir / "table7-feedback-compaction-cost.md").write_text(compaction_md, encoding="utf-8")
    (out_dir / "table7-feedback-compaction-cost.tex").write_text(compaction_tex, encoding="utf-8")
    combined = "\n\n".join(
        [
            "# SemL0 P6.6 Write-Cost Paper Tables",
            import_md.rstrip(),
            compaction_md.rstrip(),
        ]
    )
    (out_dir / "paper-write-cost-tables-combined.md").write_text(combined + "\n", encoding="utf-8")

    manifest = {
        "date_tag": "20260604",
        "output_dir": args.out_dir,
        "inputs": [args.import_csv, args.compaction_csv],
        "files": [
            "table6-write-cost-import-sf1.md",
            "table6-write-cost-import-sf1.tex",
            "table7-feedback-compaction-cost.md",
            "table7-feedback-compaction-cost.tex",
            "paper-write-cost-tables-combined.md",
        ],
        "notes": [
            "Table 6 uses existing P5.7/P5.8 SF1 import logs extracted by P6.5.",
            "Table 7 uses P6.5 feedback microbenchmarks with compaction metrics present.",
            "The compaction table is microbenchmark-scale and should not be presented as a large-scale write-overhead bound.",
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_dir}")


if __name__ == "__main__":
    main()
