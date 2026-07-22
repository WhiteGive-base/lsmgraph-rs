#!/usr/bin/env python3
"""Strict tidy-TSV loading and run-level statistics for Figures 1--3."""

from __future__ import annotations

import csv
import math
import sys
import zlib
from dataclasses import dataclass
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


DEFAULT_MINIMUM_RUNS = 3
BOOTSTRAP_CI_MINIMUM_RUNS = 5


class DataContractError(RuntimeError):
    """Raised when a tidy TSV cannot satisfy the frozen figure contract."""


@dataclass(frozen=True)
class Estimate:
    center: float
    low: float
    high: float
    n: int

    @property
    def interval_kind(self) -> str:
        return "range" if self.n == DEFAULT_MINIMUM_RUNS else "bootstrap_95_ci"

    @property
    def lower_error(self) -> float:
        return max(0.0, self.center - self.low)

    @property
    def upper_error(self) -> float:
        return max(0.0, self.high - self.center)


def _read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise DataContractError(f"TSV does not exist: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames is None:
            raise DataContractError(f"TSV has no header: {path}")
        header = [name.strip() for name in reader.fieldnames]
        if len(header) != len(set(header)):
            raise DataContractError(f"TSV has duplicate column names: {path}")
        rows = []
        for line_no, raw in enumerate(reader, start=2):
            row = {key.strip(): (value or "").strip() for key, value in raw.items()}
            row["__line__"] = str(line_no)
            rows.append(row)
    if not rows:
        raise DataContractError(f"TSV has a header but no data rows: {path}")
    return header, rows


def contract_fields(figure_id: str) -> set[str]:
    header, rows = _read_tsv(CONTRACT_PATH)
    expected_header = {
        "figure_id",
        "field_name",
        "required",
    }
    missing = expected_header - set(header)
    if missing:
        raise DataContractError(
            f"Frozen contract is malformed; missing columns {sorted(missing)}: {CONTRACT_PATH}"
        )
    fields = {
        row["field_name"]
        for row in rows
        if row["figure_id"] in {"COMMON", figure_id}
        and row["required"].lower() == "yes"
    }
    if not fields:
        raise DataContractError(f"Frozen contract has no required fields for {figure_id}")
    return fields


def parse_bool(value: str, *, field: str, line: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "pass", "passed"}:
        return True
    if normalized in {"0", "false", "no", "fail", "failed"}:
        return False
    raise DataContractError(
        f"line {line}: {field} must be a boolean/pass token, got {value!r}"
    )


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
) -> int:
    value = number(row, field, positive=positive, nonnegative=nonnegative)
    assert value is not None
    if not value.is_integer():
        raise DataContractError(
            f"line {row.get('__line__', '?')}: {field} must be an integer, got {value}"
        )
    return int(value)


def load_rows(
    input_path: Path,
    *,
    figure_id: str,
    experiment_id: str,
    plot_fields: Iterable[str],
) -> list[dict[str, str]]:
    header, rows = _read_tsv(input_path)
    required = contract_fields(figure_id) | set(plot_fields)
    missing = sorted(required - set(header))
    if missing:
        raise DataContractError(
            f"{figure_id}: input is missing required contract columns: {', '.join(missing)}"
        )

    selected = [row for row in rows if row["experiment_id"] == experiment_id]
    if not selected:
        seen = sorted({row.get("experiment_id", "") for row in rows})
        raise DataContractError(
            f"{figure_id}: no rows for experiment_id={experiment_id!r}; present={seen}"
        )

    passing: list[dict[str, str]] = []
    rejected = 0
    for row in selected:
        digest_ok = parse_bool(
            row["digest_pass"], field="digest_pass", line=row["__line__"]
        )
        mismatches = integer(row, "mismatch_count", nonnegative=True)
        if digest_ok and mismatches == 0:
            passing.append(row)
        else:
            rejected += 1

    if not passing:
        raise DataContractError(
            f"{figure_id}: experiment {experiment_id!r} has no digest-pass, "
            "mismatch-free rows; refusing to draw a performance figure"
        )
    if rejected:
        print(
            f"WARNING: {figure_id} excluded {rejected} digest-failed/mismatching rows; "
            "they must remain in the correctness report.",
            file=sys.stderr,
        )
    return passing


def require_constant(rows: Sequence[Mapping[str, str]], fields: Iterable[str]) -> None:
    for field in fields:
        values = {row[field] for row in rows}
        if len(values) != 1:
            raise DataContractError(
                f"matched comparison requires one {field}; observed {sorted(values)}"
            )


def group_by(
    rows: Iterable[dict[str, str]], key: Callable[[dict[str, str]], str]
) -> dict[str, list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return groups


def require_groups(
    groups: Mapping[str, Sequence[Mapping[str, str]]],
    expected: Sequence[str],
    *,
    label: str,
) -> None:
    missing = [name for name in expected if name not in groups]
    if missing:
        raise DataContractError(f"missing required {label}: {', '.join(missing)}")


def require_min_runs(
    rows: Sequence[Mapping[str, str]], minimum: int, *, label: str
) -> None:
    run_ids = [row["run_id"] for row in rows]
    if len(run_ids) != len(set(run_ids)):
        raise DataContractError(f"{label}: duplicate run_id within one plotted group")
    _require_repeat_count(len(run_ids), minimum, label=label)


def _require_repeat_count(count: int, minimum: int, *, label: str) -> None:
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or minimum < DEFAULT_MINIMUM_RUNS
    ):
        raise DataContractError(
            f"{label}: minimum_runs must be an integer >= {DEFAULT_MINIMUM_RUNS}, "
            f"got {minimum!r}"
        )
    if count < minimum:
        raise DataContractError(
            f"{label}: requires at least {minimum} independent runs, found {count}"
        )
    if count == 4:
        raise DataContractError(
            f"{label}: found 4 independent runs, which is an incomplete adaptive repeat set; "
            "formal points must contain exactly 3 runs (range) or at least 5 runs "
            "(bootstrap 95% CI)"
        )


def _stable_seed(label: str) -> int:
    return zlib.crc32(label.encode("utf-8")) & 0xFFFFFFFF


def estimate(
    rows: Sequence[Mapping[str, str]],
    getter: Callable[[Mapping[str, str]], float | None],
    *,
    label: str,
    minimum_runs: int = DEFAULT_MINIMUM_RUNS,
    allow_all_missing: bool = False,
    bootstrap_samples: int = 4000,
) -> Estimate | None:
    require_min_runs(rows, minimum_runs, label=label)
    values: list[float] = []
    for row in rows:
        value = getter(row)
        if value is None:
            continue
        if not math.isfinite(value):
            raise DataContractError(f"{label}: non-finite derived value")
        values.append(float(value))
    if not values:
        if allow_all_missing:
            return None
        raise DataContractError(f"{label}: no numeric values after validation")
    if len(values) != len(rows):
        raise DataContractError(
            f"{label}: partial missingness is ambiguous; provide all values or mark the whole group N/A"
        )

    array = np.asarray(values, dtype=float)
    center = float(np.median(array))
    if len(array) == DEFAULT_MINIMUM_RUNS:
        return Estimate(
            center=center,
            low=float(np.min(array)),
            high=float(np.max(array)),
            n=len(array),
        )
    if len(array) < BOOTSTRAP_CI_MINIMUM_RUNS:
        raise AssertionError("repeat-count gate must reject n=4")
    rng = np.random.default_rng(_stable_seed(label))
    indices = rng.integers(0, len(array), size=(bootstrap_samples, len(array)))
    medians = np.median(array[indices], axis=1)
    low, high = np.quantile(medians, [0.025, 0.975])
    return Estimate(
        center=center,
        low=float(low),
        high=float(high),
        n=len(array),
    )


def estimate_field(
    rows: Sequence[Mapping[str, str]],
    field: str,
    *,
    label: str,
    minimum_runs: int = DEFAULT_MINIMUM_RUNS,
    positive: bool = False,
    nonnegative: bool = False,
    allow_all_missing: bool = False,
) -> Estimate | None:
    return estimate(
        rows,
        lambda row: number(
            row,
            field,
            positive=positive,
            nonnegative=nonnegative,
            allow_blank=allow_all_missing,
        ),
        label=label,
        minimum_runs=minimum_runs,
        allow_all_missing=allow_all_missing,
    )


def asymmetric_error(estimate_value: Estimate) -> list[list[float]]:
    return [[estimate_value.lower_error], [estimate_value.upper_error]]


def normalize_variant(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    aliases = {
        "b64": "budg-b64",
        "budget-b64": "budg-b64",
        "full": "semantic",
        "full-semantic": "semantic",
        "schema/no-degree": "schema",
        "schema-no-degree": "schema",
    }
    return aliases.get(normalized, normalized)


def format_us(value: float, _: int | None = None) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:g} s"
    if value >= 1_000:
        return f"{value / 1_000:g} ms"
    return f"{value:g} us"


def report_saved(report: Mapping[str, object]) -> None:
    status = report.get("status", "UNKNOWN")
    figure = report.get("figure", "figure")
    print(f"saved {figure}: layout={status}")
    if status != "PASS":
        print(
            "WARNING: inspect the generated .layout.json and rendered PDF before publication.",
            file=sys.stderr,
        )


__all__ = [
    "BOOTSTRAP_CI_MINIMUM_RUNS",
    "DEFAULT_MINIMUM_RUNS",
    "DataContractError",
    "Estimate",
    "HERE",
    "PALETTE",
    "SERIES_STYLE",
    "asymmetric_error",
    "configure_matplotlib",
    "estimate",
    "estimate_field",
    "format_us",
    "group_by",
    "load_rows",
    "normalize_variant",
    "number",
    "panel_label",
    "report_saved",
    "require_constant",
    "require_groups",
    "require_min_runs",
    "save_figure",
]
