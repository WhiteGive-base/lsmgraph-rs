#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple


DATE_TAG = "20260604"
NORMALIZED_DIR = Path("remote-logs") / f"p5-normalized-evaluation-{DATE_TAG}"


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(value: object) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in ("", "NA", "N/A", "baseline"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def fmt_int(value: object) -> str:
    text = str(value).strip()
    if text in ("", "NA", "N/A"):
        return text or "NA"
    return f"{int(round(num(text))):,}"


def fmt_pct(value: object) -> str:
    text = str(value).strip()
    if text in ("", "NA", "N/A"):
        return text or "NA"
    return f"{num(text):.2f}%"


def fmt_ms(value: object) -> str:
    text = str(value).strip()
    if text in ("", "NA", "N/A"):
        return text or "NA"
    return f"{num(text):,.1f}"


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    out = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(out)


def latex_escape(value: object) -> str:
    text = str(value)
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text


def latex_table(
    caption: str,
    label: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    alignment: str,
) -> str:
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        rf"\caption{{{latex_escape(caption)}}}",
        rf"\label{{{latex_escape(label)}}}",
        rf"\begin{{tabular}}{{{alignment}}}",
        r"\toprule",
        " & ".join(latex_escape(h) for h in headers) + r" \\",
        r"\midrule",
    ]
    for row in rows:
        lines.append(" & ".join(latex_escape(cell) for cell in row) + r" \\")
    lines.extend([r"\bottomrule", r"\end{tabular}", r"\end{table}"])
    return "\n".join(lines)


def write_pair(
    out_dir: Path,
    stem: str,
    title: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    caption: str,
    label: str,
    alignment: str,
) -> Tuple[str, str]:
    md = f"# {title}\n\n" + markdown_table(headers, rows) + "\n"
    tex = latex_table(caption, label, headers, rows, alignment) + "\n"
    md_path = out_dir / f"{stem}.md"
    tex_path = out_dir / f"{stem}.tex"
    md_path.write_text(md, encoding="utf-8")
    tex_path.write_text(tex, encoding="utf-8")
    return str(md_path), str(tex_path)


def by_key(rows: Iterable[Dict[str, str]], key: str) -> Dict[str, Dict[str, str]]:
    return {row.get(key, ""): row for row in rows}


def build_main_table(
    benefit_rows: List[Dict[str, str]],
    controlled_rows: List[Dict[str, str]],
) -> Tuple[List[str], List[List[str]]]:
    rows = by_key(benefit_rows, "variant")
    controlled = by_key(controlled_rows, "layout")
    order = [
        ("schema", "Schema"),
        ("full_semantic", "Full semantic"),
        ("benefit_scored", "Benefit scored"),
    ]
    table_rows: List[List[str]] = []
    for key, label in order:
        row = rows[key]
        if key == "schema":
            correctness = "baseline"
        elif key == "full_semantic":
            controlled_row = controlled.get("Full semantic", {})
            correctness = "pass" if controlled_row.get("neighbor_mismatches") == "0" else "check"
        else:
            correctness = "pass" if row.get("core_mismatches") == "0" and row.get("alltypes_mismatches") == "0" else "check"
        table_rows.append(
            [
                label,
                fmt_int(row.get("store_bytes")),
                fmt_int(row.get("l0_files")),
                fmt_int(row.get("core_read_bytes")),
                fmt_pct(row.get("core_read_reduction_pct")),
                fmt_int(row.get("alltypes_read_bytes")),
                fmt_pct(row.get("alltypes_read_reduction_pct")),
                correctness,
            ]
        )
    headers = [
        "Layout",
        "Store bytes",
        "L0 files",
        "Core read bytes",
        "Core reduction",
        "All-types read bytes",
        "All-types reduction",
        "Correctness",
    ]
    return headers, table_rows


def build_policy_table(
    benefit_rows: List[Dict[str, str]],
    controlled_rows: List[Dict[str, str]],
) -> Tuple[List[str], List[List[str]]]:
    benefit = by_key(benefit_rows, "variant")
    controlled = by_key(controlled_rows, "layout")
    entries = [
        (
            "Full semantic",
            "upper bound",
            benefit["full_semantic"].get("l0_files"),
            benefit["full_semantic"].get("manifest_bytes"),
            benefit["full_semantic"].get("core_read_reduction_pct"),
            benefit["full_semantic"].get("alltypes_read_reduction_pct"),
            "pass",
        ),
        (
            "Default budgeted",
            "fanout-only control",
            controlled["Budgeted semantic"].get("l0_files"),
            controlled["Budgeted semantic"].get("manifest_bytes"),
            controlled["Budgeted semantic"].get("core_read_reduction_pct"),
            controlled["Budgeted semantic"].get("alltypes_read_reduction_pct"),
            "pass, no read gain",
        ),
        (
            "Benefit scored",
            "candidate policy",
            benefit["benefit_scored"].get("l0_files"),
            benefit["benefit_scored"].get("manifest_bytes"),
            benefit["benefit_scored"].get("core_read_reduction_pct"),
            benefit["benefit_scored"].get("alltypes_read_reduction_pct"),
            "pass",
        ),
    ]
    headers = ["Policy", "Role", "L0 files", "Manifest bytes", "Core reduction", "All-types reduction", "Status"]
    rows = [
        [name, role, fmt_int(l0), fmt_int(manifest), fmt_pct(core), fmt_pct(alltypes), status]
        for name, role, l0, manifest, core, alltypes, status in entries
    ]
    return headers, rows


def build_feedback_table(rows: List[Dict[str, str]]) -> Tuple[List[str], List[List[str]]]:
    headers = ["Variant", "Phase", "Before L0", "After L0", "Selected L0", "Output segs", "Range changed"]
    table_rows = []
    for row in rows:
        table_rows.append(
            [
                row.get("variant", ""),
                row.get("phase", ""),
                fmt_int(row.get("before_candidate_l0")),
                fmt_int(row.get("after_candidate_l0")),
                fmt_int(row.get("selected_l0_segments")),
                fmt_int(row.get("output_segments")),
                row.get("selected_range_changed", ""),
            ]
        )
    return headers, table_rows


def build_correctness_table(rows: List[Dict[str, str]]) -> Tuple[List[str], List[List[str]]]:
    groups = [
        ("schema", ["schema_epoch", "schema_catalog", "new_edge_label", "property_presence", "legacy_metadata"]),
        ("snapshot/delta", ["tombstone_snapshot", "compaction_history", "degree_invalidation", "snapshot_gc"]),
        ("schema+snapshot", ["schema_snapshot", "property_snapshot", "property_tombstone"]),
    ]
    by_category = by_key(rows, "category")
    headers = ["Area", "Cases", "Passed", "Gate"]
    table_rows = []
    for area, categories in groups:
        present = [by_category[category] for category in categories if category in by_category]
        passed = sum(1 for row in present if row.get("latest_status") == "pass")
        gate = present[0].get("latest_gate", "") if present else ""
        table_rows.append([area, str(len(present)), str(passed), gate])
    return headers, table_rows


def build_artifact_table() -> Tuple[List[str], List[List[str]]]:
    headers = ["Goal", "Command or artifact", "Output"]
    rows = [
        [
            "Latest controlled SF1",
            "DATE_TAG=20260604-p57 bash run-p1-c1-latest-sf1-controlled.sh",
            "seml0-stage-p5-7-latest-code-sf1-controlled-results-20260604.md",
        ],
        [
            "Latest benefit-scored SF1",
            "BASE_DATE_TAG=20260604-p57 DATE_TAG=20260604-p58 INCLUDE_THRESHOLD_ROWS=false bash run-p1-c1-benefit-scored-sf1.sh",
            "remote-logs/p1-c1-sf1-benefit-scored-20260604-p58/summary.tsv",
        ],
        [
            "P5 normalized CSVs",
            "python3 summarize-p5-evaluation-tables.py --root . --out-dir remote-logs/p5-normalized-evaluation-20260604",
            "remote-logs/p5-normalized-evaluation-20260604/",
        ],
        [
            "P5 paper tables",
            "python3 render-p5-paper-tables.py --root . --out-dir remote-logs/p5-paper-tables-20260604",
            "remote-logs/p5-paper-tables-20260604/",
        ],
        [
            "Correctness gate",
            "cargo test --test engine_tests; cargo test --lib",
            "26 engine tests; 16 lib tests after P4.5",
        ],
    ]
    return headers, rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--out-dir", default=f"remote-logs/p5-paper-tables-{DATE_TAG}")
    args = parser.parse_args()

    root = Path(args.root)
    normalized = root / NORMALIZED_DIR
    out_dir = root / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    benefit_rows = read_csv(normalized / "latest_code_benefit_scored_sf1.csv")
    controlled_rows = read_csv(normalized / "latest_code_controlled_sf1.csv")
    feedback_rows = read_csv(normalized / "feedback_adaptation.csv")
    correctness_rows = read_csv(normalized / "correctness.csv")

    generated: List[str] = []

    specs = [
        (
            "table1-main-sf1-latest",
            "Table 1: Latest-Code SF1 Main Result",
            *build_main_table(benefit_rows, controlled_rows),
            "Latest-code SF1 read amplification and correctness.",
            "tab:seml0-main-sf1",
            "lrrrrrrl",
        ),
        (
            "table2-budget-policy-comparison",
            "Table 2: Budget Policy Comparison",
            *build_policy_table(benefit_rows, controlled_rows),
            "Policy-level fanout and read-reduction comparison.",
            "tab:seml0-budget-policy",
            "llrrrrl",
        ),
        (
            "table3-feedback-adaptation",
            "Table 3: Feedback Adaptation",
            *build_feedback_table(feedback_rows),
            "Feedback-driven semantic compaction across workload phases.",
            "tab:seml0-feedback",
            "llrrrrl",
        ),
        (
            "table4-correctness-summary",
            "Table 4: Correctness Summary",
            *build_correctness_table(correctness_rows),
            "Correctness evidence for schema evolution, deltas, and snapshots.",
            "tab:seml0-correctness",
            "lrrl",
        ),
        (
            "table5-artifact-commands",
            "Table 5: Artifact Commands",
            *build_artifact_table(),
            "Commands and artifacts used to reproduce the table package.",
            "tab:seml0-artifacts",
            "lll",
        ),
    ]

    combined_parts = ["# SemL0 P5 Paper Table Package", ""]
    for stem, title, headers, rows, caption, label, alignment in specs:
        md_path, tex_path = write_pair(out_dir, stem, title, headers, rows, caption, label, alignment)
        generated.extend([md_path, tex_path])
        combined_parts.append(f"## {title}")
        combined_parts.append("")
        combined_parts.append(markdown_table(headers, rows))
        combined_parts.append("")

    combined = out_dir / "paper-tables-combined.md"
    combined.write_text("\n".join(combined_parts), encoding="utf-8")
    generated.append(str(combined))

    manifest = {
        "date_tag": DATE_TAG,
        "output_dir": str(out_dir),
        "inputs": [
            str(normalized / "latest_code_benefit_scored_sf1.csv"),
            str(normalized / "latest_code_controlled_sf1.csv"),
            str(normalized / "feedback_adaptation.csv"),
            str(normalized / "correctness.csv"),
        ],
        "files": [Path(path).name for path in generated],
        "notes": [
            "Table 1 uses P5.8 latest-code benefit-scored rows.",
            "Table 2 keeps P5.7 default budgeted as a negative policy-control row.",
            "Tables are paper-ready drafts; SF30/SF100 remain future refresh targets.",
        ],
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(out_dir)


if __name__ == "__main__":
    main()
