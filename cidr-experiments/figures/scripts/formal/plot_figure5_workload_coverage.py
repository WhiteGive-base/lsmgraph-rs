#!/usr/bin/env python3
"""Render Figure 5 (workload coverage) from the frozen tidy TSV contract."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Mapping, Sequence

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import TwoSlopeNorm
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import FuncFormatter, MaxNLocator

from plot_support import (
    DataContractError,
    Estimate,
    PALETTE,
    configure_matplotlib,
    digest_passes,
    estimate_values,
    group_by,
    load_figure_rows,
    normalize_variant,
    number,
    paired_ratio_estimate,
    panel_label,
    passing_rows,
    ratio_tick,
    report_saved,
    require_constant,
    resolve_input,
    save_figure,
    text,
    warn,
)


STEM = "fig_workload_coverage"
SEMANTICS = (
    "typed_only",
    "dst_label",
    "record_time",
    "prop_present",
    "prop_absence",
    "two_hop",
)
SELECTIVITIES = ("rare", "medium", "frequent")
DEGREES = ("low", "medium", "high")
PATTERNS = ("uniform", "burst", "hotspot", "shift_A_B")
WRITE_PERCENTS = (0.0, 10.0, 50.0, 90.0)
FALLBACK_SCENARIOS = (
    "stable",
    "schema_epoch",
    "alias_drop",
    "high_tombstone",
    "old_snapshot",
    "reopen",
)
SUPPORT_STATES = {"supported", "prototype", "unsupported", "missing"}

PATTERN_STYLE = {
    "uniform": {"color": PALETTE["blue"], "marker": "o", "linestyle": "-"},
    "burst": {"color": PALETTE["orange"], "marker": "s", "linestyle": "--"},
    "hotspot": {"color": PALETTE["yellow"], "marker": "^", "linestyle": "-."},
    "shift_A_B": {"color": PALETTE["purple"], "marker": "P", "linestyle": "-"},
}


def _line(row: Mapping[str, str]) -> str:
    return row.get("__line__", "?")


def _partition_rows(
    rows: Sequence[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    heatmap: list[dict[str, str]] = []
    mix: list[dict[str, str]] = []
    fallback: list[dict[str, str]] = []
    for row in rows:
        grains = [
            bool(row.get("query_semantic", "")),
            bool(row.get("update_pattern", "")),
            bool(row.get("fallback_scenario", "")),
        ]
        if sum(grains) != 1:
            raise DataContractError(
                f"line {_line(row)}: F5 row must identify exactly one grain with "
                "query_semantic, update_pattern, or fallback_scenario"
            )
        if grains[0]:
            heatmap.append(row)
        elif grains[1]:
            mix.append(row)
        else:
            fallback.append(row)
    if not heatmap or not mix or not fallback:
        raise DataContractError(
            "F5 requires heatmap, workload-mix, and fallback row grains in the selected experiment"
        )
    return heatmap, mix, fallback


def _validate_heatmap_rows(rows: Sequence[dict[str, str]]) -> None:
    allowed_variants = {"naive", "budg-b64"}
    for row in rows:
        semantic = text(row, "query_semantic")
        selectivity = text(row, "edge_selectivity_class")
        degree = text(row, "source_degree_class")
        state = text(row, "support_state")
        if semantic not in SEMANTICS:
            raise DataContractError(
                f"line {_line(row)}: unknown query_semantic {semantic!r}"
            )
        if selectivity not in SELECTIVITIES:
            raise DataContractError(
                f"line {_line(row)}: unknown edge_selectivity_class {selectivity!r}"
            )
        if degree not in DEGREES:
            raise DataContractError(
                f"line {_line(row)}: unknown source_degree_class {degree!r}"
            )
        if state not in SUPPORT_STATES:
            raise DataContractError(
                f"line {_line(row)}: unknown support_state {state!r}"
            )
        if state != "supported" and not row.get("unsupported_reason", "").strip():
            raise DataContractError(
                f"line {_line(row)}: unsupported_reason is required for support_state={state!r}"
            )
        if state in {"supported", "prototype"}:
            variant = normalize_variant(text(row, "variant"))
            if variant not in allowed_variants:
                raise DataContractError(
                    f"line {_line(row)}: numeric workload cell has unexpected variant {variant!r}"
                )
            number(row, "latency_p99_us", positive=True)
            number(row, "body_read_bytes_total", nonnegative=True)
            number(row, "measured_operations", positive=True)


def _validate_mix_rows(rows: Sequence[dict[str, str]]) -> None:
    for row in rows:
        pattern = text(row, "update_pattern")
        if pattern not in PATTERNS:
            raise DataContractError(
                f"line {_line(row)}: unknown update_pattern {pattern!r}"
            )
        read_percent = number(row, "read_percent", nonnegative=True)
        write_percent = number(row, "write_percent", nonnegative=True)
        assert read_percent is not None and write_percent is not None
        if read_percent > 100.0 or write_percent > 100.0:
            raise DataContractError(
                f"line {_line(row)}: read_percent/write_percent must be in [0, 100]"
            )
        if not math.isclose(read_percent + write_percent, 100.0, abs_tol=1e-6):
            raise DataContractError(
                f"line {_line(row)}: read_percent + write_percent must equal 100"
            )
        if not any(math.isclose(write_percent, expected, abs_tol=1e-6) for expected in WRITE_PERCENTS):
            raise DataContractError(
                f"line {_line(row)}: main-panel write_percent must be one of {WRITE_PERCENTS}"
            )
        variant = normalize_variant(text(row, "variant"))
        if variant not in {"naive", "budg-b64"}:
            raise DataContractError(
                f"line {_line(row)}: workload-mix row has unexpected variant {variant!r}"
            )
        number(row, "latency_p99_us", positive=True)
        number(row, "measured_operations", positive=True)


def _validate_fallback_rows(rows: Sequence[dict[str, str]]) -> None:
    for row in rows:
        scenario = text(row, "fallback_scenario")
        if scenario not in FALLBACK_SCENARIOS:
            raise DataContractError(
                f"line {_line(row)}: unknown fallback_scenario {scenario!r}"
            )
        rate = number(row, "fallback_rate", nonnegative=True)
        assert rate is not None
        if rate > 1.0:
            raise DataContractError(
                f"line {_line(row)}: fallback_rate must be in [0, 1]"
            )
        number(row, "fallback_read_bytes_total", nonnegative=True)
        number(row, "exact_read_bytes_total", positive=True)


def _cell_state(rows: Sequence[dict[str, str]], *, label: str) -> str:
    states = {row["support_state"] for row in rows}
    if len(states) != 1:
        raise DataContractError(f"{label}: support_state disagrees across rows: {sorted(states)}")
    return next(iter(states))


def _numeric_cell(
    rows: Sequence[dict[str, str]],
    *,
    label: str,
    metric: str,
) -> Estimate | None:
    passing = passing_rows(rows, label=label)
    by_variant = group_by(passing, lambda row: normalize_variant(row["variant"]))
    if set(by_variant) != {"naive", "budg-b64"}:
        raise DataContractError(
            f"{label}: supported/prototype cell requires exactly naive and budg-b64; "
            f"observed {sorted(by_variant)}"
        )
    require_constant(
        rows,
        ("schema_state", "version_state", "dataset_id", "workload_id", "cache_state"),
        label=label,
    )
    if metric == "latency":
        getter = lambda row: float(number(row, "latency_p99_us", positive=True))
    elif metric == "body":
        def getter(row: Mapping[str, str]) -> float:
            body = number(row, "body_read_bytes_total", nonnegative=True)
            operations = number(row, "measured_operations", positive=True)
            assert body is not None and operations is not None
            return body / operations
    else:
        raise AssertionError(metric)

    try:
        return paired_ratio_estimate(
            by_variant["naive"],
            by_variant["budg-b64"],
            numerator_getter=getter,
            denominator_getter=getter,
            label=label,
            minimum_runs=5,
        )
    except DataContractError as exc:
        if metric == "body" and "ratio inputs must be finite and >0" in str(exc):
            warn(f"{label}: zero byte/op value cannot be represented on log2; marked missing")
            return None
        raise


def _build_heatmap(
    rows: Sequence[dict[str, str]], metric: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray, set[str]]:
    shape = (len(SEMANTICS), len(SELECTIVITIES) * len(DEGREES))
    values = np.full(shape, np.nan, dtype=float)
    raw_ratios = np.full(shape, np.nan, dtype=float)
    states = np.full(shape, "missing", dtype=object)
    prototype_semantics: set[str] = set()
    grouped = group_by(
        rows,
        lambda row: (
            row["query_semantic"],
            row["edge_selectivity_class"],
            row["source_degree_class"],
        ),
    )
    for row_idx, semantic in enumerate(SEMANTICS):
        for sel_idx, selectivity in enumerate(SELECTIVITIES):
            for degree_idx, degree in enumerate(DEGREES):
                col_idx = sel_idx * len(DEGREES) + degree_idx
                key = (semantic, selectivity, degree)
                cell_rows = grouped.get(key, [])
                if not cell_rows:
                    warn(f"F5 {metric} heatmap: absent cell {key}; rendered as missing")
                    continue
                state = _cell_state(cell_rows, label=f"F5 cell {key}")
                states[row_idx, col_idx] = state
                if state in {"unsupported", "missing"}:
                    continue
                if state == "prototype":
                    prototype_semantics.add(semantic)
                estimate = _numeric_cell(
                    cell_rows,
                    label=f"F5 {metric} cell {key}",
                    metric=metric,
                )
                if estimate is None:
                    states[row_idx, col_idx] = "missing"
                    continue
                ratio = estimate.center
                raw_ratios[row_idx, col_idx] = ratio
                values[row_idx, col_idx] = math.log2(ratio)
    return values, raw_ratios, states, prototype_semantics


def _plot_heatmap(
    ax: plt.Axes,
    values: np.ndarray,
    ratios: np.ndarray,
    states: np.ndarray,
    prototypes: set[str],
    *,
    title: str,
):
    norm = TwoSlopeNorm(vmin=-2.0, vcenter=0.0, vmax=4.0)
    cmap = plt.get_cmap("PuOr").copy()
    cmap.set_bad(PALETTE["paper"])
    clipped = np.clip(values, -2.0, 4.0)
    image = ax.imshow(
        np.ma.masked_invalid(clipped), cmap=cmap, norm=norm, aspect="auto", interpolation="none"
    )
    for row_idx in range(values.shape[0]):
        for col_idx in range(values.shape[1]):
            state = str(states[row_idx, col_idx])
            if state == "unsupported":
                ax.add_patch(
                    Rectangle(
                        (col_idx - 0.5, row_idx - 0.5),
                        1,
                        1,
                        facecolor="#E5E7EB",
                        edgecolor=PALETTE["gray"],
                        linewidth=0.45,
                        hatch="////",
                    )
                )
                ax.text(col_idx, row_idx, "N/S", ha="center", va="center", fontsize=6.0)
                continue
            if state == "missing" or not math.isfinite(ratios[row_idx, col_idx]):
                ax.add_patch(
                    Rectangle(
                        (col_idx - 0.5, row_idx - 0.5),
                        1,
                        1,
                        facecolor=PALETTE["paper"],
                        edgecolor=PALETTE["light_gray"],
                        linewidth=0.45,
                        hatch="xx",
                    )
                )
                ax.text(col_idx, row_idx, "--", ha="center", va="center", fontsize=6.0)
                continue
            value = values[row_idx, col_idx]
            ratio = ratios[row_idx, col_idx]
            clipped_value = float(np.clip(value, -2.0, 4.0))
            normalized = norm(clipped_value)
            color = PALETTE["paper"] if normalized < 0.18 or normalized > 0.82 else PALETTE["dark"]
            if value < -2.0:
                annotation = "<0.25x"
            elif value > 4.0:
                annotation = ">16x"
            else:
                annotation = f"{ratio:.1f}x"
            if state == "prototype":
                annotation += "*"
            ax.text(
                col_idx,
                row_idx,
                annotation,
                ha="center",
                va="center",
                fontsize=5.6,
                color=color,
            )
            if value < -2.0 or value > 4.0:
                marker = "v" if value < -2.0 else "^"
                ax.scatter(
                    col_idx + 0.34,
                    row_idx - 0.34,
                    marker=marker,
                    s=10,
                    color=color,
                    linewidths=0,
                    clip_on=True,
                )

    tick_labels = [
        f"{selectivity[0].upper()}-{degree[0].upper()}"
        for selectivity in SELECTIVITIES
        for degree in DEGREES
    ]
    row_labels = [
        semantic.replace("_", " ") + ("*" if semantic in prototypes else "")
        for semantic in SEMANTICS
    ]
    ax.set_xticks(np.arange(len(tick_labels)), labels=tick_labels, rotation=45, ha="right")
    ax.set_yticks(np.arange(len(row_labels)), labels=row_labels)
    ax.set_xlabel("Selectivity-degree cell (R/M/F x L/M/H)")
    ax.set_title(title)
    ax.set_xticks(np.arange(-0.5, len(tick_labels), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(row_labels), 1), minor=True)
    ax.grid(which="minor", color=PALETTE["paper"], linewidth=0.8)
    ax.tick_params(which="minor", bottom=False, left=False)
    return image


def _mix_estimates(
    rows: Sequence[dict[str, str]],
) -> dict[str, list[Estimate | None]]:
    grouped = group_by(
        rows,
        lambda row: (row["update_pattern"], float(row["write_percent"])),
    )
    result: dict[str, list[Estimate | None]] = {}
    for pattern in PATTERNS:
        estimates: list[Estimate | None] = []
        for write_percent in WRITE_PERCENTS:
            matching_key = next(
                (
                    key
                    for key in grouped
                    if key[0] == pattern and math.isclose(key[1], write_percent, abs_tol=1e-6)
                ),
                None,
            )
            if matching_key is None:
                warn(
                    f"F5 workload mix: absent {pattern}, write={write_percent:g}%; line will break"
                )
                estimates.append(None)
                continue
            cell_rows = grouped[matching_key]
            require_constant(
                cell_rows,
                ("schema_state", "version_state", "dataset_id", "workload_id", "cache_state"),
                label=f"F5 mix {pattern}/{write_percent:g}%",
            )
            passing = passing_rows(
                cell_rows, label=f"F5 mix {pattern}/{write_percent:g}%"
            )
            by_variant = group_by(passing, lambda row: normalize_variant(row["variant"]))
            if set(by_variant) != {"naive", "budg-b64"}:
                raise DataContractError(
                    f"F5 mix {pattern}/{write_percent:g}% requires naive and budg-b64; "
                    f"observed {sorted(by_variant)}"
                )
            getter = lambda row: float(number(row, "latency_p99_us", positive=True))
            estimates.append(
                paired_ratio_estimate(
                    by_variant["naive"],
                    by_variant["budg-b64"],
                    numerator_getter=getter,
                    denominator_getter=getter,
                    label=f"F5 mix {pattern}/{write_percent:g}% latency speedup",
                    minimum_runs=5,
                )
            )
        if not any(estimate is not None for estimate in estimates):
            raise DataContractError(f"F5 mix pattern {pattern!r} has no plottable points")
        result[pattern] = estimates
    return result


def _plot_mix(ax: plt.Axes, estimates: Mapping[str, Sequence[Estimate | None]]) -> None:
    max_point: tuple[float, float, str] | None = None
    for pattern in PATTERNS:
        series = estimates[pattern]
        centers = np.asarray(
            [math.log2(item.center) if item is not None else np.nan for item in series]
        )
        lows = np.asarray(
            [math.log2(item.low) if item is not None else np.nan for item in series]
        )
        highs = np.asarray(
            [math.log2(item.high) if item is not None else np.nan for item in series]
        )
        style = PATTERN_STYLE[pattern]
        label = pattern.replace("_", "->")
        ax.plot(
            WRITE_PERCENTS,
            centers,
            label=label,
            color=style["color"],
            marker=style["marker"],
            linestyle=style["linestyle"],
            linewidth=1.25,
            markersize=3.7,
        )
        ax.fill_between(
            WRITE_PERCENTS,
            lows,
            highs,
            color=style["color"],
            alpha=0.13,
            linewidth=0,
        )
        for x_value, center in zip(WRITE_PERCENTS, centers):
            if not math.isfinite(center):
                continue
            if center < 0:
                ax.scatter(
                    [x_value], [center], marker="x", s=23, color=PALETTE["red"], zorder=5
                )
                ax.annotate(
                    f"{2.0**center:.2g}x",
                    xy=(x_value, center),
                    xytext=(0, -9),
                    textcoords="offset points",
                    ha="center",
                    va="top",
                    fontsize=5.7,
                    color=PALETTE["red"],
                )
            if max_point is None or center > max_point[1]:
                max_point = (x_value, float(center), label)
    ax.axhline(0.0, color=PALETTE["dark"], linewidth=0.8, linestyle=":")
    if max_point is not None:
        x_value, y_value, label = max_point
        ax.annotate(
            f"max {2.0**y_value:.1f}x ({label})",
            xy=(x_value, y_value),
            xytext=(4, 5),
            textcoords="offset points",
            fontsize=6.0,
            color=PALETTE["dark"],
        )
    ax.set_xticks(WRITE_PERCENTS, labels=["100/0", "90/10", "50/50", "10/90"])
    ax.set_xlabel("Read/write mix (%)")
    ax.set_ylabel("P99 speedup (naive / B64)")
    ax.yaxis.set_major_formatter(FuncFormatter(ratio_tick))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=6))
    ax.set_title("Update-mix sensitivity")
    ax.legend(frameon=False, ncol=2, loc="best", handlelength=2.2)


def _fallback_estimate(rows: Sequence[dict[str, str]], *, label: str) -> tuple[Estimate, Estimate]:
    ratio_values: list[tuple[str, float]] = []
    rate_values: list[tuple[str, float]] = []
    for row in rows:
        fallback_bytes = number(row, "fallback_read_bytes_total", nonnegative=True)
        exact_bytes = number(row, "exact_read_bytes_total", positive=True)
        rate = number(row, "fallback_rate", nonnegative=True)
        assert fallback_bytes is not None and exact_bytes is not None and rate is not None
        if fallback_bytes <= 0:
            raise DataContractError(
                f"{label}: fallback_read_bytes_total=0 cannot be represented on log2; "
                "do not add an epsilon"
            )
        if rate > 1.0:
            raise DataContractError(f"{label}: fallback_rate must be <=1")
        ratio_values.append((row["run_id"], fallback_bytes / exact_bytes))
        rate_values.append((row["run_id"], rate))
    return (
        estimate_values(ratio_values, label=f"{label} read ratio", minimum_runs=5),
        estimate_values(rate_values, label=f"{label} fallback rate", minimum_runs=5),
    )


def _plot_fallback(
    ax: plt.Axes,
    rows: Sequence[dict[str, str]],
    *,
    selected_variant: str,
) -> list[str]:
    selected = [row for row in rows if normalize_variant(row["variant"]) == selected_variant]
    if not selected:
        raise DataContractError(
            f"F5 fallback: no rows for --fallback-variant={selected_variant!r}"
        )
    grouped = group_by(selected, lambda row: row["fallback_scenario"])
    missing = [scenario for scenario in FALLBACK_SCENARIOS if scenario not in grouped]
    if missing:
        raise DataContractError(f"F5 fallback: missing required scenarios {missing}")

    failures: list[str] = []
    centers = np.full(len(FALLBACK_SCENARIOS), np.nan)
    lows = np.full(len(FALLBACK_SCENARIOS), np.nan)
    highs = np.full(len(FALLBACK_SCENARIOS), np.nan)
    rates = np.full(len(FALLBACK_SCENARIOS), np.nan)
    for index, scenario in enumerate(FALLBACK_SCENARIOS):
        scenario_rows = grouped[scenario]
        require_constant(
            scenario_rows,
            ("schema_state", "version_state", "dataset_id", "workload_id", "cache_state"),
            label=f"F5 fallback {scenario}",
        )
        failed_rows = [row for row in scenario_rows if not digest_passes(row)]
        if failed_rows:
            failures.append(scenario)
            warn(
                f"F5 fallback {scenario}: {len(failed_rows)} correctness-failed row(s); "
                "performance bar suppressed and process will exit nonzero"
            )
            continue
        ratio, rate = _fallback_estimate(
            scenario_rows, label=f"F5 fallback {scenario}"
        )
        centers[index] = math.log2(ratio.center)
        lows[index] = math.log2(ratio.low)
        highs[index] = math.log2(ratio.high)
        rates[index] = rate.center

    x_positions = np.arange(len(FALLBACK_SCENARIOS))
    valid = np.isfinite(centers)
    if valid.any():
        bars = ax.bar(
            x_positions[valid],
            centers[valid],
            color=PALETTE["cyan"],
            edgecolor=PALETTE["dark"],
            linewidth=0.55,
            hatch="xx",
            width=0.68,
            zorder=2,
        )
        error = np.vstack((centers[valid] - lows[valid], highs[valid] - centers[valid]))
        ax.errorbar(
            x_positions[valid],
            centers[valid],
            yerr=error,
            fmt="none",
            ecolor=PALETTE["dark"],
            elinewidth=0.7,
            capsize=2,
            zorder=4,
        )
        valid_indices = np.flatnonzero(valid)
        for bar, scenario_index in zip(bars, valid_indices):
            y_value = centers[scenario_index]
            offset = 3 if y_value >= 0 else -9
            ax.annotate(
                f"fallback={100.0 * rates[scenario_index]:.1f}%",
                xy=(bar.get_x() + bar.get_width() / 2.0, y_value),
                xytext=(0, offset),
                textcoords="offset points",
                ha="center",
                va="bottom" if y_value >= 0 else "top",
                fontsize=5.8,
            )
    for scenario in failures:
        index = FALLBACK_SCENARIOS.index(scenario)
        ax.scatter(
            [index], [0.0], marker="x", s=54, linewidths=1.5, color=PALETTE["red"], zorder=6
        )
        ax.annotate(
            "digest fail",
            xy=(index, 0.0),
            xytext=(0, 7),
            textcoords="offset points",
            ha="center",
            fontsize=5.8,
            color=PALETTE["red"],
        )
    ax.axhline(0.0, color=PALETTE["dark"], linewidth=0.8, linestyle=":")
    labels = [
        "stable",
        "schema\nepoch",
        "alias\ndrop",
        "high\ntombstone",
        "old\nsnapshot",
        "reopen",
    ]
    ax.set_xticks(x_positions, labels=labels)
    ax.set_ylabel("Fallback / exact read bytes")
    ax.yaxis.set_major_formatter(FuncFormatter(ratio_tick))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=6))
    ax.set_title(f"Fallback cost ({selected_variant})")
    return failures


def build_figure(
    rows: Sequence[dict[str, str]], *, fallback_variant: str
) -> tuple[plt.Figure, list[str]]:
    require_constant(
        rows,
        ("system", "host_fingerprint", "git_sha", "binary_sha256"),
        label="F5 campaign",
    )
    heatmap_rows, mix_rows, fallback_rows = _partition_rows(rows)
    _validate_heatmap_rows(heatmap_rows)
    _validate_mix_rows(mix_rows)
    _validate_fallback_rows(fallback_rows)

    latency_values, latency_ratios, latency_states, latency_prototypes = _build_heatmap(
        heatmap_rows, "latency"
    )
    body_values, body_ratios, body_states, body_prototypes = _build_heatmap(
        heatmap_rows, "body"
    )
    mix_estimates = _mix_estimates(mix_rows)

    configure_matplotlib()
    fig, axes = plt.subplots(2, 2, figsize=(7.05, 4.45))
    fig.subplots_adjust(left=0.095, right=0.985, bottom=0.115, top=0.89, wspace=0.31, hspace=0.48)
    ax_latency, ax_body, ax_mix, ax_fallback = axes.flat
    image = _plot_heatmap(
        ax_latency,
        latency_values,
        latency_ratios,
        latency_states,
        latency_prototypes,
        title="P99 latency speedup",
    )
    _plot_heatmap(
        ax_body,
        body_values,
        body_ratios,
        body_states,
        body_prototypes,
        title="Body-read reduction",
    )
    _plot_mix(ax_mix, mix_estimates)
    failures = _plot_fallback(
        ax_fallback, fallback_rows, selected_variant=fallback_variant
    )
    for label, ax in zip("abcd", axes.flat):
        panel_label(ax, label)

    colorbar_axis = fig.add_axes([0.36, 0.505, 0.28, 0.016])
    colorbar = fig.colorbar(image, cax=colorbar_axis, orientation="horizontal")
    colorbar.set_ticks([-2, 0, 2, 4], labels=["0.25x", "1x", "4x", "16x"])
    colorbar.set_label("log2 ratio (naive / B64)", labelpad=1)
    legend_handles = [
        Patch(facecolor="#E5E7EB", edgecolor=PALETTE["gray"], hatch="////", label="unsupported"),
        Patch(facecolor=PALETTE["paper"], edgecolor=PALETTE["light_gray"], hatch="xx", label="missing"),
        Patch(facecolor=PALETTE["paper"], edgecolor=PALETTE["dark"], label="* prototype"),
    ]
    fig.legend(
        handles=legend_handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.995),
        ncol=3,
        frameon=False,
        handlelength=1.6,
        columnspacing=1.5,
    )
    return fig, failures


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render Figure 5 from a frozen-contract tidy TSV (no embedded results)."
    )
    parser.add_argument("--data-dir", type=Path, default=Path("."))
    parser.add_argument(
        "--input",
        type=Path,
        help="Input TSV; defaults to <data-dir>/F5.tsv",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--experiment-id", default="E06")
    parser.add_argument("--fallback-variant", default="budg-b64")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        input_path = resolve_input(args.data_dir, args.input, "F5")
        rows = load_figure_rows(
            input_path,
            figure_id="F5",
            experiment_ids=(args.experiment_id,),
        )
        fallback_variant = normalize_variant(args.fallback_variant)
        fig, failures = build_figure(rows, fallback_variant=fallback_variant)
        report = save_figure(fig, args.out_dir, STEM)
        report_saved(report)
        if failures:
            print(
                "ERROR: fallback correctness gate failed for scenarios: "
                + ", ".join(failures),
                file=sys.stderr,
            )
            return 3
        return 0
    except DataContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
