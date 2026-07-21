#!/usr/bin/env python3
"""Render Figure 4: dynamic-compaction timelines from the frozen F4 TSV."""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D

from plot_support import (
    DataContractError,
    Estimate,
    PALETTE,
    bool_field,
    configure_matplotlib,
    estimate_values,
    group_by,
    integer,
    load_figure_rows,
    number,
    panel_label,
    report_saved,
    require_constant,
    require_groups,
    resolve_input,
    save_figure,
    text,
    warn,
)


FIGURE_ID = "F4"
DEFAULT_EXPERIMENT_ID = "E05"
STEM = "fig_dynamic_compaction_timeline"
WINDOW_SECONDS = 30.0
MINIMUM_RUNS = 3

POLICIES = (
    "none",
    "capacity_naive",
    "semantic_static",
    "semantic_feedback",
)
POLICY_LABEL = {
    "none": "none (diagnostic)",
    "capacity_naive": "capacity-naive",
    "semantic_static": "semantic-static",
    "semantic_feedback": "semantic-feedback",
}
POLICY_STYLE = {
    "none": {"color": PALETTE["gray"], "linestyle": ":", "marker": "o"},
    "capacity_naive": {
        "color": PALETTE["orange"],
        "linestyle": "--",
        "marker": "X",
    },
    "semantic_static": {
        "color": PALETTE["green"],
        "linestyle": "-",
        "marker": "D",
    },
    "semantic_feedback": {
        "color": PALETTE["purple"],
        "linestyle": "-",
        "marker": "P",
    },
}
PHASES = ("steady", "burst", "shift_A_B", "recovery")
PHASE_LABEL = {
    "steady": "steady",
    "burst": "burst",
    "shift_A_B": "A→B",
    "recovery": "recovery",
}
PHASE_COLOR = {
    "steady": PALETTE["gray"],
    "burst": PALETTE["orange"],
    "shift_A_B": PALETTE["purple"],
    "recovery": PALETTE["green"],
}


def _line(row: Mapping[str, str]) -> str:
    return row.get("__line__", "?")


def _window_key(row: Mapping[str, str]) -> tuple[float, float]:
    start = number(row, "window_start_s", nonnegative=True)
    end = number(row, "window_end_s", positive=True)
    assert start is not None and end is not None
    return (start, end)


def _validate_row(row: Mapping[str, str]) -> None:
    line = _line(row)
    policy = text(row, "compaction_policy")
    if policy not in POLICIES:
        raise DataContractError(
            f"line {line}: compaction_policy must be one of {list(POLICIES)}, "
            f"got {policy!r}"
        )
    phase = text(row, "workload_phase")
    if phase not in PHASES:
        raise DataContractError(
            f"line {line}: workload_phase must be one of {list(PHASES)}, got {phase!r}"
        )

    start, end = _window_key(row)
    if end <= start:
        raise DataContractError(
            f"line {line}: window_end_s must exceed window_start_s, got {start}..{end}"
        )
    if not math.isclose(end - start, WINDOW_SECONDS, abs_tol=1e-6):
        raise DataContractError(
            f"line {line}: Figure 4 requires fixed 30-second windows, got {end-start:g}s"
        )
    if not math.isclose(start / WINDOW_SECONDS, round(start / WINDOW_SECONDS), abs_tol=1e-8):
        raise DataContractError(
            f"line {line}: window_start_s={start:g} is not aligned to the 30-second grid"
        )

    latency = number(row, "read_latency_p99_us", positive=True, allow_blank=True)
    if latency is None and not row.get("unsupported_reason", "").strip():
        raise DataContractError(
            f"line {line}: blank read_latency_p99_us requires unsupported_reason"
        )
    number(row, "offered_read_qps", nonnegative=True)
    number(row, "achieved_read_qps", nonnegative=True)
    number(row, "writer_throughput_ops_s", nonnegative=True)
    integer(row, "candidate_bytes_total", nonnegative=True)
    integer(row, "completed_reads", nonnegative=True)
    kind = text(row, "candidate_metric_kind")
    if kind not in {"full_execution", "metadata_proxy"}:
        raise DataContractError(
            f"line {line}: candidate_metric_kind must be full_execution or "
            f"metadata_proxy, got {kind!r}"
        )
    integer(row, "live_l0_file_count", nonnegative=True)
    cpu = number(row, "cpu_quota_percent", nonnegative=True)
    assert cpu is not None
    if cpu > 105.0:
        raise DataContractError(
            f"line {line}: cpu_quota_percent={cpu:g} exceeds the allowed 100% "
            "quota plus 5% measurement tolerance"
        )
    integer(row, "rss_bytes", positive=True)
    integer(row, "swap_in_bytes", nonnegative=True)
    integer(row, "swap_out_bytes", nonnegative=True)
    integer(row, "compaction_count_cumulative", nonnegative=True)
    integer(row, "compaction_input_bytes_cumulative", nonnegative=True)
    integer(row, "compaction_output_bytes_cumulative", nonnegative=True)
    write_amplification = number(row, "write_amplification", positive=True)
    assert write_amplification is not None
    if write_amplification < 1.0:
        raise DataContractError(
            f"line {line}: write_amplification={write_amplification:g} is below 1; "
            "the frozen F4 contract requires >=1 unless a documented-exception field "
            "is added to the contract"
        )
    integer(row, "writer_error_count", nonnegative=True)


def _validate_runs(rows: Sequence[dict[str, str]]) -> None:
    by_run = group_by(rows, lambda row: row["run_id"])
    for run_id, run_rows in by_run.items():
        require_constant(
            run_rows,
            ("compaction_policy", "variant", "measurement_s"),
            label=f"run_id={run_id}",
        )
        policy = run_rows[0]["compaction_policy"]
        # The policy column is authoritative, but a changing/mislabelled variant is
        # too risky for a paper figure even when the free-form spelling differs.
        if not run_rows[0]["variant"].strip():
            raise DataContractError(f"run_id={run_id}: variant is blank")

        ordered = sorted(run_rows, key=_window_key)
        seen: set[tuple[float, float]] = set()
        previous_end: float | None = None
        cumulative_fields = (
            "compaction_count_cumulative",
            "compaction_input_bytes_cumulative",
            "compaction_output_bytes_cumulative",
        )
        previous_cumulative: dict[str, int] = {}
        for row in ordered:
            key = _window_key(row)
            if key in seen:
                raise DataContractError(
                    f"run_id={run_id}, policy={policy}: duplicate window {key}"
                )
            seen.add(key)
            if previous_end is not None and key[0] < previous_end - 1e-8:
                raise DataContractError(
                    f"run_id={run_id}, policy={policy}: overlapping windows near {key}"
                )
            previous_end = key[1]
            for field in cumulative_fields:
                value = integer(row, field, nonnegative=True)
                assert value is not None
                if field in previous_cumulative and value < previous_cumulative[field]:
                    raise DataContractError(
                        f"run_id={run_id}, policy={policy}: {field} decreases at "
                        f"window {key}: {previous_cumulative[field]} -> {value}"
                    )
                previous_cumulative[field] = value


def _select_paired_valid_runs(
    rows: Sequence[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, tuple[str, ...]]]:
    by_run = group_by(rows, lambda row: row["run_id"])
    valid_run_rows: dict[str, list[dict[str, str]]] = {}
    rejected: list[str] = []
    for run_id, run_rows in by_run.items():
        valid = all(
            bool_field(row, "digest_pass")
            and integer(row, "mismatch_count", nonnegative=True) == 0
            and integer(row, "writer_error_count", nonnegative=True) == 0
            for row in run_rows
        )
        if valid:
            valid_run_rows[str(run_id)] = run_rows
        else:
            rejected.append(str(run_id))
    if rejected:
        warn(
            "F4: excluded complete runs that contain a digest failure, mismatch, or "
            f"writer error: {', '.join(sorted(rejected))}"
        )

    by_policy_repeat: dict[str, dict[int, str]] = {policy: {} for policy in POLICIES}
    for run_id, run_rows in valid_run_rows.items():
        policy = run_rows[0]["compaction_policy"]
        repeat = integer(run_rows[0], "repeat_index", positive=True)
        assert repeat is not None
        if repeat in by_policy_repeat[policy]:
            raise DataContractError(
                f"policy={policy}: two valid run_id values use repeat_index={repeat}"
            )
        by_policy_repeat[policy][repeat] = run_id

    require_groups(
        {policy: ids for policy, ids in by_policy_repeat.items() if ids},
        POLICIES,
        label="compaction policies with valid runs",
    )
    complete_repeats = set.intersection(
        *(set(by_policy_repeat[policy]) for policy in POLICIES)
    )
    if len(complete_repeats) < MINIMUM_RUNS:
        counts = {policy: len(by_policy_repeat[policy]) for policy in POLICIES}
        raise DataContractError(
            "F4 requires at least 3 repeat_index values with valid runs under all four "
            f"policies; complete={sorted(complete_repeats)}, valid_counts={counts}"
        )

    dropped = {
        policy: sorted(set(by_policy_repeat[policy]) - complete_repeats)
        for policy in POLICIES
    }
    dropped = {policy: repeats for policy, repeats in dropped.items() if repeats}
    if dropped:
        warn(f"F4: excluded unpaired valid repeats before policy comparison: {dropped}")

    selected_run_ids: dict[str, tuple[str, ...]] = {}
    selected_rows: list[dict[str, str]] = []
    matched_fields = (
        "dataset_id",
        "directed_edge_count",
        "input_sha256",
        "workload_id",
        "query_trace_sha256",
        "cache_state",
        "concurrency",
    )
    for repeat in sorted(complete_repeats):
        reference = valid_run_rows[by_policy_repeat[POLICIES[0]][repeat]][0]
        for policy in POLICIES[1:]:
            candidate = valid_run_rows[by_policy_repeat[policy][repeat]][0]
            for field in matched_fields:
                if candidate[field] != reference[field]:
                    raise DataContractError(
                        f"repeat_index={repeat}: policy runs differ in {field}: "
                        f"{reference[field]!r} vs {candidate[field]!r}"
                    )
            if candidate["seed"] != reference["seed"]:
                raise DataContractError(
                    f"repeat_index={repeat}: policy runs use different seeds: "
                    f"{reference['seed']!r} vs {candidate['seed']!r}"
                )
    for policy in POLICIES:
        run_ids = tuple(by_policy_repeat[policy][repeat] for repeat in sorted(complete_repeats))
        selected_run_ids[policy] = run_ids
        for run_id in run_ids:
            selected_rows.extend(valid_run_rows[run_id])
    return selected_rows, selected_run_ids


def _validate_campaign(rows: Sequence[dict[str, str]]) -> None:
    require_constant(
        rows,
        (
            "experiment_id",
            "host_fingerprint",
            "git_sha",
            "binary_sha256",
            "system",
            "dataset_id",
            "vertex_count",
            "directed_edge_count",
            "input_sha256",
            "workload_id",
            "query_trace_sha256",
            "cache_state",
            "concurrency",
            "warmup_s",
            "measurement_s",
        ),
        label="F4 synchronized campaign",
    )

    by_window = group_by(rows, _window_key)
    for key, window_rows in by_window.items():
        phases = {row["workload_phase"] for row in window_rows}
        if len(phases) != 1:
            raise DataContractError(
                f"window={key}: workload phase is not synchronized: {sorted(phases)}"
            )
        offered = {
            float(number(row, "offered_read_qps", nonnegative=True)) for row in window_rows
        }
        if len(offered) != 1:
            raise DataContractError(
                f"window={key}: offered_read_qps is not fixed across runs/policies: "
                f"{sorted(offered)}"
            )


def _all_windows(rows: Sequence[dict[str, str]]) -> list[tuple[float, float]]:
    keys = sorted({_window_key(row) for row in rows})
    if not keys:
        raise DataContractError("F4: no windows remain after correctness gates")
    first = keys[0][0]
    last = keys[-1][1]
    count_float = (last - first) / WINDOW_SECONDS
    if not math.isclose(count_float, round(count_float), abs_tol=1e-8):
        raise DataContractError("F4: campaign extent is not divisible into 30-second windows")
    return [
        (first + index * WINDOW_SECONDS, first + (index + 1) * WINDOW_SECONDS)
        for index in range(round(count_float))
    ]


def _metric_estimates(
    rows: Sequence[dict[str, str]],
    run_ids: Sequence[str],
    windows: Sequence[tuple[float, float]],
    *,
    label: str,
    getter: Callable[[Mapping[str, str]], float | None],
) -> tuple[list[Estimate | None], list[str | None]]:
    by_run_window: dict[tuple[str, tuple[float, float]], dict[str, str]] = {}
    for row in rows:
        key = (row["run_id"], _window_key(row))
        if key in by_run_window:
            raise DataContractError(f"{label}: duplicate row for run/window {key}")
        by_run_window[key] = row

    estimates: list[Estimate | None] = []
    kinds: list[str | None] = []
    gaps = 0
    for window in windows:
        values: list[tuple[str, float]] = []
        observed_kinds: set[str] = set()
        complete = True
        for run_id in run_ids:
            row = by_run_window.get((run_id, window))
            if row is None:
                complete = False
                break
            value = getter(row)
            if value is None or not math.isfinite(value):
                complete = False
                break
            values.append((run_id, float(value)))
            if label.endswith("candidate MiB/op"):
                observed_kinds.add(row["candidate_metric_kind"])
        if not complete:
            # None becomes NaN at plotting time. Matplotlib then breaks the line;
            # deliberately do not estimate or interpolate across this window.
            estimates.append(None)
            kinds.append(None)
            gaps += 1
            continue
        if len(observed_kinds) > 1:
            raise DataContractError(
                f"{label}, window={window}: full_execution and metadata_proxy are "
                "mixed across independent runs"
            )
        estimates.append(
            estimate_values(
                values,
                label=f"F4 {label} window={window}",
                minimum_runs=MINIMUM_RUNS,
            )
        )
        kinds.append(next(iter(observed_kinds)) if observed_kinds else None)
    if gaps:
        warn(
            f"F4 {label}: left {gaps}/{len(windows)} windows blank because at least "
            "one selected run/window/value was missing; no interpolation was applied"
        )
    return estimates, kinds


def _arrays(estimates: Sequence[Estimate | None]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    center = np.asarray(
        [estimate.center if estimate is not None else np.nan for estimate in estimates],
        dtype=float,
    )
    low = np.asarray(
        [estimate.low if estimate is not None else np.nan for estimate in estimates],
        dtype=float,
    )
    high = np.asarray(
        [estimate.high if estimate is not None else np.nan for estimate in estimates],
        dtype=float,
    )
    return center, low, high


def _plot_estimates(
    ax: plt.Axes,
    x: np.ndarray,
    estimates: Sequence[Estimate | None],
    *,
    policy: str,
    step: bool = False,
    candidate_kinds: Sequence[str | None] | None = None,
) -> np.ndarray:
    center, low, high = _arrays(estimates)
    style = POLICY_STYLE[policy]
    if step:
        # ``step(..., where='post')`` can otherwise extend the last finite value
        # all the way to the following NaN checkpoint. Draw each contiguous block
        # separately so a missing window is blank over its entire interval.
        finite = np.isfinite(center) & np.isfinite(low) & np.isfinite(high)
        boundaries = np.flatnonzero(np.diff(np.r_[False, finite, False]))
        for start, stop in boundaries.reshape(-1, 2):
            section = slice(int(start), int(stop))
            ax.step(
                x[section],
                center[section],
                where="post",
                color=style["color"],
                linestyle=style["linestyle"],
                linewidth=1.0,
                marker=style["marker"],
                markersize=2.5,
                markeredgewidth=0.55,
                zorder=3,
            )
            ax.fill_between(
                x[section],
                low[section],
                high[section],
                step="post",
                color=style["color"],
                alpha=0.10,
                linewidth=0,
                zorder=1,
            )
    elif candidate_kinds is None:
        ax.plot(
            x,
            center,
            color=style["color"],
            linestyle=style["linestyle"],
            linewidth=1.0,
            marker=style["marker"],
            markersize=2.5,
            markeredgewidth=0.55,
            zorder=3,
        )
        ax.fill_between(
            x,
            low,
            high,
            color=style["color"],
            alpha=0.10,
            linewidth=0,
            zorder=1,
        )
    else:
        ax.plot(
            x,
            center,
            color=style["color"],
            linestyle=style["linestyle"],
            linewidth=1.0,
            zorder=2,
        )
        ax.fill_between(
            x,
            low,
            high,
            color=style["color"],
            alpha=0.10,
            linewidth=0,
            zorder=1,
        )
        for kind, hollow in (("full_execution", False), ("metadata_proxy", True)):
            mask = np.asarray(
                [
                    candidate_kind == kind and np.isfinite(value)
                    for candidate_kind, value in zip(candidate_kinds, center)
                ]
            )
            if np.any(mask):
                ax.scatter(
                    x[mask],
                    center[mask],
                    s=10,
                    marker=style["marker"],
                    facecolors="none" if hollow else style["color"],
                    edgecolors=style["color"],
                    linewidths=0.65,
                    zorder=4,
                )
    return center


def _phase_runs(
    rows: Sequence[dict[str, str]], windows: Sequence[tuple[float, float]]
) -> list[tuple[float, float, str]]:
    phase_by_window: dict[tuple[float, float], str] = {}
    for window, window_rows in group_by(rows, _window_key).items():
        phase_by_window[window] = window_rows[0]["workload_phase"]
    missing = [window for window in windows if window not in phase_by_window]
    if missing:
        raise DataContractError(
            "F4: no row provides workload_phase for expected windows: "
            + ", ".join(str(window) for window in missing)
        )
    runs: list[tuple[float, float, str]] = []
    start, end = windows[0]
    phase = phase_by_window[windows[0]]
    for window in windows[1:]:
        next_phase = phase_by_window[window]
        if next_phase == phase:
            end = window[1]
        else:
            runs.append((start, end, phase))
            start, end, phase = window[0], window[1], next_phase
    runs.append((start, end, phase))
    return runs


def _draw_phases(
    axes: Sequence[plt.Axes], phase_runs: Sequence[tuple[float, float, str]]
) -> None:
    for ax in axes:
        for start, end, phase in phase_runs:
            ax.axvspan(
                start / 60.0,
                end / 60.0,
                color=PHASE_COLOR[phase],
                alpha=0.04,
                linewidth=0,
                zorder=0,
            )
    top = axes[0]
    for start, end, phase in phase_runs:
        label = top.text(
            (start + end) / 120.0,
            1.015,
            PHASE_LABEL[phase],
            transform=top.get_xaxis_transform(),
            ha="center",
            va="bottom",
            fontsize=6.0,
            color=PALETTE["dark"],
            clip_on=False,
        )
        label.set_gid("qa-ignore")


def _event_windows(run_rows: Sequence[dict[str, str]]) -> tuple[float, ...]:
    ordered = sorted(run_rows, key=_window_key)
    previous = 0
    events: list[float] = []
    for row in ordered:
        value = integer(row, "compaction_count_cumulative", nonnegative=True)
        assert value is not None
        if value > previous:
            events.append(_window_key(row)[1] / 60.0)
        previous = value
    return tuple(events)


def _draw_event_rugs(
    axes: Sequence[plt.Axes],
    rows: Sequence[dict[str, str]],
    selected_run_ids: Mapping[str, Sequence[str]],
) -> None:
    by_run = group_by(rows, lambda row: row["run_id"])
    for policy_index, policy in enumerate(POLICIES):
        event_sets = [_event_windows(by_run[run_id]) for run_id in selected_run_ids[policy]]
        if len(set(event_sets)) != 1:
            warn(
                f"F4 policy={policy}: compaction event windows are not aligned across "
                "runs; omitted rugs because no representative_run column is in the contract"
            )
            continue
        y = 0.995 - policy_index * 0.018
        for ax in axes:
            for event_x in event_sets[0]:
                ax.plot(
                    [event_x],
                    [y],
                    marker="|",
                    markersize=3.0,
                    markeredgewidth=0.8,
                    color=POLICY_STYLE[policy]["color"],
                    transform=ax.get_xaxis_transform(),
                    clip_on=False,
                    zorder=8,
                )


def _annotate_phase_ratios(
    ax: plt.Axes,
    x: np.ndarray,
    phase_runs: Sequence[tuple[float, float, str]],
    latency: Mapping[str, np.ndarray],
) -> None:
    capacity = latency["capacity_naive"]
    feedback = latency["semantic_feedback"]
    for _, phase_end_s, _ in phase_runs:
        eligible = np.flatnonzero(x <= phase_end_s / 60.0 + 1e-9)
        if eligible.size == 0:
            continue
        index = int(eligible[-1])
        top, bottom = capacity[index], feedback[index]
        if np.isfinite(top) and np.isfinite(bottom) and top > 0 and bottom > 0:
            annotation = ax.annotate(
                f"cap/fb {top / bottom:.2g}×",
                xy=(x[index], feedback[index]),
                xytext=(-2, 4),
                textcoords="offset points",
                ha="right",
                va="bottom",
                fontsize=6.0,
                color=PALETTE["dark"],
            )
            annotation.set_gid("qa-ignore")


def _annotate_compaction_endpoints(
    ax: plt.Axes,
    x: np.ndarray,
    output: Mapping[str, np.ndarray],
    count: Mapping[str, np.ndarray],
    wa: Mapping[str, np.ndarray],
) -> None:
    offsets = {
        "none": (3, -8),
        "capacity_naive": (3, 7),
        "semantic_static": (3, -8),
        "semantic_feedback": (3, 7),
    }
    for policy in POLICIES:
        valid = np.flatnonzero(
            np.isfinite(output[policy])
            & np.isfinite(count[policy])
            & np.isfinite(wa[policy])
        )
        if valid.size == 0:
            continue
        index = int(valid[-1])
        annotation = ax.annotate(
            f"n={count[policy][index]:.0f}, WA={wa[policy][index]:.2g}×",
            xy=(x[index], output[policy][index]),
            xytext=offsets[policy],
            textcoords="offset points",
            ha="left",
            va="center",
            fontsize=6.0,
            color=POLICY_STYLE[policy]["color"],
        )
        annotation.set_gid("qa-ignore")


def build_figure(
    rows: Sequence[dict[str, str]], selected_run_ids: Mapping[str, Sequence[str]]
) -> plt.Figure:
    configure_matplotlib()
    windows = _all_windows(rows)
    x = np.asarray([end / 60.0 for _, end in windows], dtype=float)
    phase_runs = _phase_runs(rows, windows)
    by_policy = group_by(rows, lambda row: row["compaction_policy"])

    fig, axes_array = plt.subplots(
        4,
        2,
        figsize=(7.05, 5.10),
        sharex=True,
        gridspec_kw={"hspace": 0.10, "wspace": 0.22},
    )
    axes = list(axes_array.flat)
    _draw_phases(axes, phase_runs)

    metric_specs: list[
        tuple[str, plt.Axes, Callable[[Mapping[str, str]], float | None], bool]
    ] = [
        (
            "read P99",
            axes[0],
            lambda row: number(row, "read_latency_p99_us", positive=True, allow_blank=True),
            False,
        ),
        (
            "achieved read QPS",
            axes[1],
            lambda row: number(row, "achieved_read_qps", nonnegative=True),
            False,
        ),
        (
            "writer throughput",
            axes[2],
            lambda row: number(row, "writer_throughput_ops_s", nonnegative=True),
            False,
        ),
        (
            "candidate MiB/op",
            axes[3],
            lambda row: (
                None
                if integer(row, "completed_reads", nonnegative=True) == 0
                or integer(row, "candidate_bytes_total", nonnegative=True) == 0
                else float(integer(row, "candidate_bytes_total", nonnegative=True))
                / float(integer(row, "completed_reads", positive=True))
                / (1024.0**2)
            ),
            False,
        ),
        (
            "live L0 files",
            axes[4],
            lambda row: float(integer(row, "live_l0_file_count", nonnegative=True)),
            True,
        ),
        (
            "quota-normalized CPU",
            axes[5],
            lambda row: number(row, "cpu_quota_percent", nonnegative=True),
            False,
        ),
        (
            "RSS GiB",
            axes[6],
            lambda row: float(integer(row, "rss_bytes", positive=True)) / (1024.0**3),
            False,
        ),
        (
            "compaction output GiB",
            axes[7],
            lambda row: float(
                integer(row, "compaction_output_bytes_cumulative", nonnegative=True)
            )
            / (1024.0**3),
            True,
        ),
    ]

    centers: dict[str, dict[str, np.ndarray]] = {label: {} for label, *_ in metric_specs}
    for policy in POLICIES:
        policy_rows = by_policy[policy]
        for label, ax, getter, step in metric_specs:
            estimates, kinds = _metric_estimates(
                policy_rows,
                selected_run_ids[policy],
                windows,
                label=f"policy={policy} {label}",
                getter=getter,
            )
            centers[label][policy] = _plot_estimates(
                ax,
                x,
                estimates,
                policy=policy,
                step=step,
                candidate_kinds=kinds if label == "candidate MiB/op" else None,
            )

    # Offered load is a synchronized context trace. Keep the same completeness
    # rule as measured series so a missing window is never visually reconstructed.
    all_selected_run_ids = tuple(
        run_id for policy in POLICIES for run_id in selected_run_ids[policy]
    )
    offered_estimates, _ = _metric_estimates(
        rows,
        all_selected_run_ids,
        windows,
        label="offered read QPS",
        getter=lambda row: number(row, "offered_read_qps", nonnegative=True),
    )
    offered, _, _ = _arrays(offered_estimates)
    axes[1].plot(
        x,
        offered,
        color=PALETTE["dark"],
        linestyle=":",
        linewidth=0.8,
        label="offered",
        zorder=5,
    )

    count_centers: dict[str, np.ndarray] = {}
    wa_centers: dict[str, np.ndarray] = {}
    for policy in POLICIES:
        count_estimates, _ = _metric_estimates(
            by_policy[policy],
            selected_run_ids[policy],
            windows,
            label=f"policy={policy} compaction count annotation",
            getter=lambda row: float(
                integer(row, "compaction_count_cumulative", nonnegative=True)
            ),
        )
        wa_estimates, _ = _metric_estimates(
            by_policy[policy],
            selected_run_ids[policy],
            windows,
            label=f"policy={policy} write amplification annotation",
            getter=lambda row: number(row, "write_amplification", positive=True),
        )
        count_centers[policy], _, _ = _arrays(count_estimates)
        wa_centers[policy], _, _ = _arrays(wa_estimates)

    _annotate_phase_ratios(axes[0], x, phase_runs, centers["read P99"])
    _annotate_compaction_endpoints(
        axes[7],
        x,
        centers["compaction output GiB"],
        count_centers,
        wa_centers,
    )
    _draw_event_rugs((axes[4], axes[7]), rows, selected_run_ids)

    # Mark (rather than label) the median RSS peak for each policy.
    for policy in POLICIES:
        rss = centers["RSS GiB"][policy]
        if np.any(np.isfinite(rss)):
            peak_index = int(np.nanargmax(rss))
            axes[6].scatter(
                [x[peak_index]],
                [rss[peak_index]],
                marker="^",
                s=16,
                facecolors="none",
                edgecolors=POLICY_STYLE[policy]["color"],
                linewidths=0.7,
                zorder=6,
            )

    labels = (
        "Read P99 (µs)",
        "Achieved read QPS",
        "Writer throughput (ops/s)",
        "Candidate (MiB/op)",
        "Live L0 files",
        "CPU / quota (%)",
        "RSS (GiB)",
        "Compaction out (GiB)",
    )
    for index, (ax, ylabel) in enumerate(zip(axes, labels)):
        panel_label(ax, f"({chr(ord('a') + index)})")
        ax.set_ylabel(ylabel)
        ax.margins(x=0.01)
    for ax in axes[6:]:
        ax.set_xlabel("Elapsed time (min)")

    for ax in (axes[0], axes[3]):
        ax.set_yscale("log")
    for ax in (axes[1], axes[2], axes[4], axes[5], axes[6], axes[7]):
        ax.set_ylim(bottom=0)
    axes[5].set_ylim(0, 105)

    swap_in = sum(int(integer(row, "swap_in_bytes", nonnegative=True)) for row in rows)
    swap_out = sum(int(integer(row, "swap_out_bytes", nonnegative=True)) for row in rows)
    if swap_in or swap_out:
        warn(
            "F4: nonzero swap traffic observed in selected rows: "
            f"swap_in_bytes={swap_in}, swap_out_bytes={swap_out}; report it in the table/caption"
        )

    legend_handles = [
        Line2D(
            [0],
            [0],
            color=POLICY_STYLE[policy]["color"],
            linestyle=POLICY_STYLE[policy]["linestyle"],
            marker=POLICY_STYLE[policy]["marker"],
            markersize=3.3,
            linewidth=1.0,
            label=POLICY_LABEL[policy],
        )
        for policy in POLICIES
    ]
    legend_handles.extend(
        [
            Line2D(
                [0], [0], color=PALETTE["dark"], linestyle=":", linewidth=0.8, label="offered"
            ),
            Line2D(
                [0],
                [0],
                color=PALETTE["dark"],
                marker="o",
                markerfacecolor="none",
                linestyle="none",
                markersize=3.3,
                label="metadata proxy",
            ),
        ]
    )
    fig.legend(
        handles=legend_handles,
        loc="upper center",
        bbox_to_anchor=(0.53, 0.997),
        ncol=6,
        frameon=False,
        handlelength=2.0,
        columnspacing=0.8,
    )
    fig.subplots_adjust(left=0.090, right=0.985, bottom=0.085, top=0.910)
    return fig


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render Figure 4 from a strict frozen-contract tidy TSV."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("."),
        help="Directory containing F4.tsv when --input is omitted (default: current directory).",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Explicit TSV path; a relative path is resolved below --data-dir.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Output directory for PNG, SVG, PDF, and layout JSON.",
    )
    parser.add_argument(
        "--experiment-id",
        default=DEFAULT_EXPERIMENT_ID,
        help=f"Experiment ID to select (default: {DEFAULT_EXPERIMENT_ID}).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        input_path = resolve_input(args.data_dir, args.input, FIGURE_ID)
        rows = load_figure_rows(
            input_path,
            figure_id=FIGURE_ID,
            experiment_ids=(args.experiment_id,),
        )
        for row in rows:
            _validate_row(row)
        _validate_runs(rows)
        selected_rows, selected_run_ids = _select_paired_valid_runs(rows)
        _validate_campaign(selected_rows)
        figure = build_figure(selected_rows, selected_run_ids)
        report = save_figure(figure, args.out_dir, STEM)
        report_saved(report)
    except DataContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

