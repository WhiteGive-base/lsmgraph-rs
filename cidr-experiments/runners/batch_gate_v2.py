#!/usr/bin/env python3
"""Short clean-window and 24-hour batch-lease protocol for CIDR runs.

This module is deliberately additive.  It does not reinterpret or rewrite a
v1 P03/P02B artifact.  A v1 clean-window may seed a v2 lease only when it still
contains the original >=15-sample evidence; a short five-sample window must be
validated as v2, including its timestamp-gap checks.

The implementation uses Python 3.8 syntax so that it can run on the formal
Linux host without changing the benchmark environment.
"""

from __future__ import print_function

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import platform
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    import pwd as _pwd
except ImportError:  # pragma: no cover - local Windows staging only
    _pwd = None  # type: ignore


LEASE_SCHEMA = "cidr-batch-lease-v2"
LEASE_MARKER_SCHEMA = "cidr-batch-lease-marker-v2"
P03_BINDING_SCHEMA = "p02b-clean-ready-binding-v2"
GUARD_SCHEMA = "cidr-p31-integrity-guard-v2"
P02B_RESULT_SCHEMA = "p02b-sf10-sentinel-result-v2"
P02B_PROVENANCE_SCHEMA = "p02b-sentinel-provenance-v1"
P02B_GATE_CONTRACT_SCHEMA = "p02b-gate-contract-v1"
P02B_GATE_METHOD = "quantization-aware-tail-v1"
LEASE_DURATION_SECONDS = 24 * 60 * 60
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_HEAD_RE = re.compile(r"^[0-9a-f]{40,64}$")
P03_REQUIRED_COLUMNS = {
    "timestamp",
    "sample",
    "metric_pass",
    "service_pass",
    "sample_pass",
    "streak",
    "reasons",
    "gate_mode",
}
GUARD_COLUMNS = [
    "timestamp_utc",
    "sample_index",
    "monotonic_s",
    "gap_s",
    "lease_remaining_s",
    "repo_head",
    "repo_dirty",
    "binary_size_bytes",
    "binary_mtime_ns",
    "contamination_count",
    "contamination_pids",
    "sample_pass",
    "reasons",
]
P02B_GATE_CONTRACT_KEYS = {
    "schema_version",
    "method",
    "quantile",
    "tail_bounds_us",
    "sigma_multiplier",
    "qps_cv_max",
    "mean_storage_latency_cv_max",
    "require_zero_overflow",
    "stability_result",
    "tools",
    "contract_sha256",
}
P02B_GATE_TOOL_FILENAMES = {
    "extract_run_metrics": "extract_run_metrics.py",
    "calculate_stability": "calculate_stability.py",
    "validate_sentinel_result": "validate_sentinel_result.py",
}


class GateError(ValueError):
    """A fail-closed protocol violation."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_timestamp(value: Any, context: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise GateError("{} timestamp is missing".format(context))
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise GateError("{} has an invalid ISO-8601 timestamp".format(context)) from exc
    if parsed.tzinfo is None:
        raise GateError("{} timestamp must include a timezone".format(context))
    return parsed.astimezone(dt.timezone.utc)


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    value = {}  # type: Dict[str, Any]
    for key, item in pairs:
        if key in value:
            raise GateError("duplicate JSON key: {}".format(key))
        value[key] = item
    return value


def read_json(path: Path, context: str) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateError("cannot read {} {}: {}".format(context, path, exc)) from exc
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, GateError) as exc:
        raise GateError("cannot parse {} {}: {}".format(context, path, exc)) from exc
    if not isinstance(value, dict):
        raise GateError("{} must contain one JSON object".format(context))
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp.{}".format(os.getpid()))
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(str(temporary), str(path))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_hex64(value: Any, context: str) -> str:
    if not isinstance(value, str) or HEX64_RE.fullmatch(value) is None:
        raise GateError("{} must be a lowercase SHA-256".format(context))
    return value


def canonical_sha256(value: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def canonical_file(path: Path, context: str, executable: bool = False) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise GateError("{} is not a file: {}".format(context, resolved))
    if executable and not os.access(str(resolved), os.X_OK):
        raise GateError("{} is not executable: {}".format(context, resolved))
    return resolved


def canonical_dir(path: Path, context: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise GateError("{} is not a directory: {}".format(context, resolved))
    return resolved


def file_ref(path: Path, include_stat_identity: bool = False) -> Dict[str, Any]:
    resolved = canonical_file(path, "artifact")
    stat = resolved.stat()
    result = {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "sha256": sha256_file(resolved),
    }
    if include_stat_identity:
        result["mtime_ns"] = stat.st_mtime_ns
    return result


def verify_file_ref(reference: Any, context: str, include_stat_identity: bool = False) -> Path:
    if not isinstance(reference, dict):
        raise GateError("{} reference is not an object".format(context))
    raw_path = reference.get("path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise GateError("{} path is not absolute".format(context))
    path = canonical_file(Path(raw_path), context)
    stat = path.stat()
    if reference.get("size_bytes") != stat.st_size:
        raise GateError("{} size changed".format(context))
    if sha256_file(path) != require_hex64(reference.get("sha256"), "{} SHA".format(context)):
        raise GateError("{} SHA-256 changed".format(context))
    if include_stat_identity and reference.get("mtime_ns") != stat.st_mtime_ns:
        raise GateError("{} mtime changed".format(context))
    return path


def parse_env_file(path: Path) -> Dict[str, str]:
    result = {}  # type: Dict[str, str]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GateError("cannot read env marker {}: {}".format(path, exc)) from exc
    for index, line in enumerate(lines, start=1):
        if not line or "=" not in line:
            raise GateError("{}:{} is not KEY=VALUE".format(path, index))
        key, value = line.split("=", 1)
        if not key or key in result:
            raise GateError("{}:{} has an empty/duplicate key".format(path, index))
        result[key] = value
    return result


def read_tsv(path: Path, required: Set[str]) -> Tuple[List[str], List[Dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames is None:
                raise GateError("{} has no TSV header".format(path))
            if len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise GateError("{} has duplicate TSV columns".format(path))
            missing = sorted(required - set(reader.fieldnames))
            if missing:
                raise GateError("{} is missing columns: {}".format(path, missing))
            rows = list(reader)
    except OSError as exc:
        raise GateError("cannot read TSV {}: {}".format(path, exc)) from exc
    if not rows:
        raise GateError("{} has no rows".format(path))
    return list(reader.fieldnames), rows


def _int(mapping: Dict[str, str], key: str, context: str) -> int:
    try:
        return int(mapping[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise GateError("{} has invalid integer {}".format(context, key)) from exc


def git_facts(repo_root: Path) -> Dict[str, Any]:
    repo = canonical_dir(repo_root, "repository")

    def output(arguments: Sequence[str]) -> str:
        completed = subprocess.run(
            list(arguments),
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            raise GateError(
                "git command failed ({}): {}".format(" ".join(arguments), completed.stderr.strip())
            )
        return completed.stdout.strip()

    head = output(["git", "rev-parse", "HEAD"])
    if GIT_HEAD_RE.fullmatch(head) is None:
        raise GateError("current Git HEAD is malformed")
    status = output(["git", "status", "--porcelain=v1", "--untracked-files=normal"])
    return {
        "root": str(repo),
        "head": head,
        "dirty": bool(status),
        "status_lines": status.splitlines(),
    }


def _command_version(arguments: Sequence[str]) -> Tuple[bool, str]:
    try:
        value = subprocess.check_output(
            list(arguments), stderr=subprocess.DEVNULL, text=True, timeout=10
        ).strip()
        return True, value
    except (OSError, subprocess.SubprocessError):
        return False, ""


def host_facts() -> Dict[str, Any]:
    """Use the exact P31 host-fingerprint algorithm."""
    cpu_model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem_total = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    pidstat_ok, pidstat_version = _command_version(["pidstat", "-V"])
    iostat_ok, iostat_version = _command_version(["iostat", "-V"])
    facts = {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu_model": cpu_model,
        "logical_cpu_count": os.cpu_count(),
        "mem_total_bytes": mem_total,
        "pidstat_version_command_ok": pidstat_ok,
        "pidstat_version": pidstat_version,
        "iostat_version_command_ok": iostat_ok,
        "iostat_version": iostat_version,
    }
    facts["fingerprint_sha256"] = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return facts


def host_summary(facts: Dict[str, Any]) -> Dict[str, str]:
    hostname = facts.get("hostname")
    fingerprint = facts.get("fingerprint_sha256")
    if not isinstance(hostname, str) or not hostname:
        raise GateError("host hostname is missing")
    require_hex64(fingerprint, "host fingerprint")
    return {"hostname": hostname, "fingerprint_sha256": fingerprint}


def validate_p03_ready_v2(
    ready_path: Path,
    expected_repo_head: str,
    expected_hostname: str,
    now: Optional[dt.datetime] = None,
    max_age_seconds: float = 300.0,
    minimum_samples: int = 5,
    expected_interval_seconds: int = 60,
    gap_tolerance_seconds: float = 15.0,
) -> Dict[str, Any]:
    """Validate five one-minute samples, while retaining valid 15/15 history."""
    if minimum_samples != 5 or expected_interval_seconds != 60:
        raise GateError("formal v2 clean window is frozen at 5 x 60 seconds")
    if max_age_seconds <= 0 or gap_tolerance_seconds < 0:
        raise GateError("invalid v2 freshness/gap limits")
    if GIT_HEAD_RE.fullmatch(expected_repo_head) is None:
        raise GateError("expected repository HEAD is malformed")
    ready = canonical_file(ready_path, "P03 READY")
    run_dir = ready.parent
    p03_dir = run_dir.parent.parent
    artifacts = {
        "READY": ready,
        "COMPLETE": run_dir / "COMPLETE",
        "classification.env": run_dir / "classification.env",
        "STATE": run_dir / "STATE",
        "samples.tsv": run_dir / "samples.tsv",
        "latest.tsv": run_dir / "latest.tsv",
        "monitor_clean_window.sh": p03_dir / "monitor_clean_window.sh",
    }
    for name, path in artifacts.items():
        artifacts[name] = canonical_file(path, "P03 {}".format(name))
    if (run_dir / "STOPPED").exists():
        raise GateError("P03 run has a STOPPED marker")

    ready_env = parse_env_file(artifacts["READY"])
    classification = parse_env_file(artifacts["classification.env"])
    state = parse_env_file(artifacts["STATE"])
    complete = parse_env_file(artifacts["COMPLETE"])
    if ready_env.get("readiness_gate") != "PASS":
        raise GateError("P03 READY does not declare PASS")
    for source, value in (("READY", ready_env), ("classification", classification), ("STATE", state)):
        if value.get("performance_eligible") != "false":
            raise GateError("P03 {} may not claim performance eligibility".format(source))
    if ready_env.get("gate_mode") != "seml0" or classification.get("gate_mode") != "seml0":
        raise GateError("formal P03 gate_mode must be seml0")
    if classification.get("purpose") != "clean_window_readiness_only":
        raise GateError("P03 classification purpose drift")
    run_id = ready_env.get("run_id", "")
    if not run_id or classification.get("run_id") != run_id or complete.get("run_id") != run_id:
        raise GateError("P03 run identity is inconsistent")
    if classification.get("git_head") != expected_repo_head:
        raise GateError("P03 HEAD differs from the P02B start HEAD")
    if classification.get("host") != expected_hostname:
        raise GateError("P03 host differs from the P02B start host")
    if classification.get("script_sha256") != sha256_file(artifacts["monitor_clean_window.sh"]):
        raise GateError("P03 monitor script changed")
    declared_latest = ready_env.get("latest_sample", "")
    if not declared_latest or Path(declared_latest).resolve() != artifacts["latest.tsv"].resolve():
        raise GateError("P03 READY does not bind adjacent latest.tsv")

    configured = _int(classification, "ready_samples", "P03 classification")
    interval = _int(classification, "sample_interval_seconds", "P03 classification")
    ready_passes = _int(ready_env, "consecutive_passes", "P03 READY")
    ready_count = _int(ready_env, "samples", "P03 READY")
    state_streak = _int(state, "streak", "P03 STATE")
    state_required = _int(state, "required_streak", "P03 STATE")
    if interval != expected_interval_seconds:
        raise GateError("P03 v2 sample interval must be 60 seconds")
    if configured < minimum_samples or state_required != configured:
        raise GateError("P03 v2 requires at least five configured samples")
    if ready_passes < configured or state_streak < configured:
        raise GateError("P03 clean streak is shorter than its configured window")
    if state.get("sample_pass") != "1" or state.get("reasons") != "none":
        raise GateError("P03 final STATE is not clean")

    header, rows = read_tsv(artifacts["samples.tsv"], P03_REQUIRED_COLUMNS)
    latest_header, latest_rows = read_tsv(artifacts["latest.tsv"], P03_REQUIRED_COLUMNS)
    if header != latest_header or len(latest_rows) != 1 or latest_rows[0] != rows[-1]:
        raise GateError("P03 latest.tsv is not the exact final sample")
    if _int(rows[-1], "sample", "P03 final sample") != ready_count:
        raise GateError("P03 READY sample count differs from samples.tsv")
    tail = rows[-configured:]
    if len(tail) != configured:
        raise GateError("P03 samples.tsv lacks the configured passing tail")

    timestamps = []  # type: List[dt.datetime]
    prior_number = None  # type: Optional[int]
    for index, row in enumerate(tail, start=1):
        if (
            row.get("metric_pass") != "1"
            or row.get("service_pass") != "1"
            or row.get("sample_pass") != "1"
            or row.get("reasons") != "none"
            or row.get("gate_mode") != "seml0"
        ):
            raise GateError("P03 passing-tail row {} is contaminated".format(index))
        number = _int(row, "sample", "P03 passing tail")
        if prior_number is not None and number != prior_number + 1:
            raise GateError("P03 sample numbers contain a gap")
        prior_number = number
        timestamps.append(parse_timestamp(row.get("timestamp"), "P03 sample"))

    lower = expected_interval_seconds - gap_tolerance_seconds
    upper = expected_interval_seconds + gap_tolerance_seconds
    gaps = []  # type: List[float]
    for left, right in zip(timestamps, timestamps[1:]):
        gap = (right - left).total_seconds()
        gaps.append(gap)
        if gap < lower:
            raise GateError("P03 samples are too close together: {:.3f}s".format(gap))
        if gap > upper:
            raise GateError("P03 sampling gap exceeds v2 limit: {:.3f}s".format(gap))

    ready_time = parse_timestamp(ready_env.get("ready_time"), "P03 READY")
    if ready_time < timestamps[-1]:
        raise GateError("P03 READY predates the final passing sample")
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc)
    age = (current - ready_time).total_seconds()
    if age < -60 or age > max_age_seconds:
        raise GateError("P03 READY freshness failed: age={:.3f}s".format(age))

    return {
        "schema_version": P03_BINDING_SCHEMA,
        "state": "PASS",
        "protocol_version": "short-clean-window-v2",
        "run_id": run_id,
        "ready_time": ready_env["ready_time"],
        "age_seconds_at_binding": max(0.0, age),
        "required_consecutive_samples": configured,
        "observed_consecutive_samples": ready_passes,
        "source_v1_history_preserved": configured >= 15,
        "git_head": expected_repo_head,
        "host": expected_hostname,
        "timing": {
            "expected_interval_seconds": expected_interval_seconds,
            "gap_tolerance_seconds": gap_tolerance_seconds,
            "observed_gap_seconds": gaps,
            "maximum_observed_gap_seconds": max(gaps) if gaps else 0.0,
            "gap_check_pass": True,
        },
        "artifacts": {name: file_ref(path) for name, path in artifacts.items()},
    }


def run_p02b_validator(
    validator: Path,
    result: Path,
    repo_root: Path,
    repo_head: str,
    binary_sha256: str,
    maximum_age_seconds: float,
) -> Dict[str, Dict[str, Any]]:
    validator = canonical_file(validator, "canonical P02B validator")
    result = canonical_file(result, "P02B result")
    admissions = {}  # type: Dict[str, Dict[str, Any]]
    for consumer in ("P10", "P20"):
        command = [
            sys.executable,
            str(validator),
            "--result",
            str(result),
            "--consumer",
            consumer,
            "--require-formal",
            "--expected-repo-root",
            str(repo_root.resolve()),
            "--expected-repo-head",
            repo_head,
            "--expected-binary-sha256",
            binary_sha256,
            "--max-age-seconds",
            str(maximum_age_seconds),
        ]
        completed = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if completed.returncode != 0:
            raise GateError(
                "P02B validator rejected {} lease admission: {}".format(
                    consumer, completed.stderr.strip()
                )
            )
        try:
            receipt = json.loads(completed.stdout, object_pairs_hook=_unique_object)
        except (ValueError, GateError) as exc:
            raise GateError("P02B validator emitted invalid JSON") from exc
        if not isinstance(receipt, dict):
            raise GateError("P02B validator receipt is not an object")
        admissions[consumer] = receipt
    return admissions


def _validate_clean_source(
    result: Dict[str, Any], expected_repo_head: str, expected_hostname: str
) -> Dict[str, Any]:
    clean = result.get("clean_ready")
    if not isinstance(clean, dict):
        raise GateError("P02B result lacks clean_ready evidence")
    schema = clean.get("schema_version")
    required = clean.get("required_consecutive_samples")
    observed = clean.get("observed_consecutive_samples")
    if isinstance(required, bool) or not isinstance(required, int):
        raise GateError("P02B clean_ready required sample count is invalid")
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < required:
        raise GateError("P02B clean_ready observed sample count is invalid")
    if clean.get("git_head") != expected_repo_head:
        raise GateError("P02B clean_ready HEAD differs from the leased HEAD")
    if clean.get("host") != expected_hostname:
        raise GateError("P02B clean_ready host differs from the leased host")
    if schema == "p02b-clean-ready-binding-v1":
        if required < 15 or observed < 15:
            raise GateError("short windows may not use the v1 no-gap binding")
        return {
            "schema_version": schema,
            "mode": "legacy-15-sample-v1",
            "required_samples": required,
            "observed_samples": observed,
            "historical_evidence_preserved": True,
        }
    if schema == P03_BINDING_SCHEMA:
        timing = clean.get("timing")
        if required < 5 or not isinstance(timing, dict) or timing.get("gap_check_pass") is not True:
            raise GateError("short v2 clean_ready lacks five-sample gap evidence")
        if timing.get("expected_interval_seconds") != 60:
            raise GateError("short v2 clean_ready interval drift")
        return {
            "schema_version": schema,
            "mode": "short-5x60-v2",
            "required_samples": required,
            "observed_samples": observed,
            "historical_evidence_preserved": bool(clean.get("source_v1_history_preserved")),
        }
    raise GateError("unsupported P02B clean_ready binding schema")


def _validate_gate_contract(
    contract: Any,
    expected_contract_sha256: Any,
    result_path: Path,
    validator_path: Path,
    context: str,
) -> Dict[str, Any]:
    """Independently bind the prospective V2 gate and its executable tools."""

    if not isinstance(contract, dict) or set(contract) != P02B_GATE_CONTRACT_KEYS:
        raise GateError("{} gate_contract keys drift".format(context))
    if (
        contract.get("schema_version") != P02B_GATE_CONTRACT_SCHEMA
        or contract.get("method") != P02B_GATE_METHOD
    ):
        raise GateError("{} gate_contract schema/method drift".format(context))
    if contract.get("quantile") != {"numerator": 99, "denominator": 100}:
        raise GateError("{} gate_contract quantile drift".format(context))
    if contract.get("tail_bounds_us") != {"lower": 150000, "upper": 250000}:
        raise GateError("{} gate_contract tail-bound drift".format(context))
    if contract.get("sigma_multiplier") != 3:
        raise GateError("{} gate_contract sigma multiplier drift".format(context))
    for key, expected in (
        ("qps_cv_max", 0.07),
        ("mean_storage_latency_cv_max", 0.07),
    ):
        value = contract.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) != expected
        ):
            raise GateError("{} gate_contract {} drift".format(context, key))
    if contract.get("require_zero_overflow") is not True:
        raise GateError("{} gate_contract overflow policy drift".format(context))

    stability_path = verify_file_ref(
        contract.get("stability_result"),
        "{} gate stability result".format(context),
    )
    expected_stability = result_path.parent / "stability-result.json"
    if stability_path != expected_stability.resolve():
        raise GateError("{} gate stability-result path drift".format(context))

    tools = contract.get("tools")
    if not isinstance(tools, dict) or set(tools) != set(P02B_GATE_TOOL_FILENAMES):
        raise GateError("{} gate_contract tools drift".format(context))
    validator_dir = validator_path.resolve().parent
    for name, filename in P02B_GATE_TOOL_FILENAMES.items():
        tool_path = verify_file_ref(
            tools.get(name),
            "{} gate tool {}".format(context, name),
        )
        expected_path = validator_dir / filename
        if tool_path != expected_path.resolve():
            raise GateError("{} gate tool {} path drift".format(context, name))

    unsigned = dict(contract)
    embedded_digest = require_hex64(
        unsigned.pop("contract_sha256"),
        "{} gate contract SHA-256".format(context),
    )
    if canonical_sha256(unsigned) != embedded_digest:
        raise GateError("{} gate_contract canonical SHA-256 drift".format(context))
    if (
        require_hex64(
            expected_contract_sha256,
            "{} receipt gate_contract_sha256".format(context),
        )
        != embedded_digest
    ):
        raise GateError("{} receipt gate_contract SHA-256 drift".format(context))
    return contract


def _validate_p02b_admission_receipt(
    receipt: Any,
    consumer: str,
    result_path: Path,
    validator_path: Path,
    repo_root: Path,
    repo_head: str,
    binary_reference: Dict[str, Any],
    host: Dict[str, str],
) -> Dict[str, Any]:
    """Revalidate one canonical receipt and its mutable external artifacts.

    The lease seals the receipt itself, but the receipt points at the P02B PASS
    marker and provenance as external files.  Those files must therefore be
    checked at issue time and again on every lease reuse.
    """
    if not isinstance(receipt, dict):
        raise GateError("missing canonical {} P02B admission".format(consumer))
    result_sha = sha256_file(result_path)
    expected = {
        "state": "PASS",
        "consumer": consumer,
        "formal_required": True,
        "fixture_only": False,
        "scope": "host-global",
        "sentinel_result_sha256": result_sha,
        "repo_head": repo_head,
        "binary_sha256": binary_reference["sha256"],
        "host": host,
        "scale": "sf10",
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise GateError("{} P02B admission drift at {}".format(consumer, key))
    try:
        receipt_result = Path(receipt.get("sentinel_result", "")).resolve()
        receipt_repo = Path(receipt.get("repo_root", "")).resolve()
    except (OSError, TypeError):
        raise GateError("{} P02B admission path is invalid".format(consumer))
    if receipt_result != result_path or receipt_repo != repo_root.resolve():
        raise GateError("{} P02B admission path drift".format(consumer))
    if not isinstance(receipt.get("run_id"), str) or not receipt["run_id"]:
        raise GateError("{} P02B admission omitted run_id".format(consumer))
    parse_timestamp(receipt.get("completed_at_utc"), "{} P02B completion".format(consumer))
    if not isinstance(receipt.get("protocol"), dict):
        raise GateError("{} P02B admission omitted protocol".format(consumer))

    artifacts = {}  # type: Dict[str, Dict[str, Any]]
    expected_artifacts = (
        ("pass_marker", "pass_marker_sha256", "PASS"),
        ("provenance", "provenance_sha256", "provenance.json"),
    )
    for path_key, sha_key, expected_name in expected_artifacts:
        raw_path = receipt.get(path_key)
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise GateError("{} P02B receipt omitted {}".format(consumer, path_key))
        path = canonical_file(Path(raw_path), "{} P02B {}".format(consumer, path_key))
        if path.parent != result_path.parent or path.name != expected_name:
            raise GateError("{} P02B {} path drift".format(consumer, path_key))
        expected_sha = require_hex64(
            receipt.get(sha_key), "{} P02B {}".format(consumer, sha_key)
        )
        if sha256_file(path) != expected_sha:
            raise GateError("{} P02B {} SHA-256 changed".format(consumer, path_key))
        artifacts[path_key] = file_ref(path)

    marker = read_json(Path(artifacts["pass_marker"]["path"]), "P02B PASS marker")
    if (
        marker.get("state") != "PASS"
        or marker.get("fixture_only") is not False
        or Path(str(marker.get("result", ""))).resolve() != result_path
        or marker.get("result_sha256") != result_sha
    ):
        raise GateError("{} P02B PASS marker binding drift".format(consumer))

    provenance = read_json(Path(artifacts["provenance"]["path"]), "P02B provenance")
    provenance_repo = provenance.get("repo")
    provenance_binary = provenance.get("files", {}).get("binary")
    if (
        provenance.get("schema_version") != P02B_PROVENANCE_SCHEMA
        or provenance.get("fixture_mode") is not False
        or not isinstance(provenance_repo, dict)
        or Path(str(provenance_repo.get("root", ""))).resolve() != repo_root.resolve()
        or provenance_repo.get("head") != repo_head
        or provenance_repo.get("dirty") is not False
        or not isinstance(provenance_binary, dict)
        or Path(str(provenance_binary.get("path", ""))).resolve()
        != Path(str(binary_reference["path"])).resolve()
        or provenance_binary.get("size_bytes") != binary_reference["size_bytes"]
        or provenance_binary.get("sha256") != binary_reference["sha256"]
    ):
        raise GateError("{} P02B provenance identity drift".format(consumer))

    result = read_json(result_path, "{} P02B result".format(consumer))
    if result.get("schema_version") != P02B_RESULT_SCHEMA:
        raise GateError("{} P02B result schema drift".format(consumer))
    gate_contract = _validate_gate_contract(
        receipt.get("gate_contract"),
        receipt.get("gate_contract_sha256"),
        result_path,
        validator_path,
        "{} P02B admission".format(consumer),
    )
    if result.get("gate_contract") != gate_contract:
        raise GateError("{} P02B result/receipt gate_contract drift".format(consumer))
    stability_path = Path(str(gate_contract["stability_result"]["path"])).resolve()
    if result.get("stability") != read_json(
        stability_path, "{} P02B stability result".format(consumer)
    ):
        raise GateError("{} P02B embedded stability drift".format(consumer))
    return gate_contract


def issue_lease_from_admissions(
    admissions: Dict[str, Dict[str, Any]],
    p02b_result_path: Path,
    p02b_validator_path: Path,
    repo_root: Path,
    binary_path: Path,
    lease_path: Path,
    now: Optional[dt.datetime] = None,
    current_repo: Optional[Dict[str, Any]] = None,
    current_host: Optional[Dict[str, Any]] = None,
    duration_seconds: int = LEASE_DURATION_SECONDS,
) -> Dict[str, Any]:
    if duration_seconds != LEASE_DURATION_SECONDS:
        raise GateError("formal batch lease duration is frozen at 24 hours")
    result_path = canonical_file(p02b_result_path, "P02B result")
    validator_path = canonical_file(p02b_validator_path, "P02B validator")
    repo = current_repo or git_facts(repo_root)
    host = host_summary(current_host or host_facts())
    binary = canonical_file(binary_path, "P02B anchor binary", executable=True)
    binary_reference = file_ref(binary, include_stat_identity=True)
    if repo.get("dirty") is not False:
        raise GateError("a batch lease requires a clean repository")
    if Path(str(repo.get("root", ""))).resolve() != repo_root.resolve():
        raise GateError("current repository root drift")
    head = repo.get("head")
    if not isinstance(head, str) or GIT_HEAD_RE.fullmatch(head) is None:
        raise GateError("current repository HEAD is malformed")

    gate_contracts = {}  # type: Dict[str, Dict[str, Any]]
    for consumer in ("P10", "P20"):
        gate_contracts[consumer] = _validate_p02b_admission_receipt(
            admissions.get(consumer),
            consumer,
            result_path,
            validator_path,
            repo_root,
            head,
            binary_reference,
            host,
        )
    if gate_contracts["P10"] != gate_contracts["P20"]:
        raise GateError("P10/P20 P02B gate_contract mismatch")
    gate_contract = gate_contracts["P10"]

    result = read_json(result_path, "P02B result")
    if (
        result.get("schema_version") != P02B_RESULT_SCHEMA
        or result.get("state") != "PASS"
        or result.get("fixture_only") is not False
        or result.get("formal_gate_eligible") is not True
        or result.get("downstream_release_eligible") is not True
        or result.get("gate_contract") != gate_contract
    ):
        raise GateError("P02B result is not a formal PASS")
    clean_source = _validate_clean_source(result, head, host["hostname"])

    issued = now or dt.datetime.now(dt.timezone.utc)
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=dt.timezone.utc)
    issued = issued.astimezone(dt.timezone.utc)
    expires = issued + dt.timedelta(seconds=duration_seconds)
    lease = {
        "schema_version": LEASE_SCHEMA,
        "state": "PASS",
        "scope": "single-host-single-head-formal-batch",
        "classification": {
            "performance_eligible": False,
            "purpose": "downstream-admission-only",
        },
        "issued_at_utc": issued.isoformat().replace("+00:00", "Z"),
        "expires_at_utc": expires.isoformat().replace("+00:00", "Z"),
        "duration_seconds": duration_seconds,
        "consumers": ["P10", "P20"],
        "identity": {
            "host": host,
            "repo": {
                "root": str(repo_root.resolve()),
                "head": head,
                "clean_at_issue": True,
            },
            "binary": binary_reference,
        },
        "p02b": {
            "result": file_ref(result_path),
            "validator": file_ref(validator_path),
            "admissions": admissions,
            "gate_contract": gate_contract,
            "gate_contract_sha256": gate_contract["contract_sha256"],
            "clean_window": clean_source,
        },
        "repeat_guard_policy": {
            "required": True,
            "interval_seconds": 1.0,
            "maximum_gap_seconds": 3.0,
            "repo_head_each_sample": True,
            "repo_clean_each_sample": True,
            "binary_stat_each_sample": True,
            "binary_sha256_at_boundaries": True,
            "known_interference_each_sample": True,
            "fail_closed": True,
        },
    }
    lease_path = lease_path.resolve()
    marker_path = lease_path.with_name(lease_path.name + ".PASS.json")
    if lease_path.exists():
        raise GateError("refusing to overwrite batch lease: {}".format(lease_path))
    if marker_path.exists():
        raise GateError("refusing to overwrite batch lease marker")
    atomic_json(lease_path, lease)
    atomic_json(
        marker_path,
        {
            "schema_version": LEASE_MARKER_SCHEMA,
            "state": "PASS",
            "lease": str(lease_path),
            "lease_sha256": sha256_file(lease_path),
        },
    )
    return lease


def validate_lease(
    lease_path: Path,
    consumer: str,
    repo_root: Path,
    binary_path: Path,
    now: Optional[dt.datetime] = None,
    current_repo: Optional[Dict[str, Any]] = None,
    current_host: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if consumer not in ("P10", "P20"):
        raise GateError("unsupported batch-lease consumer")
    lease_path = canonical_file(lease_path, "batch lease")
    lease = read_json(lease_path, "batch lease")
    if lease.get("schema_version") != LEASE_SCHEMA or lease.get("state") != "PASS":
        raise GateError("batch lease schema/state drift")
    if lease.get("scope") != "single-host-single-head-formal-batch":
        raise GateError("batch lease scope drift")
    if lease.get("duration_seconds") != LEASE_DURATION_SECONDS:
        raise GateError("batch lease duration is not exactly 24 hours")
    if lease.get("consumers") != ["P10", "P20"] or consumer not in lease["consumers"]:
        raise GateError("batch lease consumer list drift")
    classification = lease.get("classification")
    if (
        not isinstance(classification, dict)
        or classification.get("performance_eligible") is not False
        or classification.get("purpose") != "downstream-admission-only"
    ):
        raise GateError("batch lease must remain admission-only")
    issued = parse_timestamp(lease.get("issued_at_utc"), "lease issue")
    expires = parse_timestamp(lease.get("expires_at_utc"), "lease expiry")
    if (expires - issued).total_seconds() != LEASE_DURATION_SECONDS:
        raise GateError("batch lease timestamp duration drift")
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc)
    if current < issued - dt.timedelta(seconds=60):
        raise GateError("batch lease issue time is in the future")
    if current >= expires:
        raise GateError("batch lease expired")

    marker_path = lease_path.with_name(lease_path.name + ".PASS.json")
    marker = read_json(canonical_file(marker_path, "batch lease marker"), "batch lease marker")
    if (
        marker.get("schema_version") != LEASE_MARKER_SCHEMA
        or marker.get("state") != "PASS"
        or Path(str(marker.get("lease", ""))).resolve() != lease_path
        or marker.get("lease_sha256") != sha256_file(lease_path)
    ):
        raise GateError("batch lease marker binding drift")

    identity = lease.get("identity")
    if not isinstance(identity, dict):
        raise GateError("batch lease identity is missing")
    repo_expected = identity.get("repo")
    if not isinstance(repo_expected, dict):
        raise GateError("batch lease repo identity is missing")
    repo = current_repo or git_facts(repo_root)
    if (
        Path(str(repo_expected.get("root", ""))).resolve() != repo_root.resolve()
        or Path(str(repo.get("root", ""))).resolve() != repo_root.resolve()
        or repo.get("head") != repo_expected.get("head")
        or repo.get("dirty") is not False
        or repo_expected.get("clean_at_issue") is not True
    ):
        raise GateError("batch lease repository HEAD/clean identity changed")
    host = host_summary(current_host or host_facts())
    if identity.get("host") != host:
        raise GateError("batch lease host identity changed")
    expected_binary = identity.get("binary")
    binary = verify_file_ref(expected_binary, "batch lease binary", include_stat_identity=True)
    if binary != binary_path.resolve():
        raise GateError("batch lease binary path differs from selected binary")
    p02b = lease.get("p02b")
    if not isinstance(p02b, dict):
        raise GateError("batch lease P02B binding is missing")
    result_path = verify_file_ref(p02b.get("result"), "leased P02B result")
    validator_path = verify_file_ref(p02b.get("validator"), "leased P02B validator")
    admissions = p02b.get("admissions")
    if not isinstance(admissions, dict) or set(admissions) != {"P10", "P20"}:
        raise GateError("batch lease P02B admissions are incomplete")
    gate_contracts = {}  # type: Dict[str, Dict[str, Any]]
    for admitted_consumer in ("P10", "P20"):
        gate_contracts[admitted_consumer] = _validate_p02b_admission_receipt(
            admissions.get(admitted_consumer),
            admitted_consumer,
            result_path,
            validator_path,
            repo_root,
            str(repo_expected.get("head", "")),
            expected_binary,
            host,
        )
    if gate_contracts["P10"] != gate_contracts["P20"]:
        raise GateError("leased P10/P20 P02B gate_contract mismatch")
    leased_gate_contract = _validate_gate_contract(
        p02b.get("gate_contract"),
        p02b.get("gate_contract_sha256"),
        result_path,
        validator_path,
        "leased P02B",
    )
    if gate_contracts["P10"] != leased_gate_contract:
        raise GateError("batch lease P02B gate_contract binding drift")
    result = read_json(result_path, "leased P02B result")
    if (
        result.get("schema_version") != P02B_RESULT_SCHEMA
        or result.get("state") != "PASS"
        or result.get("fixture_only") is not False
        or result.get("formal_gate_eligible") is not True
        or result.get("downstream_release_eligible") is not True
        or result.get("gate_contract") != leased_gate_contract
    ):
        raise GateError("leased P02B result is no longer a formal PASS")
    clean_source = _validate_clean_source(result, str(repo_expected.get("head", "")), host["hostname"])
    if p02b.get("clean_window") != clean_source:
        raise GateError("batch lease clean-window binding drift")
    policy = lease.get("repeat_guard_policy")
    if not isinstance(policy, dict) or policy.get("required") is not True or policy.get("fail_closed") is not True:
        raise GateError("batch lease repeat-guard policy drift")
    return {
        "schema_version": "cidr-batch-lease-admission-v2",
        "state": "PASS",
        "consumer": consumer,
        "lease": str(lease_path),
        "lease_sha256": sha256_file(lease_path),
        "issued_at_utc": lease["issued_at_utc"],
        "expires_at_utc": lease["expires_at_utc"],
        "remaining_seconds": (expires - current).total_seconds(),
        "host": host,
        "repo_head": repo["head"],
        "binary_sha256": expected_binary["sha256"],
        "gate_contract": leased_gate_contract,
        "gate_contract_sha256": leased_gate_contract["contract_sha256"],
    }


def _proc_snapshot() -> Dict[int, Dict[str, Any]]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        raise GateError("/proc is required for formal interference monitoring")
    result = {}  # type: Dict[int, Dict[str, Any]]
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            values = {}  # type: Dict[str, str]
            for line in (entry / "status").read_text(encoding="utf-8", errors="replace").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    values[key] = value.strip()
            uid = int(values.get("Uid", "-1").split()[0])
            rss_tokens = values.get("VmRSS", "0 kB").split()
            rss_kib = int(rss_tokens[0]) if rss_tokens else 0
            ppid = int(values.get("PPid", "0"))
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
            raw_cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").strip()
            command = raw_cmd.decode("utf-8", errors="replace")
            result[pid] = {
                "pid": pid,
                "ppid": ppid,
                "uid": uid,
                "rss_kib": rss_kib,
                "comm": comm,
                "command": command,
            }
        except (OSError, ValueError, IndexError):
            continue
    return result


def _descendants(snapshot: Dict[int, Dict[str, Any]], roots: Iterable[int]) -> Set[int]:
    allowed = set(int(pid) for pid in roots if int(pid) > 0)
    changed = True
    while changed:
        changed = False
        for pid, record in snapshot.items():
            if pid not in allowed and record.get("ppid") in allowed:
                allowed.add(pid)
                changed = True
    return allowed


def resolve_container_roots(names: Sequence[str]) -> Set[int]:
    roots = set()  # type: Set[int]
    for name in names:
        try:
            raw = subprocess.check_output(
                ["docker", "inspect", "--type", "container", "--format", "{{.State.Pid}}", name],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            ).strip()
            pid = int(raw)
            if pid > 0:
                roots.add(pid)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return roots


def scan_known_interference(
    allowed_roots: Iterable[int], allowed_containers: Sequence[str]
) -> List[Dict[str, Any]]:
    """Continuously apply the attributable process predicates used by P03.

    Global CPU/disk utilisation cannot be used while the benchmark itself is
    active.  The guard therefore records attributable competing workloads:
    zcl >=1 GiB, zcl rsync/ssh-rsync, gpstore grpc, and TuGraph lgraph.  Declared
    benchmark process/container trees are excluded explicitly.
    """
    snapshot = _proc_snapshot()
    roots = set(int(pid) for pid in allowed_roots if int(pid) > 0)
    roots.update(resolve_container_roots(allowed_containers))
    roots.add(os.getpid())
    allowed = _descendants(snapshot, roots)
    try:
        zcl_uid = _pwd.getpwnam("zcl").pw_uid if _pwd is not None else -1
    except KeyError:
        zcl_uid = -1
    findings = []  # type: List[Dict[str, Any]]
    for pid, record in snapshot.items():
        if pid in allowed:
            continue
        comm = str(record.get("comm", ""))
        command = str(record.get("command", ""))
        reasons = []  # type: List[str]
        if record.get("uid") == zcl_uid and int(record.get("rss_kib", 0)) >= 1048576:
            reasons.append("zcl_big")
        if record.get("uid") == zcl_uid and (
            comm == "rsync" or (comm == "ssh" and "rsync --server" in command)
        ):
            reasons.append("zcl_rsync")
        if comm == "grpc" and "/gpstore/" in command:
            reasons.append("gpstore")
        if comm == "lgraph" or "lgraph_server" in command:
            reasons.append("tugraph")
        if reasons:
            findings.append(
                {
                    "pid": pid,
                    "reasons": reasons,
                    "rss_kib": record.get("rss_kib", 0),
                    "comm": comm,
                    "command": command,
                }
            )
    return sorted(findings, key=lambda item: int(item["pid"]))


def _terminate_formal_group(root_pid: int) -> None:
    if root_pid <= 1:
        return
    try:
        if os.getpgid(root_pid) == root_pid:
            os.killpg(root_pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def run_guard(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise GateError("refusing non-empty guard directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    admission = validate_lease(
        args.lease,
        args.consumer,
        args.repo_root,
        args.binary,
    )
    lease = read_json(args.lease.resolve(), "batch lease")
    expected_head = admission["repo_head"]
    expected_binary = lease["identity"]["binary"]
    expires = parse_timestamp(lease["expires_at_utc"], "lease expiry")
    samples_path = output_dir / "integrity-samples.tsv"
    ready_path = output_dir / "READY.json"
    status_path = output_dir / "status.json"
    failure_path = output_dir / "FAILED.json"
    started_mono = time.monotonic()
    previous_mono = None  # type: Optional[float]
    errors = []  # type: List[str]
    rows = []  # type: List[Dict[str, Any]]
    stop_requested = False

    def on_signal(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    with samples_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=GUARD_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        next_sample = time.monotonic()
        while True:
            # Latch stop state before the sample begins.  A marker that appears
            # while git/process checks are in flight must not make this
            # in-progress row masquerade as post-command coverage.  The next
            # iteration will observe the marker first, take one final complete
            # sample, and only then seal the guard.
            stop_marker_seen = args.stop_file.exists()
            stop_signal_seen = stop_requested
            now_mono = time.monotonic()
            now_wall = dt.datetime.now(dt.timezone.utc)
            gap = 0.0 if previous_mono is None else now_mono - previous_mono
            reasons = []  # type: List[str]
            if previous_mono is not None and gap > args.max_gap_seconds:
                reasons.append("sample_gap")
            repo = git_facts(args.repo_root)
            if repo["head"] != expected_head:
                reasons.append("head_changed")
            if repo["dirty"]:
                reasons.append("repo_dirty")
            stat = args.binary.resolve().stat()
            if stat.st_size != expected_binary["size_bytes"] or stat.st_mtime_ns != expected_binary["mtime_ns"]:
                reasons.append("binary_stat_changed")
            if now_wall >= expires:
                reasons.append("lease_expired")
            findings = scan_known_interference(
                [args.root_pid] + list(args.allowed_pid), list(args.allowed_container)
            )
            if findings:
                reasons.append("external_contamination")
            row = {
                "timestamp_utc": now_wall.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "sample_index": len(rows),
                "monotonic_s": "{:.6f}".format(now_mono - started_mono),
                "gap_s": "{:.6f}".format(gap),
                "lease_remaining_s": "{:.3f}".format((expires - now_wall).total_seconds()),
                "repo_head": repo["head"],
                "repo_dirty": int(bool(repo["dirty"])),
                "binary_size_bytes": stat.st_size,
                "binary_mtime_ns": stat.st_mtime_ns,
                "contamination_count": len(findings),
                "contamination_pids": ",".join(str(item["pid"]) for item in findings),
                "sample_pass": int(not reasons),
                "reasons": "none" if not reasons else ",".join(sorted(set(reasons))),
            }
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
            rows.append(row)
            previous_mono = now_mono
            if len(rows) == 1 and not reasons:
                atomic_json(
                    ready_path,
                    {
                        "schema_version": GUARD_SCHEMA,
                        "state": "READY",
                        "ready_at_utc": row["timestamp_utc"],
                        "sample_index": 0,
                        "lease_sha256": admission["lease_sha256"],
                        "repo_head": expected_head,
                    },
                )
            if reasons:
                errors.extend(reasons)
                if args.terminate_pgid_on_failure:
                    _terminate_formal_group(args.root_pid)
                break
            if stop_marker_seen or stop_signal_seen:
                if stop_signal_seen and not stop_marker_seen:
                    errors.append("guard_signal_without_stop_marker")
                break
            next_sample += args.interval_seconds
            now_after = time.monotonic()
            if next_sample <= now_after:
                next_sample = now_after + args.interval_seconds
            time.sleep(max(0.01, next_sample - time.monotonic()))

    if len(rows) < 2:
        errors.append("fewer_than_two_integrity_samples")
    try:
        if sha256_file(args.binary.resolve()) != expected_binary["sha256"]:
            errors.append("binary_sha256_changed")
    except OSError:
        errors.append("binary_unreadable_at_end")
    state = "PASS" if not errors else "FAILED"
    status = {
        "schema_version": GUARD_SCHEMA,
        "state": state,
        "started_at_utc": rows[0]["timestamp_utc"] if rows else utc_now(),
        "ended_at_utc": rows[-1]["timestamp_utc"] if rows else utc_now(),
        "sample_count": len(rows),
        "interval_seconds": args.interval_seconds,
        "maximum_gap_seconds": args.max_gap_seconds,
        "maximum_observed_gap_seconds": max(
            [float(row["gap_s"]) for row in rows[1:]] or [0.0]
        ),
        "errors": sorted(set(errors)),
        "lease": file_ref(args.lease.resolve()),
        "samples": file_ref(samples_path),
        "repo_head": expected_head,
        "binary_sha256": expected_binary["sha256"],
    }
    atomic_json(status_path, status)
    if errors:
        atomic_json(
            failure_path,
            {
                "schema_version": GUARD_SCHEMA,
                "state": "FAILED",
                "status": str(status_path),
                "status_sha256": sha256_file(status_path),
                "errors": status["errors"],
            },
        )
        return 2
    return 0


def validate_guard_evidence(
    guard_dir: Path,
    expected_lease: Path,
    expected_repo_head: str,
) -> Dict[str, Any]:
    guard_dir = canonical_dir(guard_dir, "P31 integrity guard directory")
    status_path = canonical_file(guard_dir / "status.json", "guard status")
    ready_path = canonical_file(guard_dir / "READY.json", "guard READY")
    if (guard_dir / "FAILED.json").exists():
        raise GateError("integrity guard has a FAILED marker")
    status = read_json(status_path, "guard status")
    ready = read_json(ready_path, "guard READY")
    if status.get("schema_version") != GUARD_SCHEMA or status.get("state") != "PASS":
        raise GateError("integrity guard status is not PASS")
    if status.get("errors") != []:
        raise GateError("integrity guard retained errors")
    if ready.get("schema_version") != GUARD_SCHEMA or ready.get("state") != "READY":
        raise GateError("integrity guard readiness drift")
    lease_ref = status.get("lease")
    lease_path = verify_file_ref(lease_ref, "guard lease")
    if lease_path != expected_lease.resolve():
        raise GateError("integrity guard used another lease")
    lease = read_json(lease_path, "guard lease")
    marker_path = canonical_file(
        lease_path.with_name(lease_path.name + ".PASS.json"), "guard lease marker"
    )
    marker = read_json(marker_path, "guard lease marker")
    lease_sha = sha256_file(lease_path)
    if (
        marker.get("schema_version") != LEASE_MARKER_SCHEMA
        or marker.get("state") != "PASS"
        or Path(str(marker.get("lease", ""))).resolve() != lease_path
        or marker.get("lease_sha256") != lease_sha
    ):
        raise GateError("integrity guard lease marker binding drift")
    issued = parse_timestamp(lease.get("issued_at_utc"), "lease issue")
    expires = parse_timestamp(lease.get("expires_at_utc"), "lease expiry")
    if status.get("repo_head") != expected_repo_head or ready.get("repo_head") != expected_repo_head:
        raise GateError("integrity guard HEAD binding drift")
    samples_path = verify_file_ref(status.get("samples"), "guard samples")
    header, rows = read_tsv(samples_path, set(GUARD_COLUMNS))
    if header != GUARD_COLUMNS:
        raise GateError("integrity guard TSV schema drift")
    if status.get("sample_count") != len(rows) or len(rows) < 2:
        raise GateError("integrity guard sample coverage is incomplete")
    interval = float(status.get("interval_seconds", 0))
    maximum_gap = float(status.get("maximum_gap_seconds", 0))
    if interval <= 0 or maximum_gap < interval:
        raise GateError("integrity guard interval/gap policy is invalid")
    policy = lease.get("repeat_guard_policy")
    if (
        not isinstance(policy, dict)
        or float(policy.get("interval_seconds", -1)) != interval
        or float(policy.get("maximum_gap_seconds", -1)) != maximum_gap
    ):
        raise GateError("integrity guard policy differs from the lease")
    binary_identity = lease.get("identity", {}).get("binary", {})
    expected_binary_size = binary_identity.get("size_bytes")
    expected_binary_mtime = binary_identity.get("mtime_ns")
    expected_binary_sha = binary_identity.get("sha256")
    if status.get("binary_sha256") != expected_binary_sha:
        raise GateError("integrity guard binary SHA binding drift")
    if (
        ready.get("sample_index") != 0
        or ready.get("lease_sha256") != lease_sha
        or ready.get("ready_at_utc") != rows[0].get("timestamp_utc")
        or status.get("started_at_utc") != rows[0].get("timestamp_utc")
        or status.get("ended_at_utc") != rows[-1].get("timestamp_utc")
    ):
        raise GateError("integrity guard readiness/lifetime binding drift")
    observed_maximum = 0.0
    prior_mono = None  # type: Optional[float]
    for index, row in enumerate(rows):
        if _int(row, "sample_index", "guard sample") != index:
            raise GateError("integrity guard sample indexes contain a gap")
        try:
            mono = float(row["monotonic_s"])
            gap = float(row["gap_s"])
            contamination = int(row["contamination_count"])
            repo_dirty = int(row["repo_dirty"])
            sample_pass = int(row["sample_pass"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GateError("integrity guard sample is malformed") from exc
        if not all(math.isfinite(value) for value in (mono, gap)):
            raise GateError("integrity guard sample contains non-finite time")
        if prior_mono is not None:
            actual_gap = mono - prior_mono
            if actual_gap <= 0 or actual_gap > maximum_gap:
                raise GateError("integrity guard has an unmonitored time gap")
            if not math.isclose(actual_gap, gap, rel_tol=1e-4, abs_tol=0.01):
                raise GateError("integrity guard recorded gap differs from monotonic time")
            observed_maximum = max(observed_maximum, actual_gap)
        elif abs(gap) > 0.01:
            raise GateError("integrity guard first gap must be zero")
        prior_mono = mono
        timestamp = parse_timestamp(row.get("timestamp_utc"), "guard sample")
        if timestamp < issued or timestamp >= expires:
            raise GateError("integrity guard sample lies outside the lease")
        if (
            row.get("repo_head") != expected_repo_head
            or repo_dirty != 0
            or _int(row, "binary_size_bytes", "guard sample") != expected_binary_size
            or _int(row, "binary_mtime_ns", "guard sample") != expected_binary_mtime
            or contamination != 0
            or row.get("contamination_pids") not in ("", None)
            or sample_pass != 1
            or row.get("reasons") != "none"
        ):
            raise GateError("integrity guard detected contamination/identity drift")
    recorded_maximum = float(status.get("maximum_observed_gap_seconds", -1))
    if not math.isclose(recorded_maximum, observed_maximum, rel_tol=1e-4, abs_tol=0.01):
        raise GateError("integrity guard maximum-gap summary drift")
    return {
        "schema_version": "cidr-p31-integrity-admission-v2",
        "state": "PASS",
        "guard_dir": str(guard_dir),
        "status_sha256": sha256_file(status_path),
        "samples_sha256": sha256_file(samples_path),
        "sample_count": len(rows),
        "maximum_observed_gap_seconds": observed_maximum,
        "repo_head": expected_repo_head,
        "lease_sha256": lease_sha,
    }


def command_validate_p03(args: argparse.Namespace) -> int:
    result = validate_p03_ready_v2(
        args.ready,
        args.expected_repo_head,
        args.expected_hostname,
        max_age_seconds=args.max_age_seconds,
    )
    if args.output:
        atomic_json(args.output.resolve(), result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_issue_lease(args: argparse.Namespace) -> int:
    repo = git_facts(args.repo_root)
    binary = canonical_file(args.binary, "P02B anchor binary", executable=True)
    admissions = run_p02b_validator(
        args.p02b_validator,
        args.p02b_result,
        args.repo_root,
        repo["head"],
        sha256_file(binary),
        args.p02b_max_age_seconds,
    )
    lease = issue_lease_from_admissions(
        admissions,
        args.p02b_result,
        args.p02b_validator,
        args.repo_root,
        binary,
        args.output,
        current_repo=repo,
    )
    print(json.dumps(lease, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_validate_lease(args: argparse.Namespace) -> int:
    result = validate_lease(
        args.lease,
        args.consumer,
        args.repo_root,
        args.binary,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_validate_guard(args: argparse.Namespace) -> int:
    result = validate_guard_evidence(args.guard_dir, args.lease, args.expected_repo_head)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    p03 = commands.add_parser("validate-p03")
    p03.add_argument("--ready", required=True, type=Path)
    p03.add_argument("--expected-repo-head", required=True)
    p03.add_argument("--expected-hostname", required=True)
    p03.add_argument("--max-age-seconds", type=float, default=300.0)
    p03.add_argument("--output", type=Path)
    p03.set_defaults(function=command_validate_p03)

    issue = commands.add_parser("issue-lease")
    issue.add_argument("--p02b-result", required=True, type=Path)
    issue.add_argument("--p02b-validator", required=True, type=Path)
    issue.add_argument("--p02b-max-age-seconds", type=float, default=1800.0)
    issue.add_argument("--repo-root", required=True, type=Path)
    issue.add_argument("--binary", required=True, type=Path)
    issue.add_argument("--output", required=True, type=Path)
    issue.set_defaults(function=command_issue_lease)

    validate = commands.add_parser("validate-lease")
    validate.add_argument("--lease", required=True, type=Path)
    validate.add_argument("--consumer", required=True, choices=("P10", "P20"))
    validate.add_argument("--repo-root", required=True, type=Path)
    validate.add_argument("--binary", required=True, type=Path)
    validate.set_defaults(function=command_validate_lease)

    guard = commands.add_parser("guard")
    guard.add_argument("--lease", required=True, type=Path)
    guard.add_argument("--consumer", required=True, choices=("P10", "P20"))
    guard.add_argument("--repo-root", required=True, type=Path)
    guard.add_argument("--binary", required=True, type=Path)
    guard.add_argument("--root-pid", required=True, type=int)
    guard.add_argument("--allowed-pid", action="append", type=int, default=[])
    guard.add_argument("--allowed-container", action="append", default=[])
    guard.add_argument("--stop-file", required=True, type=Path)
    guard.add_argument("--output-dir", required=True, type=Path)
    guard.add_argument("--interval-seconds", type=float, default=1.0)
    guard.add_argument("--max-gap-seconds", type=float, default=3.0)
    guard.add_argument("--terminate-pgid-on-failure", action="store_true")
    guard.set_defaults(function=run_guard)

    guard_validate = commands.add_parser("validate-guard")
    guard_validate.add_argument("--guard-dir", required=True, type=Path)
    guard_validate.add_argument("--lease", required=True, type=Path)
    guard_validate.add_argument("--expected-repo-head", required=True)
    guard_validate.set_defaults(function=command_validate_guard)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "guard":
            if args.interval_seconds <= 0 or args.max_gap_seconds < args.interval_seconds:
                raise GateError("guard interval/gap limits are invalid")
            if args.root_pid <= 1:
                raise GateError("guard root PID must be greater than one")
        return int(args.function(args))
    except (GateError, OSError, ValueError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
