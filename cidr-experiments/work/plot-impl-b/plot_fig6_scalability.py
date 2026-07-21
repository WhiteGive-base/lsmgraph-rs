#!/usr/bin/env python3
"""Render Figure 6 (data-size and concurrency scaling) from frozen tidy TSV."""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.ticker import NullFormatter

from plot_support import (
    DataContractError,
    Estimate,
    PALETTE,
    SERIES_STYLE,
    bool_field,
    configure_matplotlib,
    estimate_values,
    format_edges,
    group_by,
    load_figure_rows,
    normalize_variant,
    number,
    panel_label,
    passing_rows,
    report_saved,
    require_constant,
    resolve_input,
    save_figure,
    text,
    warn,
)


STEM = "fig_scaling_concurrency"
VARIANTS = ("naive", "schema", "budg-b64", "semantic")
CONCURRENCIES = (1, 4, 8, 16, 32)
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")

VARIANT_LABELS = {
    "naive": "Naive",
    "schema": "Schema",
    "budg-b64": "B64",
    "semantic": "Semantic",
}

SCALE_METRICS: Mapping[str, tuple[str, str]] = {
    "latency": ("P99 latency (us)", "log"),
    "body": ("Body read (MiB/op)", "log"),
    "disk": ("Final database (GiB)", "log"),
    "rss": ("Steady RSS (GiB)", "log"),
}


@dataclass(frozen=True)
class ScalePoint:
    edge_count: int
    scale_factor: float | None
    metrics: Mapping[str, Estimate] | None
    read_kind: str | None
    oom: bool
    resource_limit_bytes: float


@dataclass(frozen=True)
class ConcurrencyPoint:
    concurrency: int
    qps: Estimate | None
    latency: Estimate | None
    timeout_rate: Estimate | None
    timed_out: bool
    oom: bool
    resource_limit_bytes: float


def _line(row: Mapping[str, str]) -> str:
    return row.get("__line__", "?")


def _canonical_variant(row: Mapping[str, str]) -> str:
    variant = normalize_variant(text(row, "variant"))
    if variant not in VARIANTS:
        raise DataContractError(
            f"line {_line(row)}: F6 variant must be one of {VARIANTS}, got {variant!r}"
        )
    return variant


def _resource_limit(row: Mapping[str, str]) -> float:
    value = number(row, "resource_limit_bytes", positive=True)
    assert value is not None
    return value


def _validate_scale_row(row: Mapping[str, str]) -> None:
    _canonical_variant(row)
    generator_sha = text(row, "generator_config_sha256")
    if not HEX64.fullmatch(generator_sha):
        raise DataContractError(
            f"line {_line(row)}: generator_config_sha256 must be exactly 64 hex characters"
        )
    text(row, "sampling_method_version")
    protocol = text(row, "protocol")
    if protocol not in {"open_loop", "closed_loop"}:
        raise DataContractError(
            f"line {_line(row)}: protocol must be open_loop or closed_loop"
        )
    _resource_limit(row)
    oom = bool_field(row, "oom_flag")
    if oom:
        return
    number(row, "latency_p99_us", positive=True)
    body = number(row, "body_read_bytes_total", nonnegative=True)
    operations = number(row, "measured_operations", positive=True)
    assert body is not None and operations is not None
    if body / operations <= 0:
        raise DataContractError(
            f"line {_line(row)}: body bytes/op is zero and cannot be plotted on log scale; "
            "do not add an epsilon"
        )
    kind = text(row, "read_metric_kind")
    if kind not in {"full_execution", "metadata_proxy"}:
        raise DataContractError(
            f"line {_line(row)}: read_metric_kind must be full_execution or metadata_proxy"
        )
    number(row, "final_disk_bytes", positive=True)
    number(row, "steady_rss_bytes", positive=True)


def _validate_concurrency_row(row: Mapping[str, str]) -> None:
    _canonical_variant(row)
    concurrency = int(float(text(row, "concurrency")))
    if concurrency not in CONCURRENCIES:
        raise DataContractError(
            f"line {_line(row)}: concurrency must be one of {CONCURRENCIES}"
        )
    quota = number(row, "cpu_quota_cores", positive=True)
    assert quota is not None
    protocol = text(row, "protocol")
    if protocol not in {"open_loop", "closed_loop"}:
        raise DataContractError(
            f"line {_line(row)}: protocol must be open_loop or closed_loop"
        )
    _resource_limit(row)
    oom = bool_field(row, "oom_flag")
    if oom:
        return
    number(row, "completed_qps", nonnegative=True)
    timeout_rate = number(row, "timeout_rate", nonnegative=True)
    assert timeout_rate is not None
    if timeout_rate > 1.0:
        raise DataContractError(
            f"line {_line(row)}: timeout_rate must be in [0, 1]"
        )
    number(
        row,
        "latency_p99_us",
        positive=True,
        allow_blank=timeout_rate > 0.0,
    )


def _estimate(
    rows: Sequence[Mapping[str, str]],
    getter: Callable[[Mapping[str, str]], float],
    *,
    label: str,
    minimum_runs: int = 5,
) -> Estimate:
    values = [(row["run_id"], float(getter(row))) for row in rows]
    return estimate_values(values, label=label, minimum_runs=minimum_runs)


def _scale_getter(metric: str) -> Callable[[Mapping[str, str]], float]:
    if metric == "latency":
        return lambda row: float(number(row, "latency_p99_us", positive=True))
    if metric == "body":
        def body(row: Mapping[str, str]) -> float:
            total = number(row, "body_read_bytes_total", nonnegative=True)
            operations = number(row, "measured_operations", positive=True)
            assert total is not None and operations is not None
            value = total / operations / (1024.0**2)
            if value <= 0:
                raise DataContractError(
                    f"line {_line(row)}: zero body MiB/op cannot be represented on log scale"
                )
            return value
        return body
    if metric == "disk":
        return lambda row: float(number(row, "final_disk_bytes", positive=True)) / (1024.0**3)
    if metric == "rss":
        return lambda row: float(number(row, "steady_rss_bytes", positive=True)) / (1024.0**3)
    raise AssertionError(metric)


def _require_scale_matches(rows: Sequence[dict[str, str]], edge_count: int) -> None:
    require_constant(
        rows,
        (
            "scale_factor",
            "vertex_count",
            "directed_edge_count",
            "dataset_id",
            "input_sha256",
            "generator_config_sha256",
            "sampling_method_version",
            "workload_id",
            "query_trace_sha256",
            "cache_state",
            "concurrency",
            "protocol",
            "host_fingerprint",
            "resource_limit_bytes",
        ),
        label=f"F6 scale edge_count={edge_count}",
    )


def _build_scale_points(
    rows: Sequence[dict[str, str]],
) -> tuple[dict[str, list[ScalePoint]], list[int]]:
    for row in rows:
        _validate_scale_row(row)
    require_constant(
        rows,
        ("sampling_method_version", "cache_state", "concurrency", "protocol", "host_fingerprint"),
        label="F6 scale campaign",
    )
    edges = sorted({int(row["directed_edge_count"]) for row in rows})
    if not edges:
        raise DataContractError("F6 scale campaign has no edge-count points")
    for edge_count in edges:
        _require_scale_matches(
            [row for row in rows if int(row["directed_edge_count"]) == edge_count],
            edge_count,
        )

    grouped = group_by(
        rows,
        lambda row: (_canonical_variant(row), int(row["directed_edge_count"])),
    )
    expected = {(variant, edge) for variant in VARIANTS for edge in edges}
    missing = sorted(expected - set(grouped))
    if missing:
        raise DataContractError(
            "F6 scale campaign is missing explicit variant/edge points (OOM must be retained): "
            + repr(missing)
        )

    result: dict[str, list[ScalePoint]] = {variant: [] for variant in VARIANTS}
    for variant in VARIANTS:
        for edge_count in edges:
            point_rows = grouped[(variant, edge_count)]
            limits = {_resource_limit(row) for row in point_rows}
            if len(limits) != 1:
                raise DataContractError(
                    f"F6 scale {variant}/{edge_count}: inconsistent resource_limit_bytes"
                )
            oom_rows = [row for row in point_rows if bool_field(row, "oom_flag")]
            scale_values = {
                float(row["scale_factor"])
                for row in point_rows
                if row.get("scale_factor", "").strip()
            }
            if len(scale_values) > 1:
                raise DataContractError(
                    f"F6 scale {variant}/{edge_count}: inconsistent scale_factor"
                )
            scale_factor = next(iter(scale_values)) if scale_values else None
            if oom_rows:
                if len({row["run_id"] for row in point_rows}) < 5:
                    warn(
                        f"F6 scale {variant}/{edge_count}: OOM censoring has fewer than five independent attempts"
                    )
                if len(oom_rows) != len(point_rows):
                    warn(
                        f"F6 scale {variant}/{edge_count}: mixed OOM/success outcomes; "
                        "the point is conservatively censored and excluded from fits"
                    )
                result[variant].append(
                    ScalePoint(
                        edge_count,
                        scale_factor,
                        None,
                        None,
                        True,
                        next(iter(limits)),
                    )
                )
                continue

            passing = passing_rows(
                point_rows, label=f"F6 scale {variant}/{edge_count}"
            )
            kinds = {row["read_metric_kind"] for row in passing}
            if len(kinds) != 1:
                raise DataContractError(
                    f"F6 scale {variant}/{edge_count}: read_metric_kind changes across runs"
                )
            metrics = {
                metric: _estimate(
                    passing,
                    _scale_getter(metric),
                    label=f"F6 scale {variant}/{edge_count} {metric}",
                )
                for metric in SCALE_METRICS
            }
            result[variant].append(
                ScalePoint(
                    edge_count,
                    scale_factor,
                    metrics,
                    next(iter(kinds)),
                    False,
                    next(iter(limits)),
                )
            )
    return result, edges


def _build_concurrency_points(
    rows: Sequence[dict[str, str]],
) -> dict[str, list[ConcurrencyPoint]]:
    for row in rows:
        _validate_concurrency_row(row)
    require_constant(
        rows,
        (
            "cpu_quota_cores",
            "protocol",
            "cache_state",
            "host_fingerprint",
            "dataset_id",
            "input_sha256",
            "directed_edge_count",
            "resource_limit_bytes",
        ),
        label="F6 concurrency campaign",
    )
    grouped = group_by(
        rows,
        lambda row: (_canonical_variant(row), int(row["concurrency"])),
    )
    expected = {(variant, concurrency) for variant in VARIANTS for concurrency in CONCURRENCIES}
    missing = sorted(expected - set(grouped))
    extra = sorted(set(grouped) - expected)
    if missing or extra:
        raise DataContractError(
            f"F6 concurrency grid mismatch; missing={missing}, extra={extra}"
        )
    result: dict[str, list[ConcurrencyPoint]] = {variant: [] for variant in VARIANTS}
    for variant in VARIANTS:
        for concurrency in CONCURRENCIES:
            point_rows = grouped[(variant, concurrency)]
            limits = {_resource_limit(row) for row in point_rows}
            if len(limits) != 1:
                raise DataContractError(
                    f"F6 concurrency {variant}/C={concurrency}: inconsistent resource limit"
                )
            oom_rows = [row for row in point_rows if bool_field(row, "oom_flag")]
            if oom_rows:
                if len({row["run_id"] for row in point_rows}) < 5:
                    warn(
                        f"F6 concurrency {variant}/C={concurrency}: OOM censoring has fewer than five attempts"
                    )
                if len(oom_rows) != len(point_rows):
                    warn(
                        f"F6 concurrency {variant}/C={concurrency}: mixed OOM/success outcomes; point censored"
                    )
                result[variant].append(
                    ConcurrencyPoint(
                        concurrency,
                        None,
                        None,
                        None,
                        False,
                        True,
                        next(iter(limits)),
                    )
                )
                continue
            passing = passing_rows(
                point_rows, label=f"F6 concurrency {variant}/C={concurrency}"
            )
            qps = _estimate(
                passing,
                lambda row: float(number(row, "completed_qps", nonnegative=True)),
                label=f"F6 concurrency {variant}/C={concurrency} QPS",
            )
            timeout_rate = _estimate(
                passing,
                lambda row: float(number(row, "timeout_rate", nonnegative=True)),
                label=f"F6 concurrency {variant}/C={concurrency} timeout rate",
            )
            timed_out = any(float(row["timeout_rate"]) > 0.0 for row in passing)
            latency: Estimate | None = None
            if not timed_out:
                latency = _estimate(
                    passing,
                    lambda row: float(number(row, "latency_p99_us", positive=True)),
                    label=f"F6 concurrency {variant}/C={concurrency} P99",
                )
            result[variant].append(
                ConcurrencyPoint(
                    concurrency,
                    qps,
                    latency,
                    timeout_rate,
                    timed_out,
                    False,
                    next(iter(limits)),
                )
            )
    return result


def _point_arrays(
    points: Sequence[ScalePoint], metric: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    centers = np.asarray(
        [point.metrics[metric].center if point.metrics is not None else np.nan for point in points]
    )
    lows = np.asarray(
        [point.metrics[metric].low if point.metrics is not None else np.nan for point in points]
    )
    highs = np.asarray(
        [point.metrics[metric].high if point.metrics is not None else np.nan for point in points]
    )
    return centers, lows, highs


def _fit_slope(
    points: Sequence[ScalePoint], metric: str, *, label: str
) -> tuple[float, float] | None:
    successful = [point for point in points if point.metrics is not None]
    if len(successful) < 4:
        warn(f"{label}: fewer than four successful scale points; no slope label")
        return None
    x_values = np.log10([point.edge_count for point in successful])
    y_values = np.log10([point.metrics[metric].center for point in successful])
    slope, intercept = np.polyfit(x_values, y_values, 1)
    fitted = slope * x_values + intercept
    residual = float(np.sum((y_values - fitted) ** 2))
    total = float(np.sum((y_values - np.mean(y_values)) ** 2))
    r_squared = 1.0 if total == 0.0 and residual == 0.0 else 1.0 - residual / total
    if r_squared < 0.9:
        warn(f"{label}: log-log R^2={r_squared:.3f} < 0.9; no slope label")
        return None
    return float(slope), float(r_squared)


def _plot_scale_panel(
    ax: plt.Axes,
    data: Mapping[str, Sequence[ScalePoint]],
    edges: Sequence[int],
    *,
    metric: str,
    allow_slopes: bool,
    annotate_oom: bool,
) -> None:
    oom_marks: list[tuple[str, ScalePoint]] = []
    has_value = False
    proxy_present = False
    for variant in VARIANTS:
        points = data[variant]
        x_values = np.asarray([point.edge_count for point in points], dtype=float)
        centers, lows, highs = _point_arrays(points, metric)
        style = SERIES_STYLE[variant]
        ax.plot(
            x_values,
            centers,
            color=style["color"],
            linestyle=style["linestyle"],
            linewidth=1.25,
            label=VARIANT_LABELS[variant],
        )
        ax.fill_between(
            x_values,
            lows,
            highs,
            color=style["color"],
            alpha=0.12,
            linewidth=0,
        )
        for point, x_value, center in zip(points, x_values, centers):
            if point.oom:
                oom_marks.append((variant, point))
                continue
            if not math.isfinite(center):
                continue
            has_value = True
            hollow = metric == "body" and point.read_kind == "metadata_proxy"
            proxy_present = proxy_present or hollow
            ax.scatter(
                [x_value],
                [center],
                marker=style["marker"],
                s=18,
                facecolors=PALETTE["paper"] if hollow else style["color"],
                edgecolors=style["color"],
                linewidths=0.8,
                zorder=4,
            )
        if allow_slopes and metric in {"latency", "body"}:
            fitted = _fit_slope(
                points,
                metric,
                label=f"F6 {variant} {metric} slope",
            )
            if fitted is not None:
                slope, r_squared = fitted
                successful = [point for point in points if point.metrics is not None]
                last = successful[-1]
                ax.annotate(
                    f"N^{slope:.2f}\nR2={r_squared:.2f}",
                    xy=(last.edge_count, last.metrics[metric].center),
                    xytext=(4, (VARIANTS.index(variant) - 1.5) * 5),
                    textcoords="offset points",
                    color=style["color"],
                    fontsize=5.6,
                )
    if not has_value:
        raise DataContractError(f"F6 scale panel {metric}: every point is censored/unplottable")
    ax.set_xscale("log", base=10)
    ax.set_yscale("log", base=10)
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_ylabel(SCALE_METRICS[metric][0])
    ax.set_xticks(edges)
    tick_labels: list[str] = []
    for edge_index, edge_count in enumerate(edges):
        point = data[VARIANTS[0]][edge_index]
        if point.scale_factor is None:
            tick_labels.append(format_edges(edge_count))
        else:
            tick_labels.append(f"SF{point.scale_factor:g}\n{format_edges(edge_count)}")
    ax.set_xticklabels(tick_labels)
    ax.set_xlabel("Actual directed edges")

    if oom_marks:
        low, high = ax.get_ylim()
        oom_y = high
        ax.set_ylim(low, 10.0 ** (math.log10(high) + 0.14 * (math.log10(high) - math.log10(low))))
        offsets = {"naive": -0.018, "schema": -0.006, "budg-b64": 0.006, "semantic": 0.018}
        for variant, point in oom_marks:
            x_value = 10.0 ** (math.log10(point.edge_count) + offsets[variant])
            ax.scatter(
                [x_value],
                [oom_y],
                marker="^",
                s=25,
                color=SERIES_STYLE[variant]["color"],
                edgecolor=PALETTE["dark"],
                linewidth=0.45,
                zorder=6,
                clip_on=False,
            )
            if annotate_oom:
                limit_gib = point.resource_limit_bytes / (1024.0**3)
                ax.annotate(
                    f"OOM {limit_gib:g}GiB",
                    xy=(x_value, oom_y),
                    xytext=(0, 3),
                    textcoords="offset points",
                    ha="center",
                    va="bottom",
                    fontsize=5.5,
                    color=SERIES_STYLE[variant]["color"],
                )
    if proxy_present:
        proxy_handle = Line2D(
            [],
            [],
            marker="o",
            linestyle="none",
            markerfacecolor=PALETTE["paper"],
            markeredgecolor=PALETTE["dark"],
            markersize=4,
            label="metadata proxy",
        )
        ax.legend(handles=[proxy_handle], loc="best", frameon=False)


def _annotate_terminal_ratio(
    ax: plt.Axes, data: Mapping[str, Sequence[ScalePoint]]
) -> None:
    naive = {point.edge_count: point for point in data["naive"] if point.metrics is not None}
    b64 = {point.edge_count: point for point in data["budg-b64"] if point.metrics is not None}
    common = sorted(set(naive) & set(b64))
    if not common:
        warn("F6 latency: no common successful naive/B64 endpoint for terminal ratio")
        return
    edge_count = common[-1]
    ratio = naive[edge_count].metrics["latency"].center / b64[edge_count].metrics["latency"].center
    ax.annotate(
        f"B64: {ratio:.1f}x vs naive",
        xy=(edge_count, b64[edge_count].metrics["latency"].center),
        xytext=(-5, -15),
        textcoords="offset points",
        ha="right",
        fontsize=5.8,
        color=SERIES_STYLE["budg-b64"]["color"],
    )


def _plot_concurrency_qps(
    ax: plt.Axes, data: Mapping[str, Sequence[ConcurrencyPoint]]
) -> None:
    oom_marks: list[tuple[str, ConcurrencyPoint]] = []
    for variant in VARIANTS:
        points = data[variant]
        centers = np.asarray([point.qps.center if point.qps is not None else np.nan for point in points])
        lows = np.asarray([point.qps.low if point.qps is not None else np.nan for point in points])
        highs = np.asarray([point.qps.high if point.qps is not None else np.nan for point in points])
        style = SERIES_STYLE[variant]
        ax.plot(
            CONCURRENCIES,
            centers,
            color=style["color"],
            linestyle=style["linestyle"],
            marker=style["marker"],
            markersize=3.6,
            linewidth=1.25,
        )
        ax.fill_between(
            CONCURRENCIES,
            lows,
            highs,
            color=style["color"],
            alpha=0.12,
            linewidth=0,
        )
        oom_marks.extend((variant, point) for point in points if point.oom)

    b64_base = data["budg-b64"][0]
    if b64_base.qps is None:
        warn("F6 concurrency: B64 C=1 is censored; ideal-linear reference omitted")
    else:
        ideal = np.asarray([b64_base.qps.center * value for value in CONCURRENCIES])
        ax.plot(
            CONCURRENCIES,
            ideal,
            color=PALETTE["dark"],
            linestyle=":",
            linewidth=1.0,
            label="B64 ideal linear",
        )

    for variant in VARIANTS:
        points = data[variant]
        base = points[0]
        if base.qps is None or base.latency is None:
            warn(f"F6 saturation {variant}: C=1 is censored; no saturation annotation")
            continue
        for previous, current in zip(points, points[1:]):
            if (
                previous.qps is None
                or current.qps is None
                or current.latency is None
                or previous.qps.center <= 0
            ):
                continue
            gain = current.qps.center / previous.qps.center - 1.0
            if gain < 0.10 and current.latency.center > 2.0 * base.latency.center:
                ax.annotate(
                    "sat.",
                    xy=(current.concurrency, current.qps.center),
                    xytext=(0, 6),
                    textcoords="offset points",
                    ha="center",
                    fontsize=5.8,
                    color=SERIES_STYLE[variant]["color"],
                    arrowprops={"arrowstyle": "-", "lw": 0.5, "color": SERIES_STYLE[variant]["color"]},
                )
                break

    ax.set_xscale("log", base=2)
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_xticks(CONCURRENCIES, labels=[str(value) for value in CONCURRENCIES])
    ax.set_xlabel("Concurrent clients")
    ax.set_ylabel("Completed QPS")
    ax.set_ylim(bottom=0.0)
    if b64_base.qps is not None:
        ax.legend(frameon=False, loc="upper left", handlelength=2.2)
    if oom_marks:
        _, high = ax.get_ylim()
        ax.set_ylim(top=high * 1.12 if high > 0 else 1.0)
        for variant, point in oom_marks:
            ax.scatter(
                [point.concurrency],
                [high],
                marker="X",
                s=25,
                color=SERIES_STYLE[variant]["color"],
                edgecolor=PALETTE["dark"],
                linewidth=0.45,
                clip_on=False,
            )


def _plot_concurrency_latency(
    ax: plt.Axes, data: Mapping[str, Sequence[ConcurrencyPoint]]
) -> None:
    censored: list[tuple[str, ConcurrencyPoint, str]] = []
    has_value = False
    for variant in VARIANTS:
        points = data[variant]
        centers = np.asarray(
            [point.latency.center if point.latency is not None else np.nan for point in points]
        )
        lows = np.asarray(
            [point.latency.low if point.latency is not None else np.nan for point in points]
        )
        highs = np.asarray(
            [point.latency.high if point.latency is not None else np.nan for point in points]
        )
        style = SERIES_STYLE[variant]
        ax.plot(
            CONCURRENCIES,
            centers,
            color=style["color"],
            linestyle=style["linestyle"],
            marker=style["marker"],
            markersize=3.6,
            linewidth=1.25,
        )
        ax.fill_between(
            CONCURRENCIES,
            lows,
            highs,
            color=style["color"],
            alpha=0.12,
            linewidth=0,
        )
        has_value = has_value or bool(np.isfinite(centers).any())
        for point in points:
            if point.oom:
                censored.append((variant, point, "oom"))
            elif point.timed_out:
                censored.append((variant, point, "timeout"))
    if not has_value:
        raise DataContractError("F6 concurrency latency panel has no uncensored P99 points")
    ax.set_xscale("log", base=2)
    ax.set_yscale("log", base=10)
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_xticks(CONCURRENCIES, labels=[str(value) for value in CONCURRENCIES])
    ax.set_xlabel("Concurrent clients")
    ax.set_ylabel("P99 latency (us)")

    if censored:
        low, high = ax.get_ylim()
        marker_y = high
        ax.set_ylim(low, 10.0 ** (math.log10(high) + 0.16 * (math.log10(high) - math.log10(low))))
        for variant, point, reason in censored:
            marker = "^" if reason == "timeout" else "X"
            ax.scatter(
                [point.concurrency],
                [marker_y],
                marker=marker,
                s=27,
                color=SERIES_STYLE[variant]["color"],
                edgecolor=PALETTE["dark"],
                linewidth=0.45,
                clip_on=False,
                zorder=6,
            )
            if reason == "timeout" and point.timeout_rate is not None:
                label = f"{100.0 * point.timeout_rate.center:.1f}%"
            else:
                label = f"OOM {point.resource_limit_bytes / (1024.0**3):g}GiB"
            ax.annotate(
                label,
                xy=(point.concurrency, marker_y),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=5.5,
                color=SERIES_STYLE[variant]["color"],
            )


def build_figure(
    scale_rows: Sequence[dict[str, str]],
    concurrency_rows: Sequence[dict[str, str]],
) -> plt.Figure:
    scale_data, edges = _build_scale_points(scale_rows)
    concurrency_data = _build_concurrency_points(concurrency_rows)
    allow_slopes = len(edges) >= 4
    if not allow_slopes:
        warn(
            f"F6 has only {len(edges)} actual scale point(s); curves will be drawn, "
            "but empirical slope labels are forbidden"
        )

    configure_matplotlib()
    fig, axes = plt.subplots(3, 2, figsize=(7.05, 4.55))
    fig.subplots_adjust(left=0.09, right=0.985, bottom=0.1, top=0.9, wspace=0.31, hspace=0.52)
    ax_latency, ax_body, ax_disk, ax_rss, ax_qps, ax_concurrency_latency = axes.flat
    _plot_scale_panel(
        ax_latency,
        scale_data,
        edges,
        metric="latency",
        allow_slopes=allow_slopes,
        annotate_oom=True,
    )
    _annotate_terminal_ratio(ax_latency, scale_data)
    _plot_scale_panel(
        ax_body,
        scale_data,
        edges,
        metric="body",
        allow_slopes=allow_slopes,
        annotate_oom=False,
    )
    _plot_scale_panel(
        ax_disk,
        scale_data,
        edges,
        metric="disk",
        allow_slopes=False,
        annotate_oom=False,
    )
    _plot_scale_panel(
        ax_rss,
        scale_data,
        edges,
        metric="rss",
        allow_slopes=False,
        annotate_oom=False,
    )
    _plot_concurrency_qps(ax_qps, concurrency_data)
    _plot_concurrency_latency(ax_concurrency_latency, concurrency_data)
    for label, ax in zip("abcdef", axes.flat):
        panel_label(ax, label)

    handles = [
        Line2D(
            [],
            [],
            color=SERIES_STYLE[variant]["color"],
            marker=SERIES_STYLE[variant]["marker"],
            linestyle=SERIES_STYLE[variant]["linestyle"],
            linewidth=1.25,
            markersize=4,
            label=VARIANT_LABELS[variant],
        )
        for variant in VARIANTS
    ]
    fig.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.995),
        ncol=4,
        frameon=False,
        handlelength=2.2,
        columnspacing=1.7,
    )
    return fig


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render Figure 6 from frozen-contract E07/E08 tidy rows."
    )
    parser.add_argument("--data-dir", type=Path, default=Path("."))
    parser.add_argument(
        "--input", type=Path, help="Input TSV; defaults to <data-dir>/F6.tsv"
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--scale-experiment-id", default="E07")
    parser.add_argument("--concurrency-experiment-id", default="E08")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.scale_experiment_id == args.concurrency_experiment_id:
            raise DataContractError("scale and concurrency experiment IDs must differ")
        input_path = resolve_input(args.data_dir, args.input, "F6")
        rows = load_figure_rows(
            input_path,
            figure_id="F6",
            experiment_ids=(args.scale_experiment_id, args.concurrency_experiment_id),
        )
        scale_rows = [
            row for row in rows if row["experiment_id"] == args.scale_experiment_id
        ]
        concurrency_rows = [
            row for row in rows if row["experiment_id"] == args.concurrency_experiment_id
        ]
        if not scale_rows or not concurrency_rows:
            raise DataContractError(
                "F6 input must contain both selected scale and concurrency experiments"
            )
        fig = build_figure(scale_rows, concurrency_rows)
        report = save_figure(fig, args.out_dir, STEM)
        report_saved(report)
        return 0
    except DataContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
