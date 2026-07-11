#!/usr/bin/env python3
"""Generate CIDR paper figures for the SemL0 draft.

The data below is intentionally kept in this script so the figures are
reproducible without parsing the TeX draft.  Run from anywhere:

    python baseline/seml0-lifecycle-paper/cidr/scripts/plot_cidr_figures.py

Outputs are written to ../images by default, with both PDF and SVG variants.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import textwrap

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = ROOT / "images"


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
    "internal": {"color": PALETTE["orange"], "hatch": "..."},
}


SF100 = [
    {
        "variant": "naive",
        "cand_l0": 49_257_601,
        "read_mib": 3461.6,
        "avg_us": 53_513.8,
        "p50_us": 53_889,
        "p90_us": 172_222,
        "p99_us": 179_259,
        "rss_gib": 2.43,
    },
    {
        "variant": "kv-lsm",
        "cand_l0": 49_257_601,
        "read_mib": 3461.6,
        "avg_us": 53_892.0,
        "p50_us": 53_704,
        "p90_us": 172_315,
        "p99_us": 178_889,
        "rss_gib": 2.49,
    },
    {
        "variant": "schema",
        "cand_l0": 5_929_197,
        "read_mib": 820.0,
        "avg_us": 9816.8,
        "p50_us": 10_259,
        "p90_us": 26_481,
        "p99_us": 37_333,
        "rss_gib": 2.42,
    },
    {
        "variant": "edge-only",
        "cand_l0": 5_899_015,
        "read_mib": 819.9,
        "avg_us": 8676.5,
        "p50_us": 9111,
        "p90_us": 22_278,
        "p99_us": 31_074,
        "rss_gib": 2.53,
    },
    {
        "variant": "budg-b64",
        "cand_l0": 6_022_507,
        "read_mib": 819.7,
        "avg_us": 8722.1,
        "p50_us": 9065,
        "p90_us": 22_500,
        "p99_us": 30_981,
        "rss_gib": 2.21,
    },
    {
        "variant": "budg-b256",
        "cand_l0": 6_480_991,
        "read_mib": 818.4,
        "avg_us": 8588.0,
        "p50_us": 9065,
        "p90_us": 21_019,
        "p99_us": 31_019,
        "rss_gib": 2.49,
    },
    {
        "variant": "budg-b1024",
        "cand_l0": 7_929_225,
        "read_mib": 816.4,
        "avg_us": 8564.5,
        "p50_us": 9120,
        "p90_us": 20_056,
        "p99_us": 31_148,
        "rss_gib": 2.68,
    },
    {
        "variant": "semantic",
        "cand_l0": 7_782_877,
        "read_mib": 642.7,
        "avg_us": 8250.9,
        "p50_us": 8733,
        "p90_us": 20_989,
        "p99_us": 30_415,
        "rss_gib": 118.03,
    },
    {
        "variant": "oracle",
        "cand_l0": 532_193,
        "read_mib": 1257.8,
        "avg_us": 9723.8,
        "p50_us": 10_028,
        "p90_us": 25_741,
        "p99_us": 36_704,
        "rss_gib": 2.67,
    },
]


C2 = [
    {"scale": "SF1 synth", "policy": "naive", "ret": 0.0, "outputs": 1, "write_amp": 1.22, "read_after": 4.0},
    {"scale": "SF1 synth", "policy": "semantic", "ret": 1.0, "outputs": 4, "write_amp": 1.87, "read_after": 1.0},
    {"scale": "SF10c synth", "policy": "naive", "ret": 0.0, "outputs": 1, "write_amp": 1.14, "read_after": 6.0},
    {"scale": "SF10c synth", "policy": "semantic", "ret": 1.0, "outputs": 6, "write_amp": 1.85, "read_after": 1.0},
    {"scale": "SF30 real", "policy": "naive", "ret": 0.0, "outputs": 503, "write_amp": 1.07, "read_after": 6.52},
    {"scale": "SF30 real", "policy": "semantic", "ret": 1.0, "outputs": 528, "write_amp": 1.24, "read_after": 1.0},
]


DYNAMIC_SF30 = [
    {"t_s": 300, "schema_p99_us": 1270.59, "budg_b64_p99_us": 1275.69, "semantic_p99_us": 414.58},
    {"t_s": 600, "schema_p99_us": 2689.06, "budg_b64_p99_us": 2668.46, "semantic_p99_us": 557.76},
    {"t_s": 900, "schema_p99_us": 4070.45, "budg_b64_p99_us": 4019.79, "semantic_p99_us": 792.06},
    {"t_s": 1200, "schema_p99_us": 5493.15, "budg_b64_p99_us": 5558.31, "semantic_p99_us": 958.19},
    {"t_s": 1500, "schema_p99_us": 6886.92, "budg_b64_p99_us": 7029.10, "semantic_p99_us": 1096.84},
    {"t_s": 1800, "schema_p99_us": 8740.23, "budg_b64_p99_us": 8255.77, "semantic_p99_us": 1273.97},
]


BASELINES = [
    {
        "system": "LiveGraph",
        "scale": "SF10",
        "avg_us": 391.063,
        "p99_us": None,
        "disk_gib": 36.00,
        "load_s": 1457.36,
        "kind": "external",
    },
    {
        "system": "Aster",
        "scale": "SF10",
        "avg_us": 1408.27,
        "p99_us": 15_809.60,
        "disk_gib": 9.69,
        "load_s": 794.262,
        "kind": "external",
    },
    {
        "system": "TuGraph",
        "scale": "SF10",
        "avg_us": 3775.45,
        "p99_us": 158_870,
        "disk_gib": 13.93,
        "load_s": 3939.95,
        "kind": "external",
    },
    {
        "system": "Nebula",
        "scale": "SF10",
        "avg_us": 744_146.62,
        "p99_us": 14_228_169.28,
        "disk_gib": 43.02,
        "load_s": 2493.47,
        "kind": "external",
    },
    {
        "system": "Neo4j",
        "scale": "SF10",
        "avg_us": 1_974_806.09,
        "p99_us": 34_522_340.13,
        "disk_gib": 15.52,
        "load_s": 216.845,
        "kind": "external",
    },
    {
        "system": "LSM-style*",
        "scale": "SF100",
        "avg_us": 556.741,
        "p99_us": 10_000,
        "disk_gib": 130.94,
        "load_s": None,
        "kind": "internal",
    },
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
    for ext in ("pdf", "svg"):
        fig.savefig(out_dir / f"{stem}.{ext}")
    plt.close(fig)


def plot_control_plane_architecture(out_dir: Path) -> None:
    script = Path(__file__).resolve().parent / "plot_fig1_system_control_plane.py"
    subprocess.run([sys.executable, str(script), "--out-dir", str(out_dir)], check=True)


def plot_semantic_evidence_lifecycle(out_dir: Path) -> None:
    script = Path(__file__).resolve().parent / "plot_fig2_semantic_evidence_lifecycle.py"
    subprocess.run([sys.executable, str(script), "--out-dir", str(out_dir)], check=True)


def add_panel_label(ax: mpl.axes.Axes, label: str) -> None:
    ax.text(
        -0.08,
        1.05,
        label,
        transform=ax.transAxes,
        ha="left",
        va="bottom",
        fontweight="bold",
        color=PALETTE["dark"],
    )


def autolabel_bars(
    ax: mpl.axes.Axes,
    bars,
    *,
    fmt: str = "{:.2g}",
    dy: float = 2,
    min_height: float | None = None,
) -> None:
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


def plot_system_control_plane(out_dir: Path) -> None:
    fig, ax = plt.subplots(figsize=(11.6, 4.25))
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    red = "#E11D24"
    frame = "#0F3342"
    text = PALETTE["dark"]
    blue_fill = "#EAF4FB"
    green_fill = "#E8F5EE"
    storage_fill = "#F3F4F6"

    def box(x, y, w, h, label, *, fc="#FFFFFF", ec="#334155", lw=1.05, fs=8.5, bold=False):
        patch = FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.006,rounding_size=0.006",
            linewidth=lw,
            edgecolor=ec,
            facecolor=fc,
        )
        ax.add_patch(patch)
        ax.text(
            x + w / 2,
            y + h / 2,
            label,
            ha="center",
            va="center",
            fontsize=fs,
            fontweight="bold" if bold else "normal",
            color=text,
            linespacing=1.08,
        )
        return patch

    def arrow(start, end, *, color=red, rad=0.0, lw=1.45, dashed=False):
        arr = FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=10.5,
            linewidth=lw,
            color=color,
            linestyle="--" if dashed else "-",
            connectionstyle=f"arc3,rad={rad}",
        )
        ax.add_patch(arr)
        return arr

    def step(x, y, n):
        circ = mpl.patches.Circle((x, y), 0.021, facecolor=red, edgecolor=red, zorder=6)
        ax.add_patch(circ)
        ax.text(x, y - 0.001, str(n), ha="center", va="center", fontsize=8.2, color="white", fontweight="bold", zorder=7)

    def mini_segment_stack(x, y, w, h, n=3, color="#CBD5E1"):
        for i in range(n):
            dx = i * 0.012
            dy = i * 0.012
            ax.add_patch(
                mpl.patches.Rectangle(
                    (x + dx, y + dy),
                    w,
                    h,
                    facecolor="#FFFFFF",
                    edgecolor="#7B8794",
                    linewidth=0.85,
                )
            )
        top_x = x + (n - 1) * 0.012
        top_y = y + (n - 1) * 0.012
        for col in range(5):
            for row in range(2):
                ax.add_patch(
                    mpl.patches.Rectangle(
                        (top_x + 0.015 + col * 0.017, top_y + 0.020 + row * 0.024),
                        0.013,
                        0.016,
                        facecolor=color if row == 1 else "#FFFFFF",
                        edgecolor="#8A8F98",
                        linewidth=0.45,
                    )
                )

    def mini_table(x, y, w, h):
        ax.add_patch(mpl.patches.Rectangle((x, y), w, h, facecolor="#FFFFFF", edgecolor="#7B8794", linewidth=0.85))
        rows = ["sig", "state", "epoch"]
        vals = ["L,T", "Exact", "e17"]
        for i in range(1, 3):
            yy = y + h * i / 3
            ax.plot([x, x + w], [yy, yy], color="#CBD5E1", linewidth=0.65)
        ax.plot([x + w * 0.44, x + w * 0.44], [y, y + h], color="#CBD5E1", linewidth=0.65)
        for i, (left, right) in enumerate(zip(rows, vals)):
            yy = y + h - (i + 0.5) * h / 3
            ax.text(x + w * 0.22, yy, left, ha="center", va="center", fontsize=6.2, color=text)
            ax.text(x + w * 0.72, yy, right, ha="center", va="center", fontsize=6.2, color=text)

    # Memory/storage frame.
    ax.add_patch(mpl.patches.Rectangle((0.025, 0.25), 0.95, 0.62, fill=False, edgecolor=frame, linewidth=1.2))
    ax.add_patch(mpl.patches.Rectangle((0.025, 0.07), 0.95, 0.18, facecolor=storage_fill, edgecolor=frame, linewidth=1.2))
    ax.plot([0.085, 0.085], [0.07, 0.87], color=frame, linewidth=1.0)
    ax.plot([0.335, 0.335], [0.25, 0.87], color="#94A3B8", linewidth=1.0)
    ax.plot([0.635, 0.635], [0.25, 0.87], color="#111827", linewidth=1.8, linestyle="--")
    ax.text(0.055, 0.57, "Memory", ha="center", va="center", fontsize=9.5, fontweight="bold", color=text)
    ax.text(0.055, 0.155, "Storage", ha="center", va="center", fontsize=9.5, fontweight="bold", color=text)

    ax.text(0.205, 0.895, "Property-Graph Query Interface", ha="center", va="bottom", fontsize=9.5, fontweight="bold", color=text)
    ax.text(0.485, 0.895, "SemL0 Query-Semantic Control Plane", ha="center", va="bottom", fontsize=9.5, fontweight="bold", color=text)
    ax.text(0.805, 0.895, "LSM-style Graph Store", ha="center", va="bottom", fontsize=9.5, fontweight="bold", color=text)

    # Query side.
    box(0.105, 0.67, 0.19, 0.125, "Typed-neighbor /\nproperty read", fc="#FFFFFF", ec=frame, lw=1.05, fs=8.6)
    box(0.105, 0.40, 0.19, 0.185, "", fc=blue_fill, ec=frame, lw=1.05)
    ax.text(0.200, 0.555, "Signature compiler", fontsize=8.8, ha="center", va="center", color=text, fontweight="bold")
    ax.text(0.128, 0.510, "src label", fontsize=6.8, ha="left", va="center", color=text)
    ax.text(0.128, 0.480, "edge type", fontsize=6.8, ha="left", va="center", color=text)
    ax.text(0.128, 0.450, "predicate", fontsize=6.8, ha="left", va="center", color=text)
    ax.text(0.218, 0.510, "Person", fontsize=6.8, ha="left", va="center", color=PALETTE["blue"])
    ax.text(0.218, 0.480, "knows", fontsize=6.8, ha="left", va="center", color=PALETTE["blue"])
    ax.text(0.218, 0.450, "epoch e17", fontsize=6.8, ha="left", va="center", color=PALETTE["blue"])

    # Control plane center.
    box(0.365, 0.39, 0.235, 0.37, "", fc=green_fill, ec=PALETTE["green"], lw=1.25)
    ax.text(0.482, 0.725, "SemL0 query-semantic\ncontrol plane", ha="center", va="center", fontsize=8.8, fontweight="bold", color=text)
    box(0.385, 0.635, 0.195, 0.050, "Signature registry", fc="#FFFFFF", ec="#9CA3AF", fs=7.5)
    box(0.385, 0.565, 0.195, 0.050, "Exactness contract", fc="#FFFFFF", ec="#9CA3AF", fs=7.5)
    box(0.385, 0.495, 0.195, 0.050, "Budgeted rewrite policy", fc="#FFFFFF", ec="#9CA3AF", fs=7.5)
    for x, label, color in [
        (0.385, "Exact", PALETTE["green"]),
        (0.455, "Cons.", PALETTE["orange"]),
        (0.525, "Unknown", PALETTE["red"]),
    ]:
        box(x, 0.420, 0.055, 0.040, label, fc="#FFFFFF", ec=color, lw=1.0, fs=6.8, bold=True)

    # Store-side memory components.
    box(0.675, 0.635, 0.245, 0.150, "", fc="#FFFFFF", ec=frame, lw=1.05)
    ax.text(0.797, 0.752, "Read admission", ha="center", va="center", fontsize=8.8, fontweight="bold", color=text)
    ax.text(0.695, 0.705, "Exact disjoint -> SKIP", fontsize=7.0, ha="left", color=PALETTE["green"])
    ax.text(0.695, 0.675, "Conservative/Unknown -> ADMIT", fontsize=7.0, ha="left", color=PALETTE["orange"])
    mini_table(0.842, 0.660, 0.060, 0.075)

    box(0.675, 0.380, 0.245, 0.150, "", fc="#FFFFFF", ec=frame, lw=1.05)
    ax.text(0.797, 0.497, "Semantic-aware compaction", ha="center", va="center", fontsize=8.8, fontweight="bold", color=text)
    ax.text(0.695, 0.450, "Preserve exact semantic partitions", fontsize=7.0, ha="left", color=text)
    ax.text(0.695, 0.420, "Mixed/unsafe outputs -> Conservative", fontsize=7.0, ha="left", color=text)

    # Storage row.
    box(0.105, 0.105, 0.19, 0.105, "", fc="#FFFFFF", ec="#4B5563", lw=1.0)
    ax.text(0.200, 0.190, "Delta / flush records", ha="center", va="center", fontsize=8.0, color=text)
    mini_segment_stack(0.135, 0.118, 0.060, 0.042, n=3, color="#BFDBFE")
    ax.text(0.230, 0.140, "L0 exact\nsegments", ha="center", va="center", fontsize=7.0, color=text)

    box(0.350, 0.092, 0.265, 0.130, "", fc="#FFFFFF", ec=PALETTE["green"], lw=1.25)
    ax.text(0.482, 0.202, "Persistent semantic catalog", ha="center", va="center", fontsize=8.2, fontweight="bold", color=text)
    mini_table(0.370, 0.112, 0.083, 0.062)
    ax.text(
        0.535,
        0.143,
        "segment id\nsignature/evidence\nexactness + epoch",
        ha="center",
        va="center",
        fontsize=6.8,
        color=text,
        linespacing=1.05,
    )

    box(0.675, 0.105, 0.245, 0.105, "", fc="#FFFFFF", ec="#4B5563", lw=1.0)
    ax.text(0.797, 0.190, "CSR segment data", ha="center", va="center", fontsize=8.0, color=text)
    mini_segment_stack(0.695, 0.118, 0.070, 0.042, n=4, color="#D1FAE5")
    ax.plot([0.815, 0.860, 0.895, 0.875, 0.830], [0.130, 0.170, 0.152, 0.118, 0.115], color=PALETTE["blue"], linewidth=1.0)
    ax.scatter([0.815, 0.860, 0.895, 0.875, 0.830], [0.130, 0.170, 0.152, 0.118, 0.115], s=10, color=PALETTE["blue"])

    # Gray physical movement arrows.
    arrow((0.200, 0.400), (0.200, 0.215), color="#475569", lw=1.0)
    arrow((0.482, 0.390), (0.482, 0.225), color="#475569", lw=1.0)
    arrow((0.795, 0.380), (0.795, 0.215), color="#475569", lw=1.0)

    # Red numbered semantic-control path.
    arrow((0.200, 0.670), (0.200, 0.585), color=red, lw=1.55)
    step(0.230, 0.625, 1)
    arrow((0.295, 0.505), (0.365, 0.655), color=red, lw=1.55)
    step(0.330, 0.585, 2)
    arrow((0.600, 0.665), (0.675, 0.710), color=red, lw=1.55)
    step(0.638, 0.707, 3)
    arrow((0.600, 0.525), (0.675, 0.455), color=red, lw=1.55)
    step(0.637, 0.492, 4)
    arrow((0.795, 0.380), (0.795, 0.210), color=red, lw=1.55)
    step(0.828, 0.300, 5)
    arrow((0.610, 0.185), (0.675, 0.665), color=red, rad=-0.24, lw=1.35, dashed=True)
    arrow((0.610, 0.135), (0.675, 0.455), color=red, rad=-0.18, lw=1.35, dashed=True)
    step(0.632, 0.280, 6)

    # Small legend/callout for the numbered path.
    legend_x, legend_y = 0.105, 0.305
    box(legend_x, legend_y, 0.190, 0.055, "semantic evidence lifecycle", fc="#FFFFFF", ec=red, lw=1.0, fs=7.4, bold=True)
    ax.text(
        legend_x + 0.010,
        legend_y - 0.026,
        "1 compile  2 register  3 prune  4 schedule  5 rewrite  6 reuse",
        ha="left",
        va="center",
        fontsize=6.4,
        color=text,
    )
    save_figure(fig, out_dir, "fig1_system_control_plane")


# Keep the all-figures entry point aligned with the standalone Figure 1 script.
# The standalone script is the source of truth for the detailed architecture
# diagram because it can be copied and run independently.
try:
    from plot_fig1_system_control_plane import plot_system_control_plane
except ImportError:
    pass


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
    ax.set_ylabel("Normalized cost (naive=1, log)")
    ax.set_xticks(x, variants, rotation=35, ha="right")
    ax.set_title("Read amplification")
    ax.legend(
        frameon=True,
        facecolor="white",
        edgecolor="none",
        framealpha=0.9,
        loc="upper right",
    )
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
        ax.plot(
            x,
            [row[key] for row in SF100],
            marker=marker,
            linewidth=1.4,
            markersize=4,
            color=color,
            label=label,
        )
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


def c2_value(scale: str, policy: str, key: str) -> float:
    for row in C2:
        if row["scale"] == scale and row["policy"] == policy:
            return float(row[key])
    raise KeyError((scale, policy, key))


def plot_c2_lifecycle(out_dir: Path) -> None:
    script = Path(__file__).resolve().parent / "plot_fig3_c2_lifecycle_retention.py"
    subprocess.run([sys.executable, str(script), "--out-dir", str(out_dir)], check=True)


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


def baseline_bar_style(row: dict) -> tuple[str, str]:
    if row["kind"] == "internal":
        return STYLE["internal"]["color"], STYLE["internal"]["hatch"]
    return PALETTE["blue"], ""


def plot_baseline_positioning(out_dir: Path) -> None:
    script = Path(__file__).resolve().parent / "plot_fig5_baseline_positioning.py"
    subprocess.run([sys.executable, str(script), "--out-dir", str(out_dir)], check=True)


def write_readme(out_dir: Path) -> None:
    lines = [
        "# Generated CIDR Figures",
        "",
        "Generated by the scripts in `../scripts`.",
        "",
        "Each figure also has a single-figure entry point:",
        "",
        "- `plot_fig1_system_control_plane.py`",
        "- `plot_fig2_sf100_read_budget.py`",
        "- `plot_fig3_c2_lifecycle_retention.py`",
        "- `plot_fig4_dynamic_sf30_p99.py`",
        "",
        "The current PDF/SVG files were generated on the Linux host under:",
        "",
        "`/data/WorkSpace/lsmgraph-rs/baseline/seml0-lifecycle-paper/cidr/images`",
        "",
        "Each figure is emitted as both PDF and SVG:",
        "",
        "- `fig1_system_control_plane`: conceptual query-semantic control-plane architecture.",
        "- `fig2_semantic_evidence_lifecycle`: evidence rows and persistent catalog lifecycle.",
        "- `fig2_sf100_read_budget`: SF100 read amplification, latency, and memory cliff.",
        "- `fig3_c2_lifecycle_retention`: C2 lifecycle retention under compaction.",
        "- `fig4_dynamic_sf30_p99`: SF30 dynamic mixed read/write tail-latency signal.",
        "",
        textwrap.fill(
            "Baseline context is now reported as a reproducibility-gate table in "
            "the paper rather than as a standalone figure.",
            width=88,
        ),
        "",
    ]
    (out_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate SemL0 CIDR figures.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Directory for generated PDF/SVG files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = args.out_dir.resolve()
    configure_matplotlib()
    plot_control_plane_architecture(out_dir)
    plot_semantic_evidence_lifecycle(out_dir)
    plot_sf100_read_budget(out_dir)
    plot_c2_lifecycle(out_dir)
    plot_dynamic_sf30(out_dir)
    write_readme(out_dir)
    print(f"Wrote figures to {out_dir}")


if __name__ == "__main__":
    main()
