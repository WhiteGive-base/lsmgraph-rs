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
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from resource_schema import (
    ARTIFACT_FILES,
    DISK_CATEGORIES,
    DISK_COLUMNS,
    IOSTAT_COLUMNS,
    MANIFEST_SCHEMA_VERSION,
    RESOURCE_COLUMNS,
    RESOURCE_SCHEMA_VERSION,
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


def validate_resource_rows(
    rows: list[dict[str, str]],
    min_samples: int,
    strict: bool,
    errors: list[str],
    warnings: list[str],
) -> None:
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
            if strict:
                errors.append(message)
            else:
                warnings.append(message)
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
    rows: list[dict[str, str]], expected_device: str, errors: list[str]
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
            if float(row["util_pct"]) > 100.5:
                errors.append(f"{context}: util_pct is above 100%")
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
        if not ref.get("path"):
            continue
        path = Path(ref["path"])
        if ref.get("kind") == "directory" and not path.is_dir():
            errors.append(f"formal run {name} directory disappeared since launch")
        if ref.get("kind") == "file" and (
            not path.is_file() or not ref.get("sha256") or sha256_file(path) != ref["sha256"]
        ):
            errors.append(f"formal run {name} file is missing or changed since launch")


def validate_run(run_dir: Path, min_samples_override: int | None = None) -> tuple[bool, dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    manifest_path = run_dir / "run-manifest.json"
    manifest = read_json(manifest_path, errors)
    execution = read_json(run_dir / "execution.json", errors)
    collector = read_json(run_dir / "collector-status.json", errors)
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
    validate_resource_rows(
        resources,
        min_samples,
        bool(manifest.get("performance_eligible_declared")),
        errors,
        warnings,
    )
    disk_summary = validate_disk_rows(disks, manifest, errors)
    expected_device = str(manifest.get("collector", {}).get("device", ""))
    expected_mount = str(manifest.get("collector", {}).get("data_mount", ""))
    validate_iostat_rows(iostat_rows, expected_device, errors)

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
    requested_containers = set(collector_config.get("containers", []))
    resolved_containers = set(collector.get("containers_seen", {}))
    if requested_containers != resolved_containers:
        errors.append(
            f"collector-status.json: containers never resolved: {sorted(requested_containers - resolved_containers)!r}"
        )
    requested_extra_pids = set(collector_config.get("extra_pids", []))
    seen_extra_pids = set(collector.get("extra_pids_seen", []))
    if requested_extra_pids != seen_extra_pids:
        errors.append(
            f"collector-status.json: extra PIDs never observed: {sorted(requested_extra_pids - seen_extra_pids)!r}"
        )

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
    }
    artifacts: dict[str, Any] = {}
    for name in ARTIFACT_FILES:
        path = run_dir / name
        if path.is_file():
            artifacts[name] = {"size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
        else:
            errors.append(f"missing required artifact: {name}")

    passed = not errors
    validation = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "state": "PASS" if passed else "FAILED",
        "validated_at_utc": utc_now(),
        "errors": errors,
        "warnings": warnings,
        "resource_summary": resource_summary,
        "disk_summary": disk_summary,
        "iostat_samples": len(iostat_rows),
    }
    atomic_json(run_dir / "validation.json", validation)
    manifest["state"] = "PASS" if passed else "FAILED_VALIDATION"
    manifest["artifacts"] = artifacts
    manifest["summary"] = {"resources": resource_summary, "disk": disk_summary}
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
