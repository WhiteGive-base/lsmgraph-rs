#!/usr/bin/env python3
"""Strict frozen-tidy loading and run-level statistics for Figures 4--6."""

from __future__ import annotations

import csv
import math
import re
import sys
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
COMMON_DIR = PROJECT_ROOT / "figures" / "scripts"
CONTRACT_PATH = HERE.parent / "figure-design" / "FIGURE-DATA-REQUIREMENTS.tsv"

if str(COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(COMMON_DIR))

from figure_common import (  # noqa: E402
    PALETTE,
    SERIES_STYLE,
    configure_matplotlib,
    panel_label,
    save_figure,
)


HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
CACHE_STATES = {"cold", "warm", "fixed_budget"}


class DataContractError(RuntimeError):
    """Raised when a tidy TSV cannot satisfy the frozen figure contract."""


@dataclass(frozen=True)
class Estimate:
    center: float
    low: float
    high: float
    n: int

    @property
    def lower_error(self) -> float:
        return max(0.0, self.center - self.low)

    @property
    def upper_error(self) -> float:
        return max(0.0, self.high - self.center)


def warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def _read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise DataContractError(f"TSV does not exist: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames is None:
            raise DataContractError(f"TSV has no header: {path}")
        header = [name.strip() for name in reader.fieldnames]
        if not all(header):
            raise DataContractError(f"TSV contains an empty column name: {path}")
        if len(header) != len(set(header)):
            raise DataContractError(f"TSV has duplicate column names: {path}")
        rows: list[dict[str, str]] = []
        for line_no, raw in enumerate(reader, start=2):
            if None in raw:
                raise DataContractError(
                    f"line {line_no}: too many tab-separated values for the header"
                )
            row = {key.strip(): (value or "").strip() for key, value in raw.items()}
            row["__line__"] = str(line_no)
            rows.append(row)
    if not rows:
        raise DataContractError(f"TSV has a header but no data rows: {path}")
    return header, rows


def contract_fields(figure_id: str) -> set[str]:
    header, rows = _read_tsv(CONTRACT_PATH)
    required_contract_columns = {"figure_id", "field_name", "required"}
    missing = required_contract_columns - set(header)
    if missing:
        raise DataContractError(
            f"frozen contract is malformed; missing {sorted(missing)}: {CONTRACT_PATH}"
        )
    fields = {
        row["field_name"]
        for row in rows
        if row["figure_id"] in {"COMMON", figure_id}
        and row["required"].lower() == "yes"
    }
    if not fields:
        raise DataContractError(f"frozen contract has no required fields for {figure_id}")
    return fields


def resolve_input(data_dir: Path, explicit: Path | None, figure_id: str) -> Path:
    if explicit is not None:
        return explicit if explicit.is_absolute() else data_dir / explicit
    return data_dir / f"{figure_id}.tsv"


def text(
    row: Mapping[str, str],
    field: str,
    *,
    allow_blank: bool = False,
) -> str:
    value = row.get(field, "").strip()
    if not value and not allow_blank:
        raise DataContractError(
            f"line {row.get('__line__', '?')}: required field {field} is blank"
        )
    return value


def number(
    row: Mapping[str, str],
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
    allow_blank: bool = False,
) -> float | None:
    raw = row.get(field, "").strip()
    line = row.get("__line__", "?")
    if not raw:
        if allow_blank:
            return None
        raise DataContractError(f"line {line}: required numeric field {field} is blank")
    try:
        value = float(raw)
    except ValueError as exc:
        raise DataContractError(
            f"line {line}: {field} must be numeric, got {raw!r}"
        ) from exc
    if not math.isfinite(value):
        raise DataContractError(f"line {line}: {field} must be finite, got {raw!r}")
    if positive and value <= 0:
        raise DataContractError(f"line {line}: {field} must be > 0, got {value}")
    if nonnegative and value < 0:
        raise DataContractError(f"line {line}: {field} must be >= 0, got {value}")
    return value


def integer(
    row: Mapping[str, str],
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
    allow_blank: bool = False,
) -> int | None:
    value = number(
        row,
        field,
        positive=positive,
        nonnegative=nonnegative,
        allow_blank=allow_blank,
    )
    if value is None:
        return None
    if not value.is_integer():
        raise DataContractError(
            f"line {row.get('__line__', '?')}: {field} must be an integer, got {value}"
        )
    return int(value)


def parse_bool(value: str, *, field: str, line: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "pass", "passed"}:
        return True
    if normalized in {"0", "false", "no", "fail", "failed"}:
        return False
    raise DataContractError(
        f"line {line}: {field} must be a boolean/pass token, got {value!r}"
    )


def bool_field(row: Mapping[str, str], field: str) -> bool:
    return parse_bool(
        text(row, field), field=field, line=row.get("__line__", "?")
    )


def _validate_utc(value: str, *, line: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataContractError(
            f"line {line}: timestamp_utc is not ISO-8601: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise DataContractError(
            f"line {line}: timestamp_utc must carry UTC offset/Z, got {value!r}"
        )


def validate_common_row(row: Mapping[str, str]) -> None:
    line = row.get("__line__", "?")
    for field in (
        "experiment_id",
        "run_id",
        "host_fingerprint",
        "system",
        "variant",
        "dataset_id",
        "workload_id",
    ):
        text(row, field)
    integer(row, "repeat_index", positive=True)
    _validate_utc(text(row, "timestamp_utc"), line=line)
    if not HEX40.fullmatch(text(row, "git_sha")):
        raise DataContractError(f"line {line}: git_sha must be exactly 40 hex characters")
    if not HEX64.fullmatch(text(row, "binary_sha256")):
        raise DataContractError(
            f"line {line}: binary_sha256 must be exactly 64 hex characters"
        )
    if not HEX64.fullmatch(text(row, "input_sha256")):
        raise DataContractError(
            f"line {line}: input_sha256 must be exactly 64 hex characters"
        )
    if not HEX64.fullmatch(text(row, "query_trace_sha256")):
        raise DataContractError(
            f"line {line}: query_trace_sha256 must be exactly 64 hex characters"
        )
    integer(row, "vertex_count", positive=True)
    integer(row, "directed_edge_count", positive=True)
    integer(row, "property_count", nonnegative=True, allow_blank=True)
    integer(row, "seed", nonnegative=True)
    cache_state = text(row, "cache_state")
    if cache_state not in CACHE_STATES:
        raise DataContractError(
            f"line {line}: cache_state must be one of {sorted(CACHE_STATES)}, got {cache_state!r}"
        )
    integer(row, "concurrency", positive=True)
    number(row, "warmup_s", nonnegative=True)
    number(row, "measurement_s", positive=True)
    scale = number(row, "scale_factor", positive=True, allow_blank=True)
    if scale is not None and scale <= 0:
        raise AssertionError("positive=True already enforced")
    bool_field(row, "digest_pass")
    integer(row, "mismatch_count", nonnegative=True)


def _validate_run_metadata(rows: Sequence[Mapping[str, str]]) -> None:
    stable_fields = (
        "experiment_id",
        "repeat_index",
        "timestamp_utc",
        "host_fingerprint",
        "git_sha",
        "binary_sha256",
        "system",
        "variant",
        "dataset_id",
        "scale_factor",
        "vertex_count",
        "directed_edge_count",
        "property_count",
        "input_sha256",
        "workload_id",
        "query_trace_sha256",
        "seed",
        "cache_state",
        "concurrency",
        "warmup_s",
    )
    by_run = group_by(rows, lambda row: row["run_id"])
    for run_id, run_rows in by_run.items():
        for field in stable_fields:
            values = {row.get(field, "") for row in run_rows}
            if len(values) != 1:
                raise DataContractError(
                    f"run_id={run_id!r} changes {field} across tidy rows: {sorted(values)}"
                )


def digest_passes(row: Mapping[str, str]) -> bool:
    return bool_field(row, "digest_pass") and integer(
        row, "mismatch_count", nonnegative=True
    ) == 0


def load_figure_rows(
    input_path: Path,
    *,
    figure_id: str,
    experiment_ids: Iterable[str],
    extra_fields: Iterable[str] = (),
) -> list[dict[str, str]]:
    header, rows = _read_tsv(input_path)
    required = contract_fields(figure_id) | set(extra_fields)
    missing = sorted(required - set(header))
    if missing:
        raise DataContractError(
            f"{figure_id}: input is missing required frozen-contract columns: "
            + ", ".join(missing)
        )
    wanted = set(experiment_ids)
    selected = [row for row in rows if row["experiment_id"] in wanted]
    if not selected:
        present = sorted({row.get("experiment_id", "") for row in rows})
        raise DataContractError(
            f"{figure_id}: no rows for experiment_id in {sorted(wanted)}; present={present}"
        )
    for row in selected:
        validate_common_row(row)
    _validate_run_metadata(selected)
    if not any(digest_passes(row) for row in selected):
        raise DataContractError(
            f"{figure_id}: selected rows contain no digest-pass, mismatch-free run; "
            "refusing to draw a performance figure"
        )
    return selected


def passing_rows(
    rows: Sequence[dict[str, str]],
    *,
    label: str,
    warn_rejected: bool = True,
) -> list[dict[str, str]]:
    passing = [row for row in rows if digest_passes(row)]
    rejected = len(rows) - len(passing)
    if rejected and warn_rejected:
        warn(
            f"{label}: excluded {rejected} digest-failed/mismatching rows from performance aggregation; "
            "retain them in the correctness report"
        )
    if not passing:
        raise DataContractError(f"{label}: no digest-pass, mismatch-free rows")
    return passing


def group_by(
    rows: Iterable[dict[str, str]],
    key: Callable[[dict[str, str]], object],
) -> dict[object, list[dict[str, str]]]:
    groups: dict[object, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return groups


def require_groups(
    groups: Mapping[object, Sequence[Mapping[str, str]]],
    expected: Sequence[object],
    *,
    label: str,
) -> None:
    missing = [value for value in expected if value not in groups]
    if missing:
        raise DataContractError(f"missing required {label}: {missing}")


def require_constant(
    rows: Sequence[Mapping[str, str]], fields: Iterable[str], *, label: str
) -> None:
    for field in fields:
        values = {row.get(field, "") for row in rows}
        if len(values) != 1:
            raise DataContractError(
                f"{label}: requires one {field}; observed {sorted(values)}"
            )


def require_min_runs(
    rows: Sequence[Mapping[str, str]], minimum: int, *, label: str
) -> None:
    run_ids = {row["run_id"] for row in rows}
    if len(run_ids) < minimum:
        raise DataContractError(
            f"{label}: requires at least {minimum} independent run_id values, found {len(run_ids)}"
        )


def _stable_seed(label: str) -> int:
    return zlib.crc32(label.encode("utf-8")) & 0xFFFFFFFF


def estimate_values(
    values_by_run: Sequence[tuple[str, float]],
    *,
    label: str,
    minimum_runs: int,
    bootstrap_samples: int = 4000,
) -> Estimate:
    run_ids = [run_id for run_id, _ in values_by_run]
    if len(run_ids) != len(set(run_ids)):
        raise DataContractError(f"{label}: duplicate run_id in one run-level estimate")
    if len(run_ids) < minimum_runs:
        raise DataContractError(
            f"{label}: requires {minimum_runs} independent runs, found {len(run_ids)}"
        )
    values = np.asarray([value for _, value in values_by_run], dtype=float)
    if not np.all(np.isfinite(values)):
        raise DataContractError(f"{label}: non-finite run-level value")
    center = float(np.median(values))
    if len(values) == 1:
        return Estimate(center, center, center, 1)
    rng = np.random.default_rng(_stable_seed(label))
    indices = rng.integers(0, len(values), size=(bootstrap_samples, len(values)))
    medians = np.median(values[indices], axis=1)
    low, high = np.quantile(medians, [0.025, 0.975])
    return Estimate(center, float(low), float(high), len(values))


def estimate_field(
    rows: Sequence[Mapping[str, str]],
    field: str,
    *,
    label: str,
    minimum_runs: int,
    positive: bool = False,
    nonnegative: bool = False,
) -> Estimate:
    values: list[tuple[str, float]] = []
    for row in rows:
        value = number(row, field, positive=positive, nonnegative=nonnegative)
        assert value is not None
        values.append((row["run_id"], value))
    return estimate_values(values, label=label, minimum_runs=minimum_runs)


def paired_ratio_estimate(
    numerator_rows: Sequence[Mapping[str, str]],
    denominator_rows: Sequence[Mapping[str, str]],
    *,
    numerator_getter: Callable[[Mapping[str, str]], float],
    denominator_getter: Callable[[Mapping[str, str]], float],
    label: str,
    minimum_runs: int,
) -> Estimate:
    def pair_key(row: Mapping[str, str]) -> tuple[str, str]:
        return (row["repeat_index"], row["query_trace_sha256"])

    numerator = {pair_key(row): row for row in numerator_rows}
    denominator = {pair_key(row): row for row in denominator_rows}
    if len(numerator) != len(numerator_rows) or len(denominator) != len(denominator_rows):
        raise DataContractError(f"{label}: duplicate repeat/query-trace pair")
    if set(numerator) != set(denominator):
        missing_left = sorted(set(denominator) - set(numerator))
        missing_right = sorted(set(numerator) - set(denominator))
        raise DataContractError(
            f"{label}: variants are not paired; missing numerator={missing_left}, "
            f"missing denominator={missing_right}"
        )
    if len(numerator) < minimum_runs:
        raise DataContractError(
            f"{label}: requires {minimum_runs} matched independent repeats, found {len(numerator)}"
        )
    matched_fields = (
        "dataset_id",
        "input_sha256",
        "workload_id",
        "query_trace_sha256",
        "seed",
        "cache_state",
        "concurrency",
        "directed_edge_count",
    )
    values: list[tuple[str, float]] = []
    for key in sorted(numerator):
        left = numerator[key]
        right = denominator[key]
        for field in matched_fields:
            if left[field] != right[field]:
                raise DataContractError(
                    f"{label}: pair {key} differs in {field}: {left[field]!r} vs {right[field]!r}"
                )
        top = float(numerator_getter(left))
        bottom = float(denominator_getter(right))
        if not math.isfinite(top) or not math.isfinite(bottom) or top <= 0 or bottom <= 0:
            raise DataContractError(
                f"{label}: ratio inputs must be finite and >0, got {top}/{bottom}"
            )
        values.append((f"{left['run_id']}::{right['run_id']}", top / bottom))
    return estimate_values(values, label=label, minimum_runs=minimum_runs)


def normalize_variant(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    aliases = {
        "b64": "budg-b64",
        "budget-b64": "budg-b64",
        "semantic-budgeted-b64": "budg-b64",
        "full": "semantic",
        "full-semantic": "semantic",
    }
    return aliases.get(normalized, normalized)


def format_edges(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2g}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2g}M"
    if value >= 1_000:
        return f"{value / 1_000:.2g}K"
    return f"{value:g}"


def ratio_tick(value: float, _: int | None = None) -> str:
    ratio = 2.0**value
    if ratio >= 10:
        return f"{ratio:.0f}x"
    if ratio >= 1:
        return f"{ratio:.1f}x" if not ratio.is_integer() else f"{ratio:.0f}x"
    return f"{ratio:.2g}x"


def report_saved(report: Mapping[str, object]) -> None:
    status = report.get("status", "UNKNOWN")
    figure = report.get("figure", "figure")
    print(f"saved {figure}: layout={status}")
    if status != "PASS":
        warn("inspect the generated .layout.json and rendered PDF before publication")


__all__ = [
    "DataContractError",
    "Estimate",
    "HERE",
    "PALETTE",
    "SERIES_STYLE",
    "bool_field",
    "configure_matplotlib",
    "digest_passes",
    "estimate_field",
    "estimate_values",
    "format_edges",
    "group_by",
    "integer",
    "load_figure_rows",
    "normalize_variant",
    "number",
    "paired_ratio_estimate",
    "panel_label",
    "passing_rows",
    "ratio_tick",
    "report_saved",
    "require_constant",
    "require_groups",
    "require_min_runs",
    "resolve_input",
    "save_figure",
    "text",
    "warn",
]
