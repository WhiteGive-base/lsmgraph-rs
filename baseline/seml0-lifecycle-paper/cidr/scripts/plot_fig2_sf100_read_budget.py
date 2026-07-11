#!/usr/bin/env python3
"""Standalone generator for Figure 2: SF100 read/budget tradeoff."""

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
    "budget": {"color": PALETTE["cyan"], "hatch": ""},
    "schema": {"color": PALETTE["blue"], "hatch": ""},
    "oracle": {"color": PALETTE["purple"], "hatch": ""},
    "stress": {"color": PALETTE["red"], "hatch": "xx"},
}

SF100 = [
    {"variant": "naive", "cand_l0": 49_257_601, "read_mib": 3461.6, "avg_us": 53_513.8, "p50_us": 53_889, "p90_us": 172_222, "p99_us": 179_259, "rss_gib": 2.43},
    {"variant": "kv-lsm", "cand_l0": 49_257_601, "read_mib": 3461.6, "avg_us": 53_892.0, "p50_us": 53_704, "p90_us": 172_315, "p99_us": 178_889, "rss_gib": 2.49},
    {"variant": "schema", "cand_l0": 5_929_197, "read_mib": 820.0, "avg_us": 9816.8, "p50_us": 10_259, "p90_us": 26_481, "p99_us": 37_333, "rss_gib": 2.42},
    {"variant": "edge-only", "cand_l0": 5_899_015, "read_mib": 819.9, "avg_us": 8676.5, "p50_us": 9111, "p90_us": 22_278, "p99_us": 31_074, "rss_gib": 2.53},
    {"variant": "budg-b64", "cand_l0": 6_022_507, "read_mib": 819.7, "avg_us": 8722.1, "p50_us": 9065, "p90_us": 22_500, "p99_us": 30_981, "rss_gib": 2.21},
    {"variant": "budg-b256", "cand_l0": 6_480_991, "read_mib": 818.4, "avg_us": 8588.0, "p50_us": 9065, "p90_us": 21_019, "p99_us": 31_019, "rss_gib": 2.49},
    {"variant": "budg-b1024", "cand_l0": 7_929_225, "read_mib": 816.4, "avg_us": 8564.5, "p50_us": 9120, "p90_us": 20_056, "p99_us": 31_148, "rss_gib": 2.68},
    {"variant": "semantic", "cand_l0": 7_782_877, "read_mib": 642.7, "avg_us": 8250.9, "p50_us": 8733, "p90_us": 20_989, "p99_us": 30_415, "rss_gib": 118.03},
    {"variant": "oracle", "cand_l0": 532_193, "read_mib": 1257.8, "avg_us": 9723.8, "p50_us": 10_028, "p90_us": 25_741, "p99_us": 36_704, "rss_gib": 2.67},
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


def autolabel_bars(ax: mpl.axes.Axes, bars, *, fmt: str = "{:.2g}", dy: float = 2, min_height: float | None = None) -> None:
    for bar in bars:
        height = bar.get_height()
        if min_height is not None and height < min_height:
            continue
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


def variant_style(variant: str) -> dict[str, str]:
    if variant in {"naive", "kv-lsm"}:
        return STYLE["naive"]
    if variant == "schema":
        return STYLE["schema"]
    if variant.startswith("budg") or variant == "edge-only":
        return STYLE["budget"]
    if variant == "semantic":
        return STYLE["stress"]
    if variant == "oracle":
        return STYLE["oracle"]
    return {"color": PALETTE["gray"], "hatch": ""}


def plot_sf100_read_budget(out_dir: Path) -> None:
    variants = [row["variant"] for row in SF100]
    x = np.arange(len(SF100))
    naive_cand = SF100[0]["cand_l0"]
    naive_read = SF100[0]["read_mib"]

    fig, axes = plt.subplots(1, 3, figsize=(11.8, 3.3), constrained_layout=True)

    ax = axes[0]
    width = 0.38
    cand_norm = np.array([row["cand_l0"] / naive_cand for row in SF100])
    read_norm = np.array([row["read_mib"] / naive_read for row in SF100])
    bars1 = ax.bar(x - width / 2, cand_norm, width, label="L0 candidates", color=PALETTE["blue"])
    bars2 = ax.bar(x + width / 2, read_norm, width, label="Read bytes", color=PALETTE["orange"])
    ax.set_yscale("log")
    ax.set_ylim(0.009, 1.5)
    ax.axhline(1.0, color="#94A3B8", linewidth=0.9, linestyle="--")
    ax.set_ylabel("Ratio to naive (naive=1; log scale)")
    ax.set_xticks(x, variants, rotation=35, ha="right")
    ax.set_title("Read amplification")
    ax.legend(frameon=True, facecolor="white", edgecolor="none", framealpha=0.9, loc="upper right")
    add_panel_label(ax, "(a)")
    autolabel_bars(ax, bars1, fmt="{:.2f}", min_height=0.95)
    autolabel_bars(ax, bars2, fmt="{:.2f}", min_height=0.95)

    ax = axes[1]
    metrics = [
        ("avg_us", "Avg", PALETTE["blue"], "o"),
        ("p50_us", "P50", PALETTE["green"], "s"),
        ("p90_us", "P90", PALETTE["orange"], "^"),
        ("p99_us", "P99", PALETTE["red"], "D"),
    ]
    for key, label, color, marker in metrics:
        ax.plot(x, [row[key] for row in SF100], marker=marker, linewidth=1.4, markersize=4, color=color, label=label)
    ax.set_yscale("log")
    ax.set_ylabel("Latency (us, log)")
    ax.set_xticks(x, variants, rotation=35, ha="right")
    ax.set_title("Latency distribution")
    ax.legend(frameon=False, ncol=2, loc="upper right")
    add_panel_label(ax, "(b)")

    ax = axes[2]
    colors = [variant_style(v)["color"] for v in variants]
    hatches = [variant_style(v)["hatch"] for v in variants]
    bars = ax.bar(x, [row["rss_gib"] for row in SF100], color=colors, edgecolor="#374151", linewidth=0.4)
    for bar, hatch in zip(bars, hatches):
        bar.set_hatch(hatch)
    ax.set_yscale("log")
    ax.set_ylim(1.8, 160)
    ax.set_ylabel("Peak import RSS (GiB, log)")
    ax.set_xticks(x, variants, rotation=35, ha="right")
    ax.set_title("Budget and memory cliff")
    semantic_idx = variants.index("semantic")
    semantic_rss = SF100[semantic_idx]["rss_gib"]
    ax.annotate(
        "118.03 GiB",
        xy=(semantic_idx, semantic_rss),
        xytext=(0, 4),
        textcoords="offset points",
        ha="center",
        va="bottom",
        fontsize=7,
        color=PALETTE["red"],
    )
    add_panel_label(ax, "(c)")

    fig.suptitle("SF100 read amplification and budget tradeoff", y=1.03, fontweight="bold")
    save_figure(fig, out_dir, "fig2_sf100_read_budget")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Figure 2.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    configure_matplotlib()
    plot_sf100_read_budget(args.out_dir.resolve())
    print(f"Wrote Figure 2 to {args.out_dir.resolve()}")


if __name__ == "__main__":
    main()
