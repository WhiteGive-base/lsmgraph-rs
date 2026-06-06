#!/usr/bin/env python3
"""Render E16 metadata and storage-cost tables from file-summary.tsv artifacts.

Each variant's ``file-summary.tsv`` is a simple key-value format::

    store    store/e11-schema-only-...
    files    2059
    bytes    3794892645
    l0_files 2059
    l0_bytes 1234567
    manifest_bytes 12345

Outputs (all written to ``--out-dir``):
- ``metadata-cost-summary.tsv``  — raw aggregated metrics
- ``metadata-cost-summary.md``  — paper-ready markdown table
- ``metadata-cost-analysis.md`` — narrative analysis
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_tsv(path: Path) -> Dict[str, str]:
    """Parse a simple key-value TSV into a dict.

    Blank lines and lines without a tab separator are ignored.
    """
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def to_float(value: Optional[str]) -> float:
    if value is None:
        return 0.0
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def to_int(value: Optional[str]) -> int:
    if value is None:
        return 0
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return 0


def fmt_int(value: int) -> str:
    if value == 0:
        return "0"
    return f"{value:,}"


def fmt_float(value: float, decimals: int = 2) -> str:
    return f"{value:.{decimals}f}" if value else "N/A"


def fmt_pct(value: float) -> str:
    return f"{value:.2f}%" if value else "N/A"


def fmt_bytes(value: int) -> str:
    """Human-readable byte string."""
    if value == 0:
        return "0 B"
    if not value:
        return "N/A"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"


def markdown_table(
    headers: List[str],
    rows: List[List[str]],
    align: Optional[List[str]] = None,
) -> str:
    out_lines: List[str] = []
    out_lines.append("| " + " | ".join(headers) + " |")
    sep = []
    for i, _ in enumerate(headers):
        if align and align[i] in ("l", "r", "c"):
            sep.append(f"---:{align[i]}")
        else:
            sep.append("---")
    out_lines.append("| " + " | ".join(sep) + " |")
    for row in rows:
        out_lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(out_lines)


def reduction_pct(base: int, value: int) -> float:
    """Return percentage reduction from base to value. 0 if base <= 0."""
    if base <= 0:
        return 0.0
    return (base - value) / base * 100


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class VariantMetrics:
    label: str
    files: int = 0
    bytes: int = 0
    l0_files: int = 0
    l0_bytes: int = 0
    manifest_bytes: int = 0
    level_count: int = 1

    @property
    def metadata_overhead_pct(self) -> float:
        if self.bytes <= 0:
            return 0.0
        return self.manifest_bytes / self.bytes * 100

    @property
    def l0_overhead_pct(self) -> float:
        if self.bytes <= 0:
            return 0.0
        return self.l0_bytes / self.bytes * 100

    @property
    def fanout(self) -> float:
        if self.level_count <= 0:
            return 0.0
        return self.l0_files / self.level_count


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def load_variant(path: Path, label: str) -> VariantMetrics:
    data = parse_tsv(path)
    return VariantMetrics(
        label=label,
        files=to_int(data.get("files")),
        bytes=to_int(data.get("bytes")),
        l0_files=to_int(data.get("l0_files")),
        l0_bytes=to_int(data.get("l0_bytes")),
        manifest_bytes=to_int(data.get("manifest_bytes")),
        level_count=to_int(data.get("level_count") or "1"),
    )


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_tsv(out_dir: Path, variants: List[VariantMetrics]) -> Path:
    """Write metadata-cost-summary.tsv."""
    path = out_dir / "metadata-cost-summary.tsv"
    headers = [
        "variant", "files", "bytes", "l0_files", "l0_bytes",
        "manifest_bytes", "metadata_overhead_pct", "l0_overhead_pct",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=headers, delimiter="\t", extrasaction="ignore"
        )
        writer.writeheader()
        for v in variants:
            writer.writerow({
                "variant": v.label,
                "files": v.files,
                "bytes": v.bytes,
                "l0_files": v.l0_files,
                "l0_bytes": v.l0_bytes,
                "manifest_bytes": v.manifest_bytes,
                "metadata_overhead_pct": fmt_float(v.metadata_overhead_pct),
                "l0_overhead_pct": fmt_float(v.l0_overhead_pct),
            })
    return path


def write_md_table(out_dir: Path, variants: List[VariantMetrics]) -> Path:
    """Write metadata-cost-summary.md with paper-ready table."""
    headers = [
        "System",
        "Total Files",
        "Total Bytes",
        "L0 Files",
        "L0 Bytes",
        "Manifest Bytes",
        "Metadata Overhead %",
        "L0 Overhead %",
    ]
    rows: List[List[str]] = []
    for v in variants:
        rows.append([
            v.label,
            fmt_int(v.files),
            fmt_bytes(v.bytes),
            fmt_int(v.l0_files),
            fmt_bytes(v.l0_bytes),
            fmt_bytes(v.manifest_bytes),
            fmt_pct(v.metadata_overhead_pct),
            fmt_pct(v.l0_overhead_pct),
        ])

    lines = [
        "## Metadata and Storage Cost\n",
        markdown_table(headers, rows),
        "",
    ]
    path = out_dir / "metadata-cost-summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_narrative(out_dir: Path, variants: List[VariantMetrics]) -> Path:
    """Write metadata-cost-analysis.md with narrative analysis."""
    # Index by label for easy lookup
    by_label = {v.label: v for v in variants}

    # Use schema as baseline if present
    baseline_label = "schema"
    baseline = by_label.get(baseline_label)
    if baseline is None and variants:
        baseline = variants[0]

    lines: List[str] = [
        "# Metadata Cost Analysis\n",
    ]

    if not variants:
        lines.append("*No data available.*\n")
        path = out_dir / "metadata-cost-analysis.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    # --- 1. L0 file count relative to schema baseline -------------------------
    if baseline and baseline.l0_files > 0:
        l0_lines = ["## L0 File Count\n"]
        for v in variants:
            if v.l0_files <= 0:
                continue
            if v.label == baseline.label:
                l0_lines.append(
                    f"- **{v.label}** (baseline): {fmt_int(v.l0_files)} L0 files."
                )
                continue
            delta = v.l0_files - baseline.l0_files
            if delta > 0:
                pct = delta / baseline.l0_files * 100
                l0_lines.append(
                    f"- **{v.label}** increases L0 file count by "
                    f"{pct:.1f}% vs schema baseline "
                    f"({fmt_int(v.l0_files)} vs {fmt_int(baseline.l0_files)} files)."
                )
            elif delta < 0:
                pct = -delta / baseline.l0_files * 100
                l0_lines.append(
                    f"- **{v.label}** reduces L0 file count by "
                    f"{pct:.1f}% vs schema baseline "
                    f"({fmt_int(v.l0_files)} vs {fmt_int(baseline.l0_files)} files)."
                )
            else:
                l0_lines.append(
                    f"- **{v.label}** matches schema baseline "
                    f"({fmt_int(v.l0_files)} files)."
                )
        lines.extend(l0_lines)
        lines.append("")

    # --- 2. L0 byte overhead -------------------------------------------------
    if baseline and baseline.bytes > 0:
        overhead_lines = ["## L0 Byte Overhead\n"]
        for v in variants:
            if v.bytes <= 0:
                continue
            l0_pct = v.l0_overhead_pct
            if v.l0_bytes == 0:
                overhead_lines.append(
                    f"- **{v.label}**: no L0 data (fully compacted)."
                )
            else:
                overhead_lines.append(
                    f"- **{v.label}**: L0 occupies {fmt_pct(l0_pct)} "
                    f"({fmt_bytes(v.l0_bytes)} / {fmt_bytes(v.bytes)})."
                )
        lines.extend(overhead_lines)
        lines.append("")

    # --- 3. Manifest overhead ------------------------------------------------
    manifest_lines = ["## Manifest Overhead\n"]
    manifest_variants = [v for v in variants if v.manifest_bytes > 0 and v.bytes > 0]
    if manifest_variants:
        max_pct = max(v.metadata_overhead_pct for v in manifest_variants)
        min_pct = min(v.metadata_overhead_pct for v in manifest_variants)
        max_v = next(v for v in manifest_variants if v.metadata_overhead_pct == max_pct)
        min_v = next(v for v in manifest_variants if v.metadata_overhead_pct == min_pct)
        spread = max_pct - min_pct
        manifest_lines.append(
            f"Manifest overhead remains bounded across all variants: "
            f"{fmt_pct(min_pct)} ({min_v.label}) – {fmt_pct(max_pct)} ({max_v.label}), "
            f"a spread of {fmt_pct(spread)}."
        )
        for v in manifest_variants:
            manifest_lines.append(
                f"- **{v.label}**: {fmt_pct(v.metadata_overhead_pct)} "
                f"({fmt_bytes(v.manifest_bytes)} manifest / "
                f"{fmt_bytes(v.bytes)} total)."
            )
    else:
        manifest_lines.append(
            "*Manifest bytes not available for any variant.*"
        )
    lines.extend(manifest_lines)
    lines.append("")

    # --- 4. Full-compact special case ----------------------------------------
    full_compact = by_label.get("full-compact") or by_label.get("full_compact")
    if full_compact:
        compact_lines = [
            "## Full-Compact Variant\n",
            f"The *full-compact* variant eliminates the L0 layer entirely, "
            f"reducing L0 file count to {fmt_int(full_compact.l0_files)} and "
            f"L0 bytes to {fmt_bytes(full_compact.l0_bytes)}. "
            f"This eliminates L0 scan overhead on read, but shifts "
            f"compaction pressure to L0\u2192L1, potentially increasing write "
            f"amplification during sustained updates.\n",
        ]
        lines.extend(compact_lines)

    # --- 5. Semantic indexing cost summary ------------------------------------
    benefit_scored = by_label.get("benefit-scored") or by_label.get("benefit_scored")
    full_semantic = by_label.get("full-semantic") or by_label.get("full_semantic")
    if benefit_scored and baseline:
        lines.append("## Semantic Indexing Cost\n")
        if baseline.bytes > 0:
            delta_pct = (benefit_scored.bytes - baseline.bytes) / baseline.bytes * 100
            if delta_pct >= 0:
                lines.append(
                    f"The metadata cost of semantic indexing (benefit-scored) "
                    f"increases total store size by {delta_pct:.1f}% vs schema baseline "
                    f"({fmt_bytes(benefit_scored.bytes)} vs {fmt_bytes(baseline.bytes)})."
                )
            else:
                lines.append(
                    f"Semantic indexing (benefit-scored) reduces total store size by "
                    f"{-delta_pct:.1f}% vs schema baseline "
                    f"({fmt_bytes(benefit_scored.bytes)} vs {fmt_bytes(baseline.bytes)})."
                )
        if baseline.l0_bytes > 0:
            l0_delta_pct = (benefit_scored.l0_bytes - baseline.l0_bytes) / baseline.l0_bytes * 100
            if l0_delta_pct >= 0:
                lines.append(
                    f"L0 byte overhead increase is {l0_delta_pct:.1f}% "
                    f"({fmt_bytes(benefit_scored.l0_bytes)} vs {fmt_bytes(baseline.l0_bytes)})."
                )
            else:
                lines.append(
                    f"L0 byte overhead reduction is {-l0_delta_pct:.1f}% "
                    f"({fmt_bytes(benefit_scored.l0_bytes)} vs {fmt_bytes(baseline.l0_bytes)})."
                )
    elif full_semantic and baseline:
        lines.append("## Semantic Indexing Cost\n")
        if baseline.bytes > 0:
            delta_pct = (full_semantic.bytes - baseline.bytes) / baseline.bytes * 100
            if delta_pct >= 0:
                lines.append(
                    f"The metadata cost of full semantic indexing "
                    f"increases total store size by {delta_pct:.1f}% vs schema baseline "
                    f"({fmt_bytes(full_semantic.bytes)} vs {fmt_bytes(baseline.bytes)})."
                )
            else:
                lines.append(
                    f"Full semantic indexing reduces total store size by "
                    f"{-delta_pct:.1f}% vs schema baseline "
                    f"({fmt_bytes(full_semantic.bytes)} vs {fmt_bytes(baseline.bytes)})."
                )
    else:
        lines.append(
            "## Semantic Indexing Cost\n\n"
            "*Semantic variant data not available for analysis.*\n"
        )

    path = out_dir / "metadata-cost-analysis.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

HELP_EPILOG = """
Example
-------
  python scripts/analysis/current/render-e16-metadata-cost-table.py \\
      --out-dir output/e16 \\
      --variant schema       store/e11-schema-only-20260606/file-summary.tsv \\
      --variant benefit-scored store/e11-benefit-scored-20260606/file-summary.tsv \\
      --variant naive        store/e11-naive-20260606/file-summary.tsv

Output files
------------
  output/e16/metadata-cost-summary.tsv     — raw aggregated metrics
  output/e16/metadata-cost-summary.md     — paper-ready markdown table
  output/e16/metadata-cost-analysis.md   — narrative analysis
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        type=Path,
        help="Directory where all output files are written.",
    )
    parser.add_argument(
        "--variant",
        nargs=2,
        action="append",
        metavar=("LABEL", "PATH"),
        help=(
            "One variant's label and the path to its file-summary.tsv. "
            "May be specified multiple times."
        ),
    )
    args = parser.parse_args()

    if not args.variant:
        parser.error("at least one --variant is required")

    variants: List[VariantMetrics] = []
    for label, path_str in args.variant:
        path = Path(path_str)
        m = load_variant(path, label)
        variants.append(m)
        if m.files == 0 and m.bytes == 0:
            print(f"WARNING: no data found in {path}", file=sys.stderr)

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    tsv_path = write_tsv(out_dir, variants)
    print(tsv_path)

    md_path = write_md_table(out_dir, variants)
    print(md_path)

    narrative_path = write_narrative(out_dir, variants)
    print(narrative_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
