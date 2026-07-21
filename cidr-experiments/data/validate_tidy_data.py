#!/usr/bin/env python3
"""Validate figure-ready tidy TSV files against the frozen field contract."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path


# ``none`` is intentionally not a missing-value token: it is a valid frozen F4
# compaction_policy.  Optional values should use an empty cell (or NA/null).
EMPTY = {"", "na", "n/a", "null", "nan"}
HEX40 = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
HEX64 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)

F5_GRAIN_MARKERS = ("query_semantic", "update_pattern", "fallback_scenario")
F5_GRAIN_REQUIRED = {
    "query_semantic": {
        "query_semantic",
        "edge_selectivity_class",
        "source_degree_class",
        "support_state",
    },
    # measured_operations is consumed by the formal F5 workload-mix loader even
    # though the frozen table lists it under panel a-b.  Keep the validator and
    # the loader in lockstep until/unless the frozen contract is revised.
    "update_pattern": {
        "read_percent",
        "write_percent",
        "update_pattern",
        "latency_p99_us",
        "measured_operations",
    },
    "fallback_scenario": {
        "fallback_scenario",
        "fallback_rate",
        "fallback_read_bytes_total",
        "exact_read_bytes_total",
    },
}


def is_missing(value: str | None) -> bool:
    return value is None or value.strip().lower() in EMPTY


def load_contract(path: Path, figure: str) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    selected = [row for row in rows if row["figure_id"] in {"COMMON", figure}]
    if not selected:
        raise SystemExit(f"No contract rows found for {figure}")
    return selected


def parse_value(value: str, dtype: str) -> object:
    normalized = value.strip()
    if dtype in {"string", "enum"}:
        return normalized
    if dtype in {"int", "uint64"}:
        number = int(normalized)
        if dtype == "uint64" and number < 0:
            raise ValueError("expected non-negative integer")
        return number
    if dtype == "float":
        number = float(normalized)
        if not math.isfinite(number):
            raise ValueError("expected finite float")
        return number
    if dtype == "bool":
        if normalized.lower() not in {"true", "false", "1", "0"}:
            raise ValueError("expected true/false")
        return normalized.lower() in {"true", "1"}
    return normalized


def f5_grain(row: dict[str, str], row_number: int, errors: list[str]) -> str | None:
    """Return the one active F5 grain marker, reporting zero/multiple markers."""

    active = [field for field in F5_GRAIN_MARKERS if not is_missing(row.get(field))]
    if len(active) != 1:
        rendered = ", ".join(active) if active else "none"
        errors.append(
            f"row {row_number}: F5 requires exactly one grain marker among "
            f"{', '.join(F5_GRAIN_MARKERS)}; observed {rendered}"
        )
        return None
    return active[0]


def required_fields_for_row(
    figure: str,
    row: dict[str, str],
    row_number: int,
    contract: list[dict[str, str]],
    errors: list[str],
) -> set[str]:
    """Resolve per-row non-empty fields without weakening header requirements.

    A ``required=yes`` contract entry always requires the *column* to exist.  It
    only requires a value on rows to which that panel/grain applies.  F5 is the
    currently frozen union-of-grains table; its three row kinds therefore need
    an explicit discriminator-aware rule.
    """

    common = {
        spec["field_name"]
        for spec in contract
        if spec["figure_id"] == "COMMON" and spec["required"].lower() == "yes"
    }
    figure_specs = [
        spec
        for spec in contract
        if spec["figure_id"] == figure and spec["required"].lower() == "yes"
    ]
    if figure != "F5":
        return common | {spec["field_name"] for spec in figure_specs}

    always = {spec["field_name"] for spec in figure_specs if spec["panel"] == "all"}
    grain = f5_grain(row, row_number, errors)
    conditional = F5_GRAIN_REQUIRED.get(grain, set())

    # Supported/prototype heatmap cells carry numeric measurements.  Missing or
    # unsupported cells are represented explicitly and must not invent zeros.
    if grain == "query_semantic" and (row.get("support_state") or "").strip() in {
        "supported",
        "prototype",
    }:
        conditional = conditional | {
            "latency_p99_us",
            "body_read_bytes_total",
            "measured_operations",
        }
    return common | always | conditional


def validate_f4_windows(rows: list[dict[str, str]], errors: list[str]) -> None:
    """Validate F4's run/window composite key and non-overlapping windows."""

    by_run: dict[str, list[tuple[int, dict[str, str], float, float]]] = defaultdict(list)
    seen_windows: set[tuple[str, float, float]] = set()
    policies: dict[str, set[str]] = defaultdict(set)
    for row_number, row in enumerate(rows, start=2):
        run_id = (row.get("run_id") or "").strip()
        policy = (row.get("compaction_policy") or "").strip()
        if not run_id or is_missing(row.get("window_start_s")) or is_missing(row.get("window_end_s")):
            continue
        try:
            start = float(row["window_start_s"])
            end = float(row["window_end_s"])
        except (TypeError, ValueError):
            # The generic dtype check reports the precise malformed field.
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        key = (run_id, start, end)
        if key in seen_windows:
            errors.append(
                f"row {row_number}: duplicate F4 window key "
                f"(run_id={run_id}, window_start_s={start:g}, window_end_s={end:g})"
            )
        seen_windows.add(key)
        by_run[run_id].append((row_number, row, start, end))
        if policy:
            policies[run_id].add(policy)

    for run_id, observed in sorted(policies.items()):
        if len(observed) > 1:
            errors.append(
                f"run_id {run_id}: compaction_policy changes across windows: "
                + ", ".join(sorted(observed))
            )

    for run_id, windows in sorted(by_run.items()):
        previous_end: float | None = None
        for row_number, _row, start, end in sorted(
            windows, key=lambda item: (item[2], item[3], item[0])
        ):
            if end <= start:
                errors.append(
                    f"row {row_number}: F4 window_end_s must exceed window_start_s "
                    f"for run_id {run_id}"
                )
                continue
            if previous_end is not None and start < previous_end - 1e-9:
                errors.append(
                    f"row {row_number}: overlapping F4 window for run_id {run_id}: "
                    f"start {start:g} precedes prior end {previous_end:g}"
                )
            previous_end = max(previous_end or end, end)


def repeat_group_key(
    figure: str, row: dict[str, str], base_fields: list[str]
) -> tuple[tuple[str, str], ...] | None:
    """Build the logical cell key whose independent repeats are counted."""

    fields = list(base_fields)
    if figure == "F4":
        fields.extend(
            ("compaction_policy", "window_start_s", "window_end_s", "workload_phase")
        )
    elif figure == "F5":
        active = [field for field in F5_GRAIN_MARKERS if not is_missing(row.get(field))]
        if len(active) != 1:
            return None
        if active[0] == "query_semantic":
            fields.extend(
                ("query_semantic", "edge_selectivity_class", "source_degree_class")
            )
        elif active[0] == "update_pattern":
            fields.extend(("update_pattern", "read_percent", "write_percent"))
        else:
            fields.append("fallback_scenario")
    if any(is_missing(row.get(field)) for field in fields):
        return None
    return tuple((field, (row.get(field) or "").strip()) for field in fields)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--figure", required=True, choices=[f"F{i}" for i in range(1, 7)])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "plan" / "FIGURE-DATA-REQUIREMENTS.tsv",
    )
    parser.add_argument("--min-repeats", type=int, default=3)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    contract = load_contract(args.contract, args.figure)
    required = {row["field_name"]: row for row in contract if row["required"].lower() == "yes"}
    declared = {row["field_name"]: row for row in contract}

    with args.input.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        rows = list(reader)
        columns = set(reader.fieldnames or [])

    errors: list[str] = []
    warnings: list[str] = []
    missing_columns = sorted(set(required) - columns)
    if missing_columns:
        errors.append("missing required columns: " + ", ".join(missing_columns))

    seen_run_ids: set[str] = set()
    group_repeats: dict[tuple[tuple[str, str], ...], set[int]] = defaultdict(set)
    group_fields = [
        name
        for name in (
            "experiment_id",
            "system",
            "variant",
            "dataset_id",
            "workload_id",
            "concurrency",
            "cache_state",
        )
        if name in columns
    ]
    grain_counts: Counter[str] = Counter()

    for index, row in enumerate(rows, start=2):
        row_required = required_fields_for_row(args.figure, row, index, contract, errors)
        if args.figure == "F5":
            active_grains = [
                field for field in F5_GRAIN_MARKERS if not is_missing(row.get(field))
            ]
            if len(active_grains) == 1:
                grain_counts[active_grains[0]] += 1
        for field in row_required:
            if field in columns and is_missing(row.get(field)):
                errors.append(f"row {index}: required field {field} is empty")
        if (
            args.figure == "F5"
            and not is_missing(row.get("query_semantic"))
            and (row.get("support_state") or "").strip() != "supported"
            and is_missing(row.get("unsupported_reason"))
        ):
            errors.append(
                f"row {index}: unsupported_reason is required for "
                f"support_state={(row.get('support_state') or '').strip()!r}"
            )
        for field, value in row.items():
            if field not in declared or is_missing(value):
                continue
            try:
                parse_value(value or "", declared[field]["dtype"])
            except (ValueError, TypeError) as exc:
                errors.append(f"row {index}: {field}={value!r}: {exc}")

        run_id = (row.get("run_id") or "").strip()
        if run_id:
            if args.figure != "F4" and run_id in seen_run_ids:
                errors.append(f"row {index}: duplicate run_id {run_id}")
            seen_run_ids.add(run_id)
        if row.get("git_sha") and not HEX40.fullmatch(row["git_sha"].strip()):
            errors.append(f"row {index}: git_sha is not 40 hex")
        if row.get("binary_sha256") and not HEX64.fullmatch(row["binary_sha256"].strip()):
            errors.append(f"row {index}: binary_sha256 is not 64 hex")
        if row.get("input_sha256") and not HEX64.fullmatch(row["input_sha256"].strip()):
            errors.append(f"row {index}: input_sha256 is not 64 hex")
        if row.get("query_trace_sha256") and not HEX64.fullmatch(row["query_trace_sha256"].strip()):
            errors.append(f"row {index}: query_trace_sha256 is not 64 hex")

        digest_pass = (row.get("digest_pass") or "").strip().lower()
        mismatch = (row.get("mismatch_count") or "").strip()
        if digest_pass in {"true", "1"} and mismatch:
            try:
                if int(mismatch) != 0:
                    errors.append(
                        f"row {index}: digest_pass=true but mismatch_count={mismatch}"
                    )
            except ValueError:
                # The dtype parser above already records the malformed value.
                pass

        group = repeat_group_key(args.figure, row, group_fields)
        if group is not None and not is_missing(row.get("repeat_index")):
            try:
                group_repeats[group].add(int(row["repeat_index"]))
            except ValueError:
                # The dtype parser above already records the malformed value.
                pass

    if args.figure == "F4":
        validate_f4_windows(rows, errors)

    for group, repeats in sorted(group_repeats.items()):
        if len(repeats) < args.min_repeats:
            errors.append(
                f"group {dict(group)} has {len(repeats)} repeats; "
                f"need {args.min_repeats}"
            )

    if not rows:
        errors.append("input has no data rows")
    undeclared = sorted(columns - set(declared))
    if undeclared:
        warnings.append("undeclared extra columns retained: " + ", ".join(undeclared))

    report = {
        "figure": args.figure,
        "input": str(args.input.resolve()),
        "row_count": len(rows),
        "column_count": len(columns),
        "run_ids": len(seen_run_ids),
        "required_fields": len(required),
        "row_grain_counts": dict(sorted(grain_counts.items())),
        "group_count": len(group_repeats),
        "errors": errors,
        "warnings": warnings,
        "status": "PASS" if not errors else "FAIL",
        "failure_counts": dict(Counter(error.split(":", 1)[0] for error in errors)),
    }
    report_path = args.report or args.input.with_suffix(".validation.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(report_path)
    if errors:
        raise SystemExit("; ".join(errors[:8]))


if __name__ == "__main__":
    main()
