#!/usr/bin/env python3
"""Render Figure 2: query-control component ablation from a strict tidy TSV."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
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


STAGES = ["A0", "A1", "A2", "A3", "A4", "A5", "A6"]
PLOT_FIELDS = {
    "ablation_stage",
    "feature_switches",
    "latency_p99_us",
    "candidate_segments_total",
    "body_read_segments_total",
    "measured_operations",
    "body_read_bytes_total",
    "cpu_signature_ns",
    "cpu_admission_ns",
    "cpu_routing_ns",
    "cpu_body_decode_filter_ns",
    "cpu_mvcc_result_ns",
    "cpu_total_ns",
}

CPU_PHASES = [
    ("cpu_signature_ns", "Signature", "#BFD7EA"),
    ("cpu_admission_ns", "Admission", "#84B6D7"),
    ("cpu_routing_ns", "Routing", "#4E92C4"),
    ("cpu_body_decode_filter_ns", "Body/filter", "#9CA3AF"),
    ("cpu_mvcc_result_ns", "MVCC/result", "#5E7D6A"),
]


def canonical_stage(value: str) -> str:
    stage = value.strip().upper()
    if stage not in STAGES:
        raise DataContractError(f"unexpected ablation_stage={value!r}; expected {STAGES}")
    return stage


def _json_object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_nonstandard_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _switch_names(values: object, *, context: str) -> frozenset[str]:
    if not isinstance(values, list):
        raise DataContractError(f"{context}: expected a JSON array of switch names")
    normalized: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise DataContractError(
                f"{context}: switch name at array index {index} must be a non-empty string"
            )
        normalized.append(value.strip())
    if len(normalized) != len(set(normalized)):
        raise DataContractError(
            f"{context}: enabled-switch array contains duplicate names after trimming"
        )
    return frozenset(normalized)


def _enabled_from_boolean_map(values: Mapping[str, object], *, context: str) -> frozenset[str]:
    normalized: dict[str, bool] = {}
    for raw_name, raw_state in values.items():
        name = raw_name.strip()
        if not name:
            raise DataContractError(f"{context}: switch names must be non-empty strings")
        if name in normalized:
            raise DataContractError(
                f"{context}: switch map contains duplicate names after trimming: {name!r}"
            )
        if not isinstance(raw_state, bool):
            raise DataContractError(
                f"{context}: switch {name!r} must have JSON boolean state, "
                f"got {type(raw_state).__name__}"
            )
        normalized[name] = raw_state
    return frozenset(name for name, enabled in normalized.items() if enabled)


def parse_feature_switches(value: str, *, line: str = "?") -> frozenset[str]:
    """Parse supported machine-readable switch states into the enabled-name set.

    Accepted JSON representations are an array of enabled names, a direct
    ``{name: boolean}`` map, ``{"enabled": [...], "disabled": [...]}``, or a
    single ``{"switches": ...}`` wrapper around an array/map. Ambiguous scalar,
    numeric, string-boolean, duplicate-key, and mixed representations are rejected.
    """

    context = f"line {line}: feature_switches"
    if not value.strip():
        raise DataContractError(f"{context} is blank")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_json_object_without_duplicate_keys,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise DataContractError(f"{context} must be valid strict JSON: {exc}") from exc

    if isinstance(parsed, list):
        return _switch_names(parsed, context=context)
    if not isinstance(parsed, dict):
        raise DataContractError(
            f"{context} must be a JSON array or object, got {type(parsed).__name__}"
        )

    if all(isinstance(state, bool) for state in parsed.values()):
        return _enabled_from_boolean_map(parsed, context=context)

    if set(parsed) == {"switches"}:
        wrapped = parsed["switches"]
        if isinstance(wrapped, list):
            return _switch_names(wrapped, context=f"{context}.switches")
        if isinstance(wrapped, dict) and all(
            isinstance(state, bool) for state in wrapped.values()
        ):
            return _enabled_from_boolean_map(wrapped, context=f"{context}.switches")
        raise DataContractError(
            f"{context}.switches must be an enabled-name array or boolean switch map"
        )

    if set(parsed).issubset({"enabled", "disabled"}) and parsed:
        enabled = _switch_names(parsed.get("enabled", []), context=f"{context}.enabled")
        disabled = _switch_names(
            parsed.get("disabled", []), context=f"{context}.disabled"
        )
        overlap = enabled & disabled
        if overlap:
            raise DataContractError(
                f"{context}: switches cannot be both enabled and disabled: {sorted(overlap)}"
            )
        return enabled

    raise DataContractError(
        f"{context}: unsupported/ambiguous JSON representation; use an enabled-name "
        "array, boolean map, enabled/disabled arrays, or a switches wrapper"
    )


def validate_feature_switch_staircase(
    groups: Mapping[str, Sequence[dict[str, str]]],
) -> dict[str, frozenset[str]]:
    states: dict[str, frozenset[str]] = {}
    for stage in STAGES:
        parsed_rows = [
            (
                parse_feature_switches(
                    row["feature_switches"], line=row.get("__line__", "?")
                ),
                row.get("__line__", "?"),
            )
            for row in groups[stage]
        ]
        distinct = {state for state, _ in parsed_rows}
        if len(distinct) != 1:
            rendered = [f"line {line}={sorted(state)}" for state, line in parsed_rows]
            raise DataContractError(
                f"F2/{stage}: feature_switches differs semantically across runs: "
                + "; ".join(rendered)
            )
        states[stage] = parsed_rows[0][0]

    for previous_stage, current_stage in zip(STAGES, STAGES[1:]):
        previous = states[previous_stage]
        current = states[current_stage]
        removed = previous - current
        added = current - previous
        if removed or len(added) != 1:
            raise DataContractError(
                f"F2: {previous_stage}->{current_stage} must preserve every enabled "
                "switch and enable exactly one new switch; "
                f"added={sorted(added)}, removed={sorted(removed)}"
            )
    return states


def per_operation(row: dict[str, str], numerator: str, *, scale: float = 1.0) -> float:
    total = number(row, numerator, nonnegative=True)
    operations = number(row, "measured_operations", positive=True)
    assert total is not None and operations is not None
    return total / operations / scale


def validate_cpu_accounting(row: dict[str, str]) -> None:
    phase_sum = 0.0
    for field, _, _ in CPU_PHASES:
        value = number(row, field, nonnegative=True)
        assert value is not None
        phase_sum += value
    total = number(row, "cpu_total_ns", positive=True)
    assert total is not None
    if phase_sum > total * 1.01:
        raise DataContractError(
            f"line {row['__line__']}: mutually-exclusive CPU phases sum to {phase_sum}, "
            f"exceeding cpu_total_ns={total} by more than 1%"
        )


def unattributed_cpu_us_per_op(row: dict[str, str]) -> float:
    phase_sum = sum(float(number(row, field, nonnegative=True)) for field, _, _ in CPU_PHASES)
    total = float(number(row, "cpu_total_ns", positive=True))
    difference = total - phase_sum
    if difference < 0 and abs(difference) <= total * 0.01:
        difference = 0.0
    operations = float(number(row, "measured_operations", positive=True))
    return difference / operations / 1000.0


def aggregate(groups: dict[str, list[dict[str, str]]], minimum_runs: int):
    summary: dict[str, dict[str, object]] = {}
    for stage in STAGES:
        rows = groups[stage]
        for row in rows:
            validate_cpu_accounting(row)
            candidates = number(row, "candidate_segments_total", nonnegative=True)
            reads = number(row, "body_read_segments_total", nonnegative=True)
            assert candidates is not None and reads is not None
            if reads > candidates:
                raise DataContractError(
                    f"line {row['__line__']}: body reads cannot exceed candidate segments"
                )
        stage_summary: dict[str, object] = {
            "p99": estimate_field(
                rows,
                "latency_p99_us",
                label=f"F2/{stage}/P99",
                minimum_runs=minimum_runs,
                positive=True,
            ),
            "candidates": estimate(
                rows,
                lambda row: per_operation(row, "candidate_segments_total"),
                label=f"F2/{stage}/candidates-per-op",
                minimum_runs=minimum_runs,
            ),
            "body_reads": estimate(
                rows,
                lambda row: per_operation(row, "body_read_segments_total"),
                label=f"F2/{stage}/body-reads-per-op",
                minimum_runs=minimum_runs,
            ),
            "read_mib": estimate(
                rows,
                lambda row: per_operation(row, "body_read_bytes_total", scale=1024**2),
                label=f"F2/{stage}/read-MiB-per-op",
                minimum_runs=minimum_runs,
            ),
            "cpu_total": estimate(
                rows,
                lambda row: per_operation(row, "cpu_total_ns", scale=1000.0),
                label=f"F2/{stage}/cpu-us-per-op",
                minimum_runs=minimum_runs,
            ),
        }
        for field, label, _ in CPU_PHASES:
            stage_summary[field] = estimate(
                rows,
                lambda row, phase=field: per_operation(row, phase, scale=1000.0),
                label=f"F2/{stage}/{label}-cpu-us-per-op",
                minimum_runs=minimum_runs,
            )
        stage_summary["cpu_unattributed"] = estimate(
            rows,
            unattributed_cpu_us_per_op,
            label=f"F2/{stage}/Unattributed-cpu-us-per-op",
            minimum_runs=minimum_runs,
        )
        summary[stage] = stage_summary
    return summary


def _stage_color(stage: str) -> str:
    if stage == "A0":
        return PALETTE["gray"]
    if stage == "A6":
        return PALETTE["green"]
    return PALETTE["cyan"]


def build_figure(summary):
    fig, axes = plt.subplots(2, 2, figsize=(7.05, 3.75), layout="constrained")
    ax_latency, ax_candidates, ax_bytes, ax_cpu = axes.flat
    x = np.arange(len(STAGES))

    p99 = [summary[stage]["p99"] for stage in STAGES]
    centers = np.array([item.center for item in p99])
    ax_latency.plot(x, centers, color=PALETTE["blue"], linewidth=1.15, zorder=1)
    for idx, stage in enumerate(STAGES):
        item = p99[idx]
        ax_latency.errorbar(
            idx,
            item.center,
            yerr=asymmetric_error(item),
            fmt="o",
            color=_stage_color(stage),
            markeredgecolor=PALETTE["dark"],
            markeredgewidth=0.45,
            markersize=4.0,
            capsize=1.5,
            elinewidth=0.65,
            zorder=3,
        )
    for idx in range(1, len(STAGES)):
        previous = p99[idx - 1]
        current = p99[idx]
        changed = current.high < previous.low or current.low > previous.high
        delta = (current.center / previous.center - 1.0) * 100.0
        if changed and abs(delta) >= 10.0:
            y = (current.center * previous.center) ** 0.5
            ax_latency.annotate(
                f"{delta:+.0f}%",
                ((idx - 0.5), y),
                xytext=(0, 4 if delta < 0 else -9),
                textcoords="offset points",
                ha="center",
                va="center",
                fontsize=6.0,
                color=PALETTE["dark"],
            )
    ax_latency.set_yscale("log")
    ax_latency.set_ylabel("P99 latency (us)")
    ax_latency.set_xticks(x, STAGES)
    ax_latency.set_title("Tail latency")
    panel_label(ax_latency, "(a)")

    candidate_styles = [
        ("candidates", "Candidates/op", PALETTE["blue"], "o", "-"),
        ("body_reads", "Body reads/op", PALETTE["orange"], "s", "--"),
    ]
    positive_candidate_values = 0
    zero_annotations: list[tuple[int, str]] = []
    for key, label, color, marker, linestyle in candidate_styles:
        values = [summary[stage][key] for stage in STAGES]
        centers = np.array([item.center if item.center > 0 else np.nan for item in values])
        positive_candidate_values += int(np.isfinite(centers).sum())
        for idx, item in enumerate(values):
            if item.center <= 0:
                zero_annotations.append((idx, label))
        ax_candidates.errorbar(
            x,
            centers,
            yerr=np.array(
                [
                    [item.lower_error if item.center > 0 else 0 for item in values],
                    [item.upper_error if item.center > 0 else 0 for item in values],
                ]
            ),
            label=label,
            color=color,
            marker=marker,
            linestyle=linestyle,
            linewidth=1.2,
            markersize=3.7,
            capsize=1.5,
        )
    if positive_candidate_values == 0:
        raise DataContractError("F2: candidates/body-read panel has no positive values for log scale")
    ax_candidates.set_yscale("log")
    ax_candidates.set_ylabel("Segments/op")
    ax_candidates.set_xticks(x, STAGES)
    ax_candidates.set_title("Admission consequence")
    ax_candidates.legend(frameon=False, loc="best")
    panel_label(ax_candidates, "(b)")
    for idx, label in zero_annotations:
        ax_candidates.text(
            idx,
            0.02,
            f"{label}=0",
            transform=ax_candidates.get_xaxis_transform(),
            rotation=90,
            va="bottom",
            ha="center",
            fontsize=5.6,
            color=PALETTE["gray"],
        )

    read_values = [summary[stage]["read_mib"] for stage in STAGES]
    if any(item.center <= 0 for item in read_values):
        bad = [stage for stage, item in zip(STAGES, read_values) if item.center <= 0]
        raise DataContractError(
            f"F2: read MiB/op must be >0 for the contracted log panel; nonpositive={bad}"
        )
    bars = ax_bytes.bar(
        x,
        [item.center for item in read_values],
        yerr=np.array(
            [
                [item.lower_error for item in read_values],
                [item.upper_error for item in read_values],
            ]
        ),
        capsize=1.5,
        color=[_stage_color(stage) for stage in STAGES],
        edgecolor=PALETTE["dark"],
        linewidth=0.45,
    )
    baseline = read_values[0].center
    for bar, item in zip(bars, read_values):
        ratio = baseline / item.center
        ax_bytes.annotate(
            f"{ratio:.2g}x",
            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=5.8,
        )
    ax_bytes.set_yscale("log")
    ax_bytes.set_ylabel("Body read (MiB/op)")
    ax_bytes.set_xticks(x, STAGES)
    ax_bytes.set_title("Physical read volume")
    panel_label(ax_bytes, "(c)")

    bottoms = np.zeros(len(STAGES), dtype=float)
    plot_phases = CPU_PHASES + [("cpu_unattributed", "Unattributed", "#4B5563")]
    for field, label, color in plot_phases:
        values = np.array([summary[stage][field].center for stage in STAGES])
        ax_cpu.bar(
            x,
            values,
            bottom=bottoms,
            label=label,
            color=color,
            edgecolor="white",
            linewidth=0.25,
        )
        bottoms += values
    totals = [summary[stage]["cpu_total"] for stage in STAGES]
    ax_cpu.errorbar(
        x,
        [item.center for item in totals],
        yerr=np.array(
            [
                [item.lower_error for item in totals],
                [item.upper_error for item in totals],
            ]
        ),
        fmt="none",
        ecolor=PALETTE["dark"],
        capsize=1.8,
        elinewidth=0.7,
        zorder=4,
    )
    for idx, item in enumerate(totals):
        ax_cpu.annotate(
            f"{item.center:.1f}",
            (idx, item.center),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            fontsize=5.8,
        )
    ax_cpu.set_ylim(bottom=0)
    ax_cpu.set_ylabel("CPU (us/op)")
    ax_cpu.set_xticks(x, STAGES)
    ax_cpu.set_title("CPU attribution")
    ax_cpu.legend(frameon=False, ncol=3, loc="upper center", handlelength=1.2, columnspacing=0.7)
    panel_label(ax_cpu, "(d)")
    return fig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Tidy TSV satisfying F2+COMMON contract")
    parser.add_argument("--out-dir", type=Path, default=HERE / "output")
    parser.add_argument("--experiment-id", default="E03")
    parser.add_argument("--min-runs", type=int, default=5)
    parser.add_argument("--stem", default="fig_component_ablation")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rows = load_rows(
            args.input.resolve(),
            figure_id="F2",
            experiment_id=args.experiment_id,
            plot_fields=PLOT_FIELDS,
        )
        require_constant(
            rows,
            [
                "host_fingerprint",
                "git_sha",
                "binary_sha256",
                "system",
                "dataset_id",
                "input_sha256",
                "workload_id",
                "query_trace_sha256",
                "directed_edge_count",
                "cache_state",
                "concurrency",
            ],
            label="F2 comparison campaign",
        )
        groups = group_by(rows, lambda row: canonical_stage(row["ablation_stage"]))
        require_groups(groups, STAGES, label="F2 stages")
        validate_feature_switch_staircase(groups)
        summary = aggregate(groups, args.min_runs)
        configure_matplotlib()
        fig = build_figure(summary)
        report = save_figure(fig, args.out_dir.resolve(), args.stem)
        report_saved(report)
        return 0
    except DataContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
