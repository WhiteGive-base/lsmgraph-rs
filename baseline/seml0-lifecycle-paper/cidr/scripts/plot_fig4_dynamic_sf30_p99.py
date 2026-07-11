#!/usr/bin/env python3
"""Standalone generator for Figure 4: SF30 dynamic p99 latency."""

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

DYNAMIC_SF30 = [
    {"t_s": 300, "schema_p99_us": 1270.59, "budg_b64_p99_us": 1275.69, "semantic_p99_us": 414.58},
    {"t_s": 600, "schema_p99_us": 2689.06, "budg_b64_p99_us": 2668.46, "semantic_p99_us": 557.76},
    {"t_s": 900, "schema_p99_us": 4070.45, "budg_b64_p99_us": 4019.79, "semantic_p99_us": 792.06},
    {"t_s": 1200, "schema_p99_us": 5493.15, "budg_b64_p99_us": 5558.31, "semantic_p99_us": 958.19},
    {"t_s": 1500, "schema_p99_us": 6886.92, "budg_b64_p99_us": 7029.10, "semantic_p99_us": 1096.84},
    {"t_s": 1800, "schema_p99_us": 8740.23, "budg_b64_p99_us": 8255.77, "semantic_p99_us": 1273.97},
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


def plot_dynamic_sf30(out_dir: Path) -> None:
    t = np.array([row["t_s"] for row in DYNAMIC_SF30])
    fig, ax = plt.subplots(figsize=(6.4, 3.15), constrained_layout=True)

    series = [
        ("schema_p99_us", "Schema", PALETTE["blue"], "o"),
        ("budg_b64_p99_us", "Budget b64", PALETTE["cyan"], "s"),
        ("semantic_p99_us", "Semantic", PALETTE["green"], "D"),
    ]
    for key, label, color, marker in series:
        y = np.array([row[key] for row in DYNAMIC_SF30])
        ax.plot(t, y, color=color, marker=marker, linewidth=1.7, markersize=4.5, label=label)

    ax.set_yscale("log")
    ax.set_xlabel("Elapsed time (s)")
    ax.set_ylabel("P99 latency (us, log)")
    ax.set_title("SF30 dynamic mixed read/write run")
    ax.legend(frameon=False, loc="upper left")
    ax.set_xticks(t)
    ax.text(
        0.98,
        0.08,
        "30 min, 6 checkpoints, ~163 q/s, 0 writer errors",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=7,
        color=PALETTE["dark"],
    )
    save_figure(fig, out_dir, "fig4_dynamic_sf30_p99")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Figure 4.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    configure_matplotlib()
    plot_dynamic_sf30(args.out_dir.resolve())
    print(f"Wrote Figure 4 to {args.out_dir.resolve()}")


if __name__ == "__main__":
    main()
