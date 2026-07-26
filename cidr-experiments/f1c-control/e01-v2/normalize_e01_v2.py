#!/usr/bin/env python3
"""Fail-closed normalizer for the Figure 1 / E01 formal manifest v2.

This module is deliberately independent of the P10 runner and renderer.  It
only consumes a sealed manifest plus small, explicitly referenced evidence
files.  A successful normalization is not a claim receipt: paper-claim
eligibility remains false until a separate Figure/QA gate signs it.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "e01-manifest-v2.schema.json"

SYSTEMS: Tuple[Tuple[str, str], ...] = (
    ("seml0", "SemL0"),
    ("seml0-naive", "SemL0-naive"),
    ("livegraph", "LiveGraph"),
    ("aster", "Aster RocksGraph"),
    ("tugraph", "TuGraph"),
    ("nebulagraph", "NebulaGraph"),
    ("neo4j", "Neo4j"),
)
SYSTEM_KEYS = tuple(item[0] for item in SYSTEMS)
SYSTEM_NAMES = dict(SYSTEMS)
UNDERLYING_SYSTEM = {"seml0-naive": "seml0"}
RUN_KEYS = tuple(
    f"{system}:r{repeat}" for system, _ in SYSTEMS for repeat in (1, 2, 3)
)
SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
TSV_COLUMNS = [
    "experiment_id",
    "run_id",
    "run_key",
    "repeat_index",
    "timestamp_utc",
    "host_fingerprint",
    "git_sha",
    "binary_sha256",
    "system",
    "system_key",
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
    "measurement_s",
    "digest_pass",
    "mismatch_count",
    "latency_p50_us",
    "latency_p95_us",
    "latency_p99_us",
    "completed_queries",
    "timeout_queries",
    "offered_qps",
    "completed_qps",
    "load_wall_s",
    "final_disk_bytes",
    "load_peak_rss_bytes",
    "interface_scope",
    "system_version",
    "unsupported_reason",
    "formal_eligible",
    "performance_eligible",
    "paper_claim_eligible",
    "validated_result_sha256",
    "p31_manifest_sha256",
    "p31_validation_sha256",
    "p31_done_sha256",
    "batch_admission_sha256",
    "cost_receipt_sha256",
]
ASSET_ROLES = (
    "validated_result",
    "adapter_request",
    "adapter_provenance",
    "p31_manifest",
    "p31_validation",
    "p31_done",
    "batch_admission",
    "cost_receipt",
)


class ContractError(RuntimeError):
    """An input cannot satisfy the frozen E01 contract."""


def _fail(message: str) -> None:
    raise ContractError(message)


def _is_dict(value: Any) -> bool:
    return type(value) is dict


def _is_list(value: Any) -> bool:
    return type(value) is list


def _no_duplicate_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def read_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_no_duplicate_pairs)
    except ContractError:
        raise
    except (OSError, ValueError) as exc:
        _fail(f"{label}: cannot read strict JSON {path}: {exc}")
    if not _is_dict(value):
        _fail(f"{label}: top-level JSON object required: {path}")
    return value


def read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        _fail(f"{label}: cannot read {path}: {exc}")
    return b""  # unreachable


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, label: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        _fail(f"{label}: cannot hash {path}: {exc}")
    return digest.hexdigest()


def require_keys(
    obj: Mapping[str, Any],
    required: Iterable[str],
    optional: Iterable[str] = (),
    label: str = "object",
) -> None:
    required_set = set(required)
    optional_set = set(optional)
    missing = sorted(required_set - set(obj))
    extra = sorted(set(obj) - required_set - optional_set)
    if missing:
        _fail(f"{label}: missing keys {missing}")
    if extra:
        _fail(f"{label}: unexpected keys {extra}")


def text(obj: Mapping[str, Any], key: str, label: str) -> str:
    value = obj.get(key)
    if type(value) is not str or not value.strip():
        _fail(f"{label}.{key}: non-empty string required")
    return value.strip()


def exact(obj: Mapping[str, Any], key: str, expected: Any, label: str) -> None:
    if obj.get(key) != expected:
        _fail(f"{label}.{key}: expected {expected!r}, got {obj.get(key)!r}")


def integer(
    obj: Mapping[str, Any],
    key: str,
    label: str,
    *,
    minimum: Optional[int] = None,
) -> int:
    value = obj.get(key)
    if type(value) is not int:
        _fail(f"{label}.{key}: integer required")
    if minimum is not None and value < minimum:
        _fail(f"{label}.{key}: must be >= {minimum}")
    return value


def number(
    obj: Mapping[str, Any],
    key: str,
    label: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    value = obj.get(key)
    if type(value) not in (int, float) or isinstance(value, bool):
        _fail(f"{label}.{key}: finite number required")
    value = float(value)
    if not math.isfinite(value):
        _fail(f"{label}.{key}: finite number required")
    if positive and value <= 0:
        _fail(f"{label}.{key}: must be > 0")
    if nonnegative and value < 0:
        _fail(f"{label}.{key}: must be >= 0")
    return value


def sha(value: Any, label: str, pattern: re.Pattern[str] = SHA256_RE) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        _fail(f"{label}: invalid digest")
    return value.lower()


def utc(value: Any, label: str) -> dt.datetime:
    if type(value) is not str:
        _fail(f"{label}: UTC ISO-8601 string required")
    raw = value.strip()
    if not raw.endswith("Z"):
        _fail(f"{label}: timestamp must use Z suffix")
    try:
        parsed = dt.datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as exc:
        _fail(f"{label}: invalid UTC timestamp {raw!r}: {exc}")
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        _fail(f"{label}: timestamp must be UTC")
    return parsed


def _canonical_alias(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    return {
        "b64": "budg-b64",
        "budget-b64": "budg-b64",
        "semantic-budgeted-b64": "budg-b64",
    }.get(normalized, normalized)


def validate_schema_document(schema_path: Path) -> str:
    schema = read_json(schema_path, "manifest schema")
    require_keys(schema, ("$schema", "$id", "title", "description", "type",
                          "additionalProperties", "required", "properties", "$defs"),
                 label="manifest schema")
    exact(schema, "type", "object", "manifest schema")
    exact(schema, "additionalProperties", False, "manifest schema")
    return sha256_file(schema_path, "manifest schema")


class AssetVerifier:
    """Resolve and hash only explicit, non-symlink, in-root asset references."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._seen: Dict[str, str] = {}

    def path(self, ref: Mapping[str, Any], label: str) -> Path:
        require_keys(ref, ("path", "sha256", "size_bytes"), label=label)
        raw = text(ref, "path", label)
        if "\\" in raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            _fail(f"{label}.path: only relative POSIX paths are allowed")
        parts = PurePosixPath(raw).parts
        if not parts or any(part in ("", ".", "..") for part in parts):
            _fail(f"{label}.path: path traversal/empty component is forbidden")
        expected_sha = sha(ref["sha256"], f"{label}.sha256")
        expected_size = integer(ref, "size_bytes", label, minimum=0)
        candidate = (self.root.joinpath(*parts)).resolve()
        try:
            inside = os.path.commonpath((str(self.root), str(candidate))) == str(self.root)
        except ValueError:
            inside = False
        if not inside:
            _fail(f"{label}.path: resolves outside manifest root")
        cursor = self.root
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                _fail(f"{label}.path: symlink components are forbidden: {raw}")
        if not candidate.is_file() or candidate.is_symlink():
            _fail(f"{label}.path: regular file required: {raw}")
        actual_size = candidate.stat().st_size
        if actual_size != expected_size:
            _fail(f"{label}: size mismatch for {raw}: {actual_size} != {expected_size}")
        actual_sha = sha256_file(candidate, label)
        if actual_sha != expected_sha:
            _fail(f"{label}: SHA-256 mismatch for {raw}")
        key = str(candidate)
        prior = self._seen.get(key)
        if prior is not None and prior != actual_sha:
            _fail(f"{label}: same path has conflicting SHA references")
        self._seen[key] = actual_sha
        return candidate

    def json(self, ref: Mapping[str, Any], label: str) -> Tuple[Path, Dict[str, Any]]:
        path = self.path(ref, label)
        return path, read_json(path, label)


def validate_classification(obj: Mapping[str, Any], label: str) -> None:
    require_keys(
        obj,
        (
            "execution_mode",
            "conditional_waiver",
            "diagnostic_only",
            "formal_eligible",
            "performance_eligible",
            "paper_claim_eligible",
        ),
        label=label,
    )
    exact(obj, "execution_mode", "formal", label)
    exact(obj, "conditional_waiver", False, label)
    exact(obj, "diagnostic_only", False, label)
    exact(obj, "formal_eligible", True, label)
    exact(obj, "performance_eligible", True, label)
    # Claim eligibility is intentionally held until independent Figure/QA.
    exact(obj, "paper_claim_eligible", False, label)


def validate_protocol(protocol: Mapping[str, Any]) -> Dict[str, Any]:
    require_keys(
        protocol,
        (
            "systems",
            "repeat_indices",
            "dataset_id",
            "scale_factor",
            "vertex_count",
            "directed_edge_count",
            "property_count",
            "query_count",
            "input_sha256",
            "workload_id",
            "query_trace_sha256",
            "seed",
            "cache_state",
            "concurrency",
            "interface_scope",
            "host_fingerprint",
            "harness_git_sha",
        ),
        label="protocol",
    )
    systems = protocol["systems"]
    if not _is_list(systems) or len(systems) != len(SYSTEMS):
        _fail("protocol.systems: exactly seven fixed systems are required")
    specs: Dict[str, Dict[str, str]] = {}
    for index, ((expected_key, expected_name), item) in enumerate(zip(SYSTEMS, systems)):
        label = f"protocol.systems[{index}]"
        if not _is_dict(item):
            _fail(f"{label}: object required")
        require_keys(item, ("system_key", "display_name", "variant"), label=label)
        key = text(item, "system_key", label)
        name = text(item, "display_name", label)
        variant = text(item, "variant", label)
        if key != expected_key or name != expected_name:
            _fail(
                f"{label}: fixed order/name mismatch; expected "
                f"{expected_key}/{expected_name}, got {key}/{name}"
            )
        if key == "seml0" and _canonical_alias(variant) != "budg-b64":
            _fail("protocol SemL0 variant must be budg-b64")
        if key == "seml0-naive" and _canonical_alias(variant) != "naive":
            _fail("protocol SemL0-naive variant must be naive")
        specs[key] = {"display_name": name, "variant": variant}
    if protocol["repeat_indices"] != [1, 2, 3]:
        _fail("protocol.repeat_indices must be exactly [1, 2, 3]")
    text(protocol, "dataset_id", "protocol")
    number(protocol, "scale_factor", "protocol", positive=True)
    integer(protocol, "vertex_count", "protocol", minimum=1)
    integer(protocol, "directed_edge_count", "protocol", minimum=1)
    integer(protocol, "property_count", "protocol", minimum=0)
    integer(protocol, "query_count", "protocol", minimum=1)
    sha(protocol["input_sha256"], "protocol.input_sha256")
    text(protocol, "workload_id", "protocol")
    sha(protocol["query_trace_sha256"], "protocol.query_trace_sha256")
    integer(protocol, "seed", "protocol", minimum=0)
    if protocol["cache_state"] not in {"cold", "warm", "fixed_budget"}:
        _fail("protocol.cache_state: unsupported cache state")
    integer(protocol, "concurrency", "protocol", minimum=1)
    text(protocol, "interface_scope", "protocol")
    host = sha(protocol["host_fingerprint"], "protocol.host_fingerprint")
    git = sha(protocol["harness_git_sha"], "protocol.harness_git_sha", SHA1_RE)
    return {
        "systems": specs,
        "dataset_id": protocol["dataset_id"],
        "scale_factor": protocol["scale_factor"],
        "vertex_count": protocol["vertex_count"],
        "directed_edge_count": protocol["directed_edge_count"],
        "property_count": protocol["property_count"],
        "query_count": protocol["query_count"],
        "input_sha256": protocol["input_sha256"].lower(),
        "workload_id": protocol["workload_id"],
        "query_trace_sha256": protocol["query_trace_sha256"].lower(),
        "seed": protocol["seed"],
        "cache_state": protocol["cache_state"],
        "concurrency": protocol["concurrency"],
        "interface_scope": protocol["interface_scope"],
        "host_fingerprint": host,
        "harness_git_sha": git,
    }


def _ref_sha(ref: Mapping[str, Any]) -> str:
    return str(ref["sha256"]).lower()


def _nested_ref_sha(value: Any) -> Optional[str]:
    if not _is_dict(value):
        return None
    item = value.get("sha256")
    return item.lower() if type(item) is str and SHA256_RE.fullmatch(item) else None


def validate_admission_epochs(
    manifest: Mapping[str, Any],
    protocol: Mapping[str, Any],
    verifier: AssetVerifier,
) -> Dict[str, Dict[str, Any]]:
    epochs = manifest["admission_epochs"]
    if not _is_list(epochs) or not epochs:
        _fail("admission_epochs: at least one epoch is required")
    expected_keys = set(RUN_KEYS)
    assigned: Dict[str, Dict[str, Any]] = {}
    epoch_ids: Set[str] = set()
    for index, epoch in enumerate(epochs):
        label = f"admission_epochs[{index}]"
        if not _is_dict(epoch):
            _fail(f"{label}: object required")
        require_keys(
            epoch,
            ("epoch_id", "run_keys", "gate_result", "batch_lease", "batch_lease_marker"),
            label=label,
        )
        epoch_id = text(epoch, "epoch_id", label)
        if epoch_id in epoch_ids:
            _fail(f"{label}: duplicate epoch_id")
        epoch_ids.add(epoch_id)
        run_keys = epoch["run_keys"]
        if not _is_list(run_keys) or not run_keys:
            _fail(f"{label}.run_keys: non-empty array required")
        if len(run_keys) != len(set(run_keys)):
            _fail(f"{label}.run_keys: duplicate run key")
        for run_key in run_keys:
            if run_key not in expected_keys:
                _fail(f"{label}.run_keys: unknown run key {run_key!r}")
            if run_key in assigned:
                _fail(f"{label}.run_keys: run assigned to multiple epochs: {run_key}")

        gate_ref = epoch["gate_result"]
        lease_ref = epoch["batch_lease"]
        marker_ref = epoch["batch_lease_marker"]
        _, gate = verifier.json(gate_ref, f"{label}.gate_result")
        lease_path, lease = verifier.json(lease_ref, f"{label}.batch_lease")
        _, marker = verifier.json(marker_ref, f"{label}.batch_lease_marker")
        if (
            gate.get("schema_version") != "p02b-sf10-sentinel-result-v2"
            or gate.get("state") != "PASS"
            or gate.get("fixture_only") is not False
            or gate.get("formal_gate_eligible") is not True
            or gate.get("downstream_release_eligible") is not True
        ):
            _fail(f"{label}: gate is not a fresh formal PASS")
        if (
            lease.get("schema_version") != "cidr-batch-lease-v2"
            or lease.get("state") != "PASS"
            or lease.get("scope") != "single-host-single-head-formal-batch"
            or lease.get("consumers") != ["P10", "P20"]
        ):
            _fail(f"{label}: batch lease schema/state/scope drift")
        classification = lease.get("classification")
        if (
            not _is_dict(classification)
            or classification.get("performance_eligible") is not False
            or classification.get("purpose") != "downstream-admission-only"
        ):
            _fail(f"{label}: admission lease must remain admission-only")
        issued = utc(lease.get("issued_at_utc"), f"{label}.batch_lease.issued_at_utc")
        expires = utc(lease.get("expires_at_utc"), f"{label}.batch_lease.expires_at_utc")
        if expires <= issued or (expires - issued).total_seconds() != 86400:
            _fail(f"{label}: lease duration must be exactly 24 hours")
        identity = lease.get("identity")
        if not _is_dict(identity):
            _fail(f"{label}: lease identity missing")
        host = identity.get("host")
        host_fingerprint = host.get("fingerprint_sha256") if _is_dict(host) else None
        if type(host_fingerprint) is not str or host_fingerprint.lower() != protocol["host_fingerprint"]:
            _fail(f"{label}: lease host fingerprint drift")
        repo = identity.get("repo")
        if not _is_dict(repo) or repo.get("head") != protocol["harness_git_sha"] or repo.get("clean_at_issue") is not True:
            _fail(f"{label}: lease repository identity drift")
        binary = identity.get("binary")
        if not _is_dict(binary):
            _fail(f"{label}: lease binary identity missing")
        p02b = lease.get("p02b")
        if not _is_dict(p02b):
            _fail(f"{label}: lease P02B binding missing")
        p02b_result = p02b.get("result")
        if _nested_ref_sha(p02b_result) != _ref_sha(gate_ref):
            _fail(f"{label}: lease P02B result is not bound to gate result")
        gate_contract = p02b.get("gate_contract")
        if (
            not _is_dict(gate_contract)
            or p02b.get("gate_contract_sha256") != gate_contract.get("contract_sha256")
        ):
            _fail(f"{label}: lease gate contract SHA drift")
        if (
            marker.get("schema_version") != "cidr-batch-lease-marker-v2"
            or marker.get("state") != "PASS"
            or marker.get("lease_sha256") != _ref_sha(lease_ref)
        ):
            _fail(f"{label}: lease marker binding drift")
        epoch_info = {
            "epoch_id": epoch_id,
            "issued": issued,
            "expires": expires,
            "lease_sha256": _ref_sha(lease_ref),
            "lease_binary_sha256": str(binary.get("sha256", "")).lower(),
            "lease_repo_head": protocol["harness_git_sha"],
        }
        for run_key in run_keys:
            assigned[run_key] = epoch_info
    if set(assigned) != expected_keys:
        missing = sorted(expected_keys - set(assigned))
        _fail(f"admission epochs do not cover exactly 21 runs; missing={missing}")
    return assigned


def _assert_optional_formal_flags(value: Any, label: str) -> None:
    """Reject explicit conditional/fixture/false-eligibility provenance."""

    if _is_dict(value):
        for key, item in value.items():
            lowered = key.lower()
            if lowered in {"conditional_waiver", "conditional_execution"} and item is True:
                _fail(f"{label}: conditional provenance is not formal")
            if lowered in {"formal_eligible", "performance_eligible"} and item is False:
                _fail(f"{label}: explicit false eligibility")
            if lowered in {"mode", "execution_mode", "service_mode"} and item in {
                "conditional",
                "fixture",
            }:
                _fail(f"{label}: execution mode is {item!r}, not formal")
            _assert_optional_formal_flags(item, f"{label}.{key}")
    elif _is_list(value):
        for index, item in enumerate(value):
            _assert_optional_formal_flags(item, f"{label}[{index}]")


def _same_number(left: Any, right: Any, label: str, tolerance: float = 1e-9) -> None:
    if type(left) not in (int, float) or type(right) not in (int, float):
        _fail(f"{label}: numeric source missing")
    if not math.isfinite(float(left)) or not math.isfinite(float(right)):
        _fail(f"{label}: non-finite numeric source")
    if abs(float(left) - float(right)) > max(tolerance, abs(float(right)) * 1e-9):
        _fail(f"{label}: source mismatch {left!r} != {right!r}")


def validate_p31(
    run: Mapping[str, Any],
    protocol: Mapping[str, Any],
    epoch: Mapping[str, Any],
    verifier: AssetVerifier,
) -> Dict[str, Any]:
    label = f"{run['run_key']}.p31"
    manifest_path, p31_manifest = verifier.json(run["artifacts"]["p31_manifest"], label + ".manifest")
    validation_path, validation = verifier.json(run["artifacts"]["p31_validation"], label + ".validation")
    done_path, done = verifier.json(run["artifacts"]["p31_done"], label + ".DONE")
    if (
        p31_manifest.get("schema_version") != "cidr-run-manifest-v1"
        or p31_manifest.get("state") != "PASS"
        or p31_manifest.get("command_exit_code") != 0
        or p31_manifest.get("collector_exit_code") != 0
        or p31_manifest.get("performance_eligible_declared") is not True
    ):
        _fail(f"{label}: P31 manifest is not a formal PASS")
    p31_validation_obj = p31_manifest.get("validation")
    if (
        not _is_dict(p31_validation_obj)
        or p31_validation_obj.get("schema_version") != "cidr-run-manifest-v1"
        or p31_validation_obj.get("state") != "PASS"
        or p31_validation_obj.get("errors") != []
        or p31_validation_obj.get("warnings") != []
    ):
        _fail(f"{label}: embedded P31 validation is not clean")
    if (
        validation.get("schema_version") != "cidr-run-manifest-v1"
        or validation.get("state") != "PASS"
        or validation.get("errors") != []
        or validation.get("warnings") != []
    ):
        _fail(f"{label}: P31 validation artifact is not clean")
    p31_done_ref = run["artifacts"]["p31_done"]
    p31_validation_ref = run["artifacts"]["p31_validation"]
    p31_manifest_ref = run["artifacts"]["p31_manifest"]
    if (
        done.get("state") != "PASS"
        or done.get("manifest_sha256") != _ref_sha(p31_manifest_ref)
        or done.get("validation_sha256") != _ref_sha(p31_validation_ref)
    ):
        _fail(f"{label}: DONE backlink mismatch")
    host = p31_manifest.get("host")
    if not _is_dict(host) or str(host.get("fingerprint_sha256", "")).lower() != protocol["host_fingerprint"]:
        _fail(f"{label}: P31 host fingerprint drift")
    repo = p31_manifest.get("repo")
    if not _is_dict(repo) or repo.get("git_sha") != protocol["harness_git_sha"]:
        _fail(f"{label}: P31 repo SHA drift")
    inputs = p31_manifest.get("inputs")
    if not _is_dict(inputs):
        _fail(f"{label}: P31 inputs missing")
    dataset = inputs.get("dataset")
    query = inputs.get("query_or_trace") or inputs.get("truth")
    if _nested_ref_sha(dataset) != protocol["input_sha256"] or _nested_ref_sha(query) != protocol["query_trace_sha256"]:
        _fail(f"{label}: P31 input/trace SHA drift")
    return {
        "manifest_path": manifest_path,
        "validation_path": validation_path,
        "done_path": done_path,
        "manifest": p31_manifest,
    }


def validate_run(
    run: Mapping[str, Any],
    protocol: Mapping[str, Any],
    epoch: Mapping[str, Any],
    verifier: AssetVerifier,
) -> Dict[str, Any]:
    run_key = text(run, "run_key", "run")
    label = run_key
    require_keys(
        run,
        (
            "run_key",
            "run_id",
            "system_key",
            "display_name",
            "variant",
            "repeat_index",
            "timestamp_utc",
            "host_fingerprint",
            "git_sha",
            "binary_sha256",
            "system_version",
            "classification",
            "outcome",
            "metrics",
            "artifacts",
        ),
        label=label,
    )
    if run_key not in RUN_KEYS:
        _fail(f"{label}: unknown run key")
    expected_system, repeat_text = run_key.split(":r")
    repeat = integer(run, "repeat_index", label, minimum=1)
    if repeat not in (1, 2, 3) or repeat != int(repeat_text):
        _fail(f"{label}: repeat index does not match run key")
    if run.get("system_key") != expected_system:
        _fail(f"{label}: system_key does not match run key")
    spec = protocol["systems"][expected_system]
    if run.get("display_name") != spec["display_name"] or run.get("variant") != spec["variant"]:
        _fail(f"{label}: display name/variant drift from protocol")
    timestamp = utc(run.get("timestamp_utc"), f"{label}.timestamp_utc")
    if timestamp < epoch["issued"] or timestamp >= epoch["expires"]:
        _fail(f"{label}: run timestamp is outside its admission lease")
    host = sha(run["host_fingerprint"], f"{label}.host_fingerprint")
    if host != protocol["host_fingerprint"]:
        _fail(f"{label}: host fingerprint drift")
    git = sha(run["git_sha"], f"{label}.git_sha", SHA1_RE)
    if git != protocol["harness_git_sha"]:
        _fail(f"{label}: harness git SHA drift")
    binary = sha(run["binary_sha256"], f"{label}.binary_sha256")
    text(run, "run_id", label)
    text(run, "system_version", label)
    validate_classification(run["classification"], f"{label}.classification")
    outcome = run["outcome"]
    if not _is_dict(outcome):
        _fail(f"{label}.outcome: object required")
    require_keys(outcome, ("state", "superseded", "failed_marker_present"), label=f"{label}.outcome")
    exact(outcome, "state", "PASS", f"{label}.outcome")
    exact(outcome, "superseded", False, f"{label}.outcome")
    exact(outcome, "failed_marker_present", False, f"{label}.outcome")

    metrics = run["metrics"]
    if not _is_dict(metrics):
        _fail(f"{label}.metrics: object required")
    require_keys(
        metrics,
        (
            "latency_p50_us",
            "latency_p95_us",
            "latency_p99_us",
            "completed_queries",
            "timeout_queries",
            "query_count",
            "measurement_s",
            "warmup_s",
            "completed_qps",
            "load_wall_s",
            "final_disk_bytes",
            "expected_digest_sha256",
            "actual_digest_sha256",
            "digest_pass",
            "mismatch_count",
        ),
        optional=("offered_qps", "load_peak_rss_bytes"),
        label=f"{label}.metrics",
    )
    p50 = number(metrics, "latency_p50_us", label + ".metrics", positive=True)
    p95 = number(metrics, "latency_p95_us", label + ".metrics", positive=True)
    p99 = number(metrics, "latency_p99_us", label + ".metrics", positive=True)
    if not (p50 <= p95 <= p99):
        _fail(f"{label}: latency quantiles are not monotonic")
    completed = integer(metrics, "completed_queries", label + ".metrics", minimum=0)
    timeout = integer(metrics, "timeout_queries", label + ".metrics", minimum=0)
    query_count = integer(metrics, "query_count", label + ".metrics", minimum=1)
    if query_count != protocol["query_count"] or completed + timeout != query_count:
        _fail(f"{label}: query count/completion/timeout accounting mismatch")
    measurement = number(metrics, "measurement_s", label + ".metrics", positive=True)
    warmup = number(metrics, "warmup_s", label + ".metrics", nonnegative=True)
    declared_qps = number(metrics, "completed_qps", label + ".metrics", positive=True)
    derived_qps = completed / measurement
    if abs(declared_qps - derived_qps) > max(1e-9, abs(derived_qps) * 0.01):
        _fail(f"{label}: completed_qps disagrees with completed_queries/measurement_s")
    load_wall = number(metrics, "load_wall_s", label + ".metrics", positive=True)
    disk = integer(metrics, "final_disk_bytes", label + ".metrics", minimum=1)
    expected_digest = sha(metrics["expected_digest_sha256"], f"{label}.metrics.expected_digest_sha256")
    actual_digest = sha(metrics["actual_digest_sha256"], f"{label}.metrics.actual_digest_sha256")
    exact(metrics, "digest_pass", True, label + ".metrics")
    exact(metrics, "mismatch_count", 0, label + ".metrics")
    if expected_digest != actual_digest:
        _fail(f"{label}: expected/actual digest mismatch")
    if "offered_qps" in metrics:
        number(metrics, "offered_qps", label + ".metrics", positive=True)
    if "load_peak_rss_bytes" in metrics:
        integer(metrics, "load_peak_rss_bytes", label + ".metrics", minimum=1)

    artifacts = run["artifacts"]
    if not _is_dict(artifacts):
        _fail(f"{label}.artifacts: object required")
    require_keys(artifacts, ASSET_ROLES, label=f"{label}.artifacts")
    # Hash every declared asset before trusting any contents.
    paths: Dict[str, Path] = {}
    for role in ASSET_ROLES:
        paths[role] = verifier.path(artifacts[role], f"{label}.{role}")

    _, request = verifier.json(artifacts["adapter_request"], label + ".adapter_request")
    if request.get("execution_mode") != "formal":
        _fail(f"{label}: adapter request is not formal")
    if request.get("system_id") not in {expected_system, UNDERLYING_SYSTEM.get(expected_system, expected_system)}:
        _fail(f"{label}: adapter request system binding drift")
    if request.get("repeat_index") != repeat:
        _fail(f"{label}: adapter request repeat binding drift")
    if request.get("run_id") not in {None, run["run_id"]}:
        _fail(f"{label}: adapter request run binding drift")

    _, provenance = verifier.json(artifacts["adapter_provenance"], label + ".adapter_provenance")
    _assert_optional_formal_flags(provenance, label + ".adapter_provenance")
    if provenance.get("variant") is not None:
        observed_variant = _canonical_alias(str(provenance["variant"]))
        if observed_variant != _canonical_alias(str(run["variant"])):
            _fail(f"{label}: adapter provenance variant drift")
    provenance_repo = provenance.get("repo")
    provenance_head = provenance_repo.get("head") if _is_dict(provenance_repo) else None
    if provenance_head not in (None, protocol["harness_git_sha"]):
        _fail(f"{label}: adapter provenance repo drift")
    binding = provenance.get("e01_binding")
    if not _is_dict(binding):
        _fail(f"{label}: adapter provenance lacks e01_binding")
    require_keys(
        binding,
        (
            "dataset_id",
            "input_sha256",
            "workload_id",
            "query_trace_sha256",
            "seed",
            "cache_state",
            "concurrency",
            "interface_scope",
            "host_fingerprint",
            "git_sha",
            "binary_sha256",
            "system_version",
        ),
        label=label + ".adapter_provenance.e01_binding",
    )
    binding_input = binding.get("input_sha256")
    binding_trace = binding.get("query_trace_sha256")
    binding_host = binding.get("host_fingerprint")
    binding_git = binding.get("git_sha")
    binding_binary = binding.get("binary_sha256")
    if (
        binding["dataset_id"] != protocol["dataset_id"]
        or type(binding_input) is not str
        or binding_input.lower() != protocol["input_sha256"]
        or binding["workload_id"] != protocol["workload_id"]
        or type(binding_trace) is not str
        or binding_trace.lower() != protocol["query_trace_sha256"]
        or binding["seed"] != protocol["seed"]
        or binding["cache_state"] != protocol["cache_state"]
        or binding["concurrency"] != protocol["concurrency"]
        or binding["interface_scope"] != protocol["interface_scope"]
        or type(binding_host) is not str
        or binding_host.lower() != protocol["host_fingerprint"]
        or type(binding_git) is not str
        or binding_git.lower() != run["git_sha"].lower()
        or type(binding_binary) is not str
        or binding_binary.lower() != run["binary_sha256"].lower()
        or binding["system_version"] != run["system_version"]
    ):
        _fail(f"{label}: e01_binding protocol/identity drift")

    p31_info = validate_p31(run, protocol, epoch, verifier)
    _, batch_admission = verifier.json(artifacts["batch_admission"], label + ".batch_admission")
    if (
        batch_admission.get("schema_version") != "cidr-p10-repeat-batch-admission-v2"
        or batch_admission.get("state") != "PASS"
        or batch_admission.get("protocol") != "batch-lease-v2"
        or batch_admission.get("consumer") != "P10"
    ):
        _fail(f"{label}: repeat batch admission is not a formal PASS")
    receipt = batch_admission.get("receipt")
    if not _is_dict(receipt):
        _fail(f"{label}: repeat batch admission receipt missing")
    if (
        receipt.get("schema_version") != "cidr-batch-lease-admission-v2"
        or receipt.get("state") != "PASS"
        or receipt.get("consumer") != "P10"
        or receipt.get("lease_sha256") != epoch["lease_sha256"]
        or receipt.get("host", {}).get("fingerprint_sha256", "").lower() != protocol["host_fingerprint"]
        or receipt.get("repo_head") != protocol["harness_git_sha"]
        or number(receipt, "remaining_seconds", label + ".batch_admission.receipt", positive=True) <= 0
    ):
        _fail(f"{label}: batch lease admission receipt binding drift")
    if _nested_ref_sha(batch_admission.get("lease")) != epoch["lease_sha256"]:
        _fail(f"{label}: repeat admission lease ref drift")
    integrity = p31_info["manifest"].get("integrity_guard")
    if integrity is not None:
        if (
            not _is_dict(integrity)
            or integrity.get("state") != "PASS"
            or integrity.get("consumer") != "P10"
            or integrity.get("lease_sha256") != epoch["lease_sha256"]
        ):
            _fail(f"{label}: P31 integrity guard binding drift")

    _, cost = verifier.json(artifacts["cost_receipt"], label + ".cost_receipt")
    require_keys(
        cost,
        (
            "schema_version",
            "state",
            "run_key",
            "load_wall_s",
            "final_disk_bytes",
            "accounting_rule",
            "inferred",
        ),
        optional=("load_peak_rss_bytes",),
        label=label + ".cost_receipt",
    )
    if (
        cost.get("schema_version") != "cidr-e01-cost-receipt-v2"
        or cost.get("state") != "PASS"
        or cost.get("run_key") != run_key
        or cost.get("inferred") is not False
    ):
        _fail(f"{label}: cost receipt is not a direct measured PASS")
    text(cost, "accounting_rule", label + ".cost_receipt")
    _same_number(cost["load_wall_s"], load_wall, label + ": load_wall_s")
    if cost["final_disk_bytes"] != disk:
        _fail(f"{label}: cost receipt final_disk_bytes mismatch")
    if "load_peak_rss_bytes" in cost:
        if "load_peak_rss_bytes" not in metrics or cost["load_peak_rss_bytes"] != metrics["load_peak_rss_bytes"]:
            _fail(f"{label}: load_peak_rss_bytes mismatch")

    # Cross-check the validated result against the manifest's normalized metrics.
    _, validated = verifier.json(artifacts["validated_result"], label + ".validated_result")
    underlying = UNDERLYING_SYSTEM.get(expected_system, expected_system)
    if (
        validated.get("schema_version") != "cidr-p10-validated-repeat-v1"
        or validated.get("system_id") != underlying
        or validated.get("repeat_index") != repeat
        or validated.get("display_name")
        not in {SYSTEM_NAMES[expected_system], SYSTEM_NAMES[underlying]}
        or validated.get("interface_scope") != protocol["interface_scope"]
        or validated.get("timeout_queries") != timeout
        or validated.get("completed_queries") != completed
        or validated.get("mismatch_queries") != 0
        or validated.get("expected_digest_sha256") != expected_digest
        or validated.get("actual_digest_sha256") != actual_digest
    ):
        _fail(f"{label}: validated-result identity/digest/count drift")
    _same_number(validated.get("latency_p50_us"), p50, label + ": validated P50")
    _same_number(validated.get("latency_p95_us"), p95, label + ": validated P95")
    _same_number(validated.get("latency_p99_us"), p99, label + ": validated P99")
    _same_number(validated.get("measurement_s"), measurement, label + ": validated measurement")
    _same_number(validated.get("warmup_s"), warmup, label + ": validated warmup")
    _same_number(validated.get("qps"), declared_qps, label + ": validated QPS", tolerance=1e-6)
    if validated.get("request", {}).get("sha256") != _ref_sha(artifacts["adapter_request"]):
        _fail(f"{label}: validated-result request SHA backlink drift")
    p31_ref = validated.get("p31")
    if not _is_dict(p31_ref):
        _fail(f"{label}: validated-result P31 backlink missing")
    if (
        p31_ref.get("manifest_sha256") != _ref_sha(artifacts["p31_manifest"])
        or p31_ref.get("validation_sha256") != _ref_sha(artifacts["p31_validation"])
        or p31_ref.get("done_sha256") != _ref_sha(artifacts["p31_done"])
    ):
        _fail(f"{label}: validated-result P31 SHA backlinks drift")
    return {
        "timestamp": timestamp,
        "binary_sha256": binary,
        "system_version": run["system_version"],
        "metrics": metrics,
        "validated": validated,
        "asset_sha": {role: _ref_sha(artifacts[role]) for role in ASSET_ROLES},
    }


def _format_number(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        return format(value, ".12g")
    return str(value)


def normalize_manifest(manifest_path: Path, out_dir: Optional[Path], schema_path: Path) -> Dict[str, Any]:
    manifest_path = manifest_path.resolve()
    if not manifest_path.is_file() or manifest_path.is_symlink():
        _fail(f"manifest must be a regular file: {manifest_path}")
    schema_sha = validate_schema_document(schema_path.resolve())
    manifest = read_json(manifest_path, "E01 manifest")
    require_keys(
        manifest,
        (
            "schema_version",
            "experiment_id",
            "campaign_id",
            "created_at_utc",
            "classification",
            "protocol",
            "admission_epochs",
            "runs",
        ),
        label="E01 manifest",
    )
    exact(manifest, "schema_version", "cidr-e01-formal-manifest-v2", "E01 manifest")
    exact(manifest, "experiment_id", "E01", "E01 manifest")
    text(manifest, "campaign_id", "E01 manifest")
    created = utc(manifest["created_at_utc"], "E01 manifest.created_at_utc")
    validate_classification(manifest["classification"], "E01 manifest.classification")
    protocol = validate_protocol(manifest["protocol"])
    verifier = AssetVerifier(manifest_path.parent)
    epoch_by_run = validate_admission_epochs(manifest, protocol, verifier)
    runs = manifest["runs"]
    if not _is_list(runs) or len(runs) != 21:
        _fail("E01 manifest.runs: exactly 21 runs are required")
    observed_keys = [run.get("run_key") if _is_dict(run) else None for run in runs]
    if tuple(observed_keys) != RUN_KEYS:
        _fail("E01 manifest.runs: fixed system/repeat order is required")
    run_ids: Set[str] = set()
    system_identity: Dict[str, Tuple[str, str]] = {}
    rows: List[Dict[str, Any]] = []
    normalized_runs: List[Dict[str, Any]] = []
    for run in runs:
        if not _is_dict(run):
            _fail("E01 manifest.runs: each item must be an object")
        run_key = run.get("run_key")
        run_id = text(run, "run_id", str(run_key))
        if run_id in run_ids:
            _fail(f"{run_key}: duplicate run_id")
        run_ids.add(run_id)
        info = validate_run(run, protocol, epoch_by_run[run_key], verifier)
        system_key = run["system_key"]
        identity = (run["binary_sha256"].lower(), run["system_version"])
        prior = system_identity.get(system_key)
        if prior is not None and prior != identity:
            _fail(f"{system_key}: binary_sha256/system_version drift across repeats")
        system_identity[system_key] = identity
        normalized_runs.append(info)
        metrics = info["metrics"]
        row: Dict[str, Any] = {
            "experiment_id": "E01",
            "run_id": run["run_id"],
            "run_key": run_key,
            "repeat_index": run["repeat_index"],
            "timestamp_utc": run["timestamp_utc"],
            "host_fingerprint": protocol["host_fingerprint"],
            "git_sha": protocol["harness_git_sha"],
            "binary_sha256": run["binary_sha256"].lower(),
            "system": run["display_name"],
            "system_key": system_key,
            "variant": run["variant"],
            "dataset_id": protocol["dataset_id"],
            "scale_factor": protocol["scale_factor"],
            "vertex_count": protocol["vertex_count"],
            "directed_edge_count": protocol["directed_edge_count"],
            "property_count": protocol["property_count"],
            "input_sha256": protocol["input_sha256"],
            "workload_id": protocol["workload_id"],
            "query_trace_sha256": protocol["query_trace_sha256"],
            "seed": protocol["seed"],
            "cache_state": protocol["cache_state"],
            "concurrency": protocol["concurrency"],
            "warmup_s": metrics["warmup_s"],
            "measurement_s": metrics["measurement_s"],
            "digest_pass": True,
            "mismatch_count": 0,
            "latency_p50_us": metrics["latency_p50_us"],
            "latency_p95_us": metrics["latency_p95_us"],
            "latency_p99_us": metrics["latency_p99_us"],
            "completed_queries": metrics["completed_queries"],
            "timeout_queries": metrics["timeout_queries"],
            "offered_qps": metrics.get("offered_qps", ""),
            "completed_qps": metrics["completed_qps"],
            "load_wall_s": metrics["load_wall_s"],
            "final_disk_bytes": metrics["final_disk_bytes"],
            "load_peak_rss_bytes": metrics.get("load_peak_rss_bytes", ""),
            "interface_scope": protocol["interface_scope"],
            "system_version": run["system_version"],
            "unsupported_reason": "",
            "formal_eligible": True,
            "performance_eligible": True,
            "paper_claim_eligible": False,
            "validated_result_sha256": info["asset_sha"]["validated_result"],
            "p31_manifest_sha256": info["asset_sha"]["p31_manifest"],
            "p31_validation_sha256": info["asset_sha"]["p31_validation"],
            "p31_done_sha256": info["asset_sha"]["p31_done"],
            "batch_admission_sha256": info["asset_sha"]["batch_admission"],
            "cost_receipt_sha256": info["asset_sha"]["cost_receipt"],
        }
        rows.append(row)

    # SemL0 and SemL0-naive must be the same engine build/version.
    if system_identity.get("seml0") != system_identity.get("seml0-naive"):
        _fail("SemL0/SemL0-naive binary or system_version is not identical")
    # The manifest timestamp must not be after any run (a common accidental copy).
    if any(info["timestamp"] > created + dt.timedelta(seconds=1) for info in normalized_runs):
        _fail("manifest created_at_utc precedes a run by more than one second")

    manifest_bytes = read_bytes(manifest_path, "E01 manifest")
    receipt: Dict[str, Any] = {
        "schema_version": "cidr-e01-normalization-receipt-v2",
        "state": "PASS",
        "experiment_id": "E01",
        "campaign_id": manifest["campaign_id"],
        "source_manifest_sha256": sha256_bytes(manifest_bytes),
        "source_schema_sha256": schema_sha,
        "row_count": len(rows),
        "run_keys": list(RUN_KEYS),
        "systems": [name for _, name in SYSTEMS],
        "source_eligibility": {
            "formal_eligible": True,
            "performance_eligible": True,
            "paper_claim_eligible": False,
            "conditional_waiver": False,
            "diagnostic_only": False,
        },
        "output_eligibility": {
            "formal_eligible": True,
            "performance_eligible": True,
            "paper_claim_eligible": False,
            "claim_status": "PENDING_INDEPENDENT_FIGURE_QA",
        },
        "checks": {
            "fixed_order_7x3": "PASS",
            "unique_run_keys": "PASS",
            "matched_input_trace_cache_host_interface": "PASS",
            "latency_monotonicity": "PASS",
            "qps_reconciliation": "PASS",
            "digest_and_mismatch": "PASS",
            "p31_and_asset_backlinks": "PASS",
            "conditional_legacy_upgrade": "NOT_PERFORMED",
            "paper_claim_upgrade": "NOT_PERFORMED",
        },
    }
    if out_dir is None:
        return {"rows": rows, "receipt": receipt, "schema_sha256": schema_sha}

    out_dir = out_dir.resolve()
    if out_dir.exists():
        _fail(f"refusing to overwrite existing output directory: {out_dir}")
    parent = out_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=".e01-normalize-", dir=str(parent)))
    try:
        # Copy exact source bytes; the copy and all generated files are checksummed.
        (temp_dir / "E01-formal-manifest-v2.json").write_bytes(manifest_bytes)
        (temp_dir / "e01-manifest-v2.schema.json").write_bytes(read_bytes(schema_path, "manifest schema"))
        tsv_path = temp_dir / "E01-results.tsv"
        with tsv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TSV_COLUMNS, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _format_number(row.get(key, "")) for key in TSV_COLUMNS})
        receipt["artifacts"] = {
            "results_tsv": {
                "path": "E01-results.tsv",
                "sha256": sha256_file(tsv_path, "E01-results.tsv"),
                "size_bytes": tsv_path.stat().st_size,
            },
            "manifest_copy": {
                "path": "E01-formal-manifest-v2.json",
                "sha256": sha256_file(temp_dir / "E01-formal-manifest-v2.json", "manifest copy"),
                "size_bytes": (temp_dir / "E01-formal-manifest-v2.json").stat().st_size,
            },
            "schema_copy": {
                "path": "e01-manifest-v2.schema.json",
                "sha256": sha256_file(temp_dir / "e01-manifest-v2.schema.json", "schema copy"),
                "size_bytes": (temp_dir / "e01-manifest-v2.schema.json").stat().st_size,
            },
        }
        receipt_path = temp_dir / "normalization-receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        checksummed = [
            "E01-formal-manifest-v2.json",
            "E01-results.tsv",
            "e01-manifest-v2.schema.json",
            "normalization-receipt.json",
        ]
        sums = "\n".join(
            f"{sha256_file(temp_dir / name, name)}  {name}" for name in checksummed
        ) + "\n"
        (temp_dir / "SHA256SUMS").write_text(sums, encoding="utf-8")
        os.replace(str(temp_dir), str(out_dir))
    except BaseException:
        shutil.rmtree(str(temp_dir), ignore_errors=True)
        raise
    return {"rows": rows, "receipt": receipt, "schema_sha256": schema_sha}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print a receipt summary without writing any output",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.dry_run and args.out_dir is not None:
            _fail("--dry-run cannot be combined with --out-dir")
        result = normalize_manifest(
            args.manifest,
            None if args.dry_run else args.out_dir,
            args.schema,
        )
        print(
            json.dumps(
                {
                    "state": result["receipt"]["state"],
                    "row_count": result["receipt"]["row_count"],
                    "campaign_id": result["receipt"]["campaign_id"],
                    "paper_claim_eligible": result["receipt"]["output_eligibility"][
                        "paper_claim_eligible"
                    ],
                    "out_dir": str(args.out_dir.resolve()) if args.out_dir else None,
                },
                sort_keys=True,
            )
        )
        return 0
    except ContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
