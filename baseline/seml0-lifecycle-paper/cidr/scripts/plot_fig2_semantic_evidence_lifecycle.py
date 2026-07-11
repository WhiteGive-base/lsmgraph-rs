#!/usr/bin/env python3
"""Standalone generator for Figure 2: semantic evidence rows and lifecycle."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle


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
    "red_light": "#FDE8E8",
    "line": "#7B8794",
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
    for ext in ("pdf", "svg", "png"):
        kwargs = {"dpi": 220} if ext == "png" else {}
        fig.savefig(out_dir / f"{stem}.{ext}", **kwargs)
    plt.close(fig)


def plot_semantic_evidence_lifecycle(out_dir: Path) -> None:
    fig, ax = plt.subplots(figsize=(10.4, 3.55))
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    ink = PALETTE["ink"]

    def txt(x, y, s, *, fs=7.2, bold=False, ha="center", va="center", color=None, z=10):
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
            zorder=z,
        )

    def box(x, y, w, h, label="", *, fc=PALETTE["white"], ec=ink, lw=1.0, fs=7.2, bold=False):
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

    def evidence_row(x, y, w, h, cells, *, title, ec, fill, fs=6.5):
        txt(x, y + h + 0.030, title, fs=8.0, bold=True, ha="left")
        ax.add_patch(Rectangle((x, y), w, h, facecolor=PALETTE["white"], edgecolor=ec, linewidth=1.0))
        total = sum(weight for _, weight in cells)
        cur = x
        for label, weight in cells:
            cw = w * weight / total
            ax.add_patch(Rectangle((cur, y), cw, h, facecolor=fill, edgecolor=ec, linewidth=0.75))
            txt(cur + cw / 2, y + h / 2, label, fs=fs, bold=True)
            cur += cw

    # Left: evidence rows and exactness rule.
    txt(0.060, 0.910, "Evidence representation", fs=8.8, bold=True, ha="left")
    evidence_row(
        0.060,
        0.765,
        0.515,
        0.055,
        [("SRC", 0.8), ("SRC-LBL", 1.0), ("ETYPE", 1.0), ("DIR", 0.7), ("DEG", 0.75), ("DST-LBL", 1.0), ("TIME", 0.8), ("PROP-PRES", 1.25)],
        title="Query row: GraphAccessSignature",
        ec=PALETTE["blue"],
        fill=PALETTE["blue_light"],
        fs=5.2,
    )
    box(0.060, 0.670, 0.235, 0.055, "Read snapshot: visibility context", fc=PALETTE["orange_light"], ec=PALETTE["orange"], fs=6.1, bold=True)
    box(0.320, 0.670, 0.255, 0.055, "Schema catalog + segment epoch: resolution context", fc=PALETTE["purple_light"], ec=PALETTE["purple"], fs=5.5, bold=True)
    evidence_row(
        0.060,
        0.550,
        0.515,
        0.055,
        [("file/level", 1.0), ("SRC/DST-LBL", 1.25), ("ETYPE", 0.85), ("DIR/DEG", 0.95), ("TIME", 0.75), ("PROP-BM", 1.0), ("SCHEMA-EPOCH", 1.25), ("COMPL", 0.9)],
        title="Segment row: CsrSegmentMeta",
        ec=PALETTE["green"],
        fill=PALETTE["green_light"],
        fs=4.8,
    )
    box(0.610, 0.575, 0.325, 0.245, "", fc=PALETTE["white"], ec=PALETTE["red"], lw=1.0)
    txt(0.630, 0.785, "Safe admission contract", fs=8.0, bold=True, ha="left", color=PALETTE["red"])
    txt(0.630, 0.735, "Exact disjoint / proven absence  ->  SKIP", fs=6.3, ha="left", color=PALETTE["green"])
    txt(0.630, 0.685, "Conservative over-approx disjoint  ->  SKIP", fs=6.1, ha="left", color=PALETTE["green"])
    txt(0.630, 0.635, "Unknown / possible overlap  ->  READ", fs=6.3, ha="left", color=PALETTE["orange"])

    # Separator.
    ax.plot([0.045, 0.955], [0.505, 0.505], color=PALETTE["line"], linewidth=0.8)

    # Bottom: catalog lifecycle as a closed loop.
    txt(0.060, 0.455, "Catalog lifecycle", fs=8.8, bold=True, ha="left")
    box(0.065, 0.275, 0.175, 0.085, "Flush / Compaction\ncreate or update\nevidence rows", fc=PALETTE["blue_light"], ec=PALETTE["blue"], fs=6.7, bold=True)
    box(0.335, 0.250, 0.235, 0.130, "Persistent metadata\nmanifest segment metadata\nschema catalog\ndegree sidecar", fc=PALETTE["white"], ec=PALETTE["green"], fs=6.8, bold=True)
    box(0.670, 0.275, 0.190, 0.085, "In-memory\nSemantic Index", fc=PALETTE["green_light"], ec=PALETTE["green"], fs=7.0, bold=True)
    box(0.670, 0.110, 0.190, 0.095, "Read admission\n+\nRewrite policy", fc=PALETTE["purple_light"], ec=PALETTE["purple"], fs=7.0, bold=True)
    box(0.110, 0.095, 0.205, 0.090, "Resolution uncertainty\nprevents exact evidence\n-> Conservative / Unknown", fc=PALETTE["orange_light"], ec=PALETTE["orange"], fs=6.5, bold=True)

    # Connection from row representation into catalog.
    arrow((0.315, 0.550), (0.405, 0.380), color=PALETTE["green"], rad=0.06)
    txt(0.365, 0.462, "publish / persist", fs=6.6, color=PALETTE["green"], ha="left")

    # Lifecycle loop.
    arrow((0.240, 0.318), (0.335, 0.318), color=PALETTE["blue"])
    txt(0.287, 0.342, "publish", fs=6.5, color=PALETTE["blue"])
    arrow((0.570, 0.318), (0.670, 0.318), color=PALETTE["green"])
    txt(0.620, 0.342, "reload", fs=6.5, color=PALETTE["green"])
    arrow((0.765, 0.275), (0.765, 0.205), color=PALETTE["purple"])
    arrow((0.670, 0.155), (0.570, 0.285), color=PALETTE["purple"], rad=-0.16)
    txt(0.602, 0.175, "publish updated\nevidence", fs=6.4, color=PALETTE["purple"], ha="center")

    # Validator path.
    arrow((0.315, 0.140), (0.335, 0.270), color=PALETTE["orange"], rad=-0.12)
    arrow((0.335, 0.285), (0.315, 0.140), color=PALETTE["orange"], rad=-0.12, dashed=True)
    txt(0.330, 0.205, "validate exactness\nor demote", fs=6.4, color=PALETTE["orange"], ha="left")

    save_figure(fig, out_dir, "fig2_semantic_evidence_lifecycle")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Figure 2.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    configure_matplotlib()
    plot_semantic_evidence_lifecycle(args.out_dir.resolve())
    print(f"Wrote Figure 2 to {args.out_dir.resolve()}")


if __name__ == "__main__":
    main()
