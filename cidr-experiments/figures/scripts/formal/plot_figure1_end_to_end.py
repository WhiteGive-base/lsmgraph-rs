#!/usr/bin/env python3
"""Render Figure 1: matched end-to-end comparison from a strict tidy TSV."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter

from plot_support import (
    DEFAULT_MINIMUM_RUNS,
    DataContractError,
    HERE,
    PALETTE,
    asymmetric_error,
    configure_matplotlib,
    estimate,
    estimate_field,
    format_us,
    group_by,
    load_rows,
    normalize_variant,
    number,
    panel_label,
    report_saved,
    require_constant,
    require_groups,
    save_figure,
)


SYSTEM_ORDER = [
    "SemL0",
    "SemL0-naive",
    "LiveGraph",
    "Aster RocksGraph",
    "TuGraph",
    "NebulaGraph",
    "Neo4j",
]

SYSTEM_STYLE = {
    "SemL0": {"color": PALETTE["cyan"], "marker": "D", "hatch": "xx"},
    "SemL0-naive": {"color": PALETTE["gray"], "marker": "o", "hatch": "///"},
    "LiveGraph": {"color": PALETTE["yellow"], "marker": "o", "hatch": ".."},
    "Aster RocksGraph": {"color": PALETTE["orange"], "marker": "^", "hatch": "\\\\"},
    "TuGraph": {"color": PALETTE["purple"], "marker": "s", "hatch": "++"},
    "NebulaGraph": {"color": PALETTE["blue"], "marker": "P", "hatch": "--"},
    "Neo4j": {"color": PALETTE["gray"], "marker": "X", "hatch": "oo"},
}

PLOT_FIELDS = {
    "latency_p50_us",
    "latency_p95_us",
    "completed_queries",
    "timeout_queries",
    "completed_qps",
    "load_wall_s",
    "final_disk_bytes",
    "interface_scope",
    "system_version",
}


def canonical_system(row: dict[str, str]) -> str | None:
    system = row["system"].strip().lower().replace(" ", "")
    variant = normalize_variant(row["variant"])
    if system in {"seml0", "lsmgraph-rs", "lsmgraphrs"}:
        if variant == "naive":
            return "SemL0-naive"
        if variant == "budg-b64":
            return "SemL0"
        return None
    aliases = {
        "livegraph": "LiveGraph",
        "aster": "Aster RocksGraph",
        "asterrocksgraph": "Aster RocksGraph",
        "tugraph": "TuGraph",
        "nebulagraph": "NebulaGraph",
        "nebula": "NebulaGraph",
        "neo4j": "Neo4j",
        "neo4jcommunity": "Neo4j",
    }
    return aliases.get(system)


def validate_engine_versions(
    groups: dict[str, list[dict[str, str]]],
) -> None:
    """Lock one build/version per underlying engine, including all SemL0 variants."""

    version_groups: dict[str, list[dict[str, str]]] = {}
    for identity, repeat_rows in groups.items():
        engine_identity = "SemL0" if identity in {"SemL0", "SemL0-naive"} else identity
        version_groups.setdefault(engine_identity, []).extend(repeat_rows)
    for engine_identity, repeat_rows in version_groups.items():
        require_constant(
            repeat_rows,
            ["git_sha", "binary_sha256", "system_version"],
            label=f"F1 {engine_identity} engine repeats/variants",
        )


def _estimate_derived(
    rows: list[dict[str, str]],
    getter,
    *,
    label: str,
    minimum_runs: int,
):
    return estimate(
        rows,
        getter,
        label=label,
        minimum_runs=minimum_runs,
    )


def _validate_qps(row: dict[str, str]) -> float:
    completed = number(row, "completed_queries", nonnegative=True)
    duration = number(row, "measurement_s", positive=True)
    declared = number(row, "completed_qps", nonnegative=True)
    assert completed is not None and duration is not None and declared is not None
    derived = completed / duration
    tolerance = max(1e-9, abs(derived) * 0.01)
    if abs(declared - derived) > tolerance:
        raise DataContractError(
            f"line {row['__line__']}: completed_qps={declared} disagrees with "
            f"completed_queries/measurement_s={derived:.6g} by more than 1%"
        )
    if derived <= 0:
        raise DataContractError(
            f"line {row['__line__']}: completed QPS must be >0 for the log panel"
        )
    return derived


def _timeout_rate(row: dict[str, str]) -> float:
    completed = number(row, "completed_queries", nonnegative=True)
    timed_out = number(row, "timeout_queries", nonnegative=True)
    assert completed is not None and timed_out is not None
    offered = completed + timed_out
    return timed_out / offered if offered > 0 else 0.0


def _aggregate(groups: dict[str, list[dict[str, str]]], minimum_runs: int):
    summary: dict[str, dict[str, object]] = {}
    for system in SYSTEM_ORDER:
        rows = groups[system]
        for row in rows:
            p50 = number(row, "latency_p50_us", positive=True)
            p95 = number(row, "latency_p95_us", positive=True)
            p99 = number(row, "latency_p99_us", positive=True, allow_blank=True)
            assert p50 is not None and p95 is not None
            if p95 < p50:
                raise DataContractError(
                    f"line {row['__line__']}: latency_p95_us must be >= latency_p50_us"
                )
            if p99 is not None and p99 < p95:
                raise DataContractError(
                    f"line {row['__line__']}: latency_p99_us must be >= latency_p95_us"
                )

        p99_est = estimate_field(
            rows,
            "latency_p99_us",
            label=f"F1/{system}/P99",
            minimum_runs=minimum_runs,
            positive=True,
            allow_all_missing=True,
        )
        summary[system] = {
            "p50": estimate_field(
                rows,
                "latency_p50_us",
                label=f"F1/{system}/P50",
                minimum_runs=minimum_runs,
                positive=True,
            ),
            "p95": estimate_field(
                rows,
                "latency_p95_us",
                label=f"F1/{system}/P95",
                minimum_runs=minimum_runs,
                positive=True,
            ),
            "p99": p99_est,
            "qps": _estimate_derived(
                rows,
                _validate_qps,
                label=f"F1/{system}/QPS",
                minimum_runs=minimum_runs,
            ),
            "timeout": _estimate_derived(
                rows,
                _timeout_rate,
                label=f"F1/{system}/timeout-rate",
                minimum_runs=minimum_runs,
            ),
            "load": estimate_field(
                rows,
                "load_wall_s",
                label=f"F1/{system}/load-wall",
                minimum_runs=minimum_runs,
                positive=True,
            ),
            "disk": _estimate_derived(
                rows,
                lambda row: float(number(row, "final_disk_bytes", positive=True)) / (1024**3),
                label=f"F1/{system}/disk-GiB",
                minimum_runs=minimum_runs,
            ),
        }
    return summary


def build_figure(summary, rows: list[dict[str, str]]):
    fig = plt.figure(figsize=(7.05, 2.55), layout="constrained")
    grid = fig.add_gridspec(1, 3, width_ratios=[1.35, 0.85, 1.05])
    ax_latency = fig.add_subplot(grid[0, 0])
    ax_qps = fig.add_subplot(grid[0, 1])
    ax_cost = fig.add_subplot(grid[0, 2])

    y_positions = np.arange(len(SYSTEM_ORDER))[::-1]
    for y, system in zip(y_positions, SYSTEM_ORDER):
        values = summary[system]
        style = SYSTEM_STYLE[system]
        p50 = values["p50"]
        p95 = values["p95"]
        p99 = values["p99"]
        assert p50 is not None and p95 is not None
        range_end = p99.center if p99 is not None else p95.center
        ax_latency.hlines(y, p50.center, range_end, color=style["color"], linewidth=1.0)
        for estimate_value, marker in ((p50, "o"), (p95, "^"), (p99, "D")):
            if estimate_value is None:
                continue
            ax_latency.errorbar(
                estimate_value.center,
                y,
                xerr=asymmetric_error(estimate_value),
                fmt=marker,
                color=style["color"],
                markerfacecolor="white" if marker != "D" else style["color"],
                markeredgewidth=0.7,
                markersize=3.6,
                capsize=1.5,
                elinewidth=0.65,
                zorder=3,
            )
        if p99 is None:
            ax_latency.annotate(
                "P99 N/A",
                (p95.center, y),
                xytext=(5, 0),
                textcoords="offset points",
                va="center",
                fontsize=6.0,
                color=PALETTE["gray"],
            )

    ax_latency.set_xscale("log")
    ax_latency.xaxis.set_major_formatter(FuncFormatter(format_us))
    ax_latency.set_yticks(y_positions, SYSTEM_ORDER)
    ax_latency.set_xlabel("End-to-end latency")
    ax_latency.set_title("Matched latency range")
    ax_latency.grid(axis="x", which="major", linewidth=0.45)
    panel_label(ax_latency, "(a)")
    quantile_handles = [
        Line2D([], [], color=PALETTE["dark"], marker="o", linestyle="none", markersize=3.5, label="P50"),
        Line2D([], [], color=PALETTE["dark"], marker="^", linestyle="none", markersize=3.5, label="P95"),
        Line2D([], [], color=PALETTE["dark"], marker="D", linestyle="none", markersize=3.5, label="P99"),
    ]
    ax_latency.legend(handles=quantile_handles, frameon=False, ncol=3, loc="lower right", handletextpad=0.2, columnspacing=0.6)
    ax_latency.text(
        0.98,
        0.03,
        "same trace; digest pass",
        transform=ax_latency.transAxes,
        ha="right",
        va="bottom",
        fontsize=6.0,
        color=PALETTE["gray"],
    )

    primary = [name for name in ("LiveGraph", "Aster RocksGraph") if summary[name]["p99"] is not None]
    if summary["SemL0"]["p99"] is not None and primary:
        strongest = min(primary, key=lambda name: summary[name]["p99"].center)
        ratio = summary[strongest]["p99"].center / summary["SemL0"]["p99"].center
        ax_latency.text(
            0.98,
            0.97,
            f"SemL0 P99: {ratio:.2g}x vs {strongest}",
            transform=ax_latency.transAxes,
            ha="right",
            va="top",
            fontsize=6.0,
            color=PALETTE["dark"],
        )

    x = np.arange(len(SYSTEM_ORDER))
    qps_values = [summary[name]["qps"].center for name in SYSTEM_ORDER]
    qps_err = np.array(
        [
            [summary[name]["qps"].lower_error for name in SYSTEM_ORDER],
            [summary[name]["qps"].upper_error for name in SYSTEM_ORDER],
        ]
    )
    bars = ax_qps.bar(
        x,
        qps_values,
        yerr=qps_err,
        capsize=1.6,
        color=[SYSTEM_STYLE[name]["color"] for name in SYSTEM_ORDER],
        edgecolor=PALETTE["dark"],
        linewidth=0.45,
    )
    for bar, name in zip(bars, SYSTEM_ORDER):
        bar.set_hatch(SYSTEM_STYLE[name]["hatch"])
    ax_qps.set_yscale("log")
    ax_qps.set_ylabel("Completed QPS")
    ax_qps.set_xticks(x, [name.replace(" RocksGraph", "") for name in SYSTEM_ORDER], rotation=32, ha="right")
    ax_qps.set_title("Fixed-concurrency throughput")
    panel_label(ax_qps, "(b)")
    for bar, name in zip(bars, SYSTEM_ORDER):
        timeout = summary[name]["timeout"].center
        if timeout > 0:
            ax_qps.annotate(
                f"to={timeout:.1%}",
                (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                rotation=90,
                fontsize=5.8,
            )

    offered_values: set[float] = set()
    for row in rows:
        offered = number(row, "offered_qps", positive=True, allow_blank=True)
        if offered is not None:
            offered_values.add(offered)
    if len(offered_values) == 1:
        ax_qps.axhline(next(iter(offered_values)), color=PALETTE["dark"], linestyle=":", linewidth=0.8, label="offered")
        ax_qps.legend(frameon=False, loc="upper right")
    elif len(offered_values) > 1:
        raise DataContractError(f"F1: open-loop rows disagree on offered_qps: {sorted(offered_values)}")

    label_offsets = {
        "SemL0": (4, 5),
        "SemL0-naive": (4, -8),
        "LiveGraph": (4, 4),
        "Aster RocksGraph": (4, -8),
        "TuGraph": (4, 4),
        "NebulaGraph": (4, -8),
        "Neo4j": (4, 4),
    }
    for system in SYSTEM_ORDER:
        disk = summary[system]["disk"]
        load = summary[system]["load"]
        style = SYSTEM_STYLE[system]
        ax_cost.errorbar(
            disk.center,
            load.center,
            xerr=asymmetric_error(disk),
            yerr=asymmetric_error(load),
            fmt=style["marker"],
            color=style["color"],
            markerfacecolor=style["color"],
            markeredgecolor=PALETTE["dark"],
            markeredgewidth=0.45,
            markersize=4.2,
            capsize=1.5,
            elinewidth=0.65,
        )
        ax_cost.annotate(
            system.replace(" RocksGraph", ""),
            (disk.center, load.center),
            xytext=label_offsets[system],
            textcoords="offset points",
            fontsize=5.9,
        )
    ax_cost.set_xscale("log")
    ax_cost.set_yscale("log")
    ax_cost.set_xlabel("Final disk (GiB)")
    ax_cost.set_ylabel("Load/build time (s)")
    ax_cost.set_title("Load and storage cost")
    ax_cost.grid(axis="x", which="major", linewidth=0.45)
    ax_cost.annotate(
        "lower is better",
        xy=(0.06, 0.08),
        xytext=(0.48, 0.34),
        xycoords="axes fraction",
        textcoords="axes fraction",
        arrowprops={"arrowstyle": "->", "color": PALETTE["gray"], "linewidth": 0.7},
        fontsize=6.0,
        color=PALETTE["gray"],
    )
    panel_label(ax_cost, "(c)")
    return fig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Tidy TSV satisfying F1+COMMON contract")
    parser.add_argument("--out-dir", type=Path, default=HERE / "output")
    parser.add_argument("--experiment-id", default="E01")
    parser.add_argument(
        "--min-runs",
        type=int,
        default=DEFAULT_MINIMUM_RUNS,
        help="Independent-run floor (default: 3; n=3 uses range, n>=5 uses bootstrap 95%% CI)",
    )
    parser.add_argument("--stem", default="fig_e2e_matched")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rows = load_rows(
            args.input.resolve(),
            figure_id="F1",
            experiment_id=args.experiment_id,
            plot_fields=PLOT_FIELDS,
        )
        require_constant(
            rows,
            [
                "dataset_id",
                "input_sha256",
                "workload_id",
                "query_trace_sha256",
                "directed_edge_count",
                "cache_state",
                "concurrency",
                "interface_scope",
                "host_fingerprint",
            ],
        )
        groups: dict[str, list[dict[str, str]]] = {}
        ignored = 0
        for row in rows:
            identity = canonical_system(row)
            if identity is None:
                ignored += 1
                continue
            groups.setdefault(identity, []).append(row)
        if ignored:
            print(f"WARNING: F1 ignored {ignored} rows outside the frozen system/variant set", file=sys.stderr)
        require_groups(groups, SYSTEM_ORDER, label="F1 systems")
        validate_engine_versions(groups)
        summary = _aggregate(groups, args.min_runs)
        configure_matplotlib()
        fig = build_figure(summary, [row for values in groups.values() for row in values])
        report = save_figure(fig, args.out_dir.resolve(), args.stem)
        report_saved(report)
        return 0
    except DataContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
