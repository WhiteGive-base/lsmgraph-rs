#!/usr/bin/env python3
"""Validate and finalize a CIDR resource-instrumented run.

Only this validator creates ``DONE``.  A non-zero command, collector failure,
missing telemetry, malformed schema, or incomplete formal provenance creates
``FAILED`` and can never be promoted by the wrapper.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from resource_schema import (
    ARTIFACT_FILES,
    CONTAINER_IDENTITY_SCHEMA_VERSION,
    DISK_CATEGORIES,
    DISK_COLUMNS,
    IOSTAT_COLUMNS,
    MANIFEST_SCHEMA_VERSION,
    RESOURCE_COLUMNS,
    RESOURCE_SCHEMA_VERSION,
    TERMINAL_CENSOR_POLICY,
    TERMINAL_CENSOR_POLICY_VERSION,
)

_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
PROCESS_IDENTITY_SCHEMA_VERSION = "cidr-process-identity-v1"
# sysstat 12.2 derives %util from a centisecond interval and kernel io_ticks.
# The latter is millisecond/jiffy-accounted, so a one-second sample can cross
# the nominal boundary by up to about one percentage point.  Keep raw values,
# warn inside that narrow observation tolerance, and fail closed above it.
IOSTAT_UTIL_NOMINAL_MAX_PCT = 100.0
IOSTAT_UTIL_ROUNDING_TOLERANCE_PCT = 1.0
TERMINAL_CENSOR_RECEIPT_SCHEMA_VERSION = "cidr-terminal-censor-receipt-v1"
TERMINAL_CENSOR_WARNING = (
    "one penultimate root-only process sample was terminal-censored after exact "
    "exit-boundary proof; process CPU and process I/O totals are lower bounds"
)
TERMINAL_CENSOR_UNREADABLE_FIELDS = (
    "process_rss_unreadable",
    "process_pss_unreadable",
    "process_io_unreadable",
)
TERMINAL_CENSOR_INTEGER_CUMULATIVE_FIELDS = (
    "process_read_bytes",
    "process_write_bytes",
    "process_cancelled_write_bytes",
    "process_rchar",
    "process_wchar",
)
TERMINAL_CENSOR_CPU_CUMULATIVE_FIELDS = (
    "process_user_cpu_s",
    "process_sys_cpu_s",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def atomic_json(path: Path, value: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("top-level JSON is not an object")
        return value
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read {path.name}: {exc}")
        return {}


def read_tsv(path: Path, expected: list[str], errors: list[str]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames != expected:
                errors.append(f"{path.name}: schema mismatch: {reader.fieldnames!r}")
                return []
            rows = list(reader)
    except (OSError, csv.Error, UnicodeError) as exc:
        errors.append(f"cannot read {path.name}: {exc}")
        return []
    for index, row in enumerate(rows):
        if row.get("schema_version") != RESOURCE_SCHEMA_VERSION:
            errors.append(f"{path.name}: row {index} has wrong schema_version")
            break
    return rows


def number(row: dict[str, str], key: str, errors: list[str], context: str, allow_empty: bool = False) -> float:
    raw = row.get(key, "")
    if raw == "" and allow_empty:
        return math.nan
    try:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("not finite")
        return value
    except (TypeError, ValueError):
        errors.append(f"{context}: {key} is not numeric: {raw!r}")
        return math.nan


def integer(row: dict[str, str], key: str, errors: list[str], context: str) -> int | None:
    raw = row.get(key, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        errors.append(f"{context}: {key} is not an integer: {raw!r}")
        return None


def max_number(rows: list[dict[str, str]], key: str) -> float:
    values: list[float] = []
    for row in rows:
        try:
            value = float(row[key])
            if math.isfinite(value):
                values.append(value)
        except (KeyError, TypeError, ValueError):
            pass
    return max(values, default=0.0)


def min_number(rows: list[dict[str, str]], key: str) -> float:
    values: list[float] = []
    for row in rows:
        try:
            value = float(row[key])
            if math.isfinite(value):
                values.append(value)
        except (KeyError, TypeError, ValueError):
            pass
    return min(values, default=0.0)


def time_weighted_mean(rows: list[dict[str, str]], key: str) -> float:
    weighted = 0.0
    elapsed_total = 0.0
    for previous, current in zip(rows, rows[1:]):
        try:
            elapsed = float(current["monotonic_s"]) - float(previous["monotonic_s"])
            value = float(current[key])
        except (KeyError, TypeError, ValueError):
            continue
        if elapsed > 0 and math.isfinite(value):
            weighted += value * elapsed
            elapsed_total += elapsed
    return weighted / elapsed_total if elapsed_total else 0.0


def _terminal_censor_policy_enabled(manifest: dict[str, Any]) -> bool:
    return manifest.get("terminal_censor_policy") == TERMINAL_CENSOR_POLICY


def _terminal_censor_pid_set(row: dict[str, str]) -> set[int]:
    raw = row.get("pids")
    if raw is None:
        raise ValueError("missing pids")
    if raw == "":
        return set()
    parts = raw.split(",")
    if any(not value.isdigit() for value in parts):
        raise ValueError("malformed pids")
    result = {int(value) for value in parts}
    if len(result) != len(parts):
        raise ValueError("duplicate pids")
    return result


def _terminal_censor_int(row: dict[str, str], key: str) -> int:
    raw = row.get(key)
    if raw is None:
        raise ValueError(f"missing {key}")
    value = int(raw)
    if str(value) != raw and not (raw.startswith("+") and str(value) == raw[1:]):
        raise ValueError(f"non-canonical integer {key}")
    return value


def _terminal_censor_float(row: dict[str, str], key: str) -> float:
    raw = row.get(key)
    if raw is None:
        raise ValueError(f"missing {key}")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"non-finite {key}")
    return value


def _terminal_censor_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("missing UTC timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("UTC timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def classify_terminal_censor_policy(
    manifest: dict[str, Any],
    execution: dict[str, Any],
    collector: dict[str, Any],
    ready: dict[str, Any],
    guard_status: dict[str, Any],
    rows: list[dict[str, str]],
) -> tuple[dict[str, Any] | None, str]:
    """Recognize one exact, explicitly enabled root exit-boundary censor.

    The classifier is intentionally independent from the generic row validator:
    callers may suppress only the three unreadable-counter errors when this
    function returns a receipt.  Every other validation error remains fatal.
    """

    def reject(reason: str) -> tuple[None, str]:
        return None, reason

    if not _terminal_censor_policy_enabled(manifest):
        return reject("terminal censor policy is not explicitly enabled")
    if manifest.get("performance_eligible_declared") is not True:
        return reject("terminal censor policy requires a performance-eligible run")

    collector_config = manifest.get("collector")
    if not isinstance(collector_config, dict):
        return reject("manifest collector configuration is malformed")
    if collector_config.get("containers") != [] or collector_config.get("extra_pids") != []:
        return reject("terminal censor policy forbids containers and extra PIDs")
    if collector.get("containers") != [] or collector.get("extra_pids") != []:
        return reject("collector tracked containers or extra PIDs")
    try:
        interval_s = float(collector_config.get("interval_s"))
    except (TypeError, ValueError):
        return reject("collector interval is malformed")
    if not math.isfinite(interval_s) or interval_s <= 0:
        return reject("collector interval is not positive and finite")
    if collector.get("interval_s") != collector_config.get("interval_s"):
        return reject("collector interval differs from the manifest")

    if (
        execution.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or execution.get("command_exit_code") != 0
        or execution.get("collector_exit_code") != 0
        or execution.get("guard_exit_code") != 0
        or execution.get("wrapper_signal") is not None
    ):
        return reject("execution is not a clean command/collector/guard success")
    if (
        collector.get("schema_version") != RESOURCE_SCHEMA_VERSION
        or collector.get("state") != "DONE"
        or collector.get("errors") != []
        or collector.get("root_seen_alive") is not True
        or collector.get("resource_samples") != len(rows)
    ):
        return reject("collector status is not a clean, row-complete DONE")
    if (
        guard_status.get("schema_version") != "cidr-p31-integrity-guard-v2"
        or guard_status.get("state") != "PASS"
        or guard_status.get("errors") != []
    ):
        return reject("integrity guard status is not a clean PASS")
    if len(rows) < 3:
        return reject("resource sample stream is too short")

    root_pid = execution.get("root_pid")
    if type(root_pid) is not int or root_pid <= 0:
        return reject("execution root PID is invalid")
    if manifest.get("root_pid") != root_pid or collector.get("root_pid") != root_pid:
        return reject("root PID differs across execution, manifest, and collector")

    try:
        indexes = [_terminal_censor_int(row, "sample_index") for row in rows]
        monotonic_values = [
            _terminal_censor_float(row, "monotonic_s") for row in rows
        ]
        utc_values = [_terminal_censor_utc(row.get("timestamp_utc")) for row in rows]
        unreadable_by_row = [
            tuple(
                _terminal_censor_int(row, field)
                for field in TERMINAL_CENSOR_UNREADABLE_FIELDS
            )
            for row in rows
        ]
    except (TypeError, ValueError, OverflowError) as exc:
        return reject(f"resource sample identity/timing fields are malformed: {exc}")
    if indexes != list(range(len(rows))):
        return reject("sample indexes are not contiguous from zero")
    if any(right <= left for left, right in zip(monotonic_values, monotonic_values[1:])):
        return reject("sample monotonic timestamps are not strictly increasing")
    if any(right <= left for left, right in zip(utc_values, utc_values[1:])):
        return reject("sample UTC timestamps are not strictly increasing")

    bad_positions = [
        index
        for index, values in enumerate(unreadable_by_row)
        if any(value != 0 for value in values)
    ]
    if bad_positions != [len(rows) - 2]:
        return reject("unreadable counters are not unique to the penultimate row")
    bad_position = bad_positions[0]
    if unreadable_by_row[bad_position] != (1, 1, 1):
        return reject("penultimate unreadable counters are not the exact RSS/PSS/I/O triple")
    if any(values != (0, 0, 0) for index, values in enumerate(unreadable_by_row) if index != bad_position):
        return reject("a non-penultimate row contains an unreadable counter")

    previous = rows[bad_position - 1]
    bad = rows[bad_position]
    final = rows[bad_position + 1]
    expected_root = {root_pid}
    try:
        if (
            _terminal_censor_pid_set(previous) != expected_root
            or _terminal_censor_int(previous, "process_count") != 1
            or _terminal_censor_int(previous, "root_alive") != 1
        ):
            return reject("row before the censor is not a readable root-only sample")
        if (
            _terminal_censor_pid_set(bad) != expected_root
            or _terminal_censor_int(bad, "process_count") != 1
            or _terminal_censor_int(bad, "root_alive") != 1
        ):
            return reject("censored row is not a live root-only sample")
        if (
            _terminal_censor_pid_set(final)
            or _terminal_censor_int(final, "process_count") != 0
            or _terminal_censor_int(final, "root_alive") != 0
        ):
            return reject("final row is not an empty process-tree sample")
        if any(row.get("root_pid") != str(root_pid) for row in rows):
            return reject("resource row root PID differs from execution")
    except (TypeError, ValueError, OverflowError) as exc:
        return reject(f"terminal root-only rows are malformed: {exc}")

    identities = collector.get("process_identity_unique_set")
    exact_identity_keys = {
        "pid",
        "ppid",
        "pgrp",
        "start_ticks",
        "first_sample_index",
        "last_sample_index",
        "sample_count",
    }
    if (
        collector.get("process_identity_schema_version")
        != PROCESS_IDENTITY_SCHEMA_VERSION
        or not isinstance(identities, list)
        or not 1 <= len(identities) <= 2
        or any(not isinstance(value, dict) or set(value) != exact_identity_keys for value in identities)
    ):
        return reject("process identity set is malformed or has unexpected members")
    root_identities = [value for value in identities if value.get("pid") == root_pid]
    if len(root_identities) != 1:
        return reject("process identity set lacks one exact root")
    root_identity = root_identities[0]
    if (
        any(type(root_identity[key]) is not int for key in exact_identity_keys)
        or root_identity["pgrp"] != root_pid
        or root_identity["start_ticks"] <= 0
        or root_identity["first_sample_index"] != 0
        or root_identity["last_sample_index"] != bad_position
        or root_identity["sample_count"] != len(rows) - 1
    ):
        return reject("root identity does not exactly cover samples zero through the censor")

    child_identities = [value for value in identities if value is not root_identity]
    child_pid: int | None = None
    if child_identities:
        child = child_identities[0]
        child_pid = child.get("pid")
        if (
            any(type(child[key]) is not int for key in exact_identity_keys)
            or type(child_pid) is not int
            or child_pid <= 0
            or child_pid == root_pid
            or child["ppid"] != root_pid
            or child["pgrp"] != root_pid
            or child["start_ticks"] <= root_identity["start_ticks"]
            or child["first_sample_index"] != 0
            or child["last_sample_index"] != 0
            or child["sample_count"] != 1
        ):
            return reject("optional helper is not one direct sample-zero child")

    expected_first_pids = {root_pid}
    if child_pid is not None:
        expected_first_pids.add(child_pid)
    try:
        for position, row in enumerate(rows):
            expected_pids = (
                expected_first_pids
                if position == 0
                else expected_root if position <= bad_position else set()
            )
            if (
                _terminal_censor_pid_set(row) != expected_pids
                or _terminal_censor_int(row, "process_count") != len(expected_pids)
                or _terminal_censor_int(row, "root_alive")
                != (0 if position == len(rows) - 1 else 1)
            ):
                return reject("resource PID sets are not root-only after the optional sample-zero helper")
    except (TypeError, ValueError, OverflowError) as exc:
        return reject(f"resource PID membership is malformed: {exc}")

    if (
        ready.get("schema_version") != CONTAINER_IDENTITY_SCHEMA_VERSION
        or ready.get("state") != "READY"
        or ready.get("root_pid") != root_pid
        or ready.get("resource_sample_index") != 0
        or ready.get("external_zero_baseline") is not True
        or ready.get("containers") != {}
        or ready.get("extra_pids") != []
        or ready.get("external_zero_baseline_pids") != []
        or ready.get("sample_pids") != sorted(expected_first_pids)
        or collector.get("collector_ready") != ready
    ):
        return reject("collector readiness does not exactly bind the sample-zero PID set")

    try:
        released_at = _terminal_censor_utc(execution.get("command_release_at_utc"))
        command_ended_at = _terminal_censor_utc(execution.get("command_ended_at_utc"))
    except (TypeError, ValueError, OverflowError) as exc:
        return reject(f"command release/end timestamps are malformed: {exc}")
    bad_at = utc_values[bad_position]
    final_at = utc_values[-1]
    if not released_at < bad_at <= command_ended_at <= final_at:
        return reject("release/censor/command-end/final timestamps are not ordered")
    if child_pid is not None and not utc_values[0] <= released_at < utc_values[1]:
        return reject("optional helper was not confined to the pre-release sample")
    bad_to_end_s = (command_ended_at - bad_at).total_seconds()
    end_to_final_s = (final_at - command_ended_at).total_seconds()
    if bad_to_end_s > interval_s:
        return reject("command ended more than one collector interval after the censor")
    if end_to_final_s > 2.0 * interval_s:
        return reject("final empty row arrived more than two intervals after command end")
    if monotonic_values[-1] - monotonic_values[bad_position] > 3.0 * interval_s:
        return reject("terminal monotonic gap exceeds three collector intervals")
    if monotonic_values[bad_position] - monotonic_values[bad_position - 1] > 2.0 * interval_s:
        return reject("pre-censor monotonic gap exceeds two collector intervals")

    try:
        if (
            _terminal_censor_int(bad, "process_rss_bytes") != 0
            or _terminal_censor_int(bad, "process_pss_bytes") != 0
            or _terminal_censor_int(final, "process_rss_bytes") != 0
            or _terminal_censor_int(final, "process_pss_bytes") != 0
            or _terminal_censor_int(previous, "process_rss_bytes") <= 0
            or _terminal_censor_int(previous, "process_pss_bytes") <= 0
        ):
            return reject("RSS/PSS boundary values do not prove one terminal censor")
        for field in TERMINAL_CENSOR_INTEGER_CUMULATIVE_FIELDS:
            previous_value = _terminal_censor_int(previous, field)
            bad_value = _terminal_censor_int(bad, field)
            final_value = _terminal_censor_int(final, field)
            if bad_value != previous_value or final_value != bad_value:
                return reject(f"cumulative {field} changed across the censored boundary")
        for field in TERMINAL_CENSOR_CPU_CUMULATIVE_FIELDS:
            previous_value = _terminal_censor_float(previous, field)
            bad_value = _terminal_censor_float(bad, field)
            final_value = _terminal_censor_float(final, field)
            if bad_value < previous_value or final_value != bad_value:
                return reject(f"cumulative {field} is inconsistent across the censored boundary")
    except (TypeError, ValueError, OverflowError) as exc:
        return reject(f"terminal cumulative fields are malformed: {exc}")

    receipt = {
        "schema_version": TERMINAL_CENSOR_RECEIPT_SCHEMA_VERSION,
        "policy_version": TERMINAL_CENSOR_POLICY_VERSION,
        "sample_index": bad_position,
        "root_pid": root_pid,
        "root_start_ticks": root_identity["start_ticks"],
        "censored_fields": list(TERMINAL_CENSOR_UNREADABLE_FIELDS),
        "previous_sample_index": bad_position - 1,
        "final_sample_index": len(rows) - 1,
        "censored_at_utc": bad.get("timestamp_utc"),
        "command_ended_at_utc": execution.get("command_ended_at_utc"),
        "final_sample_at_utc": final.get("timestamp_utc"),
        "collector_interval_s": interval_s,
        "bad_to_command_end_s": bad_to_end_s,
        "command_end_to_final_s": end_to_final_s,
        "process_cpu_tail_lower_bound": True,
        "process_io_tail_lower_bound": True,
        "rss_pss_terminal_sample_censored": True,
    }
    return receipt, "accepted exact root-only penultimate terminal censor"


def build_terminal_censor_quality(
    manifest: dict[str, Any],
    receipt: dict[str, Any] | None,
    classification_reason: str,
) -> dict[str, Any]:
    censored = receipt is not None
    return {
        "policy_version": (
            TERMINAL_CENSOR_POLICY_VERSION
            if _terminal_censor_policy_enabled(manifest)
            else ""
        ),
        "classification_reason": classification_reason,
        "terminal_censored": censored,
        "terminal_censored_sample_index": (
            receipt["sample_index"] if receipt is not None else -1
        ),
        "process_cpu_tail_lower_bound": censored,
        "process_io_tail_lower_bound": censored,
        "rss_pss_terminal_sample_censored": censored,
        "process_cpu_quality": (
            TERMINAL_CENSOR_POLICY["process_cpu_quality"]
            if censored
            else "all-samples-readable"
        ),
        "process_io_quality": (
            TERMINAL_CENSOR_POLICY["process_io_quality"]
            if censored
            else "all-samples-readable"
        ),
        "peak_rss_pss_quality": (
            TERMINAL_CENSOR_POLICY["peak_rss_pss_quality"]
            if censored
            else "all-samples-readable"
        ),
    }


def validate_resource_rows(
    rows: list[dict[str, str]],
    min_samples: int,
    strict: bool,
    errors: list[str],
    warnings: list[str],
    terminal_censor_receipt: dict[str, Any] | None = None,
) -> None:
    terminal_censor_applies = (
        isinstance(terminal_censor_receipt, dict)
        and terminal_censor_receipt.get("schema_version")
        == TERMINAL_CENSOR_RECEIPT_SCHEMA_VERSION
        and terminal_censor_receipt.get("policy_version")
        == TERMINAL_CENSOR_POLICY_VERSION
        and terminal_censor_receipt.get("sample_index") == len(rows) - 2
        and terminal_censor_receipt.get("censored_fields")
        == list(TERMINAL_CENSOR_UNREADABLE_FIELDS)
    )
    if len(rows) < min_samples:
        errors.append(f"resource-samples.tsv: {len(rows)} rows < min_samples={min_samples}")
        return
    integer_fields = [
        "sample_index",
        "root_pid",
        "root_alive",
        "process_count",
        "process_rss_bytes",
        "process_rss_unreadable",
        "process_pss_bytes",
        "process_pss_unreadable",
        "process_io_unreadable",
        "process_read_bytes",
        "process_write_bytes",
        "process_cancelled_write_bytes",
        "process_rchar",
        "process_wchar",
        "host_mem_total_bytes",
        "host_mem_available_bytes",
        "host_swap_total_bytes",
        "host_swap_free_bytes",
        "data_free_bytes",
    ]
    float_fields = [
        "monotonic_s",
        "process_user_cpu_s",
        "process_sys_cpu_s",
        "process_cpu_pct",
        "process_read_mib_s",
        "process_write_mib_s",
        "host_load1",
        "host_load5",
        "host_load15",
    ]
    indexes: list[int] = []
    monotonic_values: list[float] = []
    cumulative_values: dict[str, list[float]] = {
        key: []
        for key in (
            "process_user_cpu_s",
            "process_sys_cpu_s",
            "process_read_bytes",
            "process_write_bytes",
            "process_rchar",
            "process_wchar",
        )
    }
    for index, row in enumerate(rows):
        context = f"resource-samples.tsv row {index}"
        for key in integer_fields:
            integer(row, key, errors, context)
        for key in float_fields:
            number(row, key, errors, context)
        for key in (
            "device_read_iops",
            "device_write_iops",
            "device_read_mib_s",
            "device_write_mib_s",
            "device_read_await_ms",
            "device_write_await_ms",
            "device_await_ms",
            "device_util_pct",
        ):
            number(row, key, errors, context, allow_empty=True)
        try:
            indexes.append(int(row["sample_index"]))
            monotonic_values.append(float(row["monotonic_s"]))
            process_count = int(row["process_count"])
            root_alive = int(row["root_alive"])
            pids = [raw for raw in row.get("pids", "").split(",") if raw]
            if any(not raw.isdigit() for raw in pids) or len(pids) != len(set(pids)):
                errors.append(f"{context}: malformed or duplicate PID list")
            if len(pids) != process_count:
                errors.append(f"{context}: process_count does not match PID list")
            if root_alive not in (0, 1):
                errors.append(f"{context}: root_alive is not 0/1")
            if root_alive and row.get("root_pid") not in pids:
                errors.append(f"{context}: live root PID is absent from process tree")
            for key, values in cumulative_values.items():
                values.append(float(row[key]))
        except (KeyError, TypeError, ValueError):
            pass
    if indexes and indexes != list(range(len(rows))):
        errors.append("resource-samples.tsv: sample_index is not contiguous from zero")
    if any(right <= left for left, right in zip(monotonic_values, monotonic_values[1:])):
        errors.append("resource-samples.tsv: monotonic_s is not strictly increasing")
    for key, values in cumulative_values.items():
        if any(right < left for left, right in zip(values, values[1:])):
            errors.append(f"resource-samples.tsv: cumulative {key} decreased")
    if not any(row.get("root_alive") == "1" for row in rows):
        errors.append("resource-samples.tsv: root process was never observed alive")
    if max_number(rows, "process_count") < 1:
        errors.append("resource-samples.tsv: no process-tree member observed")
    if max_number(rows, "process_pss_bytes") <= 0:
        errors.append("resource-samples.tsv: no readable non-zero process PSS sample")
    if max_number(rows, "host_mem_total_bytes") <= 0:
        errors.append("resource-samples.tsv: host memory facts were unreadable")
    if max_number(rows, "data_free_bytes") <= 0:
        errors.append("resource-samples.tsv: data mount was unreadable or has no free space")
    readable_active_io = False
    for row in rows:
        try:
            if float(row.get("process_count", 0)) > 0 and float(row.get("process_io_unreadable", 1)) == 0:
                readable_active_io = True
                break
        except (TypeError, ValueError):
            continue
    if not readable_active_io:
        errors.append("resource-samples.tsv: process I/O was never readable for the active tree")
    for field, description in (
        ("process_rss_unreadable", "RSS"),
        ("process_pss_unreadable", "PSS"),
        ("process_io_unreadable", "process I/O"),
    ):
        if max_number(rows, field) > 0:
            message = f"one or more {description} samples were unreadable; aggregate is a lower bound"
            if strict and not terminal_censor_applies:
                errors.append(message)
            elif not strict:
                warnings.append(message)
    if strict and terminal_censor_applies:
        warnings.append(TERMINAL_CENSOR_WARNING)
    device_fields = [
        "device_read_iops",
        "device_write_iops",
        "device_read_mib_s",
        "device_write_mib_s",
        "device_read_await_ms",
        "device_write_await_ms",
        "device_await_ms",
        "device_util_pct",
    ]
    if not rows[1:] or any(any(row.get(field, "") == "" for field in device_fields) for row in rows[1:]):
        errors.append("resource-samples.tsv: one or more /proc/diskstats delta samples are incomplete")


def validate_disk_rows(
    rows: list[dict[str, str]], manifest: dict[str, Any], errors: list[str]
) -> dict[str, Any]:
    configured_paths = {
        (str(root.get("role", "")), str(root.get("label", ""))): str(root.get("path", ""))
        for root in manifest.get("disk_roots", [])
        if isinstance(root, dict)
    }
    configured = set(configured_paths)
    observed: set[tuple[str, str]] = set()
    observed_existing: set[tuple[str, str]] = set()
    roots_by_round: dict[int, set[tuple[str, str]]] = defaultdict(set)
    root_round_pairs: set[tuple[int, str, str]] = set()
    per_root: dict[str, dict[str, float]] = {}
    store_by_round: dict[int, int] = defaultdict(int)
    temp_by_round: dict[int, int] = defaultdict(int)
    categories_by_round: dict[str, dict[int, int]] = {
        category: defaultdict(int) for category in DISK_CATEGORIES
    }
    for index, row in enumerate(rows):
        context = f"disk-samples.tsv row {index}"
        observed.add((row.get("root_role", ""), row.get("root_label", "")))
        integer_fields = [
            "sample_index",
            "exists",
            "scan_complete",
            "scan_error_count",
            "total_bytes",
            "file_count",
        ] + [
            f"{category}_bytes" for category in DISK_CATEGORIES
        ]
        for key in integer_fields:
            integer(row, key, errors, context)
        number(row, "scan_elapsed_ms", errors, context)
        number(row, "monotonic_s", errors, context)
        try:
            total = int(row["total_bytes"])
            category_values = {category: int(row[f"{category}_bytes"]) for category in DISK_CATEGORIES}
            category_total = sum(category_values.values())
            sample_index = int(row["sample_index"])
            exists = int(row["exists"])
            scan_complete = int(row["scan_complete"])
            scan_error_count = int(row["scan_error_count"])
            file_count = int(row["file_count"])
        except (TypeError, ValueError):
            continue
        identity = (row.get("root_role", ""), row.get("root_label", ""))
        if identity in configured_paths and row.get("root_path") != configured_paths[identity]:
            errors.append(f"{context}: root_path differs from manifest")
        round_identity = (sample_index, *identity)
        if round_identity in root_round_pairs:
            errors.append(f"{context}: duplicate root in disk scan round")
        root_round_pairs.add(round_identity)
        roots_by_round[sample_index].add(identity)
        if exists not in (0, 1) or scan_complete not in (0, 1):
            errors.append(f"{context}: exists/scan_complete is not 0/1")
        if min(total, file_count, scan_error_count, *category_values.values()) < 0:
            errors.append(f"{context}: disk counters must be non-negative")
        if exists == 1:
            observed_existing.add(identity)
        if scan_complete != 1 or scan_error_count != 0:
            errors.append(f"{context}: disk scan was incomplete")
        if total != category_total:
            errors.append(f"{context}: category bytes {category_total} != total_bytes {total}")
        label_key = f"{row.get('root_role')}:{row.get('root_label')}"
        summary = per_root.setdefault(label_key, {"peak_total_bytes": 0, "peak_file_count": 0})
        summary["peak_total_bytes"] = max(summary["peak_total_bytes"], total)
        summary["peak_file_count"] = max(summary["peak_file_count"], file_count)
        for category in DISK_CATEGORIES:
            key = f"peak_{category}_bytes"
            summary[key] = max(summary.get(key, 0), category_values[category])
            categories_by_round[category][sample_index] += category_values[category]
        if row.get("root_role") == "store":
            store_by_round[sample_index] += total
        temp_by_round[sample_index] += category_values["temp"]
    missing = configured - observed
    if missing:
        errors.append(f"disk-samples.tsv: missing configured roots: {sorted(missing)!r}")
    unexpected = observed - configured
    if unexpected:
        errors.append(f"disk-samples.tsv: unexpected roots: {sorted(unexpected)!r}")
    never_existing = configured - observed_existing
    if never_existing:
        errors.append(f"disk-samples.tsv: configured roots never existed: {sorted(never_existing)!r}")
    for sample_index, roots_in_round in sorted(roots_by_round.items()):
        missing_in_round = configured - roots_in_round
        if missing_in_round:
            errors.append(
                f"disk-samples.tsv: round {sample_index} missing roots: {sorted(missing_in_round)!r}"
            )
    if roots_by_round and sorted(roots_by_round) != list(range(len(roots_by_round))):
        errors.append("disk-samples.tsv: scan-round sample_index is not contiguous from zero")
    if manifest.get("performance_eligible_declared") and not any(role == "store" for role, _ in configured):
        errors.append("formal run manifest has no configured store root")
    if manifest.get("performance_eligible_declared") and max(store_by_round.values(), default=0) <= 0:
        errors.append("formal run store roots remained empty")
    return {
        "roots": per_root,
        "peak_store_total_bytes": max(store_by_round.values(), default=0),
        "peak_temp_bytes": max(temp_by_round.values(), default=0),
        "peak_category_bytes": {
            category: max(by_round.values(), default=0)
            for category, by_round in categories_by_round.items()
        },
    }


def validate_iostat_rows(
    rows: list[dict[str, str]],
    expected_device: str,
    errors: list[str],
    warnings: list[str],
) -> None:
    indexes: list[int] = []
    numeric_fields = [
        "sample_index",
        "read_iops",
        "write_iops",
        "read_mib_s",
        "write_mib_s",
        "read_await_ms",
        "write_await_ms",
        "await_ms",
        "aqu_sz",
        "util_pct",
    ]
    for index, row in enumerate(rows):
        context = f"iostat-samples.tsv row {index}"
        integer(row, "sample_index", errors, context)
        values = [number(row, key, errors, context) for key in numeric_fields[1:]]
        try:
            indexes.append(int(row["sample_index"]))
        except (KeyError, TypeError, ValueError):
            pass
        if row.get("device") != expected_device:
            errors.append(f"{context}: device differs from manifest")
        if not row.get("timestamp_raw"):
            errors.append(f"{context}: raw timestamp is missing")
        finite_values = [value for value in values if math.isfinite(value)]
        if finite_values and min(finite_values) < 0:
            errors.append(f"{context}: iostat counters must be non-negative")
        try:
            util_pct = float(row["util_pct"])
            if util_pct > IOSTAT_UTIL_NOMINAL_MAX_PCT + IOSTAT_UTIL_ROUNDING_TOLERANCE_PCT:
                errors.append(
                    f"{context}: util_pct exceeds the "
                    f"{IOSTAT_UTIL_NOMINAL_MAX_PCT + IOSTAT_UTIL_ROUNDING_TOLERANCE_PCT:.1f}% "
                    "rounding/skew ceiling"
                )
            elif util_pct > IOSTAT_UTIL_NOMINAL_MAX_PCT:
                warnings.append(
                    f"{context}: util_pct={util_pct:g}% is above the nominal 100% "
                    "but within the 1.0 percentage-point iostat rounding/skew tolerance; "
                    "the raw value is preserved"
                )
        except (KeyError, TypeError, ValueError):
            pass
    if indexes and indexes != list(range(len(rows))):
        errors.append("iostat-samples.tsv: sample_index is not contiguous from zero")


def pidstat_has_pid_sample(text: str, pid: int) -> bool:
    expected = str(pid)
    for line in text.splitlines():
        if not line or line.startswith(("Linux", "#")):
            continue
        fields = re.split(r"\s+", line.strip())
        if len(fields) >= 4 and fields[1] in {"AM", "PM"} and fields[3] == expected:
            return True
        if len(fields) >= 3 and fields[1] not in {"AM", "PM"} and fields[2] == expected:
            return True
    return False


def validate_formal_provenance(manifest: dict[str, Any], errors: list[str]) -> None:
    if not manifest.get("performance_eligible_declared"):
        return
    repo = manifest.get("repo", {})
    if not repo.get("git_sha_command_ok") or not repo.get("status_command_ok"):
        errors.append("formal run could not audit repository SHA/status")
    if not repo.get("git_sha"):
        errors.append("formal run is missing git SHA")
    if repo.get("dirty"):
        errors.append("formal run was launched from a dirty repository")
    host = manifest.get("host", {})
    if not host.get("pidstat_version_command_ok") or not host.get("iostat_version_command_ok"):
        errors.append("formal run could not audit pidstat/iostat versions")
    storage_host = manifest.get("storage_host", {})
    if not storage_host.get("device", {}).get("exists"):
        errors.append("formal run device was absent from sysfs")
    if not storage_host.get("findmnt_command_ok") or not storage_host.get("findmnt"):
        errors.append("formal run could not audit the data mount")
    if not manifest.get("collector", {}).get("require_aux_tools"):
        errors.append("formal run cannot allow missing pidstat/iostat")
    inputs = manifest.get("inputs", {})
    for name in ("binary", "dataset", "config"):
        ref = inputs.get(name, {})
        if not ref.get("path") or not ref.get("exists"):
            errors.append(f"formal run is missing existing {name} path")
        if not ref.get("sha256"):
            errors.append(f"formal run is missing {name} SHA-256")
    for name in ("truth", "query_or_trace"):
        ref = inputs.get(name, {})
        if ref.get("path"):
            if not ref.get("exists"):
                errors.append(f"formal run specified missing {name} path")
            if not ref.get("sha256"):
                errors.append(f"formal run specified {name} but omitted SHA-256")
    for name, ref in inputs.items():
        if not isinstance(ref, dict):
            errors.append(f"formal run {name} input reference is malformed")
            continue
        if not ref.get("path"):
            continue
        path = Path(ref["path"])
        sha_mode = ref.get("content_sha256_mode", "verify")
        if sha_mode not in {"verify", "declared-no-read-v1"}:
            errors.append(f"formal run {name} has an unknown SHA-256 mode")
            continue
        if sha_mode == "declared-no-read-v1" and name != "dataset":
            errors.append(f"formal run {name} cannot use declared-no-read SHA-256 mode")
        if ref.get("kind") == "directory" and not path.is_dir():
            errors.append(f"formal run {name} directory disappeared since launch")
        if ref.get("kind") == "file":
            if not path.is_file() or not ref.get("sha256"):
                errors.append(f"formal run {name} file is missing or lacks SHA-256")
            elif sha_mode == "declared-no-read-v1":
                if path.stat().st_size != ref.get("size_bytes"):
                    errors.append(f"formal run {name} declared file size changed since launch")
            elif sha256_file(path) != ref["sha256"]:
                errors.append(f"formal run {name} file changed since launch")


def container_identity_tuple(
    value: object,
    context: str,
    errors: list[str],
) -> tuple[str, int, str, int] | None:
    if not isinstance(value, dict):
        errors.append(f"{context}: identity is not an object")
        return None
    container_id = value.get("container_id")
    pid = value.get("pid")
    started_at = value.get("started_at")
    restart_count = value.get("restart_count")
    if not isinstance(container_id, str) or _CONTAINER_ID.fullmatch(container_id) is None:
        errors.append(f"{context}: container_id is not a full Docker ID")
        return None
    if type(pid) is not int or pid <= 0:
        errors.append(f"{context}: pid is not a positive integer")
        return None
    if not isinstance(started_at, str) or not started_at:
        errors.append(f"{context}: started_at is empty or malformed")
        return None
    if type(restart_count) is not int or restart_count < 0:
        errors.append(f"{context}: restart_count is not a non-negative integer")
        return None
    return container_id, pid, started_at, restart_count


def validate_container_tracking(
    collector_config: dict[str, Any],
    collector: dict[str, Any],
    ready: dict[str, Any],
    resources: list[dict[str, str]],
    expected_root_pid: object,
    errors: list[str],
) -> None:
    """Fail closed on unresolved, unsampled, or changing external identities."""

    requested = collector_config.get("containers")
    configured_extra = collector_config.get("extra_pids")
    if not isinstance(requested, list) or any(not isinstance(name, str) or not name for name in requested):
        errors.append("run-manifest.json: collector containers are malformed")
        requested = []
    if len(requested) != len(set(requested)):
        errors.append("run-manifest.json: collector containers contain duplicates")
    if (
        not isinstance(configured_extra, list)
        or any(type(pid) is not int or pid <= 0 for pid in configured_extra)
        or len(configured_extra) != len(set(configured_extra))
    ):
        errors.append("run-manifest.json: extra_pids are not unique positive integers")
        configured_extra = []

    sample_pids_by_index: dict[int, set[int]] = {}
    all_sample_pids: set[int] = set()
    for row in resources:
        try:
            index = int(row["sample_index"])
            pids = {int(raw) for raw in row.get("pids", "").split(",") if raw}
        except (KeyError, TypeError, ValueError):
            continue
        sample_pids_by_index[index] = pids
        all_sample_pids.update(pids)

    seen = collector.get("containers_seen")
    history = collector.get("container_identity_history")
    unique = collector.get("container_identity_unique_set")
    expected_names = set(requested)
    for label, value in (
        ("containers_seen", seen),
        ("container_identity_history", history),
        ("container_identity_unique_set", unique),
    ):
        if not isinstance(value, dict) or set(value) != expected_names:
            errors.append(f"collector-status.json: {label} keys differ from requested containers")
    if not isinstance(seen, dict) or not isinstance(history, dict) or not isinstance(unique, dict):
        return
    if collector.get("container_identity_schema_version") != CONTAINER_IDENTITY_SCHEMA_VERSION:
        errors.append("collector-status.json: wrong container identity schema version")

    normalized_unique: dict[str, tuple[str, int, str, int]] = {}
    for name in requested:
        seen_pid = seen.get(name)
        if type(seen_pid) is not int or seen_pid <= 0:
            errors.append(f"collector-status.json: containers_seen[{name!r}] is not a positive integer")
        elif seen_pid not in all_sample_pids:
            errors.append(f"collector-status.json: containers_seen[{name!r}] PID was never sampled")

        observations = history.get(name)
        identities = unique.get(name)
        if not isinstance(observations, list) or not observations:
            errors.append(f"collector-status.json: no identity history for container {name!r}")
            observations = []
        if not isinstance(identities, list) or not identities:
            errors.append(f"collector-status.json: no unique identity for container {name!r}")
            identities = []

        derived: list[tuple[str, int, str, int]] = []
        for index, observation in enumerate(observations):
            identity = container_identity_tuple(
                observation,
                f"collector-status.json: {name} history[{index}]",
                errors,
            )
            if identity is not None and identity not in derived:
                derived.append(identity)
            before_sample = observation.get("before_sample_index") if isinstance(observation, dict) else None
            observed_at = observation.get("observed_at_utc") if isinstance(observation, dict) else None
            if type(before_sample) is not int or before_sample not in sample_pids_by_index:
                errors.append(f"collector-status.json: {name} history[{index}] has no matching sample")
            elif identity is not None and identity[1] not in sample_pids_by_index[before_sample]:
                errors.append(f"collector-status.json: {name} history[{index}] PID is absent from its sample")
            if not isinstance(observed_at, str) or not observed_at:
                errors.append(f"collector-status.json: {name} history[{index}] lacks observed_at_utc")

        declared: list[tuple[str, int, str, int]] = []
        for index, value in enumerate(identities):
            identity = container_identity_tuple(
                value,
                f"collector-status.json: {name} unique[{index}]",
                errors,
            )
            if identity is not None:
                declared.append(identity)
        if declared != derived:
            errors.append(f"collector-status.json: {name} unique set is not derived from identity history")
        if len(declared) != 1:
            errors.append(f"collector-status.json: {name} did not retain exactly one stable identity")
        else:
            normalized_unique[name] = declared[0]
            if declared[0][1] != seen_pid:
                errors.append(f"collector-status.json: {name} containers_seen PID differs from identity")
            if declared[0][1] not in all_sample_pids:
                errors.append(f"collector-status.json: {name} identity PID was never sampled")

    if ready.get("schema_version") != CONTAINER_IDENTITY_SCHEMA_VERSION or ready.get("state") != "READY":
        errors.append("collector-ready.json: missing clean READY identity gate")
    if collector.get("collector_ready") != ready:
        errors.append("collector-status.json: collector_ready differs from collector-ready.json")
    if ready.get("root_pid") != expected_root_pid:
        errors.append("collector-ready.json: root_pid differs from execution.json")
    if ready.get("external_zero_baseline") is not True:
        errors.append("collector-ready.json: external zero baseline was not established")
    ready_index = ready.get("resource_sample_index")
    if type(ready_index) is not int or ready_index not in sample_pids_by_index:
        errors.append("collector-ready.json: resource_sample_index has no matching sample")
        ready_sample_pids: set[int] = set()
    else:
        ready_sample_pids = sample_pids_by_index[ready_index]
        if ready_index != 0:
            errors.append("collector-ready.json: adapter was not released from the first resource sample")
    if not isinstance(ready.get("ready_at_utc"), str) or not ready.get("ready_at_utc"):
        errors.append("collector-ready.json: ready_at_utc is missing")
    declared_sample_pids = ready.get("sample_pids")
    if (
        not isinstance(declared_sample_pids, list)
        or any(type(pid) is not int or pid <= 0 for pid in declared_sample_pids)
        or declared_sample_pids != sorted(ready_sample_pids)
    ):
        errors.append("collector-ready.json: sample_pids differ from the readiness resource sample")
    baseline_pids = ready.get("external_zero_baseline_pids")
    if (
        not isinstance(baseline_pids, list)
        or any(type(pid) is not int or pid <= 0 for pid in baseline_pids)
        or baseline_pids != sorted(set(baseline_pids))
        or not set(baseline_pids).issubset(ready_sample_pids)
    ):
        errors.append("collector-ready.json: external_zero_baseline_pids are malformed or unsampled")
        baseline_pid_set: set[int] = set()
    else:
        baseline_pid_set = set(baseline_pids)
    if ready.get("extra_pids") != configured_extra or not set(configured_extra).issubset(baseline_pid_set):
        errors.append("collector-ready.json: extra PID baseline differs from the manifest")
    ready_containers = ready.get("containers")
    if not isinstance(ready_containers, dict) or set(ready_containers) != expected_names:
        errors.append("collector-ready.json: container keys differ from the manifest")
    else:
        for name in requested:
            identity = container_identity_tuple(
                ready_containers.get(name),
                f"collector-ready.json: containers[{name!r}]",
                errors,
            )
            if identity is not None:
                if normalized_unique.get(name) != identity:
                    errors.append(f"collector-ready.json: {name} identity differs from final unique identity")
                if identity[1] not in ready_sample_pids or identity[1] not in baseline_pid_set:
                    errors.append(f"collector-ready.json: {name} PID lacks a zero-baseline readiness sample")


def validate_process_tracking(
    collector: dict[str, Any],
    resources: list[dict[str, str]],
    errors: list[str],
) -> None:
    """Validate compact PID/start-ticks evidence used for embedded workers."""

    if collector.get("process_identity_schema_version") != PROCESS_IDENTITY_SCHEMA_VERSION:
        errors.append("collector-status.json: wrong process identity schema version")
    values = collector.get("process_identity_unique_set")
    if not isinstance(values, list) or not values:
        errors.append("collector-status.json: process identity unique set is missing or empty")
        return
    sample_pids: dict[int, set[int]] = {}
    for row in resources:
        try:
            sample_pids[int(row["sample_index"])] = {
                int(raw) for raw in row.get("pids", "").split(",") if raw
            }
        except (KeyError, TypeError, ValueError):
            continue
    normalized: list[tuple[int, int]] = []
    exact_keys = {
        "pid", "ppid", "pgrp", "start_ticks", "first_sample_index",
        "last_sample_index", "sample_count",
    }
    for index, value in enumerate(values):
        context = f"collector-status.json: process identity[{index}]"
        if not isinstance(value, dict) or set(value) != exact_keys:
            errors.append(f"{context} has malformed keys")
            continue
        if any(type(value[key]) is not int for key in exact_keys):
            errors.append(f"{context} contains a non-integer field")
            continue
        pid = value["pid"]
        start_ticks = value["start_ticks"]
        first = value["first_sample_index"]
        last = value["last_sample_index"]
        count = value["sample_count"]
        if pid <= 0 or value["ppid"] < 0 or value["pgrp"] <= 0 or start_ticks <= 0:
            errors.append(f"{context} has an invalid Linux process identity")
        if first < 0 or last < first or count <= 0 or count > last - first + 1:
            errors.append(f"{context} has an invalid sample interval/count")
        if pid not in sample_pids.get(first, set()) or pid not in sample_pids.get(last, set()):
            errors.append(f"{context} is absent from its boundary resource sample")
        normalized.append((pid, start_ticks))
    if normalized != sorted(normalized) or len(normalized) != len(set(normalized)):
        errors.append("collector-status.json: process identity set is not unique/sorted")


def container_identity_tuple(
    value: object,
    context: str,
    errors: list[str],
) -> tuple[str, int, int, str, int] | None:
    if not isinstance(value, dict):
        errors.append(f"{context}: identity is not an object")
        return None
    container_id = value.get("container_id")
    pid = value.get("pid")
    process_start_ticks = value.get("process_start_ticks")
    started_at = value.get("started_at")
    restart_count = value.get("restart_count")
    if not isinstance(container_id, str) or _CONTAINER_ID.fullmatch(container_id) is None:
        errors.append(f"{context}: container_id is not a full Docker ID")
        return None
    if type(pid) is not int or pid <= 0:
        errors.append(f"{context}: pid is not a positive integer")
        return None
    if type(process_start_ticks) is not int or process_start_ticks <= 0:
        errors.append(f"{context}: process_start_ticks is not a positive integer")
        return None
    if not isinstance(started_at, str) or not started_at:
        errors.append(f"{context}: started_at is empty or malformed")
        return None
    if type(restart_count) is not int or restart_count < 0:
        errors.append(f"{context}: restart_count is not a non-negative integer")
        return None
    return container_id, pid, process_start_ticks, started_at, restart_count


def validate_container_tracking(
    collector_config: dict[str, Any],
    collector: dict[str, Any],
    ready: dict[str, Any],
    resources: list[dict[str, str]],
    expected_root_pid: object,
    errors: list[str],
) -> None:
    """Fail closed on unresolved, unsampled, or changing external identities."""

    requested = collector_config.get("containers")
    configured_extra = collector_config.get("extra_pids")
    if not isinstance(requested, list) or any(not isinstance(name, str) or not name for name in requested):
        errors.append("run-manifest.json: collector containers are malformed")
        requested = []
    if len(requested) != len(set(requested)):
        errors.append("run-manifest.json: collector containers contain duplicates")
    if (
        not isinstance(configured_extra, list)
        or any(type(pid) is not int or pid <= 0 for pid in configured_extra)
        or len(configured_extra) != len(set(configured_extra))
    ):
        errors.append("run-manifest.json: extra_pids are not unique positive integers")
        configured_extra = []

    sample_pids_by_index: dict[int, set[int]] = {}
    all_sample_pids: set[int] = set()
    for row in resources:
        try:
            index = int(row["sample_index"])
            pids = {int(raw) for raw in row.get("pids", "").split(",") if raw}
        except (KeyError, TypeError, ValueError):
            continue
        sample_pids_by_index[index] = pids
        all_sample_pids.update(pids)

    seen = collector.get("containers_seen")
    history = collector.get("container_identity_history")
    unique = collector.get("container_identity_unique_set")
    expected_names = set(requested)
    for label, value in (
        ("containers_seen", seen),
        ("container_identity_history", history),
        ("container_identity_unique_set", unique),
    ):
        if not isinstance(value, dict) or set(value) != expected_names:
            errors.append(f"collector-status.json: {label} keys differ from requested containers")
    if not isinstance(seen, dict) or not isinstance(history, dict) or not isinstance(unique, dict):
        return
    if collector.get("container_identity_schema_version") != CONTAINER_IDENTITY_SCHEMA_VERSION:
        errors.append("collector-status.json: wrong container identity schema version")

    normalized_unique: dict[str, tuple[str, int, int, str, int]] = {}
    for name in requested:
        seen_pid = seen.get(name)
        if type(seen_pid) is not int or seen_pid <= 0:
            errors.append(f"collector-status.json: containers_seen[{name!r}] is not a positive integer")
        elif seen_pid not in all_sample_pids:
            errors.append(f"collector-status.json: containers_seen[{name!r}] PID was never sampled")

        observations = history.get(name)
        identities = unique.get(name)
        if not isinstance(observations, list) or not observations:
            errors.append(f"collector-status.json: no identity history for container {name!r}")
            observations = []
        if not isinstance(identities, list) or not identities:
            errors.append(f"collector-status.json: no unique identity for container {name!r}")
            identities = []

        derived: list[tuple[str, int, int, str, int]] = []
        for index, observation in enumerate(observations):
            identity = container_identity_tuple(
                observation,
                f"collector-status.json: {name} history[{index}]",
                errors,
            )
            if identity is not None and identity not in derived:
                derived.append(identity)
            before_sample = observation.get("before_sample_index") if isinstance(observation, dict) else None
            observed_at = observation.get("observed_at_utc") if isinstance(observation, dict) else None
            if type(before_sample) is not int or before_sample not in sample_pids_by_index:
                errors.append(f"collector-status.json: {name} history[{index}] has no matching sample")
            elif identity is not None and identity[1] not in sample_pids_by_index[before_sample]:
                errors.append(f"collector-status.json: {name} history[{index}] PID is absent from its sample")
            if not isinstance(observed_at, str) or not observed_at:
                errors.append(f"collector-status.json: {name} history[{index}] lacks observed_at_utc")

        declared: list[tuple[str, int, int, str, int]] = []
        for index, value in enumerate(identities):
            identity = container_identity_tuple(
                value,
                f"collector-status.json: {name} unique[{index}]",
                errors,
            )
            if identity is not None:
                declared.append(identity)
        if declared != derived:
            errors.append(f"collector-status.json: {name} unique set is not derived from identity history")
        if len(declared) != 1:
            errors.append(f"collector-status.json: {name} did not retain exactly one stable identity")
        else:
            normalized_unique[name] = declared[0]
            if declared[0][1] != seen_pid:
                errors.append(f"collector-status.json: {name} containers_seen PID differs from identity")
            if declared[0][1] not in all_sample_pids:
                errors.append(f"collector-status.json: {name} identity PID was never sampled")

    if ready.get("schema_version") != CONTAINER_IDENTITY_SCHEMA_VERSION or ready.get("state") != "READY":
        errors.append("collector-ready.json: missing clean READY identity gate")
    if collector.get("collector_ready") != ready:
        errors.append("collector-status.json: collector_ready differs from collector-ready.json")
    if ready.get("root_pid") != expected_root_pid:
        errors.append("collector-ready.json: root_pid differs from execution.json")
    if ready.get("external_zero_baseline") is not True:
        errors.append("collector-ready.json: external zero baseline was not established")
    ready_index = ready.get("resource_sample_index")
    if type(ready_index) is not int or ready_index not in sample_pids_by_index:
        errors.append("collector-ready.json: resource_sample_index has no matching sample")
        ready_sample_pids: set[int] = set()
    else:
        ready_sample_pids = sample_pids_by_index[ready_index]
        if ready_index != 0:
            errors.append("collector-ready.json: adapter was not released from the first resource sample")
    if not isinstance(ready.get("ready_at_utc"), str) or not ready.get("ready_at_utc"):
        errors.append("collector-ready.json: ready_at_utc is missing")
    declared_sample_pids = ready.get("sample_pids")
    if (
        not isinstance(declared_sample_pids, list)
        or any(type(pid) is not int or pid <= 0 for pid in declared_sample_pids)
        or declared_sample_pids != sorted(ready_sample_pids)
    ):
        errors.append("collector-ready.json: sample_pids differ from the readiness resource sample")
    baseline_pids = ready.get("external_zero_baseline_pids")
    if (
        not isinstance(baseline_pids, list)
        or any(type(pid) is not int or pid <= 0 for pid in baseline_pids)
        or baseline_pids != sorted(set(baseline_pids))
        or not set(baseline_pids).issubset(ready_sample_pids)
    ):
        errors.append("collector-ready.json: external_zero_baseline_pids are malformed or unsampled")
        baseline_pid_set: set[int] = set()
    else:
        baseline_pid_set = set(baseline_pids)
    if ready.get("extra_pids") != configured_extra or not set(configured_extra).issubset(baseline_pid_set):
        errors.append("collector-ready.json: extra PID baseline differs from the manifest")
    ready_containers = ready.get("containers")
    if not isinstance(ready_containers, dict) or set(ready_containers) != expected_names:
        errors.append("collector-ready.json: container keys differ from the manifest")
    else:
        for name in requested:
            identity = container_identity_tuple(
                ready_containers.get(name),
                f"collector-ready.json: containers[{name!r}]",
                errors,
            )
            if identity is not None:
                if normalized_unique.get(name) != identity:
                    errors.append(f"collector-ready.json: {name} identity differs from final unique identity")
                if identity[1] not in ready_sample_pids or identity[1] not in baseline_pid_set:
                    errors.append(f"collector-ready.json: {name} PID lacks a zero-baseline readiness sample")


def _legacy_short_container_identity_tuple(
    value: object,
    context: str,
    errors: list[str],
) -> tuple[str, int, str, int] | None:
    if not isinstance(value, dict):
        errors.append(f"{context}: identity is not an object")
        return None
    container_id = value.get("container_id")
    pid = value.get("pid")
    started_at = value.get("started_at")
    restart_count = value.get("restart_count")
    if not isinstance(container_id, str) or _CONTAINER_ID.fullmatch(container_id) is None:
        errors.append(f"{context}: container_id is not a full Docker ID")
        return None
    if type(pid) is not int or pid <= 0:
        errors.append(f"{context}: pid is not a positive integer")
        return None
    if not isinstance(started_at, str) or not started_at:
        errors.append(f"{context}: started_at is empty or malformed")
        return None
    if type(restart_count) is not int or restart_count < 0:
        errors.append(f"{context}: restart_count is not a non-negative integer")
        return None
    return container_id, pid, started_at, restart_count


def _legacy_short_validate_container_tracking(
    collector_config: dict[str, Any],
    collector: dict[str, Any],
    ready: dict[str, Any],
    resources: list[dict[str, str]],
    expected_root_pid: object,
    errors: list[str],
) -> None:
    """Fail closed on unresolved, unsampled, or changing external identities."""

    requested = collector_config.get("containers")
    configured_extra = collector_config.get("extra_pids")
    if not isinstance(requested, list) or any(not isinstance(name, str) or not name for name in requested):
        errors.append("run-manifest.json: collector containers are malformed")
        requested = []
    if len(requested) != len(set(requested)):
        errors.append("run-manifest.json: collector containers contain duplicates")
    if (
        not isinstance(configured_extra, list)
        or any(type(pid) is not int or pid <= 0 for pid in configured_extra)
        or len(configured_extra) != len(set(configured_extra))
    ):
        errors.append("run-manifest.json: extra_pids are not unique positive integers")
        configured_extra = []

    sample_pids_by_index: dict[int, set[int]] = {}
    all_sample_pids: set[int] = set()
    for row in resources:
        try:
            index = int(row["sample_index"])
            pids = {int(raw) for raw in row.get("pids", "").split(",") if raw}
        except (KeyError, TypeError, ValueError):
            continue
        sample_pids_by_index[index] = pids
        all_sample_pids.update(pids)

    seen = collector.get("containers_seen")
    history = collector.get("container_identity_history")
    unique = collector.get("container_identity_unique_set")
    expected_names = set(requested)
    for label, value in (
        ("containers_seen", seen),
        ("container_identity_history", history),
        ("container_identity_unique_set", unique),
    ):
        if not isinstance(value, dict) or set(value) != expected_names:
            errors.append(f"collector-status.json: {label} keys differ from requested containers")
    if not isinstance(seen, dict) or not isinstance(history, dict) or not isinstance(unique, dict):
        return
    if collector.get("container_identity_schema_version") != CONTAINER_IDENTITY_SCHEMA_VERSION:
        errors.append("collector-status.json: wrong container identity schema version")

    normalized_unique: dict[str, tuple[str, int, str, int]] = {}
    for name in requested:
        seen_pid = seen.get(name)
        if type(seen_pid) is not int or seen_pid <= 0:
            errors.append(f"collector-status.json: containers_seen[{name!r}] is not a positive integer")
        elif seen_pid not in all_sample_pids:
            errors.append(f"collector-status.json: containers_seen[{name!r}] PID was never sampled")

        observations = history.get(name)
        identities = unique.get(name)
        if not isinstance(observations, list) or not observations:
            errors.append(f"collector-status.json: no identity history for container {name!r}")
            observations = []
        if not isinstance(identities, list) or not identities:
            errors.append(f"collector-status.json: no unique identity for container {name!r}")
            identities = []

        derived: list[tuple[str, int, str, int]] = []
        for index, observation in enumerate(observations):
            identity = container_identity_tuple(
                observation,
                f"collector-status.json: {name} history[{index}]",
                errors,
            )
            if identity is not None and identity not in derived:
                derived.append(identity)
            before_sample = observation.get("before_sample_index") if isinstance(observation, dict) else None
            observed_at = observation.get("observed_at_utc") if isinstance(observation, dict) else None
            if type(before_sample) is not int or before_sample not in sample_pids_by_index:
                errors.append(f"collector-status.json: {name} history[{index}] has no matching sample")
            elif identity is not None and identity[1] not in sample_pids_by_index[before_sample]:
                errors.append(f"collector-status.json: {name} history[{index}] PID is absent from its sample")
            if not isinstance(observed_at, str) or not observed_at:
                errors.append(f"collector-status.json: {name} history[{index}] lacks observed_at_utc")

        declared: list[tuple[str, int, str, int]] = []
        for index, value in enumerate(identities):
            identity = container_identity_tuple(
                value,
                f"collector-status.json: {name} unique[{index}]",
                errors,
            )
            if identity is not None:
                declared.append(identity)
        if declared != derived:
            errors.append(f"collector-status.json: {name} unique set is not derived from identity history")
        if len(declared) != 1:
            errors.append(f"collector-status.json: {name} did not retain exactly one stable identity")
        else:
            normalized_unique[name] = declared[0]
            if declared[0][1] != seen_pid:
                errors.append(f"collector-status.json: {name} containers_seen PID differs from identity")
            if declared[0][1] not in all_sample_pids:
                errors.append(f"collector-status.json: {name} identity PID was never sampled")

    if ready.get("schema_version") != CONTAINER_IDENTITY_SCHEMA_VERSION or ready.get("state") != "READY":
        errors.append("collector-ready.json: missing clean READY identity gate")
    if collector.get("collector_ready") != ready:
        errors.append("collector-status.json: collector_ready differs from collector-ready.json")
    if ready.get("root_pid") != expected_root_pid:
        errors.append("collector-ready.json: root_pid differs from execution.json")
    if ready.get("external_zero_baseline") is not True:
        errors.append("collector-ready.json: external zero baseline was not established")
    ready_index = ready.get("resource_sample_index")
    if type(ready_index) is not int or ready_index not in sample_pids_by_index:
        errors.append("collector-ready.json: resource_sample_index has no matching sample")
        ready_sample_pids: set[int] = set()
    else:
        ready_sample_pids = sample_pids_by_index[ready_index]
        if ready_index != 0:
            errors.append("collector-ready.json: adapter was not released from the first resource sample")
    if not isinstance(ready.get("ready_at_utc"), str) or not ready.get("ready_at_utc"):
        errors.append("collector-ready.json: ready_at_utc is missing")
    declared_sample_pids = ready.get("sample_pids")
    if (
        not isinstance(declared_sample_pids, list)
        or any(type(pid) is not int or pid <= 0 for pid in declared_sample_pids)
        or declared_sample_pids != sorted(ready_sample_pids)
    ):
        errors.append("collector-ready.json: sample_pids differ from the readiness resource sample")
    baseline_pids = ready.get("external_zero_baseline_pids")
    if (
        not isinstance(baseline_pids, list)
        or any(type(pid) is not int or pid <= 0 for pid in baseline_pids)
        or baseline_pids != sorted(set(baseline_pids))
        or not set(baseline_pids).issubset(ready_sample_pids)
    ):
        errors.append("collector-ready.json: external_zero_baseline_pids are malformed or unsampled")
        baseline_pid_set: set[int] = set()
    else:
        baseline_pid_set = set(baseline_pids)
    if ready.get("extra_pids") != configured_extra or not set(configured_extra).issubset(baseline_pid_set):
        errors.append("collector-ready.json: extra PID baseline differs from the manifest")
    ready_containers = ready.get("containers")
    if not isinstance(ready_containers, dict) or set(ready_containers) != expected_names:
        errors.append("collector-ready.json: container keys differ from the manifest")
    else:
        for name in requested:
            identity = container_identity_tuple(
                ready_containers.get(name),
                f"collector-ready.json: containers[{name!r}]",
                errors,
            )
            if identity is not None:
                if normalized_unique.get(name) != identity:
                    errors.append(f"collector-ready.json: {name} identity differs from final unique identity")
                if identity[1] not in ready_sample_pids or identity[1] not in baseline_pid_set:
                    errors.append(f"collector-ready.json: {name} PID lacks a zero-baseline readiness sample")


def parse_utc_timestamp(value: object, context: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str) or not value:
        errors.append(f"{context}: timestamp is missing")
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        errors.append(f"{context}: timestamp is malformed")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{context}: timestamp lacks a timezone")
        return None
    return parsed.astimezone(timezone.utc)


def verify_batch_file_ref(reference: object, context: str, errors: list[str]) -> Path | None:
    if not isinstance(reference, dict):
        errors.append(f"{context}: file reference is missing")
        return None
    raw_path = reference.get("path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        errors.append(f"{context}: path is not absolute")
        return None
    path = Path(raw_path).resolve()
    if not path.is_file():
        errors.append(f"{context}: file is missing")
        return None
    if reference.get("kind") != "file" or reference.get("exists") is not True:
        errors.append(f"{context}: manifest file classification drift")
    if reference.get("size_bytes") != path.stat().st_size:
        errors.append(f"{context}: size changed")
    digest = sha256_file(path)
    if reference.get("sha256") != digest:
        errors.append(f"{context}: SHA-256 changed")
    return path


def evidence_file_ref(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def validate_batch_integrity_guard(
    run_dir: Path,
    manifest: dict[str, Any],
    execution: dict[str, Any],
    collector_ready: dict[str, Any],
    errors: list[str],
) -> dict[str, Any] | None:
    batch = manifest.get("batch_gate")
    guard_dir = run_dir / "integrity-guard"
    release_path = run_dir / "command-release.json"
    batch_execution_fields = {
        "command_release_at_utc",
        "command_ended_at_utc",
        "guard_exit_code",
    }
    if batch is None:
        if guard_dir.exists() or release_path.exists() or any(
            field in execution for field in batch_execution_fields
        ):
            errors.append("legacy P31 run contains undeclared batch-guard evidence")
        return None
    if not isinstance(batch, dict) or set(batch) != {
        "protocol_version",
        "consumer",
        "lease",
        "gate_tool",
        "anchor_binary",
        "integrity_guard_required",
    }:
        errors.append("run-manifest.json: batch_gate schema drift")
        return {}
    if (
        batch.get("protocol_version") != "short-clean-window-v2"
        or batch.get("consumer") not in {"P10", "P20"}
        or batch.get("integrity_guard_required") is not True
    ):
        errors.append("run-manifest.json: batch_gate policy drift")
    lease_path = verify_batch_file_ref(batch.get("lease"), "batch lease", errors)
    gate_tool = verify_batch_file_ref(batch.get("gate_tool"), "batch gate tool", errors)
    anchor_binary = verify_batch_file_ref(
        batch.get("anchor_binary"), "batch anchor binary", errors
    )
    if execution.get("guard_exit_code") != 0:
        errors.append(
            f"integrity guard exit code is {execution.get('guard_exit_code')!r}, expected 0"
        )
    admission: dict[str, Any] = {}
    if lease_path is not None and gate_tool is not None:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(gate_tool),
                "validate-guard",
                "--guard-dir",
                str(guard_dir),
                "--lease",
                str(lease_path),
                "--expected-repo-head",
                str(manifest.get("repo", {}).get("git_sha", "")),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            errors.append(
                "batch gate rejected integrity guard: {}".format(completed.stderr.strip())
            )
        else:
            try:
                parsed = json.loads(completed.stdout)
                if not isinstance(parsed, dict) or parsed.get("state") != "PASS":
                    raise ValueError("admission is not a PASS object")
                admission = parsed
            except (ValueError, json.JSONDecodeError) as exc:
                errors.append(f"batch gate emitted invalid guard admission: {exc}")
    release = read_json(release_path, errors)
    guard_ready = read_json(guard_dir / "READY.json", errors)
    guard_status = read_json(guard_dir / "status.json", errors)
    if (
        release.get("schema_version") != "cidr-command-release-v2"
        or release.get("state") != "RELEASED"
    ):
        errors.append("command-release.json: schema/state drift")
    if lease_path is not None and admission.get("lease_sha256") != sha256_file(lease_path):
        errors.append("integrity guard admission lease SHA differs from batch lease")
    if admission.get("guard_dir") and Path(str(admission["guard_dir"])).resolve() != guard_dir.resolve():
        errors.append("integrity guard admission points to another directory")
    if anchor_binary is not None and guard_status.get("binary_sha256") != sha256_file(anchor_binary):
        errors.append("integrity guard anchor binary SHA drift")

    release_raw = release.get("released_at_utc")
    if execution.get("command_release_at_utc") != release_raw:
        errors.append("execution command release timestamp differs from release artifact")
    started = parse_utc_timestamp(execution.get("started_at_utc"), "execution start", errors)
    collector_ready_at = parse_utc_timestamp(
        collector_ready.get("ready_at_utc"), "collector READY", errors
    )
    guard_ready_at = parse_utc_timestamp(
        guard_ready.get("ready_at_utc"), "integrity guard READY", errors
    )
    released_at = parse_utc_timestamp(release_raw, "command release", errors)
    command_ended_at = parse_utc_timestamp(
        execution.get("command_ended_at_utc"), "command end", errors
    )
    guard_ended_at = parse_utc_timestamp(
        guard_status.get("ended_at_utc"), "integrity guard end", errors
    )
    if all(
        value is not None
        for value in (
            started,
            collector_ready_at,
            guard_ready_at,
            released_at,
            command_ended_at,
            guard_ended_at,
        )
    ):
        assert started is not None
        assert collector_ready_at is not None
        assert guard_ready_at is not None
        assert released_at is not None
        assert command_ended_at is not None
        assert guard_ended_at is not None
        if not (
            started <= collector_ready_at <= released_at
            and started <= guard_ready_at <= released_at
            and released_at <= command_ended_at <= guard_ended_at
        ):
            errors.append(
                "dual READY/command/guard boundary coverage is incomplete"
            )

    evidence_paths = {
        "release": release_path,
        "ready": guard_dir / "READY.json",
        "status": guard_dir / "status.json",
        "samples": guard_dir / "integrity-samples.tsv",
    }
    evidence: dict[str, Any] = {}
    for name, path in evidence_paths.items():
        if path.is_file():
            evidence[name] = evidence_file_ref(path)
    lease_reference = batch.get("lease") if isinstance(batch.get("lease"), dict) else {}
    tool_reference = batch.get("gate_tool") if isinstance(batch.get("gate_tool"), dict) else {}
    anchor_reference = (
        batch.get("anchor_binary") if isinstance(batch.get("anchor_binary"), dict) else {}
    )
    return {
        "schema_version": "cidr-p31-batch-integrity-v2",
        "state": "PASS" if not errors else "FAILED",
        "consumer": batch.get("consumer"),
        "lease_sha256": lease_reference.get("sha256"),
        "gate_tool_sha256": tool_reference.get("sha256"),
        "anchor_binary_sha256": anchor_reference.get("sha256"),
        "admission": admission,
        "evidence": evidence,
        "coverage": {
            "collector_ready_at_utc": collector_ready.get("ready_at_utc"),
            "guard_ready_at_utc": guard_ready.get("ready_at_utc"),
            "command_release_at_utc": release_raw,
            "command_ended_at_utc": execution.get("command_ended_at_utc"),
            "guard_ended_at_utc": guard_status.get("ended_at_utc"),
        },
    }


def validate_run(run_dir: Path, min_samples_override: int | None = None) -> tuple[bool, dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    manifest_path = run_dir / "run-manifest.json"
    manifest = read_json(manifest_path, errors)
    execution = read_json(run_dir / "execution.json", errors)
    collector = read_json(run_dir / "collector-status.json", errors)
    ready = read_json(run_dir / "collector-ready.json", errors)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append("run-manifest.json: wrong schema_version")
    if manifest.get("resource_schema_version") != RESOURCE_SCHEMA_VERSION:
        errors.append("run-manifest.json: wrong resource_schema_version")
    if execution.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append("execution.json: wrong schema_version")
    if collector.get("schema_version") != RESOURCE_SCHEMA_VERSION:
        errors.append("collector-status.json: wrong schema_version")
    if execution.get("command_exit_code") != 0:
        errors.append(f"command exit code is {execution.get('command_exit_code')!r}, expected 0")
    if execution.get("collector_exit_code") != 0:
        errors.append(f"collector exit code is {execution.get('collector_exit_code')!r}, expected 0")
    if execution.get("wrapper_signal") is not None:
        errors.append(f"wrapper was interrupted by signal {execution.get('wrapper_signal')!r}")
    if collector.get("state") != "DONE" or collector.get("errors"):
        errors.append(f"collector-status.json is not clean DONE: state={collector.get('state')!r}")
    if not collector.get("root_seen_alive"):
        errors.append("collector-status.json: root process was never seen alive")

    min_samples = min_samples_override or int(manifest.get("collector", {}).get("min_samples", 2))
    resources = read_tsv(run_dir / "resource-samples.tsv", RESOURCE_COLUMNS, errors)
    disks = read_tsv(run_dir / "disk-samples.tsv", DISK_COLUMNS, errors)
    iostat_rows = read_tsv(run_dir / "iostat-samples.tsv", IOSTAT_COLUMNS, errors)
    terminal_censor_guard_status: dict[str, Any] = {}
    if (
        _terminal_censor_policy_enabled(manifest)
        and manifest.get("performance_eligible_declared") is True
    ):
        terminal_censor_guard_status = read_json(
            run_dir / "integrity-guard" / "status.json", errors
        )
    terminal_censor_receipt, terminal_censor_reason = classify_terminal_censor_policy(
        manifest,
        execution,
        collector,
        ready,
        terminal_censor_guard_status,
        resources,
    )
    validate_resource_rows(
        resources,
        min_samples,
        bool(manifest.get("performance_eligible_declared")),
        errors,
        warnings,
        terminal_censor_receipt,
    )
    disk_summary = validate_disk_rows(disks, manifest, errors)
    expected_device = str(manifest.get("collector", {}).get("device", ""))
    expected_mount = str(manifest.get("collector", {}).get("data_mount", ""))
    validate_iostat_rows(iostat_rows, expected_device, errors, warnings)

    expected_root_pid = execution.get("root_pid")
    for source, actual in (
        ("run-manifest.json", manifest.get("root_pid")),
        ("collector-status.json", collector.get("root_pid")),
    ):
        if actual != expected_root_pid:
            errors.append(f"{source}: root_pid differs from execution.json")
    if resources and any(row.get("root_pid") != str(expected_root_pid) for row in resources):
        errors.append("resource-samples.tsv: root_pid differs from execution.json")
    if resources and any(row.get("device") != expected_device for row in resources):
        errors.append("resource-samples.tsv: device differs from manifest")
    if resources and any(row.get("data_mount") != expected_mount for row in resources):
        errors.append("resource-samples.tsv: data_mount differs from manifest")
    if collector.get("device") != expected_device:
        errors.append("collector-status.json: device differs from manifest")
    if collector.get("data_mount") != expected_mount:
        errors.append("collector-status.json: data mount differs from manifest")
    for key in ("interval_s", "disk_interval_s"):
        manifest_key = "interval_s" if key == "interval_s" else "disk_interval_s"
        if collector.get(key) != manifest.get("collector", {}).get(manifest_key):
            errors.append(f"collector-status.json: {key} differs from manifest")
    for key, actual in (
        ("resource_samples", len(resources)),
        ("disk_samples", len(disks)),
        ("iostat_samples", len(iostat_rows)),
    ):
        if collector.get(key) != actual:
            errors.append(f"collector-status.json: {key} does not match artifact rows")
    try:
        disk_rounds = len({int(row["sample_index"]) for row in disks})
    except (KeyError, TypeError, ValueError):
        disk_rounds = -1
    if collector.get("disk_rounds") != disk_rounds:
        errors.append("collector-status.json: disk_rounds does not match artifact rows")
    expected_roots = manifest.get("disk_roots", [])
    if collector.get("stores") != expected_roots:
        errors.append("collector-status.json: disk roots differ from manifest")
    collector_config = manifest.get("collector", {})
    if collector.get("containers") != collector_config.get("containers"):
        errors.append("collector-status.json: containers differ from manifest")
    if collector.get("extra_pids") != collector_config.get("extra_pids"):
        errors.append("collector-status.json: extra_pids differ from manifest")
    requested_extra_pids = set(collector_config.get("extra_pids", []))
    seen_extra_pids = set(collector.get("extra_pids_seen", []))
    if requested_extra_pids != seen_extra_pids:
        errors.append(
            f"collector-status.json: extra PIDs never observed: {sorted(requested_extra_pids - seen_extra_pids)!r}"
        )
    validate_container_tracking(
        collector_config,
        collector,
        ready,
        resources,
        expected_root_pid,
        errors,
    )
    validate_process_tracking(collector, resources, errors)

    require_aux = bool(manifest.get("collector", {}).get("require_aux_tools", True))
    if require_aux:
        pidstat_path = run_dir / "pidstat.raw"
        if not collector.get("pidstat_started") or not pidstat_path.is_file() or pidstat_path.stat().st_size == 0:
            errors.append("pidstat auxiliary artifact missing or empty")
        elif not pidstat_has_pid_sample(
            pidstat_path.read_text(encoding="utf-8", errors="replace"), int(expected_root_pid)
        ):
            errors.append("pidstat auxiliary artifact has no root-PID sample")
        if not collector.get("iostat_started"):
            errors.append("iostat auxiliary collector did not start")
        if not iostat_rows:
            errors.append("iostat-samples.tsv has no parsed nvme samples")
    elif not iostat_rows:
        warnings.append("iostat samples unavailable; allowed only because require_aux_tools=false")

    command_path = Path(manifest.get("command", {}).get("path", ""))
    if not command_path.is_file() or sha256_file(command_path) != manifest.get("command", {}).get("sha256"):
        errors.append("command.txt missing or SHA-256 mismatch")
    for name in ("wrapper", "manifest_tool", "collector", "validator"):
        ref = manifest.get("harness", {}).get(name, {})
        path = Path(ref.get("path", ""))
        if not path.is_file() or sha256_file(path) != ref.get("sha256"):
            errors.append(f"{name} script missing or SHA-256 mismatch")
    validate_formal_provenance(manifest, errors)
    integrity_guard = validate_batch_integrity_guard(
        run_dir, manifest, execution, ready, errors
    )

    terminal_censor_quality = build_terminal_censor_quality(
        manifest, terminal_censor_receipt, terminal_censor_reason
    )
    resource_summary = {
        "samples": len(resources),
        "sampled_duration_s": max_number(resources, "monotonic_s"),
        "peak_process_count": int(max_number(resources, "process_count")),
        "process_user_cpu_s": max_number(resources, "process_user_cpu_s"),
        "process_sys_cpu_s": max_number(resources, "process_sys_cpu_s"),
        "peak_process_cpu_pct": max_number(resources, "process_cpu_pct"),
        "peak_rss_bytes": int(max_number(resources, "process_rss_bytes")),
        "peak_pss_bytes": int(max_number(resources, "process_pss_bytes")),
        "process_read_bytes": int(max_number(resources, "process_read_bytes")),
        "process_write_bytes": int(max_number(resources, "process_write_bytes")),
        "process_cancelled_write_bytes": int(max_number(resources, "process_cancelled_write_bytes")),
        "process_rchar": int(max_number(resources, "process_rchar")),
        "process_wchar": int(max_number(resources, "process_wchar")),
        "mean_process_cpu_pct": time_weighted_mean(resources, "process_cpu_pct"),
        "peak_process_read_mib_s": max_number(resources, "process_read_mib_s"),
        "peak_process_write_mib_s": max_number(resources, "process_write_mib_s"),
        "peak_device_read_mib_s": max_number(resources, "device_read_mib_s"),
        "peak_device_write_mib_s": max_number(resources, "device_write_mib_s"),
        "peak_device_util_pct": max_number(resources, "device_util_pct"),
        "max_device_await_ms": max_number(resources, "device_await_ms"),
        "mean_device_read_mib_s": time_weighted_mean(resources, "device_read_mib_s"),
        "mean_device_write_mib_s": time_weighted_mean(resources, "device_write_mib_s"),
        "mean_device_util_pct": time_weighted_mean(resources, "device_util_pct"),
        "mean_device_await_ms": time_weighted_mean(resources, "device_await_ms"),
        "max_host_load1": max_number(resources, "host_load1"),
        "min_host_mem_available_bytes": int(min_number(resources, "host_mem_available_bytes")),
        "min_data_free_bytes": int(min_number(resources, "data_free_bytes")),
        "terminal_censor_quality": terminal_censor_quality,
    }
    artifacts: dict[str, Any] = {}
    for name in ARTIFACT_FILES:
        path = run_dir / name
        if path.is_file():
            artifacts[name] = {"size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
        else:
            errors.append(f"missing required artifact: {name}")

    manifest["collector_result"] = {
        "container_identity_schema_version": collector.get("container_identity_schema_version"),
        "ready": ready,
        "containers_seen": collector.get("containers_seen"),
        "container_identity_history": collector.get("container_identity_history"),
        "container_identity_unique_set": collector.get("container_identity_unique_set"),
        "process_identity_schema_version": collector.get("process_identity_schema_version"),
        "process_identity_unique_set": collector.get("process_identity_unique_set"),
        "status_artifact": {
            "path": str((run_dir / "collector-status.json").resolve()),
            **artifacts.get("collector-status.json", {}),
        },
        "ready_artifact": {
            "path": str((run_dir / "collector-ready.json").resolve()),
            **artifacts.get("collector-ready.json", {}),
        },
    }

    passed = not errors
    validation = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "state": "PASS" if passed else "FAILED",
        "validated_at_utc": utc_now(),
        "errors": errors,
        "warnings": warnings,
        "resource_summary": resource_summary,
        "resource_quality": terminal_censor_quality,
        "disk_summary": disk_summary,
        "iostat_samples": len(iostat_rows),
    }
    if terminal_censor_receipt is not None:
        validation["terminal_censor_receipt"] = terminal_censor_receipt
    if integrity_guard is not None:
        integrity_guard["state"] = "PASS" if not errors else "FAILED"
        validation["integrity_guard"] = integrity_guard
    atomic_json(run_dir / "validation.json", validation)
    manifest["state"] = "PASS" if passed else "FAILED_VALIDATION"
    manifest["artifacts"] = artifacts
    manifest["summary"] = {
        "resources": resource_summary,
        "disk": disk_summary,
        "resource_quality": terminal_censor_quality,
    }
    if terminal_censor_receipt is not None:
        manifest["summary"]["terminal_censor_receipt"] = terminal_censor_receipt
    if integrity_guard is not None:
        manifest["summary"]["integrity_guard"] = integrity_guard
    manifest["validation"] = validation
    atomic_json(manifest_path, manifest)

    if passed:
        if (run_dir / "FAILED").exists():
            errors.append("stale FAILED marker exists; refusing PASS")
            passed = False
            validation["state"] = "FAILED"
            validation["errors"] = errors
            manifest["state"] = "FAILED_VALIDATION"
            manifest["validation"] = validation
            atomic_json(run_dir / "validation.json", validation)
            atomic_json(manifest_path, manifest)
        else:
            atomic_json(
                run_dir / "DONE",
                {
                    "state": "PASS",
                    "validated_at_utc": validation["validated_at_utc"],
                    "manifest_sha256": sha256_file(manifest_path),
                    "validation_sha256": sha256_file(run_dir / "validation.json"),
                },
            )
    if not passed:
        if (run_dir / "DONE").exists():
            (run_dir / "DONE").unlink()
        atomic_json(run_dir / "FAILED", {"state": "FAILED", "validated_at_utc": utc_now(), "errors": errors})
    return passed, validation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--min-samples", type=int)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    try:
        passed, validation = validate_run(run_dir, args.min_samples)
    except Exception as exc:  # noqa: BLE001 - a fatal validator error must leave FAILED, never DONE
        failure = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "state": "FAILED",
            "validated_at_utc": utc_now(),
            "errors": [f"fatal validator exception: {type(exc).__name__}: {exc}"],
            "warnings": [],
        }
        run_dir.mkdir(parents=True, exist_ok=True)
        done = run_dir / "DONE"
        if done.exists():
            done.unlink()
        atomic_json(run_dir / "validation.json", failure)
        atomic_json(
            run_dir / "FAILED",
            {"state": "FAILED", "validated_at_utc": failure["validated_at_utc"], "errors": failure["errors"]},
        )
        print(json.dumps(failure, indent=2, sort_keys=True))
        return 2
    print(json.dumps(validation, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
