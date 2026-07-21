#!/usr/bin/env python3
"""Plot six clearly labelled provisional diagnostics from normalized legacy data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import TwoSlopeNorm
from matplotlib.ticker import FuncFormatter

from figure_common import PALETTE, SERIES_STYLE, configure_matplotlib, hide_minor_tick_labels, panel_label, save_figure


HERE = Path(__file__).resolve().parent
DEFAULT_DATA = HERE.parents[1] / "data" / "normalized" / "provisional"
DEFAULT_OUT = HERE.parent / "output" / "provisional"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if not rows:
        raise ValueError(f"No rows in {path}")
    return rows


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def value(row: dict[str, str], field: str) -> float:
    raw = row.get(field, "").strip()
    if not raw:
        return math.nan
    return float(raw.replace(",", ""))


def system_short(name: str) -> str:
    return {
        "Aster RocksGraph": "Aster",
        "Neo4j Community": "Neo4j",
        "NebulaGraph": "Nebula",
        "SemL0-budg-b64": "SemL0-B64",
    }.get(name, name)


def provisional_title(fig: plt.Figure, title: str, subtitle: str) -> None:
    fig.suptitle(title, fontweight="bold")
    layout_engine = fig.get_layout_engine()
    if layout_engine is not None and hasattr(layout_engine, "set"):
        layout_engine.set(rect=(0.0, 0.06, 1.0, 0.94))
    fig.text(
        0.995,
        0.018,
        subtitle,
        ha="right",
        va="bottom",
        fontsize=6.1,
        color="#5B616A",
    )


def plot_context(data_dir: Path, out_dir: Path) -> dict[str, object]:
    external = [
        row
        for row in read_tsv(data_dir / "external-sf10-context.tsv")
        if row["scope"] == "all-types-summary"
    ]
    w6 = read_tsv(data_dir / "w6-layout-typed-neighbor.tsv")
    b64 = next(row for row in w6 if row["scale"] == "SF10" and row["variant"] == "budg-b64")
    resources = read_tsv(data_dir / "w6-sf10-import-resource.tsv")
    b64_resource = next(row for row in resources if row["variant"] == "budg-b64")

    rows: list[dict[str, object]] = [
        {
            "system": "SemL0-B64",
            "ops": value(b64, "ops_per_repeat"),
            "avg_us": value(b64, "avg_us_mean"),
            "p50_us": value(b64, "p50_us_mean"),
            "p99_us": value(b64, "p99_us_mean"),
            "load_s": value(b64_resource, "elapsed_s"),
            "disk_bytes": value(b64, "store_gib") * 1024**3,
        }
    ]
    for row in external:
        rows.append(
            {
                "system": system_short(row["system"]),
                "ops": value(row, "ops"),
                "avg_us": value(row, "avg_us"),
                "p50_us": value(row, "p50_us"),
                "p99_us": value(row, "p99_us"),
                "load_s": value(row, "load_s"),
                "disk_bytes": value(row, "disk_total_bytes"),
            }
        )
    order = ["SemL0-B64", "LiveGraph", "Aster", "TuGraph", "Nebula", "Neo4j"]
    by_name = {str(row["system"]): row for row in rows}
    rows = [by_name[name] for name in order if name in by_name]
    colors = [PALETTE["cyan"]] + [PALETTE["gray"]] * (len(rows) - 1)

    fig, axes = plt.subplots(1, 3, figsize=(7.05, 2.55), layout="constrained")
    x = np.arange(len(rows))
    axes[0].bar(x, [row["ops"] for row in rows], color=colors, edgecolor="#4B5159", linewidth=0.45)
    axes[0].set_yscale("log")
    hide_minor_tick_labels(axes[0], x=False)
    axes[0].set_ylabel("Timed operations (log)")
    short_ticks = {
        "SemL0-B64": "S-L0",
        "LiveGraph": "LiveG",
        "Aster": "Ast",
        "TuGraph": "TuG",
        "Nebula": "Neb",
        "Neo4j": "Neo4j",
    }
    axes[0].set_xticks(x, [short_ticks[str(row["system"])] for row in rows], rotation=34, ha="right")
    axes[0].set_title("Protocols use different query counts")
    panel_label(axes[0], "(a)")

    y = np.arange(len(rows))
    for index, row in enumerate(rows):
        color = colors[index]
        avg = float(row["avg_us"])
        p50 = float(row["p50_us"])
        p99 = float(row["p99_us"])
        axes[1].scatter(avg, index, color=color, marker="o", s=18, zorder=3)
        if math.isfinite(p50) and math.isfinite(p99):
            axes[1].plot([p50, p99], [index, index], color=color, linewidth=1.1)
            axes[1].scatter(p99, index, color=color, marker="D", s=16, zorder=3)
        elif not math.isfinite(p99):
            axes[1].annotate("P99 N/A", (avg, index), xytext=(4, 0), textcoords="offset points", va="center", fontsize=6.1)
    axes[1].set_xscale("log")
    hide_minor_tick_labels(axes[1], y=False)
    axes[1].set_yticks(y, [row["system"] for row in rows])
    axes[1].invert_yaxis()
    axes[1].set_xlabel("Historical latency (us, log)")
    axes[1].set_title("Context only: timing is not matched")
    panel_label(axes[1], "(b)")

    label_offsets = {
        "SemL0-B64": (-12, 5),
        "Neo4j": (5, 7),
        "Aster": (4, 4),
        "TuGraph": (4, -10),
        "Nebula": (4, 4),
        "LiveGraph": (4, 4),
    }
    for index, row in enumerate(rows):
        disk_gib = float(row["disk_bytes"]) / 1024**3
        load_s = float(row["load_s"])
        if not (math.isfinite(disk_gib) and math.isfinite(load_s)):
            continue
        axes[2].scatter(disk_gib, load_s, color=colors[index], marker="D" if index == 0 else "o", s=24)
        axes[2].annotate(
            "SemL0" if row["system"] == "SemL0-B64" else row["system"],
            (disk_gib, load_s),
            xytext=label_offsets[str(row["system"])],
            textcoords="offset points",
            fontsize=6.1,
        )
    axes[2].set_xscale("log")
    axes[2].set_yscale("log")
    hide_minor_tick_labels(axes[2])
    axes[2].set_xlabel("Disk footprint (GiB, log)")
    axes[2].set_ylabel("Load/import wall time (s, log)")
    axes[2].set_title("Different hardware and load boundary")
    panel_label(axes[2], "(c)")

    provisional_title(fig, "Legacy SF10 system context — NOT a head-to-head comparison", "SemL0: W6 9 core types; external: 34 signed types; different API/query count; old hardware")
    return save_figure(fig, out_dir, "fig1_context_not_matched")


def plot_layout_ablation(data_dir: Path, out_dir: Path) -> dict[str, object]:
    rows = read_tsv(data_dir / "w6-layout-typed-neighbor.tsv")
    variants = ["naive", "schema", "edge-type-only", "budg-b64", "budg-b256", "budg-b1024", "semantic", "oracle"]
    labels = ["Naive", "Schema", "Type", "B64", "B256", "B1024", "Full", "Oracle"]
    styles = {"SF10": (PALETTE["blue"], "o"), "SF100": (PALETTE["orange"], "s")}
    metrics = [
        ("avg_us_mean", "Average latency (us, log)", "(a)"),
        ("p99_us_mean", "P99 latency (us, log)", "(b)"),
        ("candidate_per_op", "Candidate L0 / op (log)", "(c)"),
        ("read_bytes_per_op", "Logical read bytes / op (log)", "(d)"),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(7.05, 3.85), layout="constrained", sharex=True)
    x = np.arange(len(variants))
    for ax, (field, ylabel, label) in zip(axes.flat, metrics):
        for scale, (color, marker) in styles.items():
            selected = {row["variant"]: row for row in rows if row["scale"] == scale}
            ys = [value(selected[name], field) if name in selected else math.nan for name in variants]
            ax.plot(x, ys, color=color, marker=marker, linewidth=1.25, markersize=3.8, label=scale)
        ax.set_yscale("log")
        hide_minor_tick_labels(ax, x=False)
        ax.margins(y=0.16)
        ax.set_ylabel(ylabel)
        ax.set_xticks(x, labels, rotation=25, ha="right")
        panel_label(ax, label)
    axes[0, 0].legend(frameon=False, ncols=2, loc="upper right")
    provisional_title(fig, "W6 layout/budget diagnostic — not the A0–A6 component staircase", "3 repeats share one process/store; separate neighbor-compare gate; no causal component isolation")
    return save_figure(fig, out_dir, "fig2_layout_ablation_provisional")


def plot_import_resource(data_dir: Path, out_dir: Path) -> dict[str, object]:
    rows = read_tsv(data_dir / "w6-sf10-import-resource.tsv")
    order = ["naive", "schema", "budg-b64", "budg-b256", "budg-b1024", "semantic"]
    rows_by = {row["variant"]: row for row in rows}
    rows = [rows_by[name] for name in order]
    labels = ["Naive", "Schema", "B64", "B256", "B1024", "Full"]
    colors = [SERIES_STYLE.get(name, {"color": PALETTE["cyan"]})["color"] for name in order]
    metrics = [
        ("elapsed_s", "Import wall time (s)", "(a)"),
        (None, "Import CPU time (user + sys, s)", "(b)"),
        ("max_rss_gib", "Import peak RSS (GiB)", "(c)"),
        ("store_gib", "Final store footprint (GiB)", "(d)"),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(7.05, 3.75), layout="constrained", sharex=True)
    x = np.arange(len(rows))
    for ax, (field, ylabel, label) in zip(axes.flat, metrics):
        ys = [value(row, field) if field else value(row, "user_cpu_s") + value(row, "sys_cpu_s") for row in rows]
        bars = ax.bar(x, ys, color=colors, edgecolor="#444A52", linewidth=0.45)
        ax.set_ylabel(ylabel)
        ax.set_xticks(x, labels, rotation=24, ha="right")
        panel_label(ax, label)
        if field == "store_gib":
            for bar, row in zip(bars, rows):
                ax.annotate(
                    f"{int(value(row, 'l0_files'))} files",
                    (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                    xytext=(0, 2),
                    textcoords="offset points",
                    ha="center",
                    va="bottom",
                    rotation=90,
                    fontsize=6.1,
                )
            ax.set_ylim(0, max(ys) * 1.32)
    provisional_title(fig, "W6 SF10 import-only performance–resource envelope", "Elapsed parser fixed: h:mm:ss retained; values are import phase, not steady-state query resources")
    return save_figure(fig, out_dir, "fig3_import_resource_provisional")


def plot_compaction(data_dir: Path, out_dir: Path) -> dict[str, object]:
    rows = read_tsv(data_dir / "rq3-compaction-provisional.tsv")
    styles = {
        "none": {"label": "None", "color": PALETTE["gray"], "marker": "o", "linestyle": "--"},
        "feedback": {"label": "Feedback", "color": PALETTE["purple"], "marker": "P", "linestyle": "-"},
        "full": {"label": "Blind full", "color": PALETTE["green"], "marker": "D", "linestyle": "-"},
    }
    metrics = [
        ("p99_us", "Read P99 (us, log)", "log", "(a)"),
        ("query_rate_s", "Achieved read QPS", "linear", "(b)"),
        ("l0_files", "Live L0 files", "linear", "(c)"),
        ("compaction_rewrite_bytes_cumulative", "Cumulative rewrite (MiB)", "linear", "(d)"),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(7.05, 3.8), layout="constrained", sharex=True)
    for ax, (field, ylabel, scale, label) in zip(axes.flat, metrics):
        for arm in ("none", "feedback", "full"):
            selected = sorted((row for row in rows if row["arm"] == arm), key=lambda row: value(row, "elapsed_s"))
            x = np.array([value(row, "elapsed_s") / 60 for row in selected])
            y = np.array([value(row, field) for row in selected])
            if field == "compaction_rewrite_bytes_cumulative":
                y /= 1024**2
            style = styles[arm]
            ax.plot(x, y, label=style["label"], color=style["color"], marker=style["marker"], linestyle=style["linestyle"], linewidth=1.25, markersize=3.8)
        if scale == "log":
            ax.set_yscale("log")
            hide_minor_tick_labels(ax, x=False)
        ax.set_ylabel(ylabel)
        ax.set_xlabel("Elapsed time (min)")
        panel_label(ax, label)
    axes[0, 0].legend(frameon=False, ncols=3, loc="upper left")
    provisional_title(fig, "RQ3 SF30 compaction mechanism trace — provisional n=1", "Arms used wall-clock RNG and lack digest/CPU/RSS; do not interpret checkpoints as independent repeats")
    return save_figure(fig, out_dir, "fig4_compaction_timeline_provisional")


def plot_workload_coverage(data_dir: Path, out_dir: Path) -> dict[str, object]:
    rows = read_tsv(data_dir / "w8-property-twohop.tsv")
    row_keys = [("property", "presence"), ("property", "equality"), ("property", "absent_default"), ("two-hop", "two-hop")]
    row_labels = ["Property present", "Equality*", "Absent/default", "Two-hop"]
    variants = ["budg-b64", "semantic"]
    col_labels = ["B64", "Full"]
    metrics = [
        ("elapsed_s_mean", "Elapsed speedup", "(a)"),
        ("read_bytes_mean", "Read-byte reduction", "(b)"),
        ("candidate_l0_mean", "Candidate reduction", "(c)"),
        ("body_reads_mean", "Body-read reduction", "(d)"),
    ]
    lookup = {(row["category"], row["predicate"], row["variant"]): row for row in rows}
    fig, axes = plt.subplots(2, 2, figsize=(7.05, 4.0), layout="constrained")
    norm = TwoSlopeNorm(vmin=-0.55, vcenter=0, vmax=0.55)
    image = None
    for ax, (field, title, label) in zip(axes.flat, metrics):
        matrix = np.full((len(row_keys), len(variants)), np.nan)
        ratios = np.full_like(matrix, np.nan)
        for i, (category, predicate) in enumerate(row_keys):
            baseline = lookup[(category, predicate, "schema")]
            for j, variant in enumerate(variants):
                candidate = lookup[(category, predicate, variant)]
                ratio = value(baseline, field) / value(candidate, field)
                ratios[i, j] = ratio
                matrix[i, j] = math.log2(ratio)
        image = ax.imshow(matrix, cmap="PuOr", norm=norm, aspect="auto")
        ax.set_xticks(np.arange(len(variants)), col_labels)
        ax.set_yticks(np.arange(len(row_keys)), row_labels)
        ax.set_title(title)
        panel_label(ax, label)
        for i in range(matrix.shape[0]):
            for j in range(matrix.shape[1]):
                text_color = "white" if abs(matrix[i, j]) > 0.35 else PALETTE["dark"]
                ax.text(j, i, f"{ratios[i, j]:.2f}x", ha="center", va="center", color=text_color, fontsize=6.4)
    assert image is not None
    colorbar = fig.colorbar(image, ax=axes, shrink=0.78, pad=0.02)
    colorbar.set_label("log2(schema / variant); >0 is improvement")
    provisional_title(fig, "W8 SF30 property/two-hop coverage — provisional", "Edge type 1 only; equality is a prototype; 3 in-process repeats; no versioned query digest")
    return save_figure(fig, out_dir, "fig5_workload_coverage_provisional")


def plot_scale(data_dir: Path, out_dir: Path) -> dict[str, object]:
    rows = read_tsv(data_dir / "w6-layout-typed-neighbor.tsv")
    variants = ["naive", "schema", "budg-b64", "semantic"]
    labels = {"naive": "Naive", "schema": "Schema", "budg-b64": "B64", "semantic": "Full"}
    metrics = [
        ("avg_us_mean", "Average latency (us, log)", "(a)"),
        ("p99_us_mean", "P99 latency (us, log)", "(b)"),
        ("candidate_per_op", "Candidate L0 / op (log)", "(c)"),
        ("store_gib", "Final store (GiB, log)", "(d)"),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(7.05, 3.75), layout="constrained", sharex=True)
    scale_x = np.array([10.0, 100.0])
    for ax, (field, ylabel, label) in zip(axes.flat, metrics):
        for variant in variants:
            selected = {row["scale"]: row for row in rows if row["variant"] == variant}
            ys = np.array([value(selected[scale], field) for scale in ("SF10", "SF100")])
            style = SERIES_STYLE[variant]
            ax.plot(scale_x, ys, label=labels[variant], color=style["color"], marker=style["marker"], linestyle=style["linestyle"], linewidth=1.25, markersize=3.8)
        ax.set_xscale("log")
        ax.set_yscale("log")
        hide_minor_tick_labels(ax)
        ax.set_xticks(scale_x, ["SF10", "SF100"])
        ax.xaxis.set_minor_formatter(FuncFormatter(lambda _value, _pos: ""))
        ax.set_ylabel(ylabel)
        panel_label(ax, label)
    axes[0, 0].legend(frameon=False, ncols=2, loc="upper left")
    provisional_title(fig, "W6 two-point scale diagnostic — not a scaling curve", "Query count differs (9k vs 45k); run commit/hardware absent; no slope or complexity claim")
    return save_figure(fig, out_dir, "fig6_two_point_scale_diagnostic")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    configure_matplotlib()
    reports = [
        plot_context(args.data_dir, args.out_dir),
        plot_layout_ablation(args.data_dir, args.out_dir),
        plot_import_resource(args.data_dir, args.out_dir),
        plot_compaction(args.data_dir, args.out_dir),
        plot_workload_coverage(args.data_dir, args.out_dir),
        plot_scale(args.data_dir, args.out_dir),
    ]
    for report in reports:
        print(f"{report['figure']}: {report['status']}")
    artifacts = sorted(path for path in args.out_dir.glob("fig*.*") if path.is_file())
    manifest = {
        "status": "PASS" if all(report["status"] == "PASS" for report in reports) else "REVIEW",
        "scope": "PROVISIONAL_ONLY",
        "generator": str(Path(__file__).resolve()),
        "generator_sha256": file_sha256(Path(__file__).resolve()),
        "style_sha256": file_sha256(HERE / "figure_common.py"),
        "normalized_data_manifest": str((args.data_dir / "manifest.json").resolve()),
        "normalized_data_manifest_sha256": file_sha256(args.data_dir / "manifest.json"),
        "artifacts": [
            {"path": str(path.resolve()), "sha256": file_sha256(path), "bytes": path.stat().st_size}
            for path in artifacts
        ],
    }
    (args.out_dir / "build-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
