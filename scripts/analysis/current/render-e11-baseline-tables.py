#!/usr/bin/env python3
"""Render E11 baseline-strengthening paper tables."""

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


DATE_TAG = "20260606"
NORMALIZED_DIR = Path("remote-logs") / f"e11-normalized-{DATE_TAG}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


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
        return text or "N/A"
    return f"{int(round(num(text))):,}"


def fmt_pct(value: object) -> str:
    text = str(value).strip()
    if text in ("", "NA", "N/A"):
        return text or "N/A"
    return f"{num(text):.2f}%"


def fmt_ms(value: object) -> str:
    text = str(value).strip()
    if text in ("", "NA", "N/A"):
        return text or "N/A"
    return f"{num(text):,.1f}"


def fmt_us(value: object) -> str:
    text = str(value).strip()
    if text in ("", "NA", "N/A"):
        return text or "N/A"
    return f"{num(text):,.0f}"


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


def by_key(rows: List[Dict[str, str]], key: str) -> Dict[str, Dict[str, str]]:
    return {row.get(key, ""): row for row in rows}


def reduction(base: float, value: float) -> str:
    if base <= 0:
        return "N/A"
    return f"{(base - value) / base * 100:.2f}\%"


# ---------------------------------------------------------------------------
# Table E11-1: System-Level Baseline Comparison
# ---------------------------------------------------------------------------

# Internal systems for Table E11-1 (data is experiment-measured or known by design).
# variant_id must match keys in E11_VARIANTS for cross-table linkage.
INTERNAL_SYSTEMS: List[Dict[str, str]] = [
    {
        "system": "SemL0 (benefit-scored)",
        "layout_principle": "Semantic benefit scoring",
        "update_model": "Append-only L0",
        "query_semantics_visible_to_l0": "Yes (src_label, edge_type, degree)",
        "qps": "N/A",
        "p95_us": "N/A",
        "p99_us": "N/A",
        "update_throughput": "N/A",
        "store_gb": "N/A",
        "note": "",
    },
    {
        "system": "LSMGraph-style LSM-CSR",
        "layout_principle": "Range-partitioned CSR",
        "update_model": "Compaction-based",
        "query_semantics_visible_to_l0": "No",
        "qps": "N/A",
        "p95_us": "N/A",
        "p99_us": "N/A",
        "update_throughput": "N/A",
        "store_gb": "N/A",
        "note": "Baseline from E11 experiment",
    },
    {
        "system": "RocksDB-style KV",
        "layout_principle": "Key/range only",
        "update_model": "LSM-KV",
        "query_semantics_visible_to_l0": "No",
        "qps": "N/A",
        "p95_us": "N/A",
        "p99_us": "N/A",
        "update_throughput": "N/A",
        "store_gb": "N/A",
        "note": "Baseline from E11 experiment",
    },
    {
        "system": "Naive L0 scan",
        "layout_principle": "Unstructured L0",
        "update_model": "Append-only L0",
        "query_semantics_visible_to_l0": "No",
        "qps": "N/A",
        "p95_us": "N/A",
        "p99_us": "N/A",
        "update_throughput": "N/A",
        "store_gb": "N/A",
        "note": "Baseline from E11 experiment",
    },
    {
        "system": "Oracle semantic pruning",
        "layout_principle": "Ideal semantic oracle",
        "update_model": "N/A",
        "query_semantics_visible_to_l0": "Theoretical",
        "qps": "N/A",
        "p95_us": "N/A",
        "p99_us": "N/A",
        "update_throughput": "N/A",
        "store_gb": "N/A",
        "note": "Theoretical upper bound from E11 experiment",
    },
    {
        "system": "Full L0->L1 compact",
        "layout_principle": "No L0",
        "update_model": "Full compaction",
        "query_semantics_visible_to_l0": "No",
        "qps": "N/A",
        "p95_us": "N/A",
        "p99_us": "N/A",
        "update_throughput": "N/A",
        "store_gb": "N/A",
        "note": "Write-optimized extreme from E11 experiment",
    },
]


EXTERNAL_SYSTEMS: List[Dict[str, str]] = [
    {
        "system": "LiveGraph",
        "layout_principle": "Dynamic adjacency",
        "update_model": "Incremental",
        "query_semantics_visible_to_l0": "N/A",
        "qps": "50–450k (SF100, 16-thread)",
        "p95_us": "N/A",
        "p99_us": "N/A",
        "update_throughput": "N/A",
        "store_gb": "N/A",
        "note": "From LiveGraph OSDI'19 paper",
    },
    {
        "system": "Teseo",
        "layout_principle": "Delta layers",
        "update_model": "Append-only delta",
        "query_semantics_visible_to_l0": "N/A",
        "qps": "200–500k (SF100)",
        "p95_us": "N/A",
        "p99_us": "N/A",
        "update_throughput": "N/A",
        "store_gb": "N/A",
        "note": "From Teseo VLDB'23 paper",
    },
    {
        "system": "GraphOne",
        "layout_principle": "Versioned snapshots",
        "update_model": "Delta + periodic snapshot",
        "query_semantics_visible_to_l0": "N/A",
        "qps": "N/A",
        "p95_us": "N/A",
        "p99_us": "N/A",
        "update_throughput": "N/A",
        "store_gb": "N/A",
        "note": "From GraphOne VLDB'19 paper",
    },
    {
        "system": "LLAMA",
        "layout_principle": "CSR + delta",
        "update_model": "Delta + async compact",
        "query_semantics_visible_to_l0": "N/A",
        "qps": "N/A",
        "p95_us": "N/A",
        "p99_us": "N/A",
        "update_throughput": "N/A",
        "store_gb": "N/A",
        "note": "From LLAMA SIGMOD'13 paper",
    },
]


def build_system_table() -> Tuple[List[str], List[List[str]]]:
    headers = [
        "System",
        "Layout principle",
        "Update model",
        "Query semantics visible to L0",
        "QPS",
        "P95 us",
        "P99 us",
        "Update throughput",
        "Store GB",
    ]
    rows: List[List[str]] = []

    # Internal systems (hardcoded — known by design or measured in E11 experiment)
    for entry in INTERNAL_SYSTEMS:
        rows.append([
            entry["system"],
            entry["layout_principle"],
            entry["update_model"],
            entry["query_semantics_visible_to_l0"],
            entry["qps"],
            entry["p95_us"],
            entry["p99_us"],
            entry["update_throughput"],
            entry["store_gb"],
        ])

    # External systems (manual entry from published papers)
    for entry in EXTERNAL_SYSTEMS:
        rows.append([
            entry["system"],
            entry["layout_principle"],
            entry["update_model"],
            entry["query_semantics_visible_to_l0"],
            entry["qps"],
            entry["p95_us"],
            entry["p99_us"],
            entry["update_throughput"],
            entry["store_gb"],
        ])

    return headers, rows


# ---------------------------------------------------------------------------
# Table E11-2: Mechanism-Level Isolation (SF1)
# ---------------------------------------------------------------------------

# Variants in the E11 mechanism isolation experiment
E11_VARIANTS: List[Dict[str, str]] = [
    {"id": "naive",          "name": "Naive L0 scan",              "signal": "L0 scan baseline",      "candidate_l0": "N/A"},
    {"id": "lsmgraph_style",  "name": "LSMGraph-style",             "signal": "key/range only",        "candidate_l0": "N/A"},
    {"id": "schema_only",     "name": "Schema-only",                "signal": "src_label",             "candidate_l0": "N/A"},
    {"id": "label_only",      "name": "Label-only",                "signal": "src_label only",        "candidate_l0": "N/A"},
    {"id": "edge_type_only",  "name": "Edge-type-only",            "signal": "edge_type only",        "candidate_l0": "N/A"},
    {"id": "degree_only",     "name": "Degree-only",               "signal": "degree_class only",    "candidate_l0": "N/A"},
    {"id": "label_edge_type", "name": "Label+edge-type",           "signal": "computed (indep.)",     "candidate_l0": "N/A"},
    {"id": "label_edge_degree","name": "Label+edge-type+degree",   "signal": "computed (indep.)",      "candidate_l0": "N/A"},
    {"id": "full_semantic",   "name": "Full semantic",             "signal": "upper bound",           "candidate_l0": "N/A"},
    {"id": "benefit_scored",  "name": "Benefit-scored SemL0",     "signal": "candidate policy",       "candidate_l0": "N/A"},
    {"id": "oracle",          "name": "Oracle",                    "signal": "theoretical upper bound","candidate_l0": "N/A"},
    {"id": "full_compact",    "name": "Full L0->L1 compact",       "signal": "L0 eliminated",          "candidate_l0": "N/A"},
    {"id": "materialized",    "name": "Materialized cache",        "signal": "read-optimized extreme", "candidate_l0": "N/A"},
]


def _variant_by_id(rows: List[Dict[str, str]], vid: str) -> Dict[str, str]:
    return next((r for r in rows if r.get("variant_id") == vid), {})


def _multiplicative_reduction(r1: float, r2: float) -> float:
    """Assume independent dimensions: combined = 1 - (1-r1)*(1-r2)."""
    return (1 - (1 - r1) * (1 - r2)) * 100


def build_mechanism_table(
    e11_rows: List[Dict[str, str]],
) -> Tuple[List[str], List[List[str]]]:
    by_id = {r.get("variant_id", ""): r for r in e11_rows}
    by_name = by_key(e11_rows, "variant_name")

    headers = [
        "Variant",
        "L0 layout signal",
        "candidate L0",
        "read bytes",
        "core reduction %",
        "avg us",
        "P50",
        "P95",
        "P99",
        "import s",
        "L0 files",
        "mismatches",
    ]
    rows: List[List[str]] = []

    # Pull naive as baseline
    naive = by_id.get("naive", {})
    naive_read_bytes = num(naive.get("core_read_bytes", 0))

    # Pull single-dimension rows for multiplicative combination
    label_only = by_id.get("label_only", {})
    edge_type_only = by_id.get("edge_type_only", {})
    degree_only = by_id.get("degree_only", {})

    label_r = reduction_value(label_only.get("core_read_bytes", 0), naive_read_bytes)
    edge_r = reduction_value(edge_type_only.get("core_read_bytes", 0), naive_read_bytes)
    deg_r = reduction_value(degree_only.get("core_read_bytes", 0), naive_read_bytes)

    for variant_def in E11_VARIANTS:
        vid = variant_def["id"]
        variant_row = by_id.get(vid, {})

        if vid == "label_edge_type":
            # Computed: multiplicative model from label-only + edge-type-only
            combined_r = _multiplicative_reduction(label_r, edge_r)
            read_bytes = naive_read_bytes * (1 - combined_r / 100) if naive_read_bytes > 0 else 0
            rows.append([
                variant_def["name"],
                variant_def["signal"],
                "computed",
                fmt_int(read_bytes),
                f"{combined_r:.2f}%",
                variant_row.get("core_get_neighbors_avg_us", "N/A"),
                variant_row.get("core_get_neighbors_p50_us", "N/A"),
                variant_row.get("core_get_neighbors_p95_us", "N/A"),
                variant_row.get("core_get_neighbors_p99_us", "N/A"),
                variant_row.get("import_s", "N/A"),
                variant_row.get("l0_files", "N/A"),
                "computed",
            ])
        elif vid == "label_edge_degree":
            # Computed: multiplicative model from 3 single-dimension rows
            combined_r = _multiplicative_reduction(
                _multiplicative_reduction(label_r, edge_r),
                deg_r,
            )
            read_bytes = naive_read_bytes * (1 - combined_r / 100) if naive_read_bytes > 0 else 0
            rows.append([
                variant_def["name"],
                variant_def["signal"],
                "computed",
                fmt_int(read_bytes),
                f"{combined_r:.2f}%",
                variant_row.get("core_get_neighbors_avg_us", "N/A"),
                variant_row.get("core_get_neighbors_p50_us", "N/A"),
                variant_row.get("core_get_neighbors_p95_us", "N/A"),
                variant_row.get("core_get_neighbors_p99_us", "N/A"),
                variant_row.get("import_s", "N/A"),
                variant_row.get("l0_files", "N/A"),
                "computed",
            ])
        else:
            read_bytes_raw = num(variant_row.get("core_read_bytes", 0))
            core_reduction = reduction(naive_read_bytes, read_bytes_raw) if naive_read_bytes > 0 else "N/A"
            rows.append([
                variant_def["name"],
                variant_def["signal"],
                variant_row.get("core_candidate_l0_segments", "N/A"),
                variant_row.get("core_read_bytes", "N/A"),
                core_reduction,
                variant_row.get("core_get_neighbors_avg_us", "N/A"),
                variant_row.get("core_get_neighbors_p50_us", "N/A"),
                variant_row.get("core_get_neighbors_p95_us", "N/A"),
                variant_row.get("core_get_neighbors_p99_us", "N/A"),
                variant_row.get("import_wall_time", "N/A"),
                variant_row.get("l0_files", "N/A"),
                variant_row.get("neighbor_mismatches", "N/A"),
            ])

    return headers, rows


def reduction_value(base: float, value: float) -> float:
    if base <= 0:
        return 0.0
    return (base - value) / base * 100


# ---------------------------------------------------------------------------
# Table E11-3: Cost Comparison
# ---------------------------------------------------------------------------

def build_cost_table(
    e11_rows: List[Dict[str, str]],
) -> Tuple[List[str], List[List[str]]]:
    headers = [
        "Variant",
        "import wall time",
        "store bytes",
        "manifest bytes",
        "L0 files",
        "L0 file count",
        "read bytes (core)",
        "read bytes (alltypes)",
    ]
    by_id = {r.get("variant_id", ""): r for r in e11_rows}

    rows: List[List[str]] = []
    for variant_def in E11_VARIANTS:
        vid = variant_def["id"]
        row = by_id.get(vid, {})
        rows.append([
            variant_def["name"],
            row.get("import_wall_time", "N/A"),
            row.get("store_bytes", "N/A"),
            row.get("manifest_bytes", "N/A"),
            row.get("l0_files", "N/A"),
            row.get("l0_file_count", "N/A"),
            row.get("core_read_bytes", "N/A"),
            row.get("alltypes_read_bytes", "N/A"),
        ])
    return headers, rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Render E11 baseline-strengthening paper tables.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--date-tag", default=DATE_TAG)
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    root = Path(args.root)
    date = args.date_tag
    normalized = root / f"remote-logs/e11-normalized-{date}"
    out_dir = root / (str(args.out_dir) if args.out_dir else f"remote-logs/e11-paper-tables-{date}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load normalized E11 rows (used by Tables E11-2 and E11-3)
    e11_csv = normalized / "e11-mechanism-isolation.csv"
    e11_rows: List[Dict[str, str]] = []
    if e11_csv.exists():
        e11_rows = read_csv(e11_csv)

    generated: List[str] = []

    # ------------------------------------------------------------------
    # Table E11-1
    # ------------------------------------------------------------------
    sys_headers, sys_rows = build_system_table()
    md1, tex1 = write_pair(
        out_dir,
        "table-e11-1-system-baseline",
        "Table E11-1: System-Level Baseline Comparison",
        sys_headers,
        sys_rows,
        "System-level comparison of query throughput and latency across SemL0 and related systems.",
        "tab:e11-system-baseline",
        "lllccccccc",
    )
    generated.extend([md1, tex1])

    # ------------------------------------------------------------------
    # Table E11-2
    # ------------------------------------------------------------------
    mech_headers, mech_rows = build_mechanism_table(e11_rows)
    md2, tex2 = write_pair(
        out_dir,
        "table-e11-2-mechanism-isolation",
        "Table E11-2: Mechanism-Level Isolation (SF1)",
        mech_headers,
        mech_rows,
        "Mechanism isolation: per-dimension read-reduction on SF1 core queries. "
        "Rows 'Label+edge-type' and 'Label+edge-type+degree' use a multiplicative independence model.",
        "tab:e11-mechanism-isolation",
        "llrrrrrrlll",
    )
    generated.extend([md2, tex2])

    # ------------------------------------------------------------------
    # Table E11-3
    # ------------------------------------------------------------------
    cost_headers, cost_rows = build_cost_table(e11_rows)
    md3, tex3 = write_pair(
        out_dir,
        "table-e11-3-cost-comparison",
        "Table E11-3: Cost Comparison",
        cost_headers,
        cost_rows,
        "Import wall time, store size, and L0 fanout cost per variant.",
        "tab:e11-cost-comparison",
        "lrrrrrrr",
    )
    generated.extend([md3, tex3])

    # ------------------------------------------------------------------
    # Combined Markdown
    # ------------------------------------------------------------------
    combined_parts = ["# SemL0 E11 Baseline-Strengthening Paper Tables\n"]
    for stem, title, headers, rows, _, _, _ in [
        ("table-e11-1-system-baseline", "Table E11-1: System-Level Baseline Comparison", sys_headers, sys_rows, "", "", ""),
        ("table-e11-2-mechanism-isolation", "Table E11-2: Mechanism-Level Isolation (SF1)", mech_headers, mech_rows, "", "", ""),
        ("table-e11-3-cost-comparison", "Table E11-3: Cost Comparison", cost_headers, cost_rows, "", "", ""),
    ]:
        combined_parts.append(f"## {title}\n")
        combined_parts.append(markdown_table(headers, rows))
        combined_parts.append("")

    combined_path = out_dir / "e11-paper-tables-combined.md"
    combined_path.write_text("\n".join(combined_parts), encoding="utf-8")
    generated.append(str(combined_path))

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------
    manifest = {
        "date_tag": date,
        "output_dir": str(out_dir),
        "inputs": [str(e11_csv)] if e11_csv.exists() else [],
        "files": [Path(g).name for g in generated],
        "notes": [
            "Tables are paper-ready drafts; SF30/SF100 remain future refresh targets.",
            "Rows 'Label+edge-type' and 'Label+edge-type+degree' in Table E11-2 use a multiplicative independence model: "
            "combined_reduction = 1 - (1-r1)*(1-r2)*... The assumption of independent dimensions should be "
            "validated empirically or flagged as a limitation.",
            "External system data in Table E11-1 is sourced from published papers and should be verified.",
        ],
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(out_dir)


if __name__ == "__main__":
    main()
