#!/usr/bin/env python3
"""Standalone generator for Figure 3: C2 lifecycle retention."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np


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
    "naive": {"color": PALETTE["gray"], "hatch": "///"},
    "semantic": {"color": PALETTE["green"], "hatch": ""},
}

C2 = [
    {"scale": "SF1 synth", "policy": "naive", "ret": 0.0, "outputs": 1, "write_amp": 1.22, "read_after": 4.0},
    {"scale": "SF1 synth", "policy": "semantic", "ret": 1.0, "outputs": 4, "write_amp": 1.87, "read_after": 1.0},
    {"scale": "SF10c synth", "policy": "naive", "ret": 0.0, "outputs": 1, "write_amp": 1.14, "read_after": 6.0},
    {"scale": "SF10c synth", "policy": "semantic", "ret": 1.0, "outputs": 6, "write_amp": 1.85, "read_after": 1.0},
    {"scale": "SF30 real", "policy": "naive", "ret": 0.0, "outputs": 503, "write_amp": 1.07, "read_after": 6.52},
    {"scale": "SF30 real", "policy": "semantic", "ret": 1.0, "outputs": 528, "write_amp": 1.24, "read_after": 1.0},
]


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
    for ext in ("pdf", "svg", "png"):
        kwargs = {"dpi": 220} if ext == "png" else {}
        fig.savefig(out_dir / f"{stem}.{ext}", **kwargs)
    plt.close(fig)


def add_panel_label(ax: mpl.axes.Axes, label: str) -> None:
    ax.text(-0.08, 1.05, label, transform=ax.transAxes, ha="left", va="bottom", fontweight="bold", color=PALETTE["dark"])


def autolabel_bars(ax: mpl.axes.Axes, bars, *, fmt: str = "{:.2g}", dy: float = 2) -> None:
    for bar in bars:
        height = bar.get_height()
        ax.annotate(
            fmt.format(height),
            xy=(bar.get_x() + bar.get_width() / 2, height),
            xytext=(0, dy),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=6.8,
            color=PALETTE["dark"],
        )


def c2_value(scale: str, policy: str, key: str) -> float:
    for row in C2:
        if row["scale"] == scale and row["policy"] == policy:
            return float(row[key])
    raise KeyError((scale, policy, key))


def plot_c2_lifecycle(out_dir: Path) -> None:
    scales = ["SF1 synth", "SF10c synth", "SF30 real"]
    x = np.arange(len(scales))
    width = 0.36
    policies = [
        ("naive", STYLE["naive"], "Naive merge"),
        ("semantic", STYLE["semantic"], "Semantic merge"),
    ]

    fig, axes = plt.subplots(1, 3, figsize=(10.8, 3.45), constrained_layout=True)

    panels = [
        ("ret", "Semantic surface retention", "Retention", (0, 1.12), "{:.1f}"),
        ("write_amp", "Write amplification", "Write amp", (0.95, 2.05), "{:.2f}"),
        ("read_after", "Candidate-byte proxy", "Ratio (exact-before=1)", (0, 8.0), "{:.2g}x"),
    ]

    for ax, (key, title, ylabel, ylim, fmt) in zip(axes, panels):
        for offset, (policy, style, label) in zip([-width / 2, width / 2], policies):
            values = [c2_value(scale, policy, key) for scale in scales]
            bars = ax.bar(
                x + offset,
                values,
                width,
                label=label,
                color=style["color"],
                hatch=style["hatch"],
                edgecolor="#374151",
                linewidth=0.45,
            )
            if key != "ret":
                autolabel_bars(ax, bars, fmt=fmt)
            else:
                for bar, value in zip(bars, values):
                    y = max(value, 0.015)
                    ax.annotate(
                        fmt.format(value),
                        xy=(bar.get_x() + bar.get_width() / 2, y),
                        xytext=(0, 2),
                        textcoords="offset points",
                        ha="center",
                        va="bottom",
                        fontsize=6.8,
                        color=PALETTE["dark"],
                    )
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.set_ylim(*ylim)
        ax.set_xticks(x, scales, rotation=18, ha="right")

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        frameon=False,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.085),
        ncol=2,
        handlelength=2.0,
        columnspacing=1.4,
    )
    for i, ax in enumerate(axes):
        add_panel_label(ax, f"({chr(ord('a') + i)})")

    axes[2].text(
        0.98,
        0.955,
        "SF30 real: metadata replay",
        transform=axes[2].transAxes,
        ha="right",
        va="top",
        fontsize=7,
        color=PALETTE["dark"],
        bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.0, "alpha": 0.90},
    )
    fig.suptitle("C2 lifecycle retention under compaction", y=1.175, fontweight="bold")
    save_figure(fig, out_dir, "fig3_c2_lifecycle_retention")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Figure 3.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    configure_matplotlib()
    plot_c2_lifecycle(args.out_dir.resolve())
    print(f"Wrote Figure 3 to {args.out_dir.resolve()}")


if __name__ == "__main__":
    main()
