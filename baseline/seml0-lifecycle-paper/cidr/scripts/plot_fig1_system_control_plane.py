#!/usr/bin/env python3
"""Standalone generator for Figure 1: SemL0 query-semantic control plane."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle


DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "images"

PALETTE = {
    "ink": "#142B3A",
    "blue": "#2563A6",
    "blue_light": "#DCEEFF",
    "green": "#2F855A",
    "green_light": "#E8F5EE",
    "orange": "#C76A1D",
    "orange_light": "#FFF1DA",
    "purple": "#7655C7",
    "purple_light": "#EEE7FF",
    "red": "#C53030",
    "line": "#7B8794",
    "panel": "#F8FAFC",
    "white": "#FFFFFF",
}


def configure_matplotlib() -> None:
    mpl.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 8.2,
            "axes.titlesize": 9.5,
            "axes.labelsize": 8.5,
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
    fig, ax = plt.subplots(figsize=(11.2, 3.85))
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    ink = PALETTE["ink"]

    def txt(x, y, s, *, fs=7.4, bold=False, ha="center", va="center", color=None, z=10, bg=False):
        ax.text(
            x,
            y,
            s,
            ha=ha,
            va=va,
            fontsize=fs,
            fontweight="bold" if bold else "normal",
            color=color or ink,
            linespacing=1.08,
            bbox={"facecolor": PALETTE["white"], "edgecolor": "none", "pad": 0.7, "alpha": 0.92}
            if bg
            else None,
            zorder=z,
        )

    def box(x, y, w, h, label="", *, fc=PALETTE["white"], ec=ink, lw=1.0, fs=7.5, bold=False):
        patch = FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.006,rounding_size=0.006",
            facecolor=fc,
            edgecolor=ec,
            linewidth=lw,
        )
        ax.add_patch(patch)
        if label:
            txt(x + w / 2, y + h / 2, label, fs=fs, bold=bold)
        return patch

    def arrow(start, end, *, color=PALETTE["red"], lw=1.25, rad=0.0, dashed=False, z=5):
        arr = FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=10.5,
            linewidth=lw,
            color=color,
            linestyle="--" if dashed else "-",
            connectionstyle=f"arc3,rad={rad}",
            zorder=z,
        )
        ax.add_patch(arr)
        return arr

    def diamond(cx, cy, w, h, label, *, fc=PALETTE["white"], ec=PALETTE["blue"], fs=7.0):
        poly = Polygon(
            [(cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2), (cx - w / 2, cy)],
            closed=True,
            facecolor=fc,
            edgecolor=ec,
            linewidth=1.05,
            zorder=3,
        )
        ax.add_patch(poly)
        txt(cx, cy, label, fs=fs, bold=True, z=4)

    def row(x, y, w, h, cells, *, ec=PALETTE["blue"], fs=5.8):
        ax.add_patch(Rectangle((x, y), w, h, facecolor=PALETTE["white"], edgecolor=ec, linewidth=0.9))
        cw = w / len(cells)
        for i, cell in enumerate(cells):
            ax.add_patch(Rectangle((x + i * cw, y), cw, h, facecolor=PALETTE["blue_light"], edgecolor=ec, linewidth=0.7))
            txt(x + (i + 0.5) * cw, y + h / 2, cell, fs=fs, bold=True)

    # Column frames.
    cols = [
        (0.045, 0.295, "Query Semantics", PALETTE["blue_light"], PALETTE["blue"]),
        (0.355, 0.310, "SemL0 Control Plane", PALETTE["green_light"], PALETTE["green"]),
        (0.700, 0.255, "LSM-CSR Graph Store", "#F8FAFC", ink),
    ]
    for x, w, title, fc, ec in cols:
        ax.add_patch(Rectangle((x, 0.165), w, 0.710, facecolor=fc, edgecolor=ec, linewidth=1.05))
        txt(x + w / 2, 0.910, title, fs=9.6, bold=True)

    # Query column.
    box(0.080, 0.690, 0.225, 0.100, "Property-graph read\ntyped-neighbor / property-aware", fc=PALETTE["white"], ec=PALETTE["blue"], fs=7.2, bold=True)
    box(0.080, 0.545, 0.225, 0.075, "Signature compiler", fc=PALETTE["white"], ec=PALETTE["blue"], fs=7.6, bold=True)
    txt(0.192, 0.470, "GraphAccessSignature", fs=7.4, bold=True)
    row(0.070, 0.420, 0.245, 0.042, ["LBL", "ETYPE", "DIR", "PROP", "SNAP", "EPOCH"], ec=PALETTE["blue"], fs=5.4)
    arrow((0.192, 0.690), (0.192, 0.620), color=PALETTE["blue"])
    arrow((0.192, 0.545), (0.192, 0.463), color=PALETTE["blue"])

    # Control plane core.
    box(
        0.395,
        0.650,
        0.230,
        0.135,
        "Semantic evidence index\nExactness contract\nBudgeted rewrite policy",
        fc=PALETTE["white"],
        ec=PALETTE["green"],
        fs=7.3,
        bold=True,
    )
    diamond(0.495, 0.485, 0.140, 0.105, "Read admission\ngate", fc=PALETTE["white"], ec=PALETTE["blue"], fs=6.8)
    box(
        0.360,
        0.462,
        0.052,
        0.048,
        "SKIP",
        fc=PALETTE["green_light"],
        ec=PALETTE["green"],
        fs=7.0,
        bold=True,
    )
    box(
        0.535,
        0.315,
        0.110,
        0.110,
        "Semantic-aware\ncompaction\npreserve/rebuild\nexact partitions",
        fc=PALETTE["white"],
        ec=PALETTE["purple"],
        fs=6.1,
        bold=True,
    )
    box(
        0.395,
        0.205,
        0.230,
        0.080,
        "Persistent Evidence Catalog\nsegment evidence | schema/epoch | live files",
        fc=PALETTE["white"],
        ec=PALETTE["green"],
        fs=6.8,
        bold=True,
    )

    # Store column.
    box(0.730, 0.675, 0.190, 0.090, "Segment metadata", fc=PALETTE["green_light"], ec=PALETTE["green"], fs=7.2, bold=True)
    box(0.730, 0.500, 0.190, 0.110, "CSR segments\nsegment bodies", fc=PALETTE["white"], ec=ink, fs=7.2, bold=True)
    box(0.730, 0.330, 0.190, 0.105, "Compaction executor\napply rewrite plan\nunsafe -> Conservative", fc=PALETTE["purple_light"], ec=PALETTE["purple"], fs=6.8, bold=True)

    # Main arrows: query to evidence, metadata to gate, read output.
    arrow((0.315, 0.441), (0.395, 0.680), color=PALETTE["red"], rad=0.08)
    arrow((0.730, 0.720), (0.625, 0.720), color=PALETTE["green"], rad=0.00)
    txt(0.675, 0.742, "segment evidence", fs=6.4, color=PALETTE["green"], ha="center", bg=True)
    arrow((0.495, 0.650), (0.495, 0.538), color=PALETTE["green"], rad=0.00)
    arrow((0.565, 0.485), (0.730, 0.575), color=PALETTE["orange"], rad=0.04)
    txt(0.672, 0.602, "READ body", fs=6.4, color=PALETTE["orange"], ha="center", bg=True)
    arrow((0.425, 0.485), (0.412, 0.485), color=PALETTE["green"])

    # Rewrite path.
    arrow((0.585, 0.650), (0.590, 0.425), color=PALETTE["purple"], rad=0.00)
    arrow((0.645, 0.370), (0.730, 0.395), color=PALETTE["purple"])

    # Catalog publish/reload.
    arrow((0.825, 0.330), (0.625, 0.245), color=PALETTE["green"], dashed=True, rad=-0.12)
    txt(0.745, 0.282, "publish evidence", fs=6.3, color=PALETTE["green"], ha="center", bg=True)
    arrow((0.410, 0.285), (0.410, 0.650), color=PALETTE["green"], dashed=True)
    txt(0.427, 0.585, "reload", fs=6.3, color=PALETTE["green"], ha="left", bg=True)

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
