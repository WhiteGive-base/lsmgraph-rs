#!/usr/bin/env python3
"""Standalone generator for Figure 1: SemL0 query-semantic control plane."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


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


def configure_matplotlib() -> None:
    mpl.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 8.5,
            "axes.titlesize": 9.5,
            "axes.labelsize": 8.5,
            "legend.fontsize": 7.5,
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
        rows = ["sid", "sig", "state"]
        vals = ["42", "L,T", "Exact"]
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

    legend_x, legend_y = 0.105, 0.305
    box(legend_x, legend_y, 0.190, 0.055, "semantic evidence lifecycle", fc="#FFFFFF", ec=red, lw=1.0, fs=7.4, bold=True)
    ax.text(
        legend_x + 0.010,
        legend_y - 0.026,
        "1 compile  2 register  3 admit  4 schedule  5 rewrite  6 reuse",
        ha="left",
        va="center",
        fontsize=6.4,
        color=text,
    )
    save_figure(fig, out_dir, "fig1_system_control_plane")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Figure 1.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    configure_matplotlib()
    plot_system_control_plane(args.out_dir.resolve())
    print(f"Wrote Figure 1 to {args.out_dir.resolve()}")


if __name__ == "__main__":
    main()
