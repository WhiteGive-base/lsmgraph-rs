#!/usr/bin/env python3
"""Render Figure 3: budget/resource Pareto tradeoff from a strict tidy TSV."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from plot_support import (
    DataContractError,
    HERE,
    PALETTE,
    asymmetric_error,
    configure_matplotlib,
    estimate,
    estimate_field,
    group_by,
    load_rows,
    number,
    panel_label,
    report_saved,
    require_constant,
    require_groups,
    save_figure,
)


BUDGETS = ["schema", "B16", "B64", "B256", "B1024", "full"]
WORKLOADS = ["uniform", "zipf_hot", "shift_A_B"]
WORKLOAD_STYLE = {
    "uniform": {"label": "Uniform", "color": PALETTE["blue"], "marker": "o", "linestyle": "-"},
    "zipf_hot": {"label": "Zipf/hot", "color": PALETTE["orange"], "marker": "s", "linestyle": "--"},
    "shift_A_B": {"label": "A->B shift", "color": PALETTE["purple"], "marker": "^", "linestyle": "-."},
}

DISK_COMPONENTS = [
    ("segment_metadata_bytes", "Segment metadata", "#BFD7EA"),
    ("semantic_catalog_bytes", "Catalog/index", "#84B6D7"),
    ("degree_sidecar_bytes", "Degree sidecar", "#4E92C4"),
    ("manifest_bytes", "Manifest", "#9CA3AF"),
    ("wal_extra_bytes", "WAL extra", "#5E7D6A"),
]

PLOT_FIELDS = {
    "semantic_budget",
    "workload_distribution",
    "measurement_phase",
    "steady_rss_bytes",
    "peak_import_rss_bytes",
    "latency_p99_us",
    "body_read_bytes_total",
    "measured_operations",
    "segment_metadata_bytes",
    "semantic_catalog_bytes",
    "degree_sidecar_bytes",
    "manifest_bytes",
    "wal_extra_bytes",
    "payload_bytes",
    "live_file_count",
    "temporary_peak_disk_bytes",
    "load_wall_s",
    "reopen_wall_s",
    "cpu_total_ns",
}


def canonical_budget(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    aliases = {
        "schema/no-degree": "schema",
        "schema-no-degree": "schema",
        "b16": "B16",
        "budg-b16": "B16",
        "b64": "B64",
        "budg-b64": "B64",
        "b256": "B256",
        "budg-b256": "B256",
        "b1024": "B1024",
        "budg-b1024": "B1024",
        "semantic": "full",
        "full-semantic": "full",
        "full": "full",
    }
    result = aliases.get(normalized, normalized)
    if result not in BUDGETS:
        raise DataContractError(f"unexpected semantic_budget={value!r}; expected {BUDGETS}")
    return result


def canonical_workload(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "uniform": "uniform",
        "zipf": "zipf_hot",
        "zipf_hot": "zipf_hot",
        "hot": "zipf_hot",
        "shift_a_b": "shift_A_B",
        "a_b_shift": "shift_A_B",
    }
    result = aliases.get(normalized, value.strip())
    if result not in WORKLOADS:
        raise DataContractError(f"unexpected workload_distribution={value!r}; expected {WORKLOADS}")
    return result


def bytes_per_edge(row: dict[str, str], field: str) -> float:
    byte_count = number(row, field, nonnegative=True)
    edges = number(row, "directed_edge_count", positive=True)
    assert byte_count is not None and edges is not None
    return byte_count / edges


def control_plane_bytes_per_edge(row: dict[str, str]) -> float:
    return sum(bytes_per_edge(row, field) for field, _, _ in DISK_COMPONENTS)


def read_mib_per_op(row: dict[str, str]) -> float:
    byte_count = number(row, "body_read_bytes_total", nonnegative=True)
    operations = number(row, "measured_operations", positive=True)
    assert byte_count is not None and operations is not None
    value = byte_count / operations / (1024**2)
    if value <= 0:
        raise DataContractError(
            f"line {row['__line__']}: body read MiB/op must be >0 for the log Pareto panel"
        )
    return value


def nondominated(points: list[tuple[float, float]]) -> set[int]:
    result: set[int] = set()
    for index, (x, y) in enumerate(points):
        dominated = False
        for other_index, (other_x, other_y) in enumerate(points):
            if other_index == index:
                continue
            if other_x <= x and other_y <= y and (other_x < x or other_y < y):
                dominated = True
                break
        if not dominated:
            result.add(index)
    return result


def aggregate_performance(
    rows: list[dict[str, str]], minimum_runs: int
) -> dict[str, dict[str, dict[str, object]]]:
    result: dict[str, dict[str, dict[str, object]]] = {}
    groups = group_by(
        rows,
        lambda row: f"{canonical_workload(row['workload_distribution'])}|{canonical_budget(row['semantic_budget'])}",
    )
    expected = [f"{workload}|{budget}" for workload in WORKLOADS for budget in BUDGETS]
    require_groups(groups, expected, label="F3 performance workload/budget cells")
    for workload in WORKLOADS:
        result[workload] = {}
        for budget in BUDGETS:
            cell = groups[f"{workload}|{budget}"]
            result[workload][budget] = {
                "rss_gib": estimate(
                    cell,
                    lambda row: float(number(row, "steady_rss_bytes", positive=True)) / (1024**3),
                    label=f"F3/{workload}/{budget}/steady-RSS-GiB",
                    minimum_runs=minimum_runs,
                ),
                "p99": estimate_field(
                    cell,
                    "latency_p99_us",
                    label=f"F3/{workload}/{budget}/P99",
                    minimum_runs=minimum_runs,
                    positive=True,
                ),
                "disk_bpe": estimate(
                    cell,
                    control_plane_bytes_per_edge,
                    label=f"F3/{workload}/{budget}/control-plane-B-per-edge",
                    minimum_runs=minimum_runs,
                ),
                "read_mib": estimate(
                    cell,
                    read_mib_per_op,
                    label=f"F3/{workload}/{budget}/read-MiB-per-op",
                    minimum_runs=minimum_runs,
                ),
            }
    return result


def aggregate_breakdown(
    rows: list[dict[str, str]], minimum_runs: int
) -> dict[str, dict[str, object]]:
    groups = group_by(rows, lambda row: canonical_budget(row["semantic_budget"]))
    require_groups(groups, BUDGETS, label="F3 resource budgets")
    result: dict[str, dict[str, object]] = {}
    for budget in BUDGETS:
        cell = groups[budget]
        summary: dict[str, object] = {}
        for field, _, _ in DISK_COMPONENTS:
            summary[field] = estimate(
                cell,
                lambda row, component=field: bytes_per_edge(row, component),
                label=f"F3/{budget}/{field}-B-per-edge",
                minimum_runs=minimum_runs,
            )
        summary["files"] = estimate_field(
            cell,
            "live_file_count",
            label=f"F3/{budget}/live-files",
            minimum_runs=minimum_runs,
            positive=True,
        )
        summary["payload_bpe"] = estimate(
            cell,
            lambda row: bytes_per_edge(row, "payload_bytes"),
            label=f"F3/{budget}/payload-B-per-edge",
            minimum_runs=minimum_runs,
        )
        result[budget] = summary
    return result


def _plot_pareto(
    ax,
    performance,
    *,
    x_key: str,
    y_key: str,
    xlabel: str,
    ylabel: str,
    x_log: bool,
    panel: str,
    title: str,
):
    for workload in WORKLOADS:
        style = WORKLOAD_STYLE[workload]
        x_estimates = [performance[workload][budget][x_key] for budget in BUDGETS]
        y_estimates = [performance[workload][budget][y_key] for budget in BUDGETS]
        x_values = np.array([item.center for item in x_estimates])
        y_values = np.array([item.center for item in y_estimates])
        if x_log and np.any(x_values <= 0):
            raise DataContractError(f"F3/{workload}: {x_key} must be >0 for log x")
        if np.any(y_values <= 0):
            raise DataContractError(f"F3/{workload}: {y_key} must be >0 for log y")
        ax.plot(
            x_values,
            y_values,
            color=style["color"],
            marker=style["marker"],
            linestyle=style["linestyle"],
            linewidth=1.1,
            markersize=3.7,
            label=style["label"],
            zorder=2,
        )
        for index, (x_item, y_item) in enumerate(zip(x_estimates, y_estimates)):
            ax.errorbar(
                x_item.center,
                y_item.center,
                xerr=asymmetric_error(x_item),
                yerr=asymmetric_error(y_item),
                fmt="none",
                ecolor=style["color"],
                capsize=1.2,
                elinewidth=0.55,
                zorder=1,
            )
        frontier = nondominated(list(zip(x_values, y_values)))
        for index in frontier:
            ax.scatter(
                [x_values[index]],
                [y_values[index]],
                s=35,
                facecolors="none",
                edgecolors=PALETTE["dark"],
                linewidths=0.9,
                zorder=4,
            )
        for budget in ("B64", "full"):
            index = BUDGETS.index(budget)
            offset = (3, 4) if budget == "B64" else (3, -8)
            ax.annotate(
                budget,
                (x_values[index], y_values[index]),
                xytext=offset,
                textcoords="offset points",
                fontsize=5.8,
                color=style["color"],
            )
        if len(BUDGETS) >= 2:
            ax.annotate(
                "",
                xy=(x_values[-1], y_values[-1]),
                xytext=(x_values[-2], y_values[-2]),
                arrowprops={"arrowstyle": "->", "color": style["color"], "linewidth": 0.7},
            )
    if x_log:
        ax.set_xscale("log")
    else:
        ax.set_xlim(left=0)
    ax.set_yscale("log")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(axis="x", which="major", linewidth=0.45)
    panel_label(ax, panel)


def build_figure(performance, breakdown):
    fig = plt.figure(figsize=(7.05, 2.70), layout="constrained")
    grid = fig.add_gridspec(1, 3, width_ratios=[1.0, 1.0, 1.15])
    ax_memory = fig.add_subplot(grid[0, 0])
    ax_disk = fig.add_subplot(grid[0, 1])
    ax_breakdown = fig.add_subplot(grid[0, 2])

    _plot_pareto(
        ax_memory,
        performance,
        x_key="rss_gib",
        y_key="p99",
        xlabel="Steady RSS (GiB)",
        ylabel="P99 latency (us)",
        x_log=True,
        panel="(a)",
        title="Performance--memory",
    )
    ax_memory.annotate(
        "lower is better",
        xy=(0.08, 0.08),
        xytext=(0.48, 0.33),
        xycoords="axes fraction",
        textcoords="axes fraction",
        arrowprops={"arrowstyle": "->", "color": PALETTE["gray"], "linewidth": 0.7},
        fontsize=5.8,
        color=PALETTE["gray"],
    )

    _plot_pareto(
        ax_disk,
        performance,
        x_key="disk_bpe",
        y_key="read_mib",
        xlabel="Control-plane disk (B/edge)",
        ylabel="Body read (MiB/op)",
        x_log=False,
        panel="(b)",
        title="Read--metadata tradeoff",
    )
    handles, labels = ax_disk.get_legend_handles_labels()
    ax_disk.legend(handles, labels, frameon=False, loc="best")

    x = np.arange(len(BUDGETS))
    bottoms = np.zeros(len(BUDGETS), dtype=float)
    for field, label, color in DISK_COMPONENTS:
        values = np.array([breakdown[budget][field].center for budget in BUDGETS])
        ax_breakdown.bar(
            x,
            values,
            bottom=bottoms,
            label=label,
            color=color,
            edgecolor="white",
            linewidth=0.25,
        )
        bottoms += values
    payload_values = [breakdown[budget]["payload_bpe"].center for budget in BUDGETS]
    payload_reference = float(np.median(payload_values))
    if min(payload_values) > 0 and max(payload_values) / min(payload_values) <= 1.01:
        payload_text = f"payload ref.={payload_reference:.2f} B/edge"
    else:
        payload_text = f"payload={min(payload_values):.2f}--{max(payload_values):.2f} B/edge"
    ax_breakdown.text(
        0.99,
        0.97,
        payload_text,
        transform=ax_breakdown.transAxes,
        ha="right",
        va="top",
        fontsize=5.8,
        color=PALETTE["gray"],
    )
    for index, budget in enumerate(BUDGETS):
        files = breakdown[budget]["files"].center
        ax_breakdown.annotate(
            f"files={files:.0f}",
            (index, bottoms[index]),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            va="bottom",
            rotation=90,
            fontsize=5.4,
        )
    ax_breakdown.set_ylim(bottom=0)
    ax_breakdown.set_ylabel("Control-plane bytes/edge")
    ax_breakdown.set_xticks(x, BUDGETS, rotation=25, ha="right")
    ax_breakdown.set_title("Persistent overhead breakdown")
    ax_breakdown.legend(frameon=False, ncol=2, loc="upper left", handlelength=1.1, columnspacing=0.6)
    panel_label(ax_breakdown, "(c)")
    return fig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Tidy TSV satisfying F3+COMMON contract")
    parser.add_argument("--out-dir", type=Path, default=HERE / "output")
    parser.add_argument("--experiment-id", default="E04")
    parser.add_argument("--performance-phase", default="warm_read")
    parser.add_argument("--resource-phase", default="warm_read")
    parser.add_argument("--resource-workload", default="uniform")
    parser.add_argument("--performance-min-runs", type=int, default=5)
    parser.add_argument("--resource-min-runs", type=int, default=3)
    parser.add_argument("--stem", default="fig_budget_resource_pareto")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rows = load_rows(
            args.input.resolve(),
            figure_id="F3",
            experiment_id=args.experiment_id,
            plot_fields=PLOT_FIELDS,
        )
        require_constant(
            rows,
            [
                "system",
                "dataset_id",
                "input_sha256",
                "directed_edge_count",
                "cache_state",
                "concurrency",
                "host_fingerprint",
                "git_sha",
                "binary_sha256",
            ],
        )
        performance_rows = [row for row in rows if row["measurement_phase"] == args.performance_phase]
        if not performance_rows:
            raise DataContractError(
                f"F3: no rows for performance phase {args.performance_phase!r}"
            )
        for workload in WORKLOADS:
            workload_rows = [
                row
                for row in performance_rows
                if canonical_workload(row["workload_distribution"]) == workload
            ]
            if workload_rows:
                require_constant(workload_rows, ["workload_id", "query_trace_sha256"])
        resource_workload = canonical_workload(args.resource_workload)
        resource_rows = [
            row
            for row in rows
            if row["measurement_phase"] == args.resource_phase
            and canonical_workload(row["workload_distribution"]) == resource_workload
        ]
        if not resource_rows:
            raise DataContractError(
                f"F3: no rows for resource phase/workload "
                f"{args.resource_phase!r}/{resource_workload!r}"
            )
        require_constant(resource_rows, ["workload_id", "query_trace_sha256"])
        performance = aggregate_performance(performance_rows, args.performance_min_runs)
        breakdown = aggregate_breakdown(resource_rows, args.resource_min_runs)
        configure_matplotlib()
        fig = build_figure(performance, breakdown)
        report = save_figure(fig, args.out_dir.resolve(), args.stem)
        report_saved(report)
        return 0
    except DataContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
