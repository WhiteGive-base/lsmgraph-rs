#!/usr/bin/env python3
"""Standalone generator for Figure 5: baseline context, not head-to-head."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyBboxPatch


DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "images"

PALETTE = {
    "blue": "#2B6CB0",
    "cyan": "#2AA7B8",
    "green": "#2F855A",
    "orange": "#DD6B20",
    "red": "#C53030",
    "purple": "#805AD5",
    "gray": "#6B7280",
    "light_gray": "#E5E7EB",
    "dark": "#1F2937",
}

STYLE = {
    "internal": {"color": PALETTE["orange"], "hatch": "..."},
}

BASELINES = [
    {"system": "LiveGraph", "scale": "SF10", "avg_us": 391.063, "p99_us": None, "disk_gib": 36.00, "load_s": 1457.36, "kind": "external"},
    {"system": "Aster", "scale": "SF10", "avg_us": 1408.27, "p99_us": 15_809.60, "disk_gib": 9.69, "load_s": 794.262, "kind": "external"},
    {"system": "TuGraph", "scale": "SF10", "avg_us": 3775.45, "p99_us": 158_870, "disk_gib": 13.93, "load_s": 3939.95, "kind": "external"},
    {"system": "Nebula", "scale": "SF10", "avg_us": 744_146.62, "p99_us": 14_228_169.28, "disk_gib": 43.02, "load_s": 2493.47, "kind": "external"},
    {"system": "Neo4j", "scale": "SF10", "avg_us": 1_974_806.09, "p99_us": 34_522_340.13, "disk_gib": 15.52, "load_s": 216.845, "kind": "external"},
    {"system": "LSM-style*", "scale": "SF100", "avg_us": 556.741, "p99_us": 10_000, "disk_gib": 130.94, "load_s": None, "kind": "internal"},
]

EXTERNAL_ROWS = [row for row in BASELINES if row["kind"] == "external"]
INTERNAL_ROW = next(row for row in BASELINES if row["kind"] == "internal")


def configure_matplotlib() -> None:
    mpl.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 8.5,
            "axes.titlesize": 9.5,
            "axes.labelsize": 8.5,
            "xtick.labelsize": 7.5,
            "ytick.labelsize": 7.5,
            "legend.fontsize": 7.5,
            "figure.titlesize": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": "#E5E7EB",
            "grid.linewidth": 0.7,
            "grid.alpha": 1.0,
            "axes.axisbelow": True,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "svg.fonttype": "none",
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.03,
        }
    )


def save_figure(fig: mpl.figure.Figure, out_dir: Path, stem: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "svg"):
        fig.savefig(out_dir / f"{stem}.{ext}")
    plt.close(fig)


def add_panel_label(ax: mpl.axes.Axes, label: str) -> None:
    ax.text(-0.08, 1.05, label, transform=ax.transAxes, ha="left", va="bottom", fontweight="bold", color=PALETTE["dark"])


def plot_baseline_positioning(out_dir: Path) -> None:
    systems = [row["system"] for row in EXTERNAL_ROWS]
    x = np.arange(len(EXTERNAL_ROWS))

    fig, axes = plt.subplots(1, 4, figsize=(11.9, 3.25), constrained_layout=True, gridspec_kw={"width_ratios": [1.05, 1.0, 1.05, 0.76]})

    ax = axes[0]
    bars = ax.bar(
        x,
        [row["avg_us"] for row in EXTERNAL_ROWS],
        color=PALETTE["blue"],
        edgecolor="#374151",
        linewidth=0.45,
    )
    ax.set_yscale("log")
    ax.set_ylabel("Average latency (us, log)")
    ax.set_xticks(x, systems, rotation=28, ha="right")
    ax.set_title("External SF10 avg")
    add_panel_label(ax, "(a)")

    p99_rows = [row for row in EXTERNAL_ROWS if row["p99_us"] is not None]
    p99_systems = [row["system"] for row in p99_rows]
    p99_x = np.arange(len(p99_rows))
    ax = axes[1]
    bars = ax.bar(
        p99_x,
        [row["p99_us"] for row in p99_rows],
        color=PALETTE["cyan"],
        edgecolor="#374151",
        linewidth=0.45,
    )
    ax.set_yscale("log")
    ax.set_ylabel("P99 latency (us, log)")
    ax.set_xticks(p99_x, p99_systems, rotation=28, ha="right")
    ax.set_title("External SF10 p99")
    ax.text(
        0.02,
        0.94,
        "LiveGraph p99 not reported",
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=7,
        color=PALETTE["dark"],
    )
    add_panel_label(ax, "(b)")

    ax = axes[2]
    bars = ax.bar(
        x,
        [row["disk_gib"] for row in EXTERNAL_ROWS],
        color=PALETTE["green"],
        edgecolor="#374151",
        linewidth=0.45,
        alpha=0.92,
        label="Disk",
    )
    ax.set_ylabel("Disk footprint (GiB)")
    ax.set_xticks(x, systems, rotation=28, ha="right")
    ax.set_title("External SF10 footprint/load")

    ax2 = ax.twinx()
    load_x = [i for i, row in enumerate(EXTERNAL_ROWS) if row["load_s"] is not None]
    load_y = [row["load_s"] for row in EXTERNAL_ROWS if row["load_s"] is not None]
    ax2.plot(load_x, load_y, color=PALETTE["red"], marker="D", linewidth=1.3, markersize=4, label="Load time")
    ax2.set_ylabel("Load time (s)")
    ax2.spines["right"].set_visible(True)
    handles1, labels1 = ax.get_legend_handles_labels()
    handles2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(handles1 + handles2, labels1 + labels2, frameon=False, loc="upper left")
    add_panel_label(ax, "(c)")

    ax = axes[3]
    ax.set_axis_off()
    ax.set_title("Internal context", pad=8)
    add_panel_label(ax, "(d)")
    box = FancyBboxPatch(
        (0.04, 0.14),
        0.92,
        0.70,
        boxstyle="round,pad=0.02,rounding_size=0.03",
        facecolor="#FFF7ED",
        edgecolor=PALETTE["orange"],
        linewidth=1.0,
        transform=ax.transAxes,
    )
    ax.add_patch(box)
    ax.text(0.50, 0.74, "LSM-style* / SF100", transform=ax.transAxes, ha="center", va="center", fontweight="bold", color=PALETTE["dark"])
    ax.text(0.50, 0.58, f"avg {INTERNAL_ROW['avg_us']:.1f} us", transform=ax.transAxes, ha="center", va="center", color=PALETTE["dark"])
    ax.text(0.50, 0.46, f"p99 {INTERNAL_ROW['p99_us']:.0f} us", transform=ax.transAxes, ha="center", va="center", color=PALETTE["dark"])
    ax.text(0.50, 0.34, f"disk {INTERNAL_ROW['disk_gib']:.1f} GiB", transform=ax.transAxes, ha="center", va="center", color=PALETTE["dark"])
    ax.text(
        0.50,
        0.20,
        "internal layout row,\nnot an official external\nLSMGraph artifact",
        transform=ax.transAxes,
        ha="center",
        va="center",
        fontsize=7,
        color=PALETTE["dark"],
    )
    fig.suptitle("Baseline context, not head-to-head comparison", y=1.03, fontweight="bold")
    save_figure(fig, out_dir, "fig5_baseline_positioning")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Figure 5.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    configure_matplotlib()
    plot_baseline_positioning(args.out_dir.resolve())
    print(f"Wrote Figure 5 to {args.out_dir.resolve()}")


if __name__ == "__main__":
    main()
