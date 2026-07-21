#!/usr/bin/env python3
"""Shared Matplotlib style, export, and pre-export layout checks."""

from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.text import Text
from matplotlib.ticker import NullFormatter


PALETTE = {
    "blue": "#0072B2",
    "orange": "#D55E00",
    "green": "#009E73",
    "purple": "#CC79A7",
    "red": "#C62828",
    "cyan": "#56B4E9",
    "yellow": "#E69F00",
    "gray": "#6B7280",
    "light_gray": "#D1D5DB",
    "dark": "#252A31",
    "paper": "#FFFFFF",
}

# Color-blind-friendly and still distinguishable in grayscale with markers/hatches.
SERIES_STYLE = {
    "naive": {"color": PALETTE["gray"], "marker": "o", "linestyle": "--", "hatch": "///"},
    "schema": {"color": PALETTE["blue"], "marker": "^", "linestyle": "-.", "hatch": ".."},
    "budg-b64": {"color": PALETTE["cyan"], "marker": "s", "linestyle": "-", "hatch": "xx"},
    "semantic": {"color": PALETTE["green"], "marker": "D", "linestyle": "-", "hatch": ""},
    "oracle": {"color": PALETTE["purple"], "marker": "*", "linestyle": ":", "hatch": "++"},
}


def configure_matplotlib() -> None:
    """Apply a compact paper style shared by all six figure groups."""

    mpl.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 7.5,
            "axes.titlesize": 8.2,
            "axes.labelsize": 7.2,
            "xtick.labelsize": 6.6,
            "ytick.labelsize": 6.6,
            "legend.fontsize": 6.6,
            "figure.titlesize": 8.8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "grid.color": PALETTE["light_gray"],
            "grid.linewidth": 0.45,
            "grid.alpha": 1.0,
            "axes.axisbelow": True,
            "axes.edgecolor": "#5A6068",
            "axes.linewidth": 0.65,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "svg.fonttype": "none",
            "savefig.facecolor": PALETTE["paper"],
            "figure.facecolor": PALETTE["paper"],
        }
    )


def panel_label(ax: mpl.axes.Axes, label: str) -> None:
    """Place a panel label inside the allocated axes box to avoid clipping."""

    ax.text(
        0.01,
        0.98,
        label,
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=8.0,
        fontweight="bold",
        color=PALETTE["dark"],
        clip_on=False,
    )


def hide_minor_tick_labels(ax: mpl.axes.Axes, *, x: bool = True, y: bool = True) -> None:
    """Keep log minor ticks as guides but never render their crowded labels."""

    if x:
        ax.xaxis.set_minor_formatter(NullFormatter())
    if y:
        ax.yaxis.set_minor_formatter(NullFormatter())


def _text_boxes(fig: mpl.figure.Figure) -> list[tuple[Text, mpl.transforms.Bbox]]:
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    boxes: list[tuple[Text, mpl.transforms.Bbox]] = []
    for artist in fig.findobj(match=Text):
        if not artist.get_visible() or not artist.get_text().strip():
            continue
        if artist.get_gid() == "qa-ignore":
            continue
        box = artist.get_window_extent(renderer=renderer)
        # Log locators keep Text objects for ticks outside the active view.  They
        # are not painted, so exclude boxes that do not intersect the canvas at all.
        if box.width > 0 and box.height > 0 and box.overlaps(fig.bbox):
            boxes.append((artist, box))
    return boxes


def layout_report(fig: mpl.figure.Figure, figure_name: str) -> dict[str, object]:
    """Return conservative bounds/font/collision diagnostics for visible text.

    Tick labels belonging to the same axis can legitimately touch the axis label's
    broad bounding box, so collision checks are limited to peer text artists in the
    same semantic class (ticks with ticks, annotations with annotations, etc.).
    The generated report is a gate aid; every rendered PDF is also inspected.
    """

    boxes = _text_boxes(fig)
    fig_box = fig.bbox
    padding_px = 0.5
    out_of_bounds: list[str] = []
    small_fonts: list[str] = []
    collisions: list[dict[str, str]] = []
    tick_text_ids = {
        id(text)
        for ax in fig.axes
        for text in (
            *ax.get_xticklabels(minor=False),
            *ax.get_xticklabels(minor=True),
            *ax.get_yticklabels(minor=False),
            *ax.get_yticklabels(minor=True),
        )
    }

    def short(text: Text) -> str:
        value = " ".join(text.get_text().split())
        return value[:80]

    for artist, box in boxes:
        if id(artist) not in tick_text_ids and (
            box.x0 < fig_box.x0 - padding_px
            or box.y0 < fig_box.y0 - padding_px
            or box.x1 > fig_box.x1 + padding_px
            or box.y1 > fig_box.y1 + padding_px
        ):
            out_of_bounds.append(short(artist))
        if artist.get_fontsize() < 6.0:
            small_fonts.append(short(artist))

    semantic_map: dict[int, tuple[str, object | None]] = {}
    for ax in fig.axes:
        for text in (*ax.get_xticklabels(minor=False), *ax.get_xticklabels(minor=True)):
            semantic_map[id(text)] = ("xtick", ax)
        for text in (*ax.get_yticklabels(minor=False), *ax.get_yticklabels(minor=True)):
            semantic_map[id(text)] = ("ytick", ax)
        semantic_map[id(ax.title)] = ("title", ax)
        semantic_map[id(ax.xaxis.label)] = ("axis_label", ax)
        semantic_map[id(ax.yaxis.label)] = ("axis_label", ax)

    def semantic_info(text: Text) -> tuple[str, object | None]:
        # Tick-label Text objects do not reliably expose ``.axes``. Ownership is
        # therefore precomputed from each Axes' public artist lists.
        return semantic_map.get(id(text), ("annotation", text.axes))

    for (left_artist, left), (right_artist, right) in combinations(boxes, 2):
        left_class, left_owner = semantic_info(left_artist)
        right_class, right_owner = semantic_info(right_artist)
        if left_class != right_class:
            continue
        if left_class in {"xtick", "ytick"} and left_owner is not right_owner:
            continue
        overlap_w = min(left.x1, right.x1) - max(left.x0, right.x0)
        overlap_h = min(left.y1, right.y1) - max(left.y0, right.y0)
        if overlap_w > 1.0 and overlap_h > 1.0:
            collisions.append({"left": short(left_artist), "right": short(right_artist)})

    return {
        "figure": figure_name,
        "canvas_px": [round(fig_box.width), round(fig_box.height)],
        "visible_text_count": len(boxes),
        "out_of_bounds": sorted(set(out_of_bounds)),
        "font_below_6pt": sorted(set(small_fonts)),
        "text_collisions": collisions,
        "status": "PASS" if not out_of_bounds and not small_fonts and not collisions else "REVIEW",
    }


def save_figure(
    fig: mpl.figure.Figure,
    out_dir: Path,
    stem: str,
    *,
    dpi: int = 300,
) -> dict[str, object]:
    """Export PNG/SVG/PDF and retain a machine-readable pre-export QA report."""

    out_dir.mkdir(parents=True, exist_ok=True)
    report = layout_report(fig, stem)
    for ext in ("png", "svg", "pdf"):
        fig.savefig(
            out_dir / f"{stem}.{ext}",
            dpi=dpi if ext == "png" else None,
            bbox_inches=None,
        )
    (out_dir / f"{stem}.layout.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    plt.close(fig)
    return report
