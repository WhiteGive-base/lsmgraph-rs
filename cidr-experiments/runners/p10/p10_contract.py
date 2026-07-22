#!/usr/bin/env python3
"""Strict P10/P11 suite and adapter contracts.

The module deliberately uses only the Python standard library.  JSON Schema
files document the wire format, while these checks are the executable,
fail-closed authority used by the orchestrator.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


SUITE_SCHEMA_VERSION = "cidr-p10-suite-v1"
REQUEST_SCHEMA_VERSION = "cidr-p10-adapter-request-v1"
RESULT_SCHEMA_VERSION = "cidr-p10-adapter-result-v1"
CONTRACT_VERSION = "cidr-typed-neighbor-adapter-v1"
TRUTH_DIGEST_ALGORITHM = "mix64-dense-dst-count-sum-xor-v1"
SEQUENCE_DIGEST_ALGORITHM = "sha256-pass-query-count-sum-xor-v1"
INTERFACE_SCOPE = "typed-neighbor-dense-id-v1"
TIMING_BOUNDARY = "typed-neighbor-call-plus-result-materialization-and-digest-v1"
CLOCK_NAME = "CLOCK_MONOTONIC"
GROUP_POLICY = "report-separately-no-cross-group-speedups"
FRESH_IMPORT_PROCESS_LIFETIME = "fresh-import-and-query-process-lifetime-v1"
PREBUILT_PROCESS_LIFETIME = "prebuilt-store-query-process-lifetime-v1"
EXTERNAL_PROCESS_LIFETIME = "external-prestarted-query-process-lifetime-v1"
FIXTURE_PROCESS_LIFETIME = "fixture-process-lifetime-v1"
PROCESS_LIFETIME_POLICIES = {
    FRESH_IMPORT_PROCESS_LIFETIME,
    PREBUILT_PROCESS_LIFETIME,
    EXTERNAL_PROCESS_LIFETIME,
    FIXTURE_PROCESS_LIFETIME,
}

FROZEN_SYSTEM_GROUPS = {
    "seml0": "embedded",
    "livegraph": "embedded",
    "aster": "embedded",
    "tugraph": "embedded",
    "neo4j": "client-server",
    "nebulagraph": "client-server",
}

TRUTH_COLUMNS = ["query_index", "edge_type", "src", "count", "sum_hash", "xor_hash"]
OBSERVATION_COLUMNS = [
    "contract_version",
    "system_id",
    "group",
    "repeat_index",
    "phase",
    "pass_index",
    "query_index",
    "edge_type",
    "src",
    "expected_count",
    "actual_count",
    "expected_sum_hash",
    "actual_sum_hash",
    "expected_xor_hash",
    "actual_xor_hash",
    "status",
    "latency_ns",
]

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ContractError(ValueError):
    """A deterministic contract violation."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def exclusive_json(path: Path, value: object, context: str) -> None:
    """Publish complete JSON without replacing a concurrently created target."""

    if not isinstance(context, str) or not context:
        raise ContractError("exclusive JSON context must be non-empty")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ContractError(f"{context} parent is not one real directory: {path.parent}")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.", dir=str(path.parent)
    )
    temporary = Path(raw_temporary)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError(f"short write while publishing {context}")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise ContractError(f"refusing to overwrite {context}: {path}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def claim_empty_directory(
    root: Path,
    *,
    claim_name: str,
    claim: object,
    context: str,
) -> Path:
    """Atomically select one owner for an absent or empty output directory."""

    if not isinstance(claim_name, str) or not claim_name or Path(claim_name).name != claim_name:
        raise ContractError(f"{context} claim name is invalid")
    if root.is_symlink():
        raise ContractError(f"{context} must not be a symlink: {root}")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ContractError(f"refusing non-empty {context}: {root}")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ContractError(f"cannot create {context}: {root}: {exc}") from exc
    if root.is_symlink() or not root.is_dir():
        raise ContractError(f"{context} is not one real directory: {root}")
    claim_path = root / claim_name
    payload = (json.dumps(claim, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(claim_path, flags, 0o600)
    except FileExistsError as exc:
        raise ContractError(f"{context} is already claimed: {root}") from exc
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError(f"short write while claiming {context}")
            offset += written
        os.fsync(descriptor)
    except OSError as exc:
        # Preserve even an incomplete claim after an I/O failure.  Reusing the
        # directory would be less safe than requiring explicit inspection.
        raise ContractError(f"cannot persist {context} ownership claim: {exc}") from exc
    finally:
        os.close(descriptor)
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise ContractError(f"cannot inspect claimed {context}: {root}: {exc}") from exc
    if entries != [claim_path]:
        raise ContractError(f"{context} changed while ownership was claimed: {root}")
    return claim_path


def read_json(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{context}: cannot read JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{context}: top-level JSON must be an object")
    return value


def require_keys(
    value: object,
    *,
    required: Iterable[str],
    allowed: Iterable[str],
    context: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{context}: expected object")
    required_set = set(required)
    allowed_set = set(allowed)
    missing = sorted(required_set - set(value))
    extra = sorted(set(value) - allowed_set)
    if missing:
        raise ContractError(f"{context}: missing keys: {missing}")
    if extra:
        raise ContractError(f"{context}: unknown keys: {extra}")
    return value


def nonempty_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{context}: expected non-empty string")
    return value


def integer(value: object, context: str, minimum: int | None = None) -> int:
    if type(value) is not int:  # bool is intentionally rejected
        raise ContractError(f"{context}: expected integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{context}: expected >= {minimum}")
    return value


def boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise ContractError(f"{context}: expected boolean")
    return value


def string_list(value: object, context: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContractError(f"{context}: expected a list of strings")
    return list(value)


def normalize_sha(value: object, context: str, *, required: bool) -> str:
    if value in (None, "") and not required:
        return ""
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value.lower()):
        raise ContractError(f"{context}: expected 64 lowercase hexadecimal characters")
    return value.lower()


def resolve_path(raw: object, *, manifest_dir: Path, repo_root: Path, run_root: Path, context: str) -> Path:
    text = nonempty_string(raw, context)
    replacements = {
        "${MANIFEST_DIR}": str(manifest_dir),
        "${REPO_ROOT}": str(repo_root),
        "${RUN_ROOT}": str(run_root),
    }
    for token, replacement in replacements.items():
        text = text.replace(token, replacement)
    if "${" in text:
        raise ContractError(f"{context}: unsupported template token in {text!r}")
    path = Path(text)
    if not path.is_absolute():
        path = manifest_dir / path
    return path.resolve()


def verify_file_ref(
    value: object,
    *,
    manifest_dir: Path,
    repo_root: Path,
    run_root: Path,
    context: str,
    formal: bool,
    executable: bool = False,
) -> dict[str, str]:
    ref = require_keys(value, required=("path", "sha256"), allowed=("path", "sha256"), context=context)
    path = resolve_path(ref["path"], manifest_dir=manifest_dir, repo_root=repo_root, run_root=run_root, context=f"{context}.path")
    if not path.is_file():
        raise ContractError(f"{context}: file does not exist: {path}")
    if executable and not os.access(path, os.X_OK):
        raise ContractError(f"{context}: file is not executable: {path}")
    expected = normalize_sha(ref.get("sha256"), f"{context}.sha256", required=formal)
    actual = sha256_file(path)
    if expected and expected != actual:
        raise ContractError(f"{context}: SHA-256 mismatch for {path}")
    return {"path": str(path), "sha256": actual}


def verify_data_ref(
    value: object,
    *,
    manifest_dir: Path,
    repo_root: Path,
    run_root: Path,
    context: str,
    formal: bool,
    require_file: bool,
) -> dict[str, str]:
    ref = require_keys(value, required=("path", "sha256"), allowed=("path", "sha256"), context=context)
    path = resolve_path(ref["path"], manifest_dir=manifest_dir, repo_root=repo_root, run_root=run_root, context=f"{context}.path")
    if require_file and not path.is_file():
        raise ContractError(f"{context}: expected existing file: {path}")
    if not require_file and not path.exists():
        raise ContractError(f"{context}: path does not exist: {path}")
    expected = normalize_sha(ref.get("sha256"), f"{context}.sha256", required=formal)
    if path.is_file():
        actual = sha256_file(path)
        if expected and expected != actual:
            raise ContractError(f"{context}: SHA-256 mismatch for {path}")
        expected = actual
    elif formal and not expected:
        raise ContractError(f"{context}: directory requires a frozen lineage SHA-256")
    return {"path": str(path), "sha256": expected}


def read_truth(path: Path, expected_count: int) -> list[dict[str, int]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames != TRUTH_COLUMNS:
                raise ContractError(f"truth: header mismatch: {reader.fieldnames!r}")
            raw_rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ContractError(f"truth: cannot read {path}: {exc}") from exc
    if len(raw_rows) != expected_count:
        raise ContractError(f"truth: {len(raw_rows)} rows != expected {expected_count}")
    rows: list[dict[str, int]] = []
    for index, raw in enumerate(raw_rows):
        try:
            row = {column: int(raw[column]) for column in TRUTH_COLUMNS}
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError(f"truth: row {index} contains a non-integer field") from exc
        if row["query_index"] != index:
            raise ContractError(f"truth: row {index} has query_index={row['query_index']}")
        if row["count"] < 0:
            raise ContractError(f"truth: row {index} has negative count")
        rows.append(row)
    return rows


def _validate_protocol(value: object, *, formal: bool = False) -> dict[str, Any]:
    required = (
        "group_policy",
        "interface_scope",
        "timing_boundary",
        "clock",
        "cache_policy",
        "process_reuse_between_phases",
        "warmup_passes",
        "measured_passes",
        "repeats",
        "per_query_timeout_ms",
        "adapter_process_timeout_s",
        "concurrency",
        "max_timeouts",
    )
    protocol = require_keys(value, required=required, allowed=required, context="manifest.protocol")
    exact = {
        "group_policy": GROUP_POLICY,
        "interface_scope": INTERFACE_SCOPE,
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
    }
    for key, expected in exact.items():
        if protocol.get(key) != expected:
            raise ContractError(f"manifest.protocol.{key}: expected {expected!r}")
    if boolean(protocol["process_reuse_between_phases"], "manifest.protocol.process_reuse_between_phases") is not True:
        raise ContractError("manifest.protocol.process_reuse_between_phases must be true")
    normalized = dict(protocol)
    normalized["warmup_passes"] = integer(protocol["warmup_passes"], "manifest.protocol.warmup_passes", 1)
    normalized["measured_passes"] = integer(protocol["measured_passes"], "manifest.protocol.measured_passes", 1)
    normalized["repeats"] = integer(protocol["repeats"], "manifest.protocol.repeats", 1)
    if formal and normalized["repeats"] != 3:
        raise ContractError("formal manifest.protocol.repeats must be exactly 3")
    normalized["per_query_timeout_ms"] = integer(protocol["per_query_timeout_ms"], "manifest.protocol.per_query_timeout_ms", 1)
    normalized["adapter_process_timeout_s"] = integer(protocol["adapter_process_timeout_s"], "manifest.protocol.adapter_process_timeout_s", 1)
    normalized["concurrency"] = integer(protocol["concurrency"], "manifest.protocol.concurrency", 1)
    if normalized["concurrency"] != 1:
        raise ContractError("manifest.protocol.concurrency must be 1 for the frozen Figure 1 contract")
    normalized["max_timeouts"] = integer(protocol["max_timeouts"], "manifest.protocol.max_timeouts", 0)
    if normalized["max_timeouts"] != 0:
        raise ContractError("manifest.protocol.max_timeouts must be zero for performance-eligible data")
    return normalized


def _validate_resources(value: object) -> dict[str, Any]:
    required = ("device", "data_mount", "interval_s", "disk_interval_s", "min_samples")
    resources = require_keys(value, required=required, allowed=required, context="manifest.resources")
    normalized = {
        "device": nonempty_string(resources["device"], "manifest.resources.device"),
        "data_mount": nonempty_string(resources["data_mount"], "manifest.resources.data_mount"),
        "interval_s": integer(resources["interval_s"], "manifest.resources.interval_s", 1),
        "disk_interval_s": integer(resources["disk_interval_s"], "manifest.resources.disk_interval_s", 1),
        "min_samples": integer(resources["min_samples"], "manifest.resources.min_samples", 2),
    }
    if not Path(normalized["data_mount"]).is_absolute():
        raise ContractError("manifest.resources.data_mount must be absolute")
    return normalized


def _validate_nebulagraph_repeat_bindings(
    value: object,
    *,
    repeats: int,
    manifest_dir: Path,
    repo_root: Path,
    run_root: Path,
    context: str,
    adapter_args: list[str],
    top_level_stores: list[dict[str, str]],
    top_level_temps: list[dict[str, str]],
    top_level_containers: list[str],
) -> list[dict[str, Any]]:
    """Validate three prebuilt clones and every mutable Docker namespace up front."""

    if not isinstance(value, list) or len(value) != repeats:
        raise ContractError(f"{context} must contain exactly one entry per repeat")
    expected_hosts = {
        "metad": "seml0-nebula-meta-sf10",
        "storaged": "seml0-nebula-storage-sf10",
        "graphd": "seml0-nebula-graph-sf10",
    }
    result: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    clones: list[dict[str, Any]] = []

    def artifact(raw: object, binding_context: str, field: str) -> dict[str, str]:
        return verify_file_ref(
            raw,
            manifest_dir=manifest_dir,
            repo_root=repo_root,
            run_root=run_root,
            context=f"{binding_context}.{field}",
            formal=True,
        )

    for binding_index, raw_binding in enumerate(value):
        binding_context = f"{context}[{binding_index}]"
        binding = require_keys(
            raw_binding,
            required=(
                "repeat_index", "repeat_root", "source_store_root", "store_root",
                "logs_root", "containers", "network", "graph_port", "store_manifest",
                "clone_receipt",
            ),
            allowed=(
                "repeat_index", "repeat_root", "source_store_root", "store_root",
                "logs_root", "containers", "network", "graph_port", "store_manifest",
                "clone_receipt",
            ),
            context=binding_context,
        )
        repeat_number = integer(binding["repeat_index"], f"{binding_context}.repeat_index", 1)
        if repeat_number != binding_index + 1:
            raise ContractError(f"{binding_context}.repeat_index must be ordered and contiguous")
        repeat_root = resolve_path(
            binding["repeat_root"], manifest_dir=manifest_dir, repo_root=repo_root,
            run_root=run_root, context=f"{binding_context}.repeat_root",
        )
        source_root = resolve_path(
            binding["source_store_root"], manifest_dir=manifest_dir, repo_root=repo_root,
            run_root=run_root, context=f"{binding_context}.source_store_root",
        )
        if not repeat_root.is_dir() or repeat_root.is_symlink():
            raise ContractError(f"{binding_context}.repeat_root must be a real existing directory")
        if not source_root.is_dir() or source_root.is_symlink():
            raise ContractError(f"{binding_context}.source_store_root must be a real directory")

        raw_store = require_keys(
            binding["store_root"], required=("label", "path", "sha256"),
            allowed=("label", "path", "sha256"), context=f"{binding_context}.store_root",
        )
        if raw_store["label"] != "nebulagraph":
            raise ContractError(f"{binding_context}.store_root.label must be 'nebulagraph'")
        store_path = resolve_path(
            raw_store["path"], manifest_dir=manifest_dir, repo_root=repo_root,
            run_root=run_root, context=f"{binding_context}.store_root.path",
        )
        if not store_path.is_dir() or store_path.is_symlink():
            raise ContractError(f"{binding_context}.store_root.path must be a real directory")
        store_root = {
            "label": "nebulagraph",
            "path": str(store_path),
            "sha256": normalize_sha(
                raw_store["sha256"], f"{binding_context}.store_root.sha256", required=True
            ),
        }
        raw_logs = require_keys(
            binding["logs_root"], required=("label", "path"), allowed=("label", "path"),
            context=f"{binding_context}.logs_root",
        )
        if raw_logs["label"] != "nebulagraph-logs":
            raise ContractError(f"{binding_context}.logs_root.label must be 'nebulagraph-logs'")
        logs_path = resolve_path(
            raw_logs["path"], manifest_dir=manifest_dir, repo_root=repo_root,
            run_root=run_root, context=f"{binding_context}.logs_root.path",
        )
        if not logs_path.is_dir() or logs_path.is_symlink() or any(logs_path.iterdir()):
            raise ContractError(f"{binding_context}.logs_root.path must be a real empty directory")
        for child, label in ((store_path, "store"), (logs_path, "logs")):
            if repeat_root == child or repeat_root not in child.parents:
                raise ContractError(f"{binding_context}: {label} root must be strictly inside repeat_root")
        if (
            source_root == repeat_root
            or source_root in repeat_root.parents
            or repeat_root in source_root.parents
        ):
            raise ContractError(f"{binding_context}: source store and repeat root overlap")
        if store_path == logs_path or store_path in logs_path.parents or logs_path in store_path.parents:
            raise ContractError(f"{binding_context}: store and logs roots overlap")

        raw_containers = require_keys(
            binding["containers"], required=("metad", "storaged", "graphd"),
            allowed=("metad", "storaged", "graphd"), context=f"{binding_context}.containers",
        )
        container_map = {
            role: nonempty_string(raw_containers[role], f"{binding_context}.containers.{role}")
            for role in ("metad", "storaged", "graphd")
        }
        if (
            len(set(container_map.values())) != 3
            or any(any(character.isspace() for character in name) for name in container_map.values())
        ):
            raise ContractError(f"{binding_context}.containers are invalid or duplicated")
        network = nonempty_string(binding["network"], f"{binding_context}.network")
        if any(character.isspace() for character in network):
            raise ContractError(f"{binding_context}.network is invalid")
        graph_port = integer(binding["graph_port"], f"{binding_context}.graph_port", 1)
        if graph_port > 65535:
            raise ContractError(f"{binding_context}.graph_port exceeds 65535")
        store_manifest = artifact(binding["store_manifest"], binding_context, "store_manifest")
        clone_receipt = artifact(binding["clone_receipt"], binding_context, "clone_receipt")
        for ref_name, ref in (("store_manifest", store_manifest), ("clone_receipt", clone_receipt)):
            ref_path = Path(ref["path"])
            if repeat_root not in ref_path.parents:
                raise ContractError(f"{binding_context}.{ref_name} must be inside repeat_root")

        manifest_value = read_json(Path(store_manifest["path"]), f"{binding_context}.store_manifest")
        clone_value = read_json(Path(clone_receipt["path"]), f"{binding_context}.clone_receipt")
        manifests.append(manifest_value)
        clones.append(clone_value)
        if (
            manifest_value.get("schema_version") != "cidr-p10-nebulagraph-store-v2"
            or manifest_value.get("formal_eligible") is not True
            or manifest_value.get("performance_eligible") is not True
        ):
            raise ContractError(f"{binding_context}.store_manifest is not formal/performance eligible")
        data_root = manifest_value.get("data_root")
        if (
            not isinstance(data_root, dict)
            or Path(str(data_root.get("path", ""))).resolve() != store_path
            or data_root.get("sha256") != store_root["sha256"]
        ):
            raise ContractError(f"{binding_context}.store_manifest data root differs from binding")
        if (
            manifest_value.get("containers") != container_map
            or manifest_value.get("logical_hosts") != expected_hosts
            or manifest_value.get("network") != network
        ):
            raise ContractError(f"{binding_context}.store_manifest Docker/RAFT identity drift")
        endpoint = manifest_value.get("graph_endpoint")
        if (
            not isinstance(endpoint, dict)
            or endpoint.get("host") not in ("127.0.0.1", "localhost")
            or endpoint.get("port") != graph_port
        ):
            raise ContractError(f"{binding_context}.store_manifest graph endpoint drift")
        if manifest_value.get("clone_receipt") != clone_receipt:
            raise ContractError(f"{binding_context}.store_manifest clone receipt differs from binding")
        if (
            clone_value.get("schema_version") != "cidr-p10-nebulagraph-clone-receipt-v1"
            or clone_value.get("state") != "PASS"
            or clone_value.get("run_id") != run_root.name
            or clone_value.get("repeat_index") != repeat_number
        ):
            raise ContractError(f"{binding_context}.clone_receipt run/repeat lineage drift")
        source_pre = clone_value.get("source_pre")
        source_post = clone_value.get("source_post")
        target = clone_value.get("target")
        if (
            not isinstance(source_pre, dict)
            or not isinstance(source_post, dict)
            or not isinstance(target, dict)
            or Path(str(source_pre.get("path", ""))).resolve() != source_root
            or source_pre != source_post
            or Path(str(target.get("path", ""))).resolve() != store_path
            or target.get("sha256") != store_root["sha256"]
        ):
            raise ContractError(f"{binding_context}.clone_receipt source/target lineage drift")
        result.append(
            {
                "repeat_index": repeat_number,
                "repeat_root": str(repeat_root),
                "source_store_root": str(source_root),
                "store_root": store_root,
                "logs_root": {"label": "nebulagraph-logs", "path": str(logs_path)},
                "containers": container_map,
                "network": network,
                "graph_port": graph_port,
                "store_manifest": store_manifest,
                "clone_receipt": clone_receipt,
            }
        )

    unique_fields = {
        "repeat root": [item["repeat_root"] for item in result],
        "store root": [item["store_root"]["path"] for item in result],
        "logs root": [item["logs_root"]["path"] for item in result],
        "network": [item["network"] for item in result],
        "graph port": [item["graph_port"] for item in result],
        "store manifest": [item["store_manifest"]["path"] for item in result],
        "clone receipt": [item["clone_receipt"]["path"] for item in result],
        "container": [name for item in result for name in item["containers"].values()],
    }
    for label, values in unique_fields.items():
        if len(values) != len(set(values)):
            raise ContractError(f"{context} reuse a {label}")
    mutable_roots = [
        Path(path).resolve()
        for item in result
        for path in (item["repeat_root"], item["store_root"]["path"], item["logs_root"]["path"])
    ]
    repeat_roots = [Path(item["repeat_root"]).resolve() for item in result]
    for left_index, left in enumerate(repeat_roots):
        for right in repeat_roots[left_index + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise ContractError(f"{context} contain overlapping repeat roots")
    for source in {Path(item["source_store_root"]).resolve() for item in result}:
        for mutable in mutable_roots:
            if source == mutable or source in mutable.parents or mutable in source.parents:
                raise ContractError(f"{context} source store overlaps a mutable repeat root")
    if len({item["source_store_root"] for item in result}) != 1:
        raise ContractError(f"{context} do not share one frozen source store")
    source_identities = {
        json.dumps(clone["source_pre"], sort_keys=True, separators=(",", ":")) for clone in clones
    }
    if len(source_identities) != 1:
        raise ContractError(f"{context} clone receipts do not share one source identity")
    immutable_manifest_fields = (
        "system_version", "dataset", "truth", "space", "authentication", "edge_type_labels",
        "logical_hosts", "repo", "runtime_manifest", "import_receipt",
    )
    immutable_identities = {
        json.dumps(
            {field: manifest.get(field) for field in immutable_manifest_fields},
            sort_keys=True,
            separators=(",", ":"),
        )
        for manifest in manifests
    }
    if len(immutable_identities) != 1:
        raise ContractError(f"{context} immutable store lineage differs across repeats")
    if len({item["store_root"]["sha256"] for item in result}) != 1:
        raise ContractError(f"{context} cloned store tree SHA differs across repeats")

    first = result[0]
    if (
        top_level_stores != [first["store_root"]]
        or top_level_temps != [first["logs_root"]]
        or top_level_containers
        != [first["containers"][role] for role in ("graphd", "metad", "storaged")]
    ):
        raise ContractError(f"{context}: top-level roots/containers must equal repeat binding 1")
    positions = [index for index, token in enumerate(adapter_args) if token == "--store-manifest"]
    sha_positions = [
        index for index, token in enumerate(adapter_args) if token == "--store-manifest-sha256"
    ]
    if (
        len(positions) != 1
        or positions[0] + 1 >= len(adapter_args)
        or adapter_args[positions[0] + 1] != first["store_manifest"]["path"]
        or len(sha_positions) != 1
        or sha_positions[0] + 1 >= len(adapter_args)
        or adapter_args[sha_positions[0] + 1] != first["store_manifest"]["sha256"]
    ):
        raise ContractError(f"{context}: adapter store manifest must equal repeat binding 1")
    return result


def load_suite_manifest(
    manifest_path: Path,
    *,
    repo_root: Path,
    run_root: Path,
    mode: str,
    manifest_base_dir: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, int]]]:
    if mode not in {"fixture", "formal"}:
        raise ContractError(f"unsupported mode: {mode}")
    formal = mode == "formal"
    manifest_path = manifest_path.resolve()
    raw_manifest_dir = manifest_path.parent if manifest_base_dir is None else manifest_base_dir
    if raw_manifest_dir.is_symlink():
        raise ContractError(f"manifest base directory must not be a symbolic link: {raw_manifest_dir}")
    manifest_dir = raw_manifest_dir.resolve()
    if not manifest_dir.is_dir():
        raise ContractError(f"manifest base directory is not one real directory: {manifest_dir}")
    manifest = read_json(manifest_path, "suite manifest")
    top_keys = (
        "$schema",
        "schema_version",
        "suite_id",
        "task_ids",
        "fixture_only",
        "dataset",
        "truth",
        "protocol",
        "resources",
        "systems",
    )
    manifest = require_keys(
        manifest,
        required=tuple(key for key in top_keys if key != "$schema"),
        allowed=top_keys,
        context="manifest",
    )
    if manifest["schema_version"] != SUITE_SCHEMA_VERSION:
        raise ContractError(f"manifest.schema_version must be {SUITE_SCHEMA_VERSION!r}")
    suite_id = nonempty_string(manifest["suite_id"], "manifest.suite_id")
    task_ids = string_list(manifest["task_ids"], "manifest.task_ids")
    if task_ids != ["P10", "P11"]:
        raise ContractError("manifest.task_ids must be exactly ['P10', 'P11']")
    fixture_only = boolean(manifest["fixture_only"], "manifest.fixture_only")
    if formal and fixture_only:
        raise ContractError("formal mode rejects fixture_only suite manifests")

    dataset = verify_data_ref(
        manifest["dataset"],
        manifest_dir=manifest_dir,
        repo_root=repo_root,
        run_root=run_root,
        context="manifest.dataset",
        formal=formal,
        require_file=False,
    )
    truth_obj = require_keys(
        manifest["truth"],
        required=("path", "sha256", "query_count", "digest_algorithm"),
        allowed=("path", "sha256", "query_count", "digest_algorithm"),
        context="manifest.truth",
    )
    truth_ref = verify_data_ref(
        {"path": truth_obj["path"], "sha256": truth_obj["sha256"]},
        manifest_dir=manifest_dir,
        repo_root=repo_root,
        run_root=run_root,
        context="manifest.truth",
        formal=formal,
        require_file=True,
    )
    truth_count = integer(truth_obj["query_count"], "manifest.truth.query_count", 1)
    if truth_obj["digest_algorithm"] != TRUTH_DIGEST_ALGORITHM:
        raise ContractError(f"manifest.truth.digest_algorithm must be {TRUTH_DIGEST_ALGORITHM!r}")
    truth_rows = read_truth(Path(truth_ref["path"]), truth_count)
    protocol = _validate_protocol(manifest["protocol"], formal=formal)
    resources = _validate_resources(manifest["resources"])

    systems_value = manifest["systems"]
    if not isinstance(systems_value, list):
        raise ContractError("manifest.systems must be an array")
    normalized_systems: list[dict[str, Any]] = []
    system_keys = (
        "id",
        "display_name",
        "group",
        "interface_scope",
        "system_version",
        "fixture_only",
        "service_lifecycle",
        "process_lifetime",
        "adapter",
        "binary",
        "runtime_libraries",
        "store_roots",
        "temp_roots",
        "containers",
        "extra_pids",
        "image_digests",
    )
    for index, raw_system in enumerate(systems_value):
        context = f"manifest.systems[{index}]"
        system = require_keys(
            raw_system,
            required=system_keys,
            allowed=system_keys + ("repeat_bindings",),
            context=context,
        )
        system_id = nonempty_string(system["id"], f"{context}.id")
        if not ID_RE.fullmatch(system_id):
            raise ContractError(f"{context}.id: invalid identifier")
        expected_group = FROZEN_SYSTEM_GROUPS.get(system_id)
        if expected_group is None:
            raise ContractError(f"{context}.id: system is outside frozen Figure 1 vocabulary")
        if system["group"] != expected_group:
            raise ContractError(f"{context}.group: {system_id} must be {expected_group!r}")
        if system["interface_scope"] != INTERFACE_SCOPE:
            raise ContractError(f"{context}.interface_scope must be {INTERFACE_SCOPE!r}")
        system_fixture = boolean(system["fixture_only"], f"{context}.fixture_only")
        if formal and system_fixture:
            raise ContractError(f"{context}: formal mode rejects fixture adapters")
        lifecycle = nonempty_string(system["service_lifecycle"], f"{context}.service_lifecycle")
        expected_lifecycle = "in-process" if expected_group == "embedded" else "external-prestarted"
        if lifecycle != expected_lifecycle:
            raise ContractError(f"{context}.service_lifecycle must be {expected_lifecycle!r}")
        process_lifetime = nonempty_string(
            system["process_lifetime"], f"{context}.process_lifetime"
        )
        if process_lifetime not in PROCESS_LIFETIME_POLICIES:
            raise ContractError(f"{context}.process_lifetime is outside the frozen vocabulary")
        if process_lifetime == FRESH_IMPORT_PROCESS_LIFETIME and expected_group != "embedded":
            raise ContractError(f"{context}.process_lifetime: fresh import requires an embedded system")
        if not system_fixture and system_id == "livegraph" and process_lifetime != FRESH_IMPORT_PROCESS_LIFETIME:
            raise ContractError(
                f"{context}.process_lifetime: LiveGraph must declare {FRESH_IMPORT_PROCESS_LIFETIME!r}"
            )
        if formal and process_lifetime == FIXTURE_PROCESS_LIFETIME:
            raise ContractError(f"{context}.process_lifetime: formal mode rejects fixture policy")
        if formal and expected_group == "client-server" and process_lifetime != EXTERNAL_PROCESS_LIFETIME:
            raise ContractError(
                f"{context}.process_lifetime: client-server formal runs must declare "
                f"{EXTERNAL_PROCESS_LIFETIME!r}"
            )

        adapter_obj = require_keys(
            system["adapter"],
            required=("path", "sha256", "args"),
            allowed=("path", "sha256", "args"),
            context=f"{context}.adapter",
        )
        adapter = verify_file_ref(
            {"path": adapter_obj["path"], "sha256": adapter_obj["sha256"]},
            manifest_dir=manifest_dir,
            repo_root=repo_root,
            run_root=run_root,
            context=f"{context}.adapter",
            formal=formal,
            executable=True,
        )
        if formal and (
            "tests" in Path(adapter["path"]).parts
            or Path(adapter["path"]).name.lower().startswith(("fixture", "fake"))
        ):
            raise ContractError(f"{context}.adapter: formal mode rejects test/fixture adapter paths")
        adapter["args"] = string_list(adapter_obj["args"], f"{context}.adapter.args")
        binary = verify_file_ref(
            system["binary"],
            manifest_dir=manifest_dir,
            repo_root=repo_root,
            run_root=run_root,
            context=f"{context}.binary",
            formal=formal,
        )
        if formal and (
            "tests" in Path(binary["path"]).parts
            or Path(binary["path"]).name.lower().startswith(("fixture", "fake"))
        ):
            raise ContractError(f"{context}.binary: formal mode rejects test/fixture binary paths")
        raw_runtime_libraries = system["runtime_libraries"]
        if not isinstance(raw_runtime_libraries, list):
            raise ContractError(f"{context}.runtime_libraries must be an array")
        runtime_libraries: list[dict[str, str]] = []
        for library_index, raw_library in enumerate(raw_runtime_libraries):
            runtime_libraries.append(
                verify_file_ref(
                    raw_library,
                    manifest_dir=manifest_dir,
                    repo_root=repo_root,
                    run_root=run_root,
                    context=f"{context}.runtime_libraries[{library_index}]",
                    formal=formal,
                )
            )
        runtime_paths = [library["path"] for library in runtime_libraries]
        if len(runtime_paths) != len(set(runtime_paths)):
            raise ContractError(f"{context}.runtime_libraries contains duplicate paths")
        if formal and system_id == "livegraph" and not runtime_libraries:
            raise ContractError(f"{context}.runtime_libraries: formal LiveGraph requires liblivegraph SHA")

        def roots(raw: object, role: str) -> list[dict[str, str]]:
            if not isinstance(raw, list):
                raise ContractError(f"{context}.{role}_roots must be an array")
            result: list[dict[str, str]] = []
            labels: set[str] = set()
            for root_index, raw_root in enumerate(raw):
                root_context = f"{context}.{role}_roots[{root_index}]"
                allowed_keys = ("label", "path", "sha256") if role == "store" else ("label", "path")
                root = require_keys(
                    raw_root,
                    required=("label", "path"),
                    allowed=allowed_keys,
                    context=root_context,
                )
                label = nonempty_string(root["label"], f"{root_context}.label")
                if not LABEL_RE.fullmatch(label) or label in labels:
                    raise ContractError(f"{root_context}.label: invalid or duplicate label")
                labels.add(label)
                path = resolve_path(root["path"], manifest_dir=manifest_dir, repo_root=repo_root, run_root=run_root, context=f"{root_context}.path")
                if formal and role == "store" and not path.exists():
                    raise ContractError(f"{root_context}.path: formal store does not exist: {path}")
                normalized = {"label": label, "path": str(path)}
                if role == "store":
                    normalized["sha256"] = normalize_sha(
                        root.get("sha256"),
                        f"{root_context}.sha256",
                        required=formal and process_lifetime != FRESH_IMPORT_PROCESS_LIFETIME,
                    )
                result.append(normalized)
            return result

        stores = roots(system["store_roots"], "store")
        temps = roots(system["temp_roots"], "temp")
        if not stores:
            raise ContractError(f"{context}.store_roots: at least one store is required")
        containers = string_list(system["containers"], f"{context}.containers")
        if any(not item.strip() or any(char.isspace() for char in item) for item in containers):
            raise ContractError(f"{context}.containers: invalid container name")
        if len(containers) != len(set(containers)):
            raise ContractError(f"{context}.containers: duplicate container name")
        raw_pids = system["extra_pids"]
        if not isinstance(raw_pids, list):
            raise ContractError(f"{context}.extra_pids must be an array")
        pids = [integer(pid, f"{context}.extra_pids", 1) for pid in raw_pids]
        if len(pids) != len(set(pids)):
            raise ContractError(f"{context}.extra_pids: duplicate PID")
        image_digests = string_list(system["image_digests"], f"{context}.image_digests")
        if any(not IMAGE_DIGEST_RE.fullmatch(item) for item in image_digests):
            raise ContractError(f"{context}.image_digests: expected sha256:<64 hex>")
        if formal and expected_group == "client-server":
            if not image_digests:
                raise ContractError(f"{context}: client-server system requires pinned image digest(s)")
            if not containers and not pids:
                raise ContractError(f"{context}: client-server system requires P31 container/PID coverage")

        repeat_bindings: list[dict[str, Any]] = []
        raw_repeat_bindings = system.get("repeat_bindings")
        if raw_repeat_bindings is not None and system_id == "nebulagraph":
            if not formal:
                raise ContractError(
                    f"{context}.repeat_bindings are restricted to formal client-server systems"
                )
            repeat_bindings = _validate_nebulagraph_repeat_bindings(
                raw_repeat_bindings,
                repeats=protocol["repeats"],
                manifest_dir=manifest_dir,
                repo_root=repo_root,
                run_root=run_root,
                context=f"{context}.repeat_bindings",
                adapter_args=adapter["args"],
                top_level_stores=stores,
                top_level_temps=temps,
                top_level_containers=containers,
            )
        elif raw_repeat_bindings is not None:
            if system_id != "neo4j" or not formal:
                raise ContractError(f"{context}.repeat_bindings are reserved for formal Neo4j")
            if not isinstance(raw_repeat_bindings, list) or len(raw_repeat_bindings) != protocol["repeats"]:
                raise ContractError(f"{context}.repeat_bindings must contain exactly one entry per repeat")
            for binding_index, raw_binding in enumerate(raw_repeat_bindings):
                binding_context = f"{context}.repeat_bindings[{binding_index}]"
                binding = require_keys(
                    raw_binding,
                    required=(
                        "repeat_index", "clone_id", "repeat_root", "source_store_root", "launcher",
                        "store_root", "logs_root", "container", "uri",
                        "store_manifest", "import_receipt",
                    ),
                    allowed=(
                        "repeat_index", "clone_id", "repeat_root", "source_store_root", "launcher",
                        "store_root", "logs_root", "container", "uri",
                        "store_manifest", "import_receipt",
                    ),
                    context=binding_context,
                )
                repeat_number = integer(binding["repeat_index"], f"{binding_context}.repeat_index", 1)
                if repeat_number != binding_index + 1:
                    raise ContractError(f"{binding_context}.repeat_index must be ordered and contiguous")
                clone_id = nonempty_string(binding["clone_id"], f"{binding_context}.clone_id")
                if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,80}", clone_id) is None:
                    raise ContractError(f"{binding_context}.clone_id is invalid")
                repeat_root = resolve_path(
                    binding["repeat_root"], manifest_dir=manifest_dir, repo_root=repo_root,
                    run_root=run_root, context=f"{binding_context}.repeat_root",
                )
                source_store_root = resolve_path(
                    binding["source_store_root"], manifest_dir=manifest_dir, repo_root=repo_root,
                    run_root=run_root, context=f"{binding_context}.source_store_root",
                )
                if not repeat_root.is_dir():
                    raise ContractError(f"{binding_context}.repeat_root is not an existing directory")
                if not source_store_root.is_dir():
                    raise ContractError(f"{binding_context}.source_store_root is not an existing directory")
                raw_store = require_keys(
                    binding["store_root"],
                    required=("label", "path", "sha256"),
                    allowed=("label", "path", "sha256"),
                    context=f"{binding_context}.store_root",
                )
                if raw_store["label"] != "neo4j-runtime":
                    raise ContractError(f"{binding_context}.store_root.label must be 'neo4j-runtime'")
                store_path = resolve_path(
                    raw_store["path"], manifest_dir=manifest_dir, repo_root=repo_root,
                    run_root=run_root, context=f"{binding_context}.store_root.path",
                )
                if not store_path.is_dir():
                    raise ContractError(f"{binding_context}.store_root.path is not an existing directory")
                binding_store = {
                    "label": "neo4j-runtime",
                    "path": str(store_path),
                    "sha256": normalize_sha(raw_store["sha256"], f"{binding_context}.store_root.sha256", required=True),
                }
                raw_logs = require_keys(
                    binding["logs_root"],
                    required=("label", "path"),
                    allowed=("label", "path"),
                    context=f"{binding_context}.logs_root",
                )
                if raw_logs["label"] != "neo4j-logs":
                    raise ContractError(f"{binding_context}.logs_root.label must be 'neo4j-logs'")
                logs_path = resolve_path(
                    raw_logs["path"], manifest_dir=manifest_dir, repo_root=repo_root,
                    run_root=run_root, context=f"{binding_context}.logs_root.path",
                )
                if not logs_path.is_dir():
                    raise ContractError(f"{binding_context}.logs_root.path is not an existing directory")
                if store_path == logs_path or store_path in logs_path.parents or logs_path in store_path.parents:
                    raise ContractError(f"{binding_context}: store and logs roots overlap")
                if repeat_root == store_path or repeat_root not in store_path.parents:
                    raise ContractError(f"{binding_context}: runtime store must be strictly inside repeat_root")
                if repeat_root == logs_path or repeat_root not in logs_path.parents:
                    raise ContractError(f"{binding_context}: logs root must be strictly inside repeat_root")
                if (
                    source_store_root == repeat_root
                    or source_store_root in repeat_root.parents
                    or repeat_root in source_store_root.parents
                ):
                    raise ContractError(f"{binding_context}: source store and repeat root overlap")
                container = nonempty_string(binding["container"], f"{binding_context}.container")
                if any(character.isspace() for character in container):
                    raise ContractError(f"{binding_context}.container is invalid")
                if not container.endswith(f"-r{repeat_number:02d}"):
                    raise ContractError(f"{binding_context}.container must end with the repeat suffix")
                uri = nonempty_string(binding["uri"], f"{binding_context}.uri")
                parsed_uri = urlparse(uri)
                if (
                    parsed_uri.scheme not in {"bolt", "neo4j"}
                    or parsed_uri.hostname not in {"127.0.0.1", "localhost"}
                    or parsed_uri.port is None
                    or parsed_uri.path not in {"", "/"}
                    or parsed_uri.params
                    or parsed_uri.query
                    or parsed_uri.fragment
                ):
                    raise ContractError(f"{binding_context}.uri must be one explicit localhost Bolt endpoint")

                def binding_artifact(field: str) -> dict[str, str]:
                    return verify_file_ref(
                        binding[field],
                        manifest_dir=manifest_dir,
                        repo_root=repo_root,
                        run_root=run_root,
                        context=f"{binding_context}.{field}",
                        formal=True,
                    )

                repeat_bindings.append(
                    {
                        "repeat_index": repeat_number,
                        "clone_id": clone_id,
                        "repeat_root": str(repeat_root),
                        "source_store_root": str(source_store_root),
                        "launcher": binding_artifact("launcher"),
                        "store_root": binding_store,
                        "logs_root": {"label": "neo4j-logs", "path": str(logs_path)},
                        "container": container,
                        "uri": uri,
                        "bolt_port": parsed_uri.port,
                        "store_manifest": binding_artifact("store_manifest"),
                        "import_receipt": binding_artifact("import_receipt"),
                    }
                )
            unique_fields = {
                "clone ID": [item["clone_id"] for item in repeat_bindings],
                "repeat root": [item["repeat_root"] for item in repeat_bindings],
                "store path": [item["store_root"]["path"] for item in repeat_bindings],
                "logs path": [item["logs_root"]["path"] for item in repeat_bindings],
                "container": [item["container"] for item in repeat_bindings],
                "URI": [item["uri"] for item in repeat_bindings],
                "Bolt port": [item["bolt_port"] for item in repeat_bindings],
                "store manifest": [item["store_manifest"]["path"] for item in repeat_bindings],
                "import receipt": [item["import_receipt"]["path"] for item in repeat_bindings],
            }
            for label, values in unique_fields.items():
                if len(values) != len(set(values)):
                    raise ContractError(f"{context}.repeat_bindings reuse a {label}")
            launcher_refs = {
                (item["launcher"]["path"], item["launcher"]["sha256"])
                for item in repeat_bindings
            }
            if len(launcher_refs) != 1:
                raise ContractError(f"{context}.repeat_bindings do not share one pinned launcher")
            all_roots = [
                Path(value).resolve()
                for item in repeat_bindings
                for value in (item["store_root"]["path"], item["logs_root"]["path"])
            ]
            for left_index, left in enumerate(all_roots):
                for right in all_roots[left_index + 1:]:
                    if left == right or left in right.parents or right in left.parents:
                        raise ContractError(f"{context}.repeat_bindings contain overlapping roots")
            repeat_roots = [Path(item["repeat_root"]).resolve() for item in repeat_bindings]
            for left_index, left in enumerate(repeat_roots):
                for right in repeat_roots[left_index + 1:]:
                    if left == right or left in right.parents or right in left.parents:
                        raise ContractError(f"{context}.repeat_bindings contain overlapping repeat roots")
            for item in repeat_bindings:
                source = Path(item["source_store_root"]).resolve()
                for repeat_root_path in repeat_roots:
                    if source == repeat_root_path or source in repeat_root_path.parents or repeat_root_path in source.parents:
                        raise ContractError(f"{context}.repeat_bindings source store overlaps a repeat root")
            first_binding = repeat_bindings[0]
            if (
                stores != [first_binding["store_root"]]
                or temps != [first_binding["logs_root"]]
                or containers != [first_binding["container"]]
            ):
                raise ContractError(f"{context}: top-level roots/container must equal repeat binding 1")

            def one_static_arg(flag: str) -> str:
                positions = [index for index, value in enumerate(adapter["args"]) if value == flag]
                if len(positions) != 1 or positions[0] + 1 >= len(adapter["args"]):
                    raise ContractError(f"{context}.adapter.args requires exactly one {flag}")
                return adapter["args"][positions[0] + 1]

            expected_static_args = {
                "--uri": first_binding["uri"],
                "--logs-root": first_binding["logs_root"]["path"],
                "--store-manifest": first_binding["store_manifest"]["path"],
                "--store-manifest-sha256": first_binding["store_manifest"]["sha256"],
                "--import-receipt": first_binding["import_receipt"]["path"],
                "--import-receipt-sha256": first_binding["import_receipt"]["sha256"],
            }
            for flag, expected in expected_static_args.items():
                if one_static_arg(flag) != expected:
                    raise ContractError(f"{context}.adapter.args {flag} differs from repeat binding 1")
        elif formal and system_id in {"neo4j", "nebulagraph"}:
            raise ContractError(
                f"{context}: formal {system_id} requires independent repeat_bindings"
            )

        normalized_systems.append(
            {
                "id": system_id,
                "display_name": nonempty_string(system["display_name"], f"{context}.display_name"),
                "group": expected_group,
                "interface_scope": INTERFACE_SCOPE,
                "system_version": nonempty_string(system["system_version"], f"{context}.system_version"),
                "fixture_only": system_fixture,
                "service_lifecycle": lifecycle,
                "process_lifetime": process_lifetime,
                "adapter": adapter,
                "binary": binary,
                "runtime_libraries": runtime_libraries,
                "store_roots": stores,
                "temp_roots": temps,
                "containers": containers,
                "extra_pids": pids,
                "image_digests": image_digests,
                "repeat_bindings": repeat_bindings,
            }
        )

    found_ids = [system["id"] for system in normalized_systems]
    if len(found_ids) != len(set(found_ids)):
        raise ContractError("manifest.systems contains duplicate ids")
    if set(found_ids) != set(FROZEN_SYSTEM_GROUPS):
        missing = sorted(set(FROZEN_SYSTEM_GROUPS) - set(found_ids))
        extra = sorted(set(found_ids) - set(FROZEN_SYSTEM_GROUPS))
        raise ContractError(f"manifest.systems must contain the frozen six systems; missing={missing}, extra={extra}")

    resolved = {
        "schema_version": SUITE_SCHEMA_VERSION,
        "suite_id": suite_id,
        "task_ids": task_ids,
        "fixture_only": fixture_only,
        "source_manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
        "dataset": dataset,
        "truth": {
            **truth_ref,
            "query_count": truth_count,
            "digest_algorithm": TRUTH_DIGEST_ALGORITHM,
        },
        "protocol": protocol,
        "resources": resources,
        "systems": normalized_systems,
    }
    return resolved, truth_rows


def phase_digest(
    phase: str,
    passes: int,
    truth_rows: list[dict[str, int]],
    observations: list[dict[str, str]] | None = None,
) -> str:
    digest = hashlib.sha256()
    if observations is None:
        for pass_index in range(passes):
            for truth in truth_rows:
                digest.update(
                    f"{pass_index}\t{truth['query_index']}\t{truth['count']}\t{truth['sum_hash']}\t{truth['xor_hash']}\n".encode()
                )
    else:
        for row in observations:
            if row["phase"] != phase:
                continue
            if row["status"] == "timeout":
                line = f"{row['pass_index']}\t{row['query_index']}\tTIMEOUT\n"
            else:
                line = (
                    f"{row['pass_index']}\t{row['query_index']}\t{row['actual_count']}\t"
                    f"{row['actual_sum_hash']}\t{row['actual_xor_hash']}\n"
                )
            digest.update(line.encode())
    return digest.hexdigest()


def nearest_rank(values: list[int], percentile: float) -> int:
    if not values:
        raise ContractError("cannot compute percentile of an empty sample")
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _read_observations(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames != OBSERVATION_COLUMNS:
                raise ContractError(f"adapter observations: header mismatch: {reader.fieldnames!r}")
            return list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ContractError(f"adapter observations: cannot read {path}: {exc}") from exc


def _parse_row_int(row: dict[str, str], key: str, context: str, minimum: int | None = None) -> int:
    try:
        value = int(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"{context}: {key} is not an integer") from exc
    if minimum is not None and value < minimum:
        raise ContractError(f"{context}: {key} must be >= {minimum}")
    return value


def _validate_events(path: Path, result: dict[str, Any]) -> None:
    events: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            value = json.loads(line)
            value = require_keys(
                value,
                required=("contract_version", "phase", "event", "monotonic_ns"),
                allowed=("contract_version", "phase", "event", "monotonic_ns"),
                context=f"phase-events.jsonl line {line_number}",
            )
            events.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"phase-events.jsonl: cannot read: {exc}") from exc
    expected_pairs = [("warmup", "start"), ("warmup", "end"), ("measured", "start"), ("measured", "end")]
    if [(item.get("phase"), item.get("event")) for item in events] != expected_pairs:
        raise ContractError("phase-events.jsonl: expected exactly warmup start/end then measured start/end")
    for index, event in enumerate(events):
        if event["contract_version"] != CONTRACT_VERSION:
            raise ContractError(f"phase-events.jsonl line {index + 1}: wrong contract_version")
        timestamp = integer(event["monotonic_ns"], f"phase-events.jsonl line {index + 1}.monotonic_ns", 0)
        phase_summary = result[event["phase"]]
        key = "started_monotonic_ns" if event["event"] == "start" else "ended_monotonic_ns"
        if timestamp != phase_summary[key]:
            raise ContractError(f"phase-events.jsonl line {index + 1}: timestamp differs from result.{event['phase']}.{key}")
    if events[1]["monotonic_ns"] > events[2]["monotonic_ns"]:
        raise ContractError("warmup and measured intervals overlap or are reversed")


def _neo4j_artifact_ref(value: object, context: str, *, verify_file: bool) -> dict[str, Any]:
    ref = require_keys(
        value,
        required=("path", "sha256"),
        allowed=("path", "sha256", "size_bytes"),
        context=context,
    )
    path = Path(nonempty_string(ref["path"], f"{context}.path")).resolve()
    sha = normalize_sha(ref["sha256"], f"{context}.sha256", required=True)
    if verify_file:
        if not path.is_file() or sha256_file(path) != sha:
            raise ContractError(f"{context}: file path/SHA-256 is invalid")
    return {"path": str(path), "sha256": sha}


def _neo4j_same_ref(observed: object, expected: object, context: str) -> None:
    if not isinstance(expected, dict):
        raise ContractError(f"{context}: expected reference is malformed")
    ref = _neo4j_artifact_ref(observed, context, verify_file=False)
    if ref["path"] != str(Path(str(expected.get("path", ""))).resolve()):
        raise ContractError(f"{context}: path differs from request")
    if ref["sha256"] != expected.get("sha256"):
        raise ContractError(f"{context}: SHA-256 differs from request")


def _validate_neo4j_provenance(
    provenance: dict[str, Any],
    request: dict[str, Any],
    system: dict[str, Any],
    process_lifetime: str,
) -> None:
    keys = (
        "schema_version",
        "performance_eligible",
        "execution_mode",
        "group",
        "system_version",
        "process_lifetime",
        "request",
        "repo",
        "p02b",
        "python_binary",
        "python_driver",
        "server_agent",
        "launch",
        "container_lifecycle",
        "readiness",
        "database_contract",
        "runtime_contract",
        "jvm_lifecycle",
        "dataset_input",
        "dataset",
        "truth",
        "store",
        "query_contract",
    )
    provenance = require_keys(
        provenance, required=keys, allowed=keys, context="Neo4j adapter provenance"
    )
    formal = request.get("execution_mode") == "formal"
    exact = {
        "schema_version": "p10-neo4j-adapter-provenance-v4",
        "performance_eligible": formal,
        "execution_mode": request.get("execution_mode"),
        "group": "client-server",
        "system_version": system["system_version"],
        "process_lifetime": process_lifetime,
        "server_agent": "Neo4j/5.26.24",
    }
    for key, expected in exact.items():
        if provenance.get(key) != expected:
            raise ContractError(f"Neo4j provenance {key} differs from request")

    request_ref = _neo4j_artifact_ref(provenance["request"], "Neo4j request", verify_file=True)
    if read_json(Path(request_ref["path"]), "Neo4j provenance request") != request:
        raise ContractError("Neo4j provenance request content differs from orchestrator request")
    _neo4j_same_ref(provenance["python_binary"], request.get("binary"), "Neo4j Python binary")
    _neo4j_same_ref(provenance["dataset_input"], request.get("dataset"), "Neo4j dataset input")
    _neo4j_same_ref(provenance["truth"], request.get("truth"), "Neo4j truth")

    driver = require_keys(
        provenance["python_driver"],
        required=("version", "expected_package_tree_sha256", "package_tree_sha256", "hash_method", "file_count", "total_bytes", "module"),
        allowed=("version", "expected_package_tree_sha256", "package_tree_sha256", "hash_method", "file_count", "total_bytes", "module"),
        context="Neo4j Python driver",
    )
    if driver["version"] != "5.28.3":
        raise ContractError("Neo4j Python driver version is not frozen at 5.28.3")
    if driver["hash_method"] != "sha256-tree-v1(relative-path,size,file-sha256)":
        raise ContractError("Neo4j Python driver tree hash method mismatch")
    expected_driver_sha = normalize_sha(
        driver["expected_package_tree_sha256"],
        "Neo4j expected Python driver tree SHA",
        required=True,
    )
    actual_driver_sha = normalize_sha(driver["package_tree_sha256"], "Neo4j Python driver tree SHA", required=True)
    if actual_driver_sha != expected_driver_sha:
        raise ContractError("Neo4j Python driver tree differs from its prebound SHA")
    adapter_args = system.get("adapter", {}).get("args", [])
    if not isinstance(adapter_args, list):
        raise ContractError("Neo4j system adapter args are malformed")
    positions = [index for index, value in enumerate(adapter_args) if value == "--expected-driver-tree-sha256"]
    if len(positions) != 1 or positions[0] + 1 >= len(adapter_args):
        raise ContractError("Neo4j adapter args lack one prebound driver tree SHA")
    if adapter_args[positions[0] + 1] != expected_driver_sha:
        raise ContractError("Neo4j driver tree SHA differs from the suite manifest")
    integer(driver["file_count"], "Neo4j Python driver file_count", 1)
    integer(driver["total_bytes"], "Neo4j Python driver total_bytes", 1)
    _neo4j_artifact_ref(driver["module"], "Neo4j Python driver module", verify_file=True)

    stores = request.get("store_roots")
    selected = next(
        (
            item
            for item in stores
            if isinstance(item, dict) and item.get("label") == "neo4j-runtime"
        ),
        None,
    ) if isinstance(stores, list) else None
    if not isinstance(selected, dict):
        raise ContractError("Neo4j request lacks neo4j-runtime store root")
    log_positions = [index for index, value in enumerate(adapter_args) if value == "--logs-root"]
    if len(log_positions) != 1 or log_positions[0] + 1 >= len(adapter_args):
        raise ContractError("Neo4j adapter args lack one explicit /logs root")
    logs_root = Path(str(adapter_args[log_positions[0] + 1])).resolve()

    lifecycle = require_keys(
        provenance["container_lifecycle"],
        required=("stable", "before", "after"),
        allowed=("stable", "before", "after"),
        context="Neo4j container lifecycle",
    )
    if lifecycle["stable"] is not True:
        raise ContractError("Neo4j container lifecycle is not stable")
    container_keys = (
        "name",
        "container_id",
        "pid",
        "started_at",
        "restart_count",
        "image_id",
        "configured_image",
        "container_user",
        "repo_digests",
        "expected_repo_digest",
        "data_mount",
        "logs_mount",
        "bolt_host",
        "bolt_port",
        "restart_policy",
        "read_only_default",
        "docker_log_config",
        "docker_memory_limit_bytes",
        "docker_memory_swap_bytes",
        "memory_configuration",
        "entrypoint",
        "command",
    )
    containers = request.get("external_service", {}).get("containers", [])
    image_digests = request.get("external_service", {}).get("image_digests", [])
    if len(containers) != 1 or len(image_digests) != 1:
        raise ContractError("Neo4j request lacks one exact container/image digest")
    normalized_containers: list[dict[str, Any]] = []
    for phase in ("before", "after"):
        item = require_keys(
            lifecycle[phase],
            required=container_keys,
            allowed=container_keys,
            context=f"Neo4j container lifecycle.{phase}",
        )
        if item["name"] != containers[0]:
            raise ContractError("Neo4j container name differs from request")
        if not isinstance(item["container_id"], str) or not item["container_id"]:
            raise ContractError("Neo4j container ID is missing")
        integer(item["pid"], f"Neo4j container {phase} PID", 1)
        nonempty_string(item["started_at"], f"Neo4j container {phase} StartedAt")
        if integer(item["restart_count"], f"Neo4j container {phase} RestartCount", 0) != 0:
            raise ContractError("Neo4j container restarted during the repeat")
        if not IMAGE_DIGEST_RE.fullmatch(str(item["image_id"])):
            raise ContractError("Neo4j container image ID is malformed")
        if re.fullmatch(r"[0-9]+:[0-9]+", str(item["container_user"])) is None:
            raise ContractError("Neo4j container user is not numeric UID:GID")
        if item["expected_repo_digest"] != image_digests[0]:
            raise ContractError("Neo4j container RepoDigest differs from request")
        repo_digests = item["repo_digests"]
        resolved_repo_digests = {
            value.split("@", 1)[1]
            for value in repo_digests
            if isinstance(value, str) and "@" in value
        } if isinstance(repo_digests, list) else set()
        if image_digests[0] not in resolved_repo_digests:
            raise ContractError("Neo4j container RepoDigest is absent from image metadata")
        if Path(str(item["data_mount"])).resolve() != Path(str(selected["path"])).resolve():
            raise ContractError("Neo4j container data mount differs from request store")
        if (
            item["configured_image"] != "neo4j:5.26.24"
            or item["bolt_host"] != "127.0.0.1"
            or not isinstance(item["bolt_port"], int)
            or item["bolt_port"] <= 0
            or item["restart_policy"] != "no"
            or item["read_only_default"] is not True
            or Path(str(item["logs_mount"])).resolve() != logs_root
            or item["docker_log_config"]
            != {"type": "none", "config": {}}
            or item["docker_memory_limit_bytes"] != 0
            or item["docker_memory_swap_bytes"] != 0
            or item["memory_configuration"]
            != {
                "configured": {"heap_initial": "8G", "heap_max": "8G", "pagecache": "16G"},
                "bytes": {
                    "heap_initial": 8 * 1024**3,
                    "heap_max": 8 * 1024**3,
                    "pagecache": 16 * 1024**3,
                },
                "settings": {
                    "heap_initial": "server.memory.heap.initial_size",
                    "heap_max": "server.memory.heap.max_size",
                    "pagecache": "server.memory.pagecache.size",
                },
                "environment_names": {
                    "heap_initial": "NEO4J_server_memory_heap_initial__size",
                    "heap_max": "NEO4J_server_memory_heap_max__size",
                    "pagecache": "NEO4J_server_memory_pagecache_size",
                },
            }
        ):
            raise ContractError("Neo4j container isolation contract is invalid")
        normalized_containers.append(item)
    stable_fields = (
        "name", "container_id", "pid", "started_at", "restart_count", "image_id",
        "configured_image", "container_user", "expected_repo_digest", "data_mount", "bolt_host", "bolt_port",
        "logs_mount", "restart_policy", "read_only_default", "docker_log_config",
        "docker_memory_limit_bytes", "docker_memory_swap_bytes", "memory_configuration",
        "entrypoint", "command",
    )
    for key in stable_fields:
        if normalized_containers[0][key] != normalized_containers[1][key]:
            raise ContractError(f"Neo4j container {key} changed during the repeat")

    readiness = require_keys(
        provenance["readiness"],
        required=("timeout_s", "attempts", "elapsed_ns"),
        allowed=("timeout_s", "attempts", "elapsed_ns"),
        context="Neo4j readiness",
    )
    timeout_s = integer(readiness["timeout_s"], "Neo4j readiness timeout_s", 1)
    if timeout_s > 900:
        raise ContractError("Neo4j readiness timeout exceeds the frozen maximum")
    integer(readiness["attempts"], "Neo4j readiness attempts", 1)
    elapsed_ns = integer(readiness["elapsed_ns"], "Neo4j readiness elapsed_ns", 1)
    if elapsed_ns > timeout_s * 1_000_000_000:
        raise ContractError("Neo4j readiness elapsed time exceeds its deadline")

    database_expected = {
        "database_name": "neo4j",
        "node_label": "V",
        "id_property": "id",
        "required_index": {
            "name": "v_id",
            "state": "ONLINE",
            "type": "RANGE",
            "entityType": "NODE",
            "labelsOrTypes": ["V"],
            "properties": ["id"],
        },
    }
    if provenance["database_contract"] != database_expected:
        raise ContractError("Neo4j live database/index contract mismatch")

    runtime_contract = require_keys(
        provenance["runtime_contract"],
        required=("database_status", "memory", "representative_plan", "host"),
        allowed=("database_status", "memory", "representative_plan", "host"),
        context="Neo4j live runtime contract",
    )
    database_status = runtime_contract["database_status"]
    if not isinstance(database_status, dict) or {
        "name": database_status.get("name"),
        "currentStatus": str(database_status.get("currentStatus", "")).lower(),
        "requestedStatus": str(database_status.get("requestedStatus", "")).lower(),
        "access": str(database_status.get("access", "")).lower(),
    } != {
        "name": "neo4j",
        "currentStatus": "online",
        "requestedStatus": "online",
        "access": "read-only",
    }:
        raise ContractError("Neo4j live SHOW DATABASES contract is invalid")
    memory = require_keys(
        runtime_contract["memory"],
        required=("preregistered", "live"),
        allowed=("preregistered", "live"),
        context="Neo4j live memory contract",
    )
    if memory["preregistered"] != normalized_containers[0]["memory_configuration"]:
        raise ContractError("Neo4j live memory preregistration differs from container environment")
    live_memory = memory["live"]
    expected_live_bytes = memory["preregistered"]["bytes"]
    if not isinstance(live_memory, dict) or set(live_memory) != set(expected_live_bytes):
        raise ContractError("Neo4j live SHOW SETTINGS set is invalid")
    for role, expected_bytes in expected_live_bytes.items():
        item = live_memory[role]
        if not isinstance(item, dict) or item.get("bytes") != expected_bytes:
            raise ContractError(f"Neo4j live memory setting {role} differs from preregistration")
    representative_plan = require_keys(
        runtime_contract["representative_plan"],
        required=("cypher", "parameters", "required_operator", "plan"),
        allowed=("cypher", "parameters", "required_operator", "plan"),
        context="Neo4j representative plan",
    )
    if representative_plan["required_operator"] != "NodeIndexSeek":
        raise ContractError("Neo4j representative plan does not require NodeIndexSeek")

    def plan_operators(value: object) -> list[str]:
        if not isinstance(value, dict) or set(value) != {"operator_type", "identifiers", "arguments", "children"}:
            raise ContractError("Neo4j representative plan tree is malformed")
        operator = nonempty_string(value["operator_type"], "Neo4j representative plan operator")
        children = value["children"]
        if not isinstance(children, list):
            raise ContractError("Neo4j representative plan children are malformed")
        operators = [operator]
        for child in children:
            operators.extend(plan_operators(child))
        return operators

    if "NodeIndexSeek" not in plan_operators(representative_plan["plan"]):
        raise ContractError("Neo4j representative EXPLAIN plan lacks NodeIndexSeek")
    host = runtime_contract["host"]
    if (
        not isinstance(host, dict)
        or set(host) != {"hostname", "fingerprint_sha256", "mem_total_bytes"}
        or not isinstance(host.get("mem_total_bytes"), int)
        or isinstance(host.get("mem_total_bytes"), bool)
        or host["mem_total_bytes"] <= 0
    ):
        raise ContractError("Neo4j live runtime host memory provenance is invalid")

    jvm_lifecycle = require_keys(
        provenance["jvm_lifecycle"],
        required=("stable", "before", "after"),
        allowed=("stable", "before", "after"),
        context="Neo4j JVM lifecycle",
    )
    if jvm_lifecycle["stable"] is not True or jvm_lifecycle["before"] != jvm_lifecycle["after"]:
        raise ContractError("Neo4j JVM lifecycle is not stable")
    jvm = require_keys(
        jvm_lifecycle["before"],
        required=("container_pid", "pid", "java_executable", "gc", "argv", "argv_sha256"),
        allowed=("container_pid", "pid", "java_executable", "gc", "argv", "argv_sha256"),
        context="Neo4j JVM process",
    )
    if (
        jvm["container_pid"] != normalized_containers[0]["pid"]
        or integer(jvm["pid"], "Neo4j JVM PID", 1) <= 0
        or jvm["gc"] != "G1GC"
        or not isinstance(jvm["argv"], list)
        or "-XX:+UseG1GC" not in jvm["argv"]
    ):
        raise ContractError("Neo4j JVM/GC provenance is invalid")
    normalize_sha(jvm["argv_sha256"], "Neo4j JVM argv SHA", required=True)

    dataset = require_keys(
        provenance["dataset"],
        required=("reference", "lineage"),
        allowed=("reference", "lineage"),
        context="Neo4j dataset provenance",
    )
    _neo4j_artifact_ref(dataset["reference"], "Neo4j dataset manifest", verify_file=True)
    lineage = dataset["lineage"]
    if not isinstance(lineage, dict):
        raise ContractError("Neo4j dataset lineage is malformed")
    if (
        Path(str(lineage.get("dataset_root", ""))).resolve()
        != Path(str(request["dataset"]["path"])).resolve()
        or lineage.get("dataset_sha256") != request["dataset"]["sha256"]
    ):
        raise ContractError("Neo4j dataset lineage differs from request")

    store = require_keys(
        provenance["store"],
        required=("reference", "lineage", "validated_sentinels", "import_receipt", "preflight"),
        allowed=("reference", "lineage", "validated_sentinels", "import_receipt", "preflight"),
        context="Neo4j store provenance",
    )
    store_reference = _neo4j_artifact_ref(store["reference"], "Neo4j store manifest", verify_file=True)
    store_lineage = store["lineage"]
    if not isinstance(store_lineage, dict):
        raise ContractError("Neo4j store lineage is malformed")
    if (
        store_lineage.get("schema_version") != "p10-neo4j-store-manifest-v3"
        or store_lineage.get("snapshot_phase") != "offline-prestart-v1"
        or Path(str(store_lineage.get("store_root", ""))).resolve()
        != Path(str(selected["path"])).resolve()
        or store_lineage.get("store_sha256") != selected.get("sha256")
        or store_lineage.get("truth_sha256") != request["truth"]["sha256"]
        or store_lineage.get("dataset_sha256") != request["dataset"]["sha256"]
    ):
        raise ContractError("Neo4j store lineage differs from request")
    runtime = store_lineage.get("runtime_compatibility")
    if (
        not isinstance(runtime, dict)
        or runtime.get("neo4j_version") != "5.26.24"
        or runtime.get("image_ref") != "neo4j:5.26.24"
        or runtime.get("image_digest") != image_digests[0]
    ):
        raise ContractError("Neo4j store runtime compatibility is invalid")
    from adapters.neo4j_store_contract import (  # Local import avoids module cycle.
        CANONICAL_SENTINELS,
        KNOWN_MUTABLE_PATTERNS,
        canonical_sentinel_records,
        validate_owner_only_offline_gate,
        validate_recorded_tree,
    )

    recorded_tree = validate_recorded_tree(store_lineage, "Neo4j store lineage")
    if store_lineage.get("known_mutable_patterns") != list(KNOWN_MUTABLE_PATTERNS):
        raise ContractError("Neo4j offline store mutable-runtime contract is invalid")
    if store_lineage.get("canonical_sentinels") != list(CANONICAL_SENTINELS):
        raise ContractError("Neo4j offline store canonical sentinels are invalid")
    if store_lineage.get("database_contract") != database_expected:
        raise ContractError("Neo4j offline store database contract is invalid")
    import_provenance = store_lineage.get("import_provenance")
    if not isinstance(import_provenance, dict):
        raise ContractError("Neo4j store import provenance is missing")
    import_status = import_provenance.get("status")
    if formal and import_status != "controlled-import-receipt-v3":
        raise ContractError("formal Neo4j store lacks a controlled importer receipt")
    if import_status == "controlled-import-receipt-v3":
        receipt = import_provenance.get("reference")
        _neo4j_same_ref(store["import_receipt"], receipt, "Neo4j controlled import receipt")
        image = import_provenance.get("image")
        if (
            not isinstance(image, dict)
            or image.get("configured_ref") != "neo4j:5.26.24"
            or image.get("selected_repo_digest") != image_digests[0]
        ):
            raise ContractError("Neo4j controlled import image identity is invalid")
    elif import_status == "unverified-historical-store":
        if formal or store["import_receipt"] is not None:
            raise ContractError("unverified Neo4j historical store cannot be upgraded")
        if import_provenance.get("reference") is not None:
            raise ContractError("unverified Neo4j historical store falsely claims a receipt")
    else:
        raise ContractError("Neo4j store import provenance has unknown status")
    offline_keys = (
        "proof_method", "before_hash", "after_hash", "per_file_stat_stability",
        "owner_only_root", "exclusive_store_lock_held_across_hash",
        "docker_mount_rescan", "proc_scan_used",
    )
    offline_audit = require_keys(
        store_lineage.get("offline_audit"),
        required=offline_keys,
        allowed=offline_keys,
        context="Neo4j offline store audit",
    )
    if (
        offline_audit["per_file_stat_stability"] is not True
        or offline_audit["owner_only_root"] is not True
        or offline_audit["exclusive_store_lock_held_across_hash"] is not True
        or offline_audit["docker_mount_rescan"] is not True
        or offline_audit["proc_scan_used"] is not False
    ):
        raise ContractError("Neo4j offline store lacks a complete stability audit")
    validate_owner_only_offline_gate(
        {key: offline_audit[key] for key in ("proof_method", "before_hash", "after_hash")},
        Path(str(selected["path"])),
        "Neo4j offline store audit",
    )
    expected_sentinels = canonical_sentinel_records(recorded_tree["files"])
    if store_lineage.get("sentinel_files") != expected_sentinels:
        raise ContractError("Neo4j offline sentinel metadata differs from its file inventory")
    validated_sentinels = store["validated_sentinels"]
    expected_validated = [
        {
            **item,
            "absolute_path": str((Path(str(selected["path"])).resolve() / item["path"]).resolve()),
        }
        for item in expected_sentinels
    ]
    if validated_sentinels != expected_validated:
        raise ContractError("Neo4j validated sentinels differ from offline manifest")

    preflight = store["preflight"]
    if formal:
        preflight = require_keys(
            preflight,
            required=("reference", "audit"),
            allowed=("reference", "audit"),
            context="Neo4j store preflight provenance",
        )
        preflight_reference = _neo4j_artifact_ref(
            preflight["reference"],
            "Neo4j store preflight",
            verify_file=True,
        )
        preflight_document = read_json(Path(preflight_reference["path"]), "Neo4j store preflight")
        if preflight_document != preflight["audit"]:
            raise ContractError("Neo4j store preflight file/content mismatch")
        validate_neo4j_store_audit(
            preflight_document,
            stage="pre",
            request_ref=request_ref,
            store_root=Path(str(selected["path"])),
            store_manifest_ref=store_reference,
        )
    elif preflight is not None:
        raise ContractError("fixture Neo4j provenance unexpectedly carries a formal store preflight")

    launch = provenance["launch"]
    if formal:
        launch_record = require_keys(
            launch,
            required=("reference", "receipt"),
            allowed=("reference", "receipt"),
            context="Neo4j launch provenance",
        )
        launch_reference = _neo4j_artifact_ref(
            launch_record["reference"],
            "Neo4j launch receipt",
            verify_file=True,
        )
        launch_file = read_json(Path(launch_reference["path"]), "Neo4j launch receipt")
        if launch_file != launch_record["receipt"]:
            raise ContractError("Neo4j launch receipt file/content mismatch")
        from adapters.launch_neo4j_runtime import validate_launch_receipt

        runtime_host = runtime_contract["host"]
        launch_receipt = validate_launch_receipt(
            launch_file,
            verify_artifacts=True,
            expected_host={
                "hostname": runtime_host["hostname"],
                "fingerprint_sha256": runtime_host["fingerprint_sha256"],
            },
        )
        active_binding = system.get("active_repeat_binding")
        expected_launcher = active_binding.get("launcher") if isinstance(active_binding, dict) else None
        if not isinstance(expected_launcher, dict):
            raise ContractError("formal Neo4j result lacks the suite-pinned launcher")
        _neo4j_same_ref(launch_receipt["producer"], expected_launcher, "Neo4j launch producer")
        if launch_receipt["repeat"]["repeat_index"] != request["repeat_index"]:
            raise ContractError("Neo4j launch repeat index differs from request")
        launch_clone = launch_receipt["clone"]
        if Path(launch_clone["runtime_store_root"]["path"]).resolve() != Path(selected["path"]).resolve():
            raise ContractError("Neo4j launch store root differs from request")
        if Path(launch_clone["logs_root"]["path"]).resolve() != logs_root:
            raise ContractError("Neo4j launch logs root differs from request")
        _neo4j_same_ref(launch_clone["store_manifest"], store_reference, "Neo4j launch store manifest")
        _neo4j_same_ref(launch_clone["store_preflight"], preflight_reference, "Neo4j launch preflight")
        launch_contract = launch_receipt["runtime_contract"]
        adapter_args = system.get("adapter", {}).get("args", [])
        uri_positions = [index for index, value in enumerate(adapter_args) if value == "--uri"]
        if len(uri_positions) != 1 or uri_positions[0] + 1 >= len(adapter_args):
            raise ContractError("formal Neo4j adapter lacks one exact --uri")
        parsed_uri = urlparse(adapter_args[uri_positions[0] + 1])
        if (
            launch_contract["container_name"] != containers[0]
            or launch_contract["bolt_host"] != "127.0.0.1"
            or launch_contract["bolt_port"] != parsed_uri.port
            or launch_receipt["image"]["selected_repo_digest"] != image_digests[0]
        ):
            raise ContractError("Neo4j launch runtime identity differs from request")
        launch_running = launch_receipt["docker"]["running"]
        launch_config = launch_running["config"]
        launch_runtime = launch_running["runtime"]
        before = normalized_containers[0]
        expected_launch_identity = {
            "name": launch_config["container_name"],
            "container_id": launch_config["container_id"],
            "pid": launch_runtime["pid"],
            "started_at": launch_runtime["started_at"],
            "restart_count": launch_runtime["restart_count"],
            "image_id": launch_config["image_id"],
            "configured_image": launch_config["configured_image"],
            "container_user": launch_config["container_user"],
            "bolt_port": launch_contract["bolt_port"],
            "restart_policy": launch_config["restart_policy"],
            "docker_log_config": launch_config["log_config"],
            "docker_memory_limit_bytes": launch_config["resource_limits"]["memory_bytes"],
            "docker_memory_swap_bytes": launch_config["resource_limits"]["memory_swap_bytes"],
            "entrypoint": launch_config["entrypoint"],
            "command": launch_config["command"],
        }
        for key, expected in expected_launch_identity.items():
            if before.get(key) != expected:
                raise ContractError(f"Neo4j adapter container {key} differs from launch receipt")
    elif launch is not None:
        raise ContractError("fixture Neo4j provenance unexpectedly carries a formal launch receipt")

    query_keys = (
        "cypher_shape", "relationship_model", "database_name", "required_index",
        "clock", "timing_boundary", "warmup_before_measured",
        "process_reuse_between_phases", "concurrency",
    )
    query = require_keys(
        provenance["query_contract"],
        required=query_keys,
        allowed=query_keys,
        context="Neo4j query contract",
    )
    if (
        query["cypher_shape"]
        != "MATCH (s:V {id: $src})-[:E_{P|N}<type>]->(d:V) RETURN d.id AS dst"
        or query["relationship_model"] != "dense-edge-type-as-outgoing-relationship-type-v1"
        or query["database_name"] != "neo4j"
        or query["required_index"] != database_expected["required_index"]
        or query["clock"] != CLOCK_NAME
        or query["timing_boundary"] != TIMING_BOUNDARY
        or query["warmup_before_measured"] is not True
        or query["process_reuse_between_phases"] is not True
        or query["concurrency"] != 1
    ):
        raise ContractError("Neo4j query contract provenance is invalid")

    if formal:
        repo = provenance["repo"]
        admission = provenance["p02b"].get("admission") if isinstance(provenance["p02b"], dict) else None
        if not isinstance(repo, dict) or repo.get("clean") is not True:
            raise ContractError("formal Neo4j provenance lacks a clean Git state")
        if not isinstance(admission, dict) or admission.get("state") != "PASS" or admission.get("formal_required") is not True:
            raise ContractError("formal Neo4j provenance lacks a formal P02B PASS admission")
    elif provenance["repo"] is not None or provenance["p02b"] is not None:
        raise ContractError("fixture Neo4j provenance must not carry formal Git/P02B admission")


def _livegraph_artifact(
    value: object,
    context: str,
    *,
    expected: object | None = None,
    verify_file: bool = True,
) -> dict[str, Any]:
    """Validate one LiveGraph artifact without re-reading sealed dense input data."""

    if not isinstance(value, dict):
        raise ContractError(f"{context}: expected artifact reference")
    required = {"path", "sha256"}
    if not required.issubset(value):
        raise ContractError(f"{context}: missing path/SHA-256")
    path = Path(nonempty_string(value["path"], f"{context}.path")).resolve()
    sha = normalize_sha(value["sha256"], f"{context}.sha256", required=True)
    if expected is not None:
        if not isinstance(expected, dict):
            raise ContractError(f"{context}: expected lineage is malformed")
        expected_path = Path(nonempty_string(expected.get("path"), f"{context}.expected.path")).resolve()
        if path != expected_path or sha != normalize_sha(
            expected.get("sha256"), f"{context}.expected.sha256", required=True
        ):
            raise ContractError(f"{context}: path/SHA-256 differs from the frozen request")
    if not path.is_file() or path.is_symlink():
        raise ContractError(f"{context}: artifact is not one regular file")
    if "size_bytes" in value:
        size = integer(value["size_bytes"], f"{context}.size_bytes", 0)
        if path.stat().st_size != size:
            raise ContractError(f"{context}: artifact size changed")
    if verify_file and sha256_file(path) != sha:
        raise ContractError(f"{context}: artifact SHA-256 mismatch")
    return {"path": str(path), "sha256": sha, **({"size_bytes": value["size_bytes"]} if "size_bytes" in value else {})}


LIVEGRAPH_NODE_IDENTITY_KEYS = (
    "device", "inode", "kind", "mode_bits", "uid", "gid", "nlink",
    "size_bytes", "mtime_ns", "ctime_ns",
)


def _livegraph_node_identity_from_stat(value: os.stat_result, *, kind: str) -> dict[str, Any]:
    if kind == "regular-file" and not stat.S_ISREG(value.st_mode):
        raise ContractError("LiveGraph node is not a regular file")
    if kind == "directory" and not stat.S_ISDIR(value.st_mode):
        raise ContractError("LiveGraph node is not a directory")
    return {
        "device": int(value.st_dev), "inode": int(value.st_ino), "kind": kind,
        "mode_bits": stat.S_IMODE(value.st_mode), "uid": int(value.st_uid),
        "gid": int(value.st_gid), "nlink": int(value.st_nlink),
        "size_bytes": int(value.st_size), "mtime_ns": int(value.st_mtime_ns),
        "ctime_ns": int(value.st_ctime_ns),
    }


def _validate_livegraph_node_identity(
    value: object,
    path: Path,
    context: str,
    *,
    kind: str,
    expected_mode: int,
) -> dict[str, Any]:
    identity = require_keys(
        value,
        required=LIVEGRAPH_NODE_IDENTITY_KEYS,
        allowed=LIVEGRAPH_NODE_IDENTITY_KEYS,
        context=f"{context} identity",
    )
    for key in LIVEGRAPH_NODE_IDENTITY_KEYS:
        if key != "kind":
            integer(identity[key], f"{context} identity.{key}", 0)
    if identity["kind"] != kind or identity["mode_bits"] != expected_mode:
        raise ContractError(f"{context} kind/mode identity drift")
    if kind == "regular-file" and identity["nlink"] != 1:
        raise ContractError(f"{context} must have exactly one hard link")
    if path.is_symlink():
        raise ContractError(f"{context} must not be a symbolic link")
    try:
        current = _livegraph_node_identity_from_stat(path.lstat(), kind=kind)
    except OSError as exc:
        raise ContractError(f"cannot stat {context}: {exc}") from exc
    if identity != current:
        raise ContractError(f"{context} changed after adapter terminal identity")
    return dict(identity)


def validate_livegraph_dataset_terminal_identity(provenance: object) -> None:
    """Recheck the sealed dense input by inode metadata, never by payload hashing."""

    if (
        not isinstance(provenance, dict)
        or provenance.get("schema_version") != "p10-livegraph-adapter-provenance-v2"
    ):
        raise ContractError("LiveGraph terminal dataset identity requires formal v2 provenance")
    artifacts = provenance.get("artifacts")
    dataset = artifacts.get("dataset") if isinstance(artifacts, dict) else None
    if not isinstance(dataset, dict):
        raise ContractError("LiveGraph provenance lacks its terminal dataset artifact")
    raw_path = Path(nonempty_string(dataset.get("path"), "LiveGraph terminal dataset path"))
    if raw_path.is_symlink():
        raise ContractError("LiveGraph terminal dataset must not be a symbolic link")
    path = raw_path.resolve()
    try:
        current_stat = path.lstat()
    except OSError as exc:
        raise ContractError(f"cannot stat LiveGraph terminal dataset: {exc}") from exc
    if not stat.S_ISREG(current_stat.st_mode):
        raise ContractError("LiveGraph terminal dataset is not one regular file")
    identity_keys = ("device", "inode", "size_bytes", "mtime_ns", "ctime_ns")
    identity = require_keys(
        dataset.get("identity_before_and_after"),
        required=identity_keys,
        allowed=identity_keys,
        context="LiveGraph terminal dataset identity",
    )
    current = {
        "device": current_stat.st_dev,
        "inode": current_stat.st_ino,
        "size_bytes": current_stat.st_size,
        "mtime_ns": current_stat.st_mtime_ns,
        "ctime_ns": current_stat.st_ctime_ns,
    }
    if identity != current or dataset.get("size_bytes") != current["size_bytes"]:
        raise ContractError("LiveGraph dataset changed after its worker-bound identity")

    seal = provenance.get("dataset_seal")
    seal_ref = _livegraph_artifact(seal, "LiveGraph terminal dataset seal")
    seal_file = read_json(Path(seal_ref["path"]), "LiveGraph terminal dataset seal")
    if not isinstance(seal, dict) or seal.get("content") != seal_file:
        raise ContractError("LiveGraph terminal dataset seal file/content mismatch")
    sealed_dataset = seal_file.get("dataset") if isinstance(seal_file, dict) else None
    if (
        seal_file.get("schema_version") != "p10-livegraph-dataset-seal-v1"
        or seal_file.get("state") != "PASS"
        or not isinstance(sealed_dataset, dict)
        or Path(str(sealed_dataset.get("path", ""))).resolve() != path
        or sealed_dataset.get("sha256") != dataset.get("sha256")
        or sealed_dataset.get("identity") != identity
    ):
        raise ContractError("LiveGraph terminal dataset seal identity drift")


def _validate_livegraph_provenance(
    provenance: object,
    request: dict[str, Any],
    system: dict[str, Any],
    process_lifetime: str,
) -> None:
    """Fail closed on the native LiveGraph worker's complete formal lineage."""

    keys = (
        "schema_version", "execution_mode", "system_id", "suite_id", "run_id",
        "repeat_index", "process_lifetime", "completed_at_utc", "artifacts", "store",
        "temp", "environment", "command", "worker_lifecycle", "formal_build",
        "p02b_admission", "dataset_seal", "formal_gate_options",
    )
    value = require_keys(provenance, required=keys, allowed=keys, context="LiveGraph adapter provenance")
    formal = request.get("execution_mode") == "formal"
    exact = {
        "schema_version": (
            "p10-livegraph-adapter-provenance-v2"
            if formal else "p10-livegraph-adapter-provenance-v1"
        ),
        "execution_mode": request.get("execution_mode"),
        "system_id": "livegraph",
        "suite_id": request.get("suite_id"),
        "run_id": request.get("run_id"),
        "repeat_index": request.get("repeat_index"),
        "process_lifetime": process_lifetime,
    }
    for key, expected in exact.items():
        if value.get(key) != expected:
            raise ContractError(f"LiveGraph provenance {key} differs from request")
    nonempty_string(value["completed_at_utc"], "LiveGraph provenance.completed_at_utc")
    if process_lifetime != FRESH_IMPORT_PROCESS_LIFETIME:
        raise ContractError("LiveGraph provenance must use the fresh-import process lifetime")

    artifact_keys = (
        "request", "adapter", "binary", "dataset", "truth", "runtime_library",
        "runtime_capability", "observations", "phase_events", "worker_summary",
        "adapter_result",
    )
    artifacts = require_keys(
        value["artifacts"], required=artifact_keys, allowed=artifact_keys,
        context="LiveGraph provenance artifacts",
    )
    _livegraph_artifact(
        artifacts["adapter"], "LiveGraph adapter", expected=system.get("adapter")
    )
    _livegraph_artifact(artifacts["binary"], "LiveGraph binary", expected=request.get("binary"))
    # The adapter already checked the dense stream before launch and records an
    # inode/size identity across the worker lifetime.  The P10/P31 consumer must
    # not turn that lineage check into another multi-GB SHA scan.
    dataset = _livegraph_artifact(
        artifacts["dataset"], "LiveGraph dataset", expected=request.get("dataset"), verify_file=False
    )
    dataset_identity = artifacts["dataset"].get("identity_before_and_after")
    identity_keys = ("device", "inode", "size_bytes", "mtime_ns", "ctime_ns")
    dataset_identity = require_keys(
        dataset_identity, required=identity_keys, allowed=identity_keys,
        context="LiveGraph dataset identity",
    )
    for key in identity_keys:
        integer(dataset_identity[key], f"LiveGraph dataset identity.{key}", 0)
    dataset_stat = Path(dataset["path"]).stat()
    current_dataset_identity = {
        "device": dataset_stat.st_dev,
        "inode": dataset_stat.st_ino,
        "size_bytes": dataset_stat.st_size,
        "mtime_ns": dataset_stat.st_mtime_ns,
        "ctime_ns": dataset_stat.st_ctime_ns,
    }
    if dataset_identity != current_dataset_identity:
        raise ContractError("LiveGraph dataset identity differs from the sealed file")
    truth = _livegraph_artifact(artifacts["truth"], "LiveGraph truth", expected=request.get("truth"))
    truth_identity = artifacts["truth"].get("identity_before_and_after")
    truth_identity = require_keys(
        truth_identity, required=identity_keys, allowed=identity_keys,
        context="LiveGraph truth identity",
    )
    truth_stat = Path(truth["path"]).stat()
    current_truth_identity = {
        "device": truth_stat.st_dev,
        "inode": truth_stat.st_ino,
        "size_bytes": truth_stat.st_size,
        "mtime_ns": truth_stat.st_mtime_ns,
        "ctime_ns": truth_stat.st_ctime_ns,
    }
    if truth_identity != current_truth_identity:
        raise ContractError("LiveGraph truth identity differs from the sealed file")
    libraries = request.get("runtime_libraries")
    if not isinstance(libraries, list) or len(libraries) != 1:
        raise ContractError("LiveGraph request must bind exactly one runtime library")
    runtime = _livegraph_artifact(
        artifacts["runtime_library"], "LiveGraph runtime library", expected=libraries[0]
    )
    for label, ref in (("binary", artifacts["binary"]), ("runtime library", artifacts["runtime_library"])):
        identity = require_keys(
            ref.get("identity_before_and_after") if isinstance(ref, dict) else None,
            required=identity_keys, allowed=identity_keys,
            context=f"LiveGraph {label} identity",
        )
        path = Path(ref["path"]).resolve()
        current_stat = path.stat()
        current_identity = {
            "device": current_stat.st_dev, "inode": current_stat.st_ino,
            "size_bytes": current_stat.st_size, "mtime_ns": current_stat.st_mtime_ns,
            "ctime_ns": current_stat.st_ctime_ns,
        }
        if identity != current_identity:
            raise ContractError(f"LiveGraph {label} identity changed across the worker lifecycle")
    for label in (
        "request", "runtime_capability", "observations", "phase_events",
        "worker_summary", "adapter_result",
    ):
        _livegraph_artifact(artifacts[label], f"LiveGraph {label}")

    request_store = [
        root for root in request.get("store_roots", [])
        if isinstance(root, dict) and root.get("label") == "livegraph"
    ]
    formal_store_keys = (
        "root", "initial_state", "final_entries", "terminal_policy", "read_only_policy",
        "root_identity_after_worker_exit", "block", "wal",
    )
    fixture_store_keys = ("root", "initial_state", "final_entries", "block", "wal")
    store_keys = formal_store_keys if formal else fixture_store_keys
    store = require_keys(
        value["store"], required=store_keys, allowed=store_keys,
        context="LiveGraph provenance store",
    )
    if len(request_store) != 1 or Path(str(store["root"])).resolve() != Path(request_store[0]["path"]).resolve():
        raise ContractError("LiveGraph provenance store differs from request")
    if store["initial_state"] != "wholly-empty" or store["final_entries"] != ["livegraph-block", "livegraph-wal"]:
        raise ContractError("LiveGraph provenance store lifecycle drift")
    store_root = Path(str(store["root"])).resolve()
    if formal:
        if (
            store["terminal_policy"] != "worker-exited-posix-read-only-stat-only-v1"
            or store["read_only_policy"] != "root-0555-files-0444-v1"
        ):
            raise ContractError("LiveGraph formal store terminal/read-only policy drift")
        root_identity = _validate_livegraph_node_identity(
            store["root_identity_after_worker_exit"],
            store_root,
            "LiveGraph formal store root",
            kind="directory",
            expected_mode=0o555,
        )
        for label, basename in (("block", "livegraph-block"), ("wal", "livegraph-wal")):
            ref = require_keys(
                store[label],
                required=("role", "path", "content_sha256_mode", "identity_after_worker_exit"),
                allowed=("role", "path", "content_sha256_mode", "identity_after_worker_exit"),
                context=f"LiveGraph store {label}",
            )
            raw_path = Path(nonempty_string(ref["path"], f"LiveGraph store {label}.path"))
            if raw_path.is_symlink():
                raise ContractError(f"LiveGraph store {label} must not be a symbolic link")
            path = raw_path.resolve()
            if (
                ref["role"] != label
                or ref["content_sha256_mode"] != "deferred-post-p31-full-sha256-v1"
                or path.parent != store_root
                or path.name != basename
            ):
                raise ContractError(f"LiveGraph {label} escaped the deferred store contract")
            _validate_livegraph_node_identity(
                ref["identity_after_worker_exit"], path, f"LiveGraph store {label}",
                kind="regular-file", expected_mode=0o444,
            )
        if root_identity["nlink"] < 2:
            raise ContractError("LiveGraph formal store root link count is invalid")
    else:
        for label, basename in (("block", "livegraph-block"), ("wal", "livegraph-wal")):
            ref = _livegraph_artifact(store[label], f"LiveGraph store {label}")
            if Path(ref["path"]).parent != store_root or Path(ref["path"]).name != basename:
                raise ContractError(f"LiveGraph {label} escaped the frozen store contract")

    temp = require_keys(
        value["temp"], required=("root", "environment_variable", "actual_value"),
        allowed=("root", "environment_variable", "actual_value"), context="LiveGraph temp",
    )
    temp_root = Path(nonempty_string(temp["root"], "LiveGraph temp.root")).resolve()
    if temp["environment_variable"] != "TMPDIR" or Path(str(temp["actual_value"])).resolve() != temp_root:
        raise ContractError("LiveGraph temp provenance is inconsistent")
    environment = require_keys(
        value["environment"],
        required=("policy", "inherited_ld_preload_present", "discarded_loader_variables", "injected"),
        allowed=("policy", "inherited_ld_preload_present", "discarded_loader_variables", "injected"),
        context="LiveGraph worker environment",
    )
    if environment["policy"] != "minimal-fixed-env-unique-frozen-lib-parent-v1" or environment["inherited_ld_preload_present"] is not False:
        raise ContractError("LiveGraph worker environment policy drift")
    string_list(environment["discarded_loader_variables"], "LiveGraph discarded loader variables")
    injected = require_keys(
        environment["injected"],
        required=("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "TMPDIR", "TMP", "TEMP"),
        allowed=("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "TMPDIR", "TMP", "TEMP"),
        context="LiveGraph injected environment",
    )
    expected_environment = {
        "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
        "LD_LIBRARY_PATH": str(Path(runtime["path"]).parent),
        "TMPDIR": str(temp_root), "TMP": str(temp_root), "TEMP": str(temp_root),
    }
    if injected != expected_environment:
        raise ContractError("LiveGraph injected worker environment differs from frozen policy")

    command = require_keys(
        value["command"], required=("argv", "cwd"), allowed=("argv", "cwd"),
        context="LiveGraph worker command",
    )
    argv = string_list(command["argv"], "LiveGraph worker argv")
    if not argv or Path(argv[0]).resolve() != Path(request["binary"]["path"]).resolve() or Path(str(command["cwd"])).resolve() != temp_root:
        raise ContractError("LiveGraph worker command binary/cwd differs from request")
    expected_flags = {
        "--store-mode": "fresh-import", "--edges": request["dataset"]["path"],
        "--truth-tsv": request["truth"]["path"], "--warmup-passes": str(request["timing"]["warmup_passes"]),
        "--measured-passes": str(request["timing"]["measured_passes"]),
        "--per-query-timeout-ms": str(request["timing"]["per_query_timeout_ms"]),
        "--expected-query-count": str(request["truth"]["query_count"]),
        "--repeat-index": str(request["repeat_index"]),
    }
    for flag, expected in expected_flags.items():
        positions = [index for index, item in enumerate(argv) if item == flag]
        if len(positions) != 1 or positions[0] + 1 >= len(argv) or argv[positions[0] + 1] != expected:
            raise ContractError(f"LiveGraph worker command {flag} differs from request")

    lifecycle = require_keys(
        value["worker_lifecycle"], required=("start", "exit", "stdout", "stderr", "identity"),
        allowed=("start", "exit", "stdout", "stderr", "identity"),
        context="LiveGraph worker lifecycle",
    )
    lifecycle_files: dict[str, dict[str, Any]] = {}
    for label in ("start", "exit", "stdout", "stderr"):
        lifecycle_files[label] = _livegraph_artifact(lifecycle[label], f"LiveGraph worker {label}")
    start_receipt = read_json(Path(lifecycle_files["start"]["path"]), "LiveGraph worker start receipt")
    exit_receipt = read_json(Path(lifecycle_files["exit"]["path"]), "LiveGraph worker exit receipt")
    identity = require_keys(
        lifecycle["identity"],
        required=("schema_version", "state", "pid", "proc_start_ticks", "process_group_id", "started_at_utc", "ended_at_utc", "started_monotonic_ns", "ended_monotonic_ns", "returncode", "same_process_alive_after_wait", "process_group_members_after_wait"),
        allowed=("schema_version", "state", "pid", "proc_start_ticks", "process_group_id", "started_at_utc", "ended_at_utc", "started_monotonic_ns", "ended_monotonic_ns", "returncode", "same_process_alive_after_wait", "process_group_members_after_wait"),
        context="LiveGraph worker identity",
    )
    if identity != exit_receipt:
        raise ContractError("LiveGraph worker exit receipt/content identity mismatch")
    pid = integer(identity["pid"], "LiveGraph worker PID", 1)
    start_ticks = integer(identity["proc_start_ticks"], "LiveGraph worker start ticks", 1)
    if (
        identity["schema_version"] != "p10-livegraph-worker-pid-v2"
        or identity["state"] != "EXITED" or identity["returncode"] != 0
        or identity["same_process_alive_after_wait"] is not False
        or identity["process_group_id"] != pid
        or identity["process_group_members_after_wait"] != []
        or start_receipt.get("schema_version") != "p10-livegraph-worker-pid-v2"
        or start_receipt.get("state") != "RUNNING"
        or start_receipt.get("pid") != pid or start_receipt.get("proc_start_ticks") != start_ticks
        or start_receipt.get("process_group_id") != pid
        or start_receipt.get("argv") != argv or Path(str(start_receipt.get("cwd", ""))).resolve() != temp_root
    ):
        raise ContractError("LiveGraph worker lifecycle identity is not one clean execution")

    gate = require_keys(
        value["formal_gate_options"],
        required=("build_receipt", "p02b_result", "p02b_max_age_seconds", "dataset_seal", "temp_base", "temp_label"),
        allowed=("build_receipt", "p02b_result", "p02b_max_age_seconds", "dataset_seal", "temp_base", "temp_label"),
        context="LiveGraph formal gate options",
    )
    if formal:
        build = require_keys(
            value["formal_build"],
            required=("receipt", "marker", "integration", "livegraph_source", "source_library", "binary", "liblivegraph"),
            allowed=("receipt", "marker", "integration", "livegraph_source", "source_library", "binary", "liblivegraph"),
            context="LiveGraph formal build",
        )
        receipt = _livegraph_artifact(build["receipt"], "LiveGraph build receipt")
        _livegraph_artifact(build["marker"], "LiveGraph build marker")
        _livegraph_artifact(build["source_library"], "LiveGraph source library")
        _livegraph_artifact(build["binary"], "LiveGraph built binary", expected=request["binary"])
        _livegraph_artifact(build["liblivegraph"], "LiveGraph built runtime library", expected=libraries[0])
        for label in ("integration", "livegraph_source"):
            state = require_keys(
                build[label], required=("root", "head", "dirty", "status_lines"),
                allowed=("root", "head", "dirty", "status_lines"), context=f"LiveGraph {label}",
            )
            if state["dirty"] is not False or state["status_lines"] != [] or not SHA256_RE.fullmatch(str(state["head"])):
                raise ContractError(f"LiveGraph {label} is not a clean Git identity")
        p02b = require_keys(
            value["p02b_admission"], required=("result", "validator", "admission"),
            allowed=("result", "validator", "admission"), context="LiveGraph P02B binding",
        )
        result_ref = _livegraph_artifact(p02b["result"], "LiveGraph P02B result")
        _livegraph_artifact(p02b["validator"], "LiveGraph P02B validator")
        admission = p02b["admission"]
        if not isinstance(admission, dict) or admission.get("state") != "PASS" or admission.get("consumer") != "P10" or admission.get("formal_required") is not True or admission.get("fixture_only") is not False or admission.get("scale") != "sf10":
            raise ContractError("formal LiveGraph provenance lacks a formal P02B PASS admission")
        if Path(str(admission.get("sentinel_result", ""))).resolve() != Path(result_ref["path"]) or admission.get("sentinel_result_sha256") != result_ref["sha256"]:
            raise ContractError("LiveGraph P02B admission result differs from its sealed input")
        for path_key, sha_key, label in (("pass_marker", "pass_marker_sha256", "PASS marker"), ("provenance", "provenance_sha256", "provenance")):
            _livegraph_artifact(
                {"path": admission.get(path_key), "sha256": admission.get(sha_key)},
                f"LiveGraph P02B {label}",
            )
        seal = require_keys(
            value["dataset_seal"], required=("path", "sha256", "size_bytes", "content"),
            allowed=("path", "sha256", "size_bytes", "content"), context="LiveGraph dataset seal",
        )
        seal_ref = _livegraph_artifact(seal, "LiveGraph dataset seal")
        seal_file = read_json(Path(seal_ref["path"]), "LiveGraph dataset seal")
        if seal_file != seal["content"]:
            raise ContractError("LiveGraph dataset seal file/content mismatch")
        sealed_dataset = seal_file.get("dataset") if isinstance(seal_file, dict) else None
        if (
            seal_file.get("schema_version") != "p10-livegraph-dataset-seal-v1"
            or seal_file.get("state") != "PASS"
            or not isinstance(sealed_dataset, dict)
            or Path(str(sealed_dataset.get("path", ""))).resolve() != Path(request["dataset"]["path"]).resolve()
            or sealed_dataset.get("sha256") != request["dataset"]["sha256"]
            or sealed_dataset.get("identity") != dataset_identity
        ):
            raise ContractError("LiveGraph dataset seal differs from adapter-bound input identity")
        validate_livegraph_dataset_terminal_identity(value)
        if (
            Path(str(gate["build_receipt"])).resolve() != Path(receipt["path"])
            or Path(str(gate["p02b_result"])).resolve() != Path(result_ref["path"])
            or Path(str(gate["dataset_seal"])).resolve() != Path(seal_ref["path"])
            or gate["p02b_max_age_seconds"] != 21600
            or gate["temp_label"] != "scratch"
            or Path(str(gate["temp_base"])).resolve() != temp_root.parent
        ):
            raise ContractError("LiveGraph formal gate options differ from validated receipts")
        expected_version = f"LiveGraph@{build['livegraph_source']['head']}"
        if system.get("system_version") != expected_version or request.get("system_version") != expected_version:
            raise ContractError("formal LiveGraph system_version differs from source HEAD")
    elif value["formal_build"] is not None or value["p02b_admission"] is not None or value["dataset_seal"] is not None or any(gate[key] is not None for key in gate):
        raise ContractError("fixture LiveGraph provenance must not claim formal build/P02B gates")


def validate_adapter_outputs(
    *,
    output_dir: Path,
    request: dict[str, Any],
    system: dict[str, Any],
    truth_rows: list[dict[str, int]],
    max_timeouts: int,
) -> dict[str, Any]:
    result_path = output_dir / "adapter-result.json"
    observations_path = output_dir / "query-observations.tsv"
    events_path = output_dir / "phase-events.jsonl"
    for path in (result_path, observations_path, events_path):
        if not path.is_file():
            raise ContractError(f"adapter omitted required artifact: {path.name}")
    process_lifetime = request.get("process_lifetime")
    legacy_fixture_lifetime = process_lifetime is None
    if legacy_fixture_lifetime:
        if request.get("execution_mode") != "fixture" or not system.get("fixture_only", False):
            raise ContractError("adapter request omitted process_lifetime outside a fixture-only validation")
        process_lifetime = FIXTURE_PROCESS_LIFETIME
    elif process_lifetime not in PROCESS_LIFETIME_POLICIES:
        raise ContractError("adapter request.process_lifetime is outside the frozen vocabulary")
    result_keys = (
        "schema_version",
        "contract_version",
        "system_id",
        "group",
        "system_version",
        "interface_scope",
        "repeat_index",
        "process_lifetime",
        "truth_sha256",
        "sequence_digest_algorithm",
        "timing_boundary",
        "clock",
        "concurrency",
        "per_query_timeout_ms",
        "warmup",
        "measured",
    )
    result = require_keys(
        read_json(result_path, "adapter result"),
        required=tuple(
            key for key in result_keys if not (legacy_fixture_lifetime and key == "process_lifetime")
        ),
        allowed=(*result_keys, "setup"),
        context="adapter result",
    )
    exact_top = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "system_id": system["id"],
        "group": system["group"],
        "system_version": system["system_version"],
        "interface_scope": INTERFACE_SCOPE,
        "repeat_index": request["repeat_index"],
        "truth_sha256": request["truth"]["sha256"],
        "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "concurrency": request["timing"]["concurrency"],
        "per_query_timeout_ms": request["timing"]["per_query_timeout_ms"],
    }
    for key, expected in exact_top.items():
        if result.get(key) != expected:
            raise ContractError(f"adapter result.{key}: {result.get(key)!r} != {expected!r}")
    if not legacy_fixture_lifetime and result.get("process_lifetime") != process_lifetime:
        raise ContractError(
            f"adapter result.process_lifetime: {result.get('process_lifetime')!r} != {process_lifetime!r}"
        )

    observations = _read_observations(observations_path)
    phase_passes = {
        "warmup": request["timing"]["warmup_passes"],
        "measured": request["timing"]["measured_passes"],
    }
    expected_total = len(truth_rows) * sum(phase_passes.values())
    if len(observations) != expected_total:
        raise ContractError(f"adapter observations: {len(observations)} rows != expected {expected_total}")

    counters = {
        phase: {"completed": 0, "timeouts": 0, "mismatches": 0, "latencies": [], "all_latencies": []}
        for phase in phase_passes
    }
    expected_order: list[tuple[str, int, dict[str, int]]] = []
    for phase in ("warmup", "measured"):
        for pass_index in range(phase_passes[phase]):
            expected_order.extend((phase, pass_index, truth) for truth in truth_rows)
    timeout_ns = request["timing"]["per_query_timeout_ms"] * 1_000_000
    for row_index, (row, expected) in enumerate(zip(observations, expected_order)):
        phase, pass_index, truth = expected
        context = f"adapter observations row {row_index}"
        exact = {
            "contract_version": CONTRACT_VERSION,
            "system_id": system["id"],
            "group": system["group"],
            "repeat_index": str(request["repeat_index"]),
            "phase": phase,
            "pass_index": str(pass_index),
            "query_index": str(truth["query_index"]),
            "edge_type": str(truth["edge_type"]),
            "src": str(truth["src"]),
            "expected_count": str(truth["count"]),
            "expected_sum_hash": str(truth["sum_hash"]),
            "expected_xor_hash": str(truth["xor_hash"]),
        }
        for key, expected_value in exact.items():
            if row.get(key) != expected_value:
                raise ContractError(f"{context}: {key}={row.get(key)!r}, expected {expected_value!r}")
        latency_ns = _parse_row_int(row, "latency_ns", context, 1)
        counters[phase]["all_latencies"].append(latency_ns)
        status = row.get("status")
        if status == "timeout":
            if any(row.get(key) != "" for key in ("actual_count", "actual_sum_hash", "actual_xor_hash")):
                raise ContractError(f"{context}: timeout row must leave actual digest fields empty")
            if latency_ns < timeout_ns:
                raise ContractError(f"{context}: timeout latency is below the frozen deadline")
            counters[phase]["timeouts"] += 1
        elif status == "ok":
            if latency_ns > timeout_ns:
                raise ContractError(f"{context}: ok latency exceeds the frozen deadline")
            actual = {
                "count": _parse_row_int(row, "actual_count", context, 0),
                "sum_hash": _parse_row_int(row, "actual_sum_hash", context, 0),
                "xor_hash": _parse_row_int(row, "actual_xor_hash", context, 0),
            }
            counters[phase]["completed"] += 1
            counters[phase]["latencies"].append(latency_ns)
            if any(actual[key] != truth[key] for key in actual):
                counters[phase]["mismatches"] += 1
        else:
            raise ContractError(f"{context}: status must be 'ok' or 'timeout'")

    phase_summary_keys = (
        "passes",
        "requested_queries",
        "completed_queries",
        "timeout_queries",
        "mismatch_queries",
        "started_monotonic_ns",
        "ended_monotonic_ns",
        "elapsed_ns",
        "expected_digest_sha256",
        "actual_digest_sha256",
    )
    for phase, passes in phase_passes.items():
        summary = require_keys(
            result[phase],
            required=phase_summary_keys,
            allowed=phase_summary_keys,
            context=f"adapter result.{phase}",
        )
        phase_rows = [row for row in observations if row["phase"] == phase]
        expected_digest = phase_digest(phase, passes, truth_rows)
        actual_digest = phase_digest(phase, passes, truth_rows, phase_rows)
        expected_values = {
            "passes": passes,
            "requested_queries": passes * len(truth_rows),
            "completed_queries": counters[phase]["completed"],
            "timeout_queries": counters[phase]["timeouts"],
            "mismatch_queries": counters[phase]["mismatches"],
            "expected_digest_sha256": expected_digest,
            "actual_digest_sha256": actual_digest,
        }
        for key, expected_value in expected_values.items():
            if summary.get(key) != expected_value:
                raise ContractError(f"adapter result.{phase}.{key}: {summary.get(key)!r} != {expected_value!r}")
        start = integer(summary["started_monotonic_ns"], f"adapter result.{phase}.started_monotonic_ns", 0)
        end = integer(summary["ended_monotonic_ns"], f"adapter result.{phase}.ended_monotonic_ns", start + 1)
        elapsed = integer(summary["elapsed_ns"], f"adapter result.{phase}.elapsed_ns", 1)
        if elapsed != end - start:
            raise ContractError(f"adapter result.{phase}.elapsed_ns differs from phase boundary")
        if elapsed < sum(counters[phase]["all_latencies"]):
            raise ContractError(
                f"adapter result.{phase}.elapsed_ns is below the sum of serial per-query latencies"
            )
        if counters[phase]["mismatches"]:
            raise ContractError(f"adapter result.{phase}: {counters[phase]['mismatches']} truth mismatches")
        if counters[phase]["timeouts"] > max_timeouts:
            raise ContractError(f"adapter result.{phase}: {counters[phase]['timeouts']} timeouts > allowed {max_timeouts}")
        if summary["actual_digest_sha256"] != summary["expected_digest_sha256"]:
            raise ContractError(f"adapter result.{phase}: sequence digest mismatch")

    _validate_events(events_path, result)
    measured_latencies = counters["measured"]["latencies"]
    measured_elapsed_ns = result["measured"]["elapsed_ns"]
    completed = counters["measured"]["completed"]
    if completed == 0 or measured_elapsed_ns <= 0:
        raise ContractError("measured phase contains no completed timed query")
    provenance_path = output_dir / "adapter-provenance.json"
    adapter_provenance: dict[str, Any] | None = None
    if provenance_path.is_file():
        adapter_provenance = read_json(provenance_path, "adapter provenance")
        if system["id"] == "seml0":
            if adapter_provenance.get("schema_version") != "p10-seml0-adapter-provenance-v1":
                raise ContractError("SemL0 adapter provenance has wrong schema_version")
            if adapter_provenance.get("variant") not in {"naive", "schema", "b64", "budg-b64", "semantic"}:
                raise ContractError("SemL0 adapter provenance has unknown variant")
            command = adapter_provenance.get("command")
            if not isinstance(command, dict) or command.get("invocations") != 1 or command.get("exit_code") != 0:
                raise ContractError("SemL0 adapter must record exactly one successful storage-bench invocation")
        elif system["id"] == "aster":
            if adapter_provenance.get("schema_version") != "p10-aster-adapter-provenance-v1":
                raise ContractError("Aster adapter provenance has wrong schema_version")
            if adapter_provenance.get("lifecycle") not in {"fresh", "reopen"}:
                raise ContractError("Aster adapter provenance has unknown lifecycle")
            if adapter_provenance.get("process_lifetime") != process_lifetime:
                raise ContractError("Aster adapter provenance process lifetime differs from request")
            provenance_libraries = adapter_provenance.get("runtime_libraries")
            request_libraries = request.get("runtime_libraries")
            if not isinstance(provenance_libraries, list) or not isinstance(request_libraries, list):
                raise ContractError("Aster adapter provenance lacks runtime-library lineage")
            if len(provenance_libraries) != len(request_libraries):
                raise ContractError("Aster adapter provenance runtime-library count differs from request")
            for index, (observed, expected) in enumerate(zip(provenance_libraries, request_libraries)):
                if not isinstance(observed, dict) or not isinstance(expected, dict):
                    raise ContractError(f"Aster runtime library {index} is not an artifact reference")
                if Path(str(observed.get("path", ""))).resolve() != Path(str(expected.get("path", ""))).resolve():
                    raise ContractError(f"Aster runtime library {index} path differs from request")
                if observed.get("sha256") != expected.get("sha256"):
                    raise ContractError(f"Aster runtime library {index} SHA-256 differs from request")
            command = adapter_provenance.get("command")
            if not isinstance(command, dict) or command.get("invocations") != 1 or command.get("exit_code") != 0:
                raise ContractError("Aster adapter must record exactly one successful RocksGraph worker invocation")
            if request.get("execution_mode") == "formal":
                if adapter_provenance.get("mode") != "formal" or adapter_provenance.get("lifecycle") != "reopen":
                    raise ContractError("formal Aster provenance must describe a reopen lifecycle")
                if process_lifetime != PREBUILT_PROCESS_LIFETIME:
                    raise ContractError("formal Aster provenance must use the prebuilt-store process lifetime")
                p02b = adapter_provenance.get("p02b")
                admission = p02b.get("admission") if isinstance(p02b, dict) else None
                if not isinstance(admission, dict) or admission.get("state") != "PASS" or admission.get("formal_required") is not True:
                    raise ContractError("formal Aster provenance lacks a formal P02B PASS admission")
        elif system["id"] == "tugraph":
            if adapter_provenance.get("schema_version") != "cidr-p10-tugraph-provenance-v1":
                raise ContractError("TuGraph adapter provenance has wrong schema_version")
            if adapter_provenance.get("execution_model") != "native-embedded-single-worker-process-v1":
                raise ContractError("TuGraph adapter provenance has wrong execution model")
            if adapter_provenance.get("process_lifetime") != process_lifetime:
                raise ContractError("TuGraph adapter provenance process lifetime differs from request")
            provenance_libraries = adapter_provenance.get("runtime_libraries")
            request_libraries = request.get("runtime_libraries")
            if not isinstance(provenance_libraries, list) or not isinstance(request_libraries, list):
                raise ContractError("TuGraph adapter provenance lacks runtime-library lineage")
            if len(provenance_libraries) != len(request_libraries):
                raise ContractError("TuGraph adapter provenance runtime-library count differs from request")
            for index, (observed, expected) in enumerate(zip(provenance_libraries, request_libraries)):
                if not isinstance(observed, dict) or not isinstance(expected, dict):
                    raise ContractError(f"TuGraph runtime library {index} is not an artifact reference")
                if Path(str(observed.get("path", ""))).resolve() != Path(str(expected.get("path", ""))).resolve():
                    raise ContractError(f"TuGraph runtime library {index} path differs from request")
                if observed.get("sha256") != expected.get("sha256"):
                    raise ContractError(f"TuGraph runtime library {index} SHA-256 differs from request")
            if request.get("execution_mode") == "formal":
                if adapter_provenance.get("execution_mode") != "formal":
                    raise ContractError("formal TuGraph provenance has wrong execution_mode")
                if process_lifetime != PREBUILT_PROCESS_LIFETIME:
                    raise ContractError("formal TuGraph provenance must use the prebuilt-store process lifetime")
                p02b = adapter_provenance.get("p02b_release")
                if not isinstance(p02b, dict) or p02b.get("require_formal") is not True:
                    raise ContractError("formal TuGraph provenance lacks a formal P02B release")
        elif system["id"] == "neo4j":
            _validate_neo4j_provenance(
                adapter_provenance,
                request,
                system,
                process_lifetime,
            )
        elif system["id"] == "livegraph":
            _validate_livegraph_provenance(
                adapter_provenance,
                request,
                system,
                process_lifetime,
            )
        elif system["id"] == "nebulagraph":
            if adapter_provenance.get("schema_version") != "p10-nebulagraph-adapter-provenance-v1":
                raise ContractError("NebulaGraph adapter provenance has wrong schema_version")
            exact = {
                "execution_mode": request.get("execution_mode"),
                "group": "client-server",
                "process_lifetime": process_lifetime,
                "system_version": system["system_version"],
            }
            for key, expected in exact.items():
                if adapter_provenance.get(key) != expected:
                    raise ContractError(f"NebulaGraph provenance {key} differs from request")

            def same_artifact(observed: object, expected: object, context: str) -> None:
                if not isinstance(observed, dict) or not isinstance(expected, dict):
                    raise ContractError(f"NebulaGraph provenance lacks {context} artifact reference")
                path = Path(str(observed.get("path", ""))).resolve()
                if path != Path(str(expected.get("path", ""))).resolve():
                    raise ContractError(f"NebulaGraph provenance {context} path differs from request")
                if observed.get("sha256") != expected.get("sha256"):
                    raise ContractError(f"NebulaGraph provenance {context} SHA-256 differs from request")

            for label in ("binary", "dataset", "truth"):
                same_artifact(adapter_provenance.get(label), request.get(label), label)
            request_store = next(
                (root for root in request.get("store_roots", []) if root.get("label") == "nebulagraph"),
                None,
            )
            same_artifact(adapter_provenance.get("store"), request_store, "store")
            for label in ("store_manifest", "runtime_manifest"):
                ref = adapter_provenance.get(label)
                if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
                    raise ContractError(f"NebulaGraph provenance lacks {label}")
                ref_path = Path(ref["path"]).resolve()
                if not ref_path.is_file() or ref.get("sha256") != sha256_file(ref_path):
                    raise ContractError(f"NebulaGraph provenance {label} path/SHA is invalid")
            client = adapter_provenance.get("client")
            if not isinstance(client, dict) or client.get("version") != "3.8.3":
                raise ContractError("NebulaGraph provenance lacks the frozen Python client version")
            client_tree = client.get("tree")
            if (
                not isinstance(client_tree, dict)
                or client_tree.get("hash_method") != "sha256-tree-v1(relative-path,size,file-sha256)"
                or not SHA256_RE.fullmatch(str(client_tree.get("sha256", "")))
            ):
                raise ContractError("NebulaGraph provenance lacks frozen client-tree lineage")
            images = adapter_provenance.get("images")
            if not isinstance(images, list) or len(images) != 3:
                raise ContractError("NebulaGraph provenance must bind exactly three images")
            roles = {item.get("role") for item in images if isinstance(item, dict)}
            if roles != {"graphd", "metad", "storaged"}:
                raise ContractError("NebulaGraph provenance image-role coverage drift")
            for image in images:
                if (
                    not isinstance(image, dict)
                    or not IMAGE_DIGEST_RE.fullmatch(str(image.get("image_id", "")))
                    or "@sha256:" not in str(image.get("repo_digest", ""))
                ):
                    raise ContractError("NebulaGraph provenance image identity is malformed")
            observed_digests = adapter_provenance.get("image_digests")
            expected_digests = system.get("image_digests", [])
            if not isinstance(observed_digests, list) or set(observed_digests) != set(expected_digests):
                raise ContractError("NebulaGraph provenance image digests differ from suite manifest")
            if len(observed_digests) != 3 or len(set(observed_digests)) != 3:
                raise ContractError("NebulaGraph provenance requires three distinct image digests")
            container_runtime = adapter_provenance.get("container_runtime")
            if not isinstance(container_runtime, list) or len(container_runtime) != 3:
                raise ContractError("NebulaGraph provenance must bind three live containers")
            runtime_roles = set()
            runtime_names = []
            image_ids = {
                image.get("role"): image.get("image_id")
                for image in images
                if isinstance(image, dict)
            }
            for item in container_runtime:
                if not isinstance(item, dict):
                    raise ContractError("NebulaGraph container runtime entry is malformed")
                role = item.get("role")
                runtime_roles.add(role)
                runtime_names.append(item.get("name"))
                if (
                    item.get("image_id") != image_ids.get(role)
                    or item.get("restart_count") != 0
                    or not isinstance(item.get("pid"), int)
                    or isinstance(item.get("pid"), bool)
                    or item.get("pid") <= 0
                    or not isinstance(item.get("process_start_ticks"), int)
                    or isinstance(item.get("process_start_ticks"), bool)
                    or item.get("process_start_ticks") <= 0
                    or not isinstance(item.get("container_id"), str)
                    or not item.get("container_id")
                ):
                    raise ContractError("NebulaGraph live-container image/PID/restart lineage is invalid")
            if runtime_roles != {"graphd", "metad", "storaged"} or runtime_names != adapter_provenance.get("container_names"):
                raise ContractError("NebulaGraph live-container roles/names differ from provenance")
            session = adapter_provenance.get("service_session")
            if (
                not isinstance(session, dict)
                or session.get("session_open_count") != 1
                or session.get("restart_count") != 0
                or session.get("warmup_measured_same_session") is not True
            ):
                raise ContractError("NebulaGraph provenance violates the one-session/no-restart lifecycle")
            if request.get("execution_mode") == "formal":
                if adapter_provenance.get("service_mode") != "external-prestarted":
                    raise ContractError("formal NebulaGraph provenance must attach to an external prestarted service")
                if adapter_provenance.get("container_names") != system.get("containers"):
                    raise ContractError("formal NebulaGraph container names differ from suite manifest")
                admission = adapter_provenance.get("p02b_admission")
                if (
                    not isinstance(admission, dict)
                    or admission.get("state") != "PASS"
                    or admission.get("formal_required") is not True
                ):
                    raise ContractError("formal NebulaGraph provenance lacks a formal P02B PASS admission")
                lifecycle_request = request.get("external_service", {}).get(
                    "orchestrated_lifecycle"
                )
                lifecycle = adapter_provenance.get("cluster_lifecycle")
                if not isinstance(lifecycle_request, dict) or not isinstance(lifecycle, dict):
                    raise ContractError(
                        "formal NebulaGraph provenance lacks orchestrated lifecycle receipts"
                    )
                same_artifact(
                    lifecycle.get("controller"),
                    lifecycle_request.get("controller"),
                    "lifecycle controller",
                )
                requested_receipts = lifecycle_request.get("receipts")
                if not isinstance(requested_receipts, dict):
                    raise ContractError("formal NebulaGraph request lacks lifecycle receipt paths")
                preflight = lifecycle.get("preflight")
                start = lifecycle.get("start")
                live_gate = lifecycle.get("live_gate")
                if not all(isinstance(value, dict) for value in (preflight, start, live_gate)):
                    raise ContractError("formal NebulaGraph lifecycle receipt references are malformed")
                for name, reference in (("preflight", preflight), ("start", start)):
                    path = Path(str(reference.get("path", ""))).resolve()
                    if (
                        path != Path(str(requested_receipts.get(name, ""))).resolve()
                        or not path.is_file()
                        or reference.get("sha256") != sha256_file(path)
                    ):
                        raise ContractError(
                            f"formal NebulaGraph {name} receipt differs from request/disk"
                        )
                if (
                    Path(str(live_gate.get("path", ""))).resolve()
                    != Path(str(start.get("path", ""))).resolve()
                    or live_gate.get("sha256") != start.get("sha256")
                    or live_gate.get("selector") != "live_gate"
                ):
                    raise ContractError("formal NebulaGraph live-gate receipt is not launch-bound")
                preflight_receipt = preflight.get("receipt")
                start_receipt = start.get("receipt")
                live_receipt = live_gate.get("receipt")
                if (
                    not isinstance(preflight_receipt, dict)
                    or preflight_receipt.get("schema_version")
                    != "cidr-p10-nebulagraph-preflight-v1"
                    or preflight_receipt.get("state") != "PASS"
                    or not isinstance(start_receipt, dict)
                    or start_receipt.get("schema_version")
                    != "cidr-p10-nebulagraph-start-receipt-v1"
                    or start_receipt.get("state") != "PASS"
                    or live_receipt != start_receipt.get("live_gate")
                    or not isinstance(live_receipt, dict)
                    or live_receipt.get("state") != "PASS"
                ):
                    raise ContractError("formal NebulaGraph lifecycle PASS evidence is incomplete")
                start_containers = start_receipt.get("containers")
                if not isinstance(start_containers, dict) or [
                    start_containers.get(role) for role in ("graphd", "metad", "storaged")
                ] != container_runtime:
                    raise ContractError(
                        "formal NebulaGraph adapter runtime differs from start receipt IDs/PIDs"
                    )
                if Path(str(lifecycle.get("planned_stop", ""))).resolve() != Path(
                    str(requested_receipts.get("stop", ""))
                ).resolve():
                    raise ContractError("formal NebulaGraph planned stop receipt path drift")
                sealed = adapter_provenance.get("sealed_admission")
                if not isinstance(sealed, dict):
                    raise ContractError("formal NebulaGraph provenance lacks sealed admission")
                sealed_path = Path(str(sealed.get("path", ""))).resolve()
                if (
                    sealed_path
                    != Path(str(requested_receipts.get("sealed_admission", ""))).resolve()
                    or not sealed_path.is_file()
                    or sealed.get("sha256") != sha256_file(sealed_path)
                ):
                    raise ContractError("formal NebulaGraph sealed admission path/SHA drift")
                sealed_receipt = sealed.get("receipt")
                if (
                    not isinstance(sealed_receipt, dict)
                    or sealed_receipt.get("schema_version")
                    != "cidr-p10-nebulagraph-sealed-admission-v1"
                    or sealed_receipt.get("state") != "PASS"
                ):
                    raise ContractError("formal NebulaGraph sealed admission is not PASS")
                sealed_validated = sealed_receipt.get("validated")
                sealed_store = sealed_receipt.get("store")
                sealed_tree = sealed_store.get("tree") if isinstance(sealed_store, dict) else None
                if (
                    not isinstance(sealed_validated, dict)
                    or sealed_validated.get("run_id") != request.get("run_id")
                    or sealed_validated.get("repeat_index") != request.get("repeat_index")
                    or not isinstance(sealed_store, dict)
                    or not isinstance(sealed_tree, dict)
                    or Path(str(sealed_store.get("path", ""))).resolve()
                    != Path(str(request_store.get("path", ""))).resolve()
                    or sealed_tree.get("sha256")
                    != request_store.get("sha256")
                ):
                    raise ContractError("formal NebulaGraph sealed store/run/repeat binding drift")
                lock = sealed_receipt.get("lock")
                owner = lock.get("owner") if isinstance(lock, dict) else None
                if (
                    not isinstance(owner, dict)
                    or not isinstance(owner.get("pid"), int)
                    or owner.get("pid") <= 1
                    or not isinstance(owner.get("process_start_ticks"), int)
                    or owner.get("process_start_ticks") <= 0
                ):
                    raise ContractError("formal NebulaGraph sealed admission lacks lock owner")
    elif system["id"] in {"seml0", "livegraph"} and not system.get("fixture_only", False):
        raise ContractError(f"formal {system['id']} adapter omitted adapter-provenance.json")
    elif system["id"] == "aster" and not system.get("fixture_only", False):
        raise ContractError("formal Aster adapter omitted adapter-provenance.json")
    elif system["id"] == "tugraph" and not system.get("fixture_only", False):
        raise ContractError("formal TuGraph adapter omitted adapter-provenance.json")
    elif system["id"] == "neo4j" and not system.get("fixture_only", False):
        raise ContractError("formal Neo4j adapter omitted adapter-provenance.json")
    elif system["id"] == "nebulagraph" and not system.get("fixture_only", False):
        raise ContractError("formal NebulaGraph adapter omitted adapter-provenance.json")
    artifact_paths = [result_path, observations_path, events_path]
    if provenance_path.is_file():
        artifact_paths.append(provenance_path)
    setup_values: dict[str, Any] = {
        "import_wall_s": "",
        "import_user_cpu_s": "",
        "import_system_cpu_s": "",
        "import_store_logical_bytes": "",
        "import_store_allocated_bytes": "",
    }
    if process_lifetime == FRESH_IMPORT_PROCESS_LIFETIME:
        if "setup" not in result:
            raise ContractError("fresh-import adapter result omitted setup metrics")
        setup_keys = (
            "policy",
            "kind",
            "started_monotonic_ns",
            "ended_monotonic_ns",
            "wall_ns",
            "user_cpu_ns",
            "system_cpu_ns",
            "store_logical_bytes",
            "store_allocated_bytes",
            "binary_sha256",
            "dataset_sha256",
            "truth_sha256",
            "runtime_libraries",
        )
        setup = require_keys(
            result["setup"], required=setup_keys, allowed=setup_keys, context="adapter result.setup"
        )
        exact_setup = {
            "policy": FRESH_IMPORT_PROCESS_LIFETIME,
            "kind": "fresh-import",
            "binary_sha256": request["binary"]["sha256"],
            "dataset_sha256": request["dataset"]["sha256"],
            "truth_sha256": request["truth"]["sha256"],
            "runtime_libraries": request["runtime_libraries"],
        }
        for key, expected in exact_setup.items():
            if setup.get(key) != expected:
                raise ContractError(f"adapter result.setup.{key}: {setup.get(key)!r} != {expected!r}")
        setup_start = integer(setup["started_monotonic_ns"], "adapter result.setup.started_monotonic_ns", 0)
        setup_end = integer(
            setup["ended_monotonic_ns"], "adapter result.setup.ended_monotonic_ns", setup_start + 1
        )
        setup_wall_ns = integer(setup["wall_ns"], "adapter result.setup.wall_ns", 1)
        if setup_wall_ns != setup_end - setup_start:
            raise ContractError("adapter result.setup.wall_ns differs from setup boundary")
        if setup_end > result["warmup"]["started_monotonic_ns"]:
            raise ContractError("fresh import overlaps the warmup interval")
        setup_user_cpu_ns = integer(setup["user_cpu_ns"], "adapter result.setup.user_cpu_ns", 0)
        setup_system_cpu_ns = integer(setup["system_cpu_ns"], "adapter result.setup.system_cpu_ns", 0)
        setup_store_logical = integer(
            setup["store_logical_bytes"], "adapter result.setup.store_logical_bytes", 1
        )
        setup_store_allocated = integer(
            setup["store_allocated_bytes"], "adapter result.setup.store_allocated_bytes", 0
        )
        setup_values = {
            "import_wall_s": setup_wall_ns / 1_000_000_000,
            "import_user_cpu_s": setup_user_cpu_ns / 1_000_000_000,
            "import_system_cpu_s": setup_system_cpu_ns / 1_000_000_000,
            "import_store_logical_bytes": setup_store_logical,
            "import_store_allocated_bytes": setup_store_allocated,
        }
    elif "setup" in result:
        raise ContractError("non-fresh adapter result must not publish fresh-import setup metrics")
    validated = {
        "schema_version": "cidr-p10-validated-repeat-v1",
        "system_id": system["id"],
        "display_name": system["display_name"],
        "group": system["group"],
        "system_version": system["system_version"],
        "interface_scope": INTERFACE_SCOPE,
        "repeat_index": request["repeat_index"],
        "query_count": len(truth_rows),
        "warmup_passes": phase_passes["warmup"],
        "measured_passes": phase_passes["measured"],
        "warmup_s": result["warmup"]["elapsed_ns"] / 1_000_000_000,
        "measurement_s": measured_elapsed_ns / 1_000_000_000,
        "completed_queries": completed,
        "timeout_queries": counters["measured"]["timeouts"],
        "mismatch_queries": counters["measured"]["mismatches"],
        "qps": completed / (measured_elapsed_ns / 1_000_000_000),
        "latency_p50_us": nearest_rank(measured_latencies, 0.50) / 1_000,
        "latency_p95_us": nearest_rank(measured_latencies, 0.95) / 1_000,
        "latency_p99_us": nearest_rank(measured_latencies, 0.99) / 1_000,
        "expected_digest_sha256": result["measured"]["expected_digest_sha256"],
        "actual_digest_sha256": result["measured"]["actual_digest_sha256"],
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "concurrency": request["timing"]["concurrency"],
        "per_query_timeout_ms": request["timing"]["per_query_timeout_ms"],
        **setup_values,
        "adapter_artifacts": {
            path.name: {"path": str(path.resolve()), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
            for path in artifact_paths
        },
        "adapter_provenance": adapter_provenance,
    }
    if not legacy_fixture_lifetime:
        validated["process_lifetime"] = process_lifetime
    return validated


def audit_neo4j_runtime_store(
    *,
    stage: str,
    store_root: Path,
    store_manifest_path: Path,
    store_manifest_sha256: str,
    request_path: Path,
    output_path: Path,
    stop_receipt_path: Path | None = None,
    stop_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    """Hash a complete Neo4j runtime tree outside the P31 measurement window."""

    from adapters.neo4j_store_contract import (  # Local import avoids module cycle.
        KNOWN_MUTABLE_PATTERNS,
        compare_runtime_tree,
        stable_tree_manifest,
        validate_recorded_tree,
    )
    from adapters.freeze_neo4j_store import store_offline_guard

    if stage not in {"pre", "post", "post-stop"}:
        raise ContractError("Neo4j store audit stage must be pre, post, or post-stop")
    if (stop_receipt_path is None) != (stop_receipt_sha256 is None):
        raise ContractError("Neo4j stop receipt path/SHA must be provided together")
    if stage == "post-stop" and stop_receipt_path is None:
        raise ContractError("Neo4j post-stop audit requires its verified stop receipt")
    if stage != "post-stop" and stop_receipt_path is not None:
        raise ContractError("only a Neo4j post-stop audit may bind a stop receipt")
    store_root = store_root.resolve()
    store_manifest_path = store_manifest_path.resolve()
    request_path = request_path.resolve()
    output_path = output_path.resolve()
    if output_path.exists():
        raise ContractError(f"refusing to overwrite Neo4j store audit: {output_path}")
    manifest_sha = normalize_sha(
        store_manifest_sha256,
        "Neo4j store manifest SHA-256",
        required=True,
    )
    if not store_manifest_path.is_file() or sha256_file(store_manifest_path) != manifest_sha:
        raise ContractError("Neo4j store manifest path/SHA is invalid")
    manifest = read_json(store_manifest_path, "Neo4j store manifest")
    if manifest.get("schema_version") != "p10-neo4j-store-manifest-v3":
        raise ContractError("Neo4j store audit requires store manifest v3")
    if Path(str(manifest.get("store_root", ""))).resolve() != store_root:
        raise ContractError("Neo4j store audit root differs from store manifest")
    baseline = validate_recorded_tree(manifest, "Neo4j store manifest")
    if manifest.get("known_mutable_patterns") != list(KNOWN_MUTABLE_PATTERNS):
        raise ContractError("Neo4j store manifest mutable patterns mismatch")
    offline_gate = None
    if stage in {"pre", "post-stop"}:
        with store_offline_guard(store_root, None) as offline_gate:
            current = stable_tree_manifest(
                store_root,
                attempts=3,
                evict_cache=True,
                allow_known_mutable_changes=False,
            )
    else:
        current = stable_tree_manifest(
            store_root,
            attempts=3,
            evict_cache=True,
            allow_known_mutable_changes=True,
        )
    comparison = compare_runtime_tree(
        baseline["files"],
        current,
        context=f"Neo4j runtime {stage} audit",
    )
    if stage == "pre" and comparison["mutable_deltas"]:
        raise ContractError("Neo4j pre-launch store differs from its frozen full-tree manifest")
    request_ref = {
        "path": str(request_path),
        "sha256": sha256_file(request_path),
        "size_bytes": request_path.stat().st_size,
    }
    manifest_ref = {
        "path": str(store_manifest_path),
        "sha256": manifest_sha,
        "size_bytes": store_manifest_path.stat().st_size,
    }
    stop_receipt_ref = None
    if stop_receipt_path is not None:
        stop_receipt_path = stop_receipt_path.resolve()
        stop_sha = normalize_sha(
            stop_receipt_sha256,
            "Neo4j stop receipt SHA-256",
            required=True,
        )
        if not stop_receipt_path.is_file() or sha256_file(stop_receipt_path) != stop_sha:
            raise ContractError("Neo4j post-stop audit stop receipt path/SHA is invalid")
        stop_receipt_ref = {
            "path": str(stop_receipt_path),
            "sha256": stop_sha,
            "size_bytes": stop_receipt_path.stat().st_size,
        }
    document = {
        "schema_version": "p10-neo4j-runtime-store-audit-v1",
        "stage": stage,
        "request": request_ref,
        "store_manifest": manifest_ref,
        "store_root": str(store_root),
        "hash_method": "sha256-tree-v1(relative-path,size,file-sha256)",
        "known_mutable_patterns": list(KNOWN_MUTABLE_PATTERNS),
        "cache_eviction": current["cache_eviction"],
        "known_mutable_changes_tolerated": current["known_mutable_changes_tolerated"],
        "offline_gate": offline_gate,
        "stop_receipt": stop_receipt_ref,
        "audit": comparison,
    }
    atomic_json(output_path, document)
    return document


def validate_neo4j_store_audit(
    value: object,
    *,
    stage: str,
    request_ref: dict[str, Any],
    store_root: Path,
    store_manifest_ref: dict[str, Any],
    stop_receipt_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one small pre/post audit receipt without rereading the store."""

    keys = (
        "schema_version",
        "stage",
        "request",
        "store_manifest",
        "store_root",
        "hash_method",
        "known_mutable_patterns",
        "cache_eviction",
        "known_mutable_changes_tolerated",
        "offline_gate",
        "stop_receipt",
        "audit",
    )
    document = require_keys(value, required=keys, allowed=keys, context="Neo4j store audit")
    if document["schema_version"] != "p10-neo4j-runtime-store-audit-v1" or document["stage"] != stage:
        raise ContractError("Neo4j store audit schema/stage mismatch")
    if Path(str(document["store_root"])).resolve() != store_root.resolve():
        raise ContractError("Neo4j store audit root mismatch")
    if document["hash_method"] != "sha256-tree-v1(relative-path,size,file-sha256)":
        raise ContractError("Neo4j store audit hash method mismatch")
    from adapters.neo4j_store_contract import (
        KNOWN_MUTABLE_PATTERNS,
        is_known_mutable,
        validate_owner_only_offline_gate,
    )

    if document["known_mutable_patterns"] != list(KNOWN_MUTABLE_PATTERNS):
        raise ContractError("Neo4j store audit mutable patterns mismatch")
    if document["cache_eviction"] != "posix-fadvise-dontneed-v1":
        raise ContractError("Neo4j store audit did not evict pages read by hashing")
    expected_mutable_tolerance = stage == "post"
    if document["known_mutable_changes_tolerated"] is not expected_mutable_tolerance:
        raise ContractError("Neo4j runtime audit used the wrong mutable-file stability contract")
    _neo4j_same_ref(document["request"], request_ref, "Neo4j store audit request")
    _neo4j_same_ref(document["store_manifest"], store_manifest_ref, "Neo4j store audit manifest")
    if stage in {"pre", "post-stop"}:
        validate_owner_only_offline_gate(
            document["offline_gate"],
            store_root,
            "Neo4j store audit offline gate",
        )
    elif document["offline_gate"] is not None:
        raise ContractError("online Neo4j store audit unexpectedly carries an offline gate")
    if stage == "post-stop":
        if stop_receipt_ref is None:
            raise ContractError("Neo4j post-stop audit validator requires the stop receipt reference")
        _neo4j_same_ref(document["stop_receipt"], stop_receipt_ref, "Neo4j post-stop receipt")
        verified_stop_ref = _neo4j_artifact_ref(
            document["stop_receipt"],
            "Neo4j post-stop receipt",
            verify_file=True,
        )
        from adapters.launch_neo4j_runtime import validate_stop_receipt

        validate_stop_receipt(
            read_json(Path(verified_stop_ref["path"]), "Neo4j post-stop receipt"),
            verify_artifacts=True,
        )
    elif document["stop_receipt"] is not None or stop_receipt_ref is not None:
        raise ContractError("non-post-stop Neo4j audit unexpectedly binds a stop receipt")
    audit = require_keys(
        document["audit"],
        required=("tree", "mutable_deltas"),
        allowed=("tree", "mutable_deltas"),
        context="Neo4j store audit result",
    )
    tree_keys = (
        "store_sha256",
        "file_count",
        "total_bytes",
        "immutable_store_sha256",
        "immutable_file_count",
        "immutable_total_bytes",
    )
    tree = require_keys(audit["tree"], required=tree_keys, allowed=tree_keys, context="Neo4j store audit tree")
    normalize_sha(tree["store_sha256"], "Neo4j store audit tree SHA", required=True)
    normalize_sha(tree["immutable_store_sha256"], "Neo4j store audit immutable SHA", required=True)
    for key in ("file_count", "total_bytes", "immutable_file_count", "immutable_total_bytes"):
        integer(tree[key], f"Neo4j store audit tree.{key}", 0)
    deltas = audit["mutable_deltas"]
    if not isinstance(deltas, list):
        raise ContractError("Neo4j store audit mutable_deltas must be an array")
    seen: set[str] = set()
    for index, delta in enumerate(deltas):
        item = require_keys(
            delta,
            required=("path", "before", "after"),
            allowed=("path", "before", "after"),
            context=f"Neo4j store audit mutable_deltas[{index}]",
        )
        relative = nonempty_string(item["path"], f"Neo4j store audit mutable_deltas[{index}].path")
        if relative in seen or not is_known_mutable(relative):
            raise ContractError("Neo4j store audit contains an invalid mutable delta")
        seen.add(relative)
    if stage == "pre" and deltas:
        raise ContractError("Neo4j pre-launch audit contains mutable deltas")
    return document


def validate_neo4j_store_audit_pair(
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    before_tree = before["audit"]["tree"]
    after_tree = after["audit"]["tree"]
    for key in ("immutable_store_sha256", "immutable_file_count", "immutable_total_bytes"):
        if before_tree[key] != after_tree[key]:
            raise ContractError(f"Neo4j immutable store {key} changed across P31 execution")


def read_p31_summary(run_dir: Path, *, performance_eligible: bool) -> dict[str, Any]:
    done = run_dir / "DONE"
    failed = run_dir / "FAILED"
    manifest_path = run_dir / "run-manifest.json"
    validation_path = run_dir / "validation.json"
    if not done.is_file() or failed.exists():
        raise ContractError(f"P31 did not publish an exclusive DONE marker: {run_dir}")
    done_value = read_json(done, "P31 DONE")
    if done_value.get("state") != "PASS":
        raise ContractError("P31 DONE state is not PASS")
    manifest = read_json(manifest_path, "P31 run manifest")
    manifest_sha = sha256_file(manifest_path)
    validation_sha: str | None = None
    if manifest.get("state") != "PASS":
        raise ContractError("P31 run manifest state is not PASS")
    if manifest.get("performance_eligible_declared") is not performance_eligible:
        raise ContractError("P31 performance_eligible_declared differs from orchestrator mode")
    summary = manifest.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("resources"), dict) or not isinstance(summary.get("disk"), dict):
        raise ContractError("P31 run manifest lacks resource/disk summary objects")
    required_resources = (
        "peak_rss_bytes",
        "peak_pss_bytes",
        "process_user_cpu_s",
        "process_sys_cpu_s",
        "process_read_bytes",
        "process_write_bytes",
    )
    required_disk = ("peak_store_total_bytes", "peak_temp_bytes")
    for section, keys in (("resources", required_resources), ("disk", required_disk)):
        for key in keys:
            value = summary[section].get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ContractError(f"P31 summary.{section}.{key} is missing or not a finite non-negative number")
    collector_result = manifest.get("collector_result")
    if performance_eligible:
        if not validation_path.is_file():
            raise ContractError(f"P31 validation.json is missing: {validation_path}")
        done_value = require_keys(
            done_value,
            required=("state", "validated_at_utc", "manifest_sha256", "validation_sha256"),
            allowed=("state", "validated_at_utc", "manifest_sha256", "validation_sha256"),
            context="P31 DONE",
        )
        validation_sha = sha256_file(validation_path)
        if normalize_sha(done_value["manifest_sha256"], "P31 DONE manifest SHA", required=True) != manifest_sha:
            raise ContractError("P31 DONE manifest_sha256 differs from run-manifest.json")
        if normalize_sha(done_value["validation_sha256"], "P31 DONE validation SHA", required=True) != validation_sha:
            raise ContractError("P31 DONE validation_sha256 differs from validation.json")
        validation = require_keys(
            read_json(validation_path, "P31 validation"),
            required=(
                "schema_version",
                "state",
                "validated_at_utc",
                "errors",
                "warnings",
                "resource_summary",
                "disk_summary",
                "iostat_samples",
            ),
            allowed=(
                "schema_version",
                "state",
                "validated_at_utc",
                "errors",
                "warnings",
                "resource_summary",
                "disk_summary",
                "iostat_samples",
            ),
            context="P31 validation",
        )
        if (
            validation["schema_version"] != "cidr-run-manifest-v1"
            or validation["state"] != "PASS"
            or validation["errors"] != []
        ):
            raise ContractError("P31 validation.json is not a clean PASS")
        if done_value["validated_at_utc"] != validation["validated_at_utc"]:
            raise ContractError("P31 DONE timestamp differs from validation.json")
        if manifest.get("validation") != validation:
            raise ContractError("P31 embedded validation differs from validation.json")
        if (
            summary["resources"] != validation["resource_summary"]
            or summary["disk"] != validation["disk_summary"]
        ):
            raise ContractError("P31 summary differs from validation.json")
        collector_result = require_keys(
            collector_result,
            required=(
                "container_identity_schema_version",
                "ready",
                "containers_seen",
                "container_identity_history",
                "container_identity_unique_set",
                "status_artifact",
                "ready_artifact",
            ),
            allowed=(
                "container_identity_schema_version",
                "ready",
                "containers_seen",
                "container_identity_history",
                "container_identity_unique_set",
                "process_identity_schema_version",
                "process_identity_unique_set",
                "status_artifact",
                "ready_artifact",
            ),
            context="P31 collector result",
        )
        if collector_result["container_identity_schema_version"] != "cidr-container-identity-v2":
            raise ContractError("P31 collector result has wrong container identity schema")

        def collector_artifact(value: object, filename: str) -> tuple[dict[str, Any], dict[str, Any]]:
            item = require_keys(
                value,
                required=("path", "size_bytes", "sha256"),
                allowed=("path", "size_bytes", "sha256"),
                context=f"P31 {filename} artifact",
            )
            path = Path(nonempty_string(item["path"], f"P31 {filename} path")).resolve()
            expected_path = (run_dir / filename).resolve()
            if path != expected_path or not path.is_file():
                raise ContractError(f"P31 {filename} artifact path is invalid")
            size = integer(item["size_bytes"], f"P31 {filename} size", 1)
            sha = normalize_sha(item["sha256"], f"P31 {filename} SHA", required=True)
            if path.stat().st_size != size or sha256_file(path) != sha:
                raise ContractError(f"P31 {filename} artifact size/SHA mismatch")
            return {"path": str(path), "size_bytes": size, "sha256": sha}, read_json(path, filename)

        status_ref, status = collector_artifact(collector_result["status_artifact"], "collector-status.json")
        ready_ref, ready = collector_artifact(collector_result["ready_artifact"], "collector-ready.json")
        if (
            status.get("schema_version") != "cidr-resource-v1"
            or status.get("state") != "DONE"
            or status.get("errors") != []
            or status.get("container_identity_schema_version") != "cidr-container-identity-v2"
        ):
            raise ContractError("P31 collector-status.json is not a clean identity-aware DONE")
        if ready.get("schema_version") != "cidr-container-identity-v2" or ready.get("state") != "READY":
            raise ContractError("P31 collector-ready.json is not READY")
        expected_result_fields = {
            "ready": ready,
            "containers_seen": status.get("containers_seen"),
            "container_identity_history": status.get("container_identity_history"),
            "container_identity_unique_set": status.get("container_identity_unique_set"),
        }
        process_fields = ("process_identity_schema_version", "process_identity_unique_set")
        if any(field in collector_result for field in process_fields):
            if not all(field in collector_result for field in process_fields):
                raise ContractError("P31 collector result has an incomplete process identity binding")
            if collector_result["process_identity_schema_version"] != "cidr-process-identity-v1":
                raise ContractError("P31 collector result has wrong process identity schema")
            expected_result_fields.update(
                {
                    "process_identity_schema_version": status.get("process_identity_schema_version"),
                    "process_identity_unique_set": status.get("process_identity_unique_set"),
                }
            )
        for key, expected in expected_result_fields.items():
            if collector_result[key] != expected:
                raise ContractError(f"P31 collector result {key} differs from its signed artifact")
        if status.get("collector_ready") != ready:
            raise ContractError("P31 status readiness differs from collector-ready.json")
        configured = manifest.get("collector")
        configured_containers = configured.get("containers") if isinstance(configured, dict) else None
        for field in ("containers_seen", "container_identity_history", "container_identity_unique_set"):
            value = collector_result[field]
            if not isinstance(value, dict) or list(value) != configured_containers:
                raise ContractError(f"P31 collector result {field} keys differ from configured containers")
        collector_result = {
            **collector_result,
            "status_artifact": status_ref,
            "ready_artifact": ready_ref,
        }
    return {
        "run_dir": str(run_dir.resolve()),
        "manifest_sha256": manifest_sha,
        "validation_sha256": validation_sha,
        "done_sha256": sha256_file(done),
        "resources": summary["resources"],
        "disk": summary["disk"],
        "repo": manifest.get("repo"),
        "host": manifest.get("host"),
        "harness": manifest.get("harness"),
        "collector": manifest.get("collector"),
        "collector_result": collector_result,
        "inputs": manifest.get("inputs"),
        "disk_roots": manifest.get("disk_roots"),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _artifact_ref(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file() or path.is_symlink():
        raise ContractError(f"artifact is not one regular file: {path}")
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def _exclusive_json(path: Path, value: object) -> None:
    exclusive_json(path, value, "LiveGraph post-P31 seal")


def _livegraph_proc_identity(pid: int) -> tuple[int, int]:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return (-1, -1)
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"cannot inspect LiveGraph worker process identity: {exc}") from exc
    close = text.rfind(")")
    fields = text[close + 2 :].split() if close >= 0 else []
    if len(fields) <= 19:
        raise ContractError("LiveGraph worker /proc stat is malformed during external seal")
    try:
        return int(fields[2]), int(fields[19])
    except ValueError as exc:
        raise ContractError("LiveGraph worker /proc identity is malformed during external seal") from exc


def _livegraph_process_group_members(pgrp: int) -> list[int]:
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as exc:
        raise ContractError(f"cannot enumerate /proc for LiveGraph external seal: {exc}") from exc
    members: list[int] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text(encoding="utf-8")
            close = text.rfind(")")
            fields = text[close + 2 :].split() if close >= 0 else []
            if len(fields) <= 2:
                raise ValueError("malformed /proc stat")
            if int(fields[2]) == pgrp:
                members.append(int(entry.name))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            raise ContractError(
                f"cannot authenticate LiveGraph process-group membership for PID {entry.name}: {exc}"
            ) from exc
    return sorted(members)


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _livegraph_p31_terminal_refs(p31: dict[str, Any]) -> dict[str, dict[str, Any]]:
    run_dir = Path(nonempty_string(p31.get("run_dir"), "LiveGraph P31 run_dir")).resolve()
    if (run_dir / "FAILED").exists():
        raise ContractError("LiveGraph P31 FAILED coexists with post-P31 sealing")
    expected = {
        "manifest": (run_dir / "run-manifest.json", p31.get("manifest_sha256")),
        "validation": (run_dir / "validation.json", p31.get("validation_sha256")),
        "done": (run_dir / "DONE", p31.get("done_sha256")),
    }
    result: dict[str, dict[str, Any]] = {}
    for label, (path, digest) in expected.items():
        current = _artifact_ref(path)
        if current["sha256"] != normalize_sha(digest, f"LiveGraph P31 {label} SHA", required=True):
            raise ContractError(f"LiveGraph P31 {label} changed before external store seal")
        result[label] = current
    collector = p31.get("collector_result")
    status = collector.get("status_artifact") if isinstance(collector, dict) else None
    if not isinstance(status, dict):
        raise ContractError("LiveGraph P31 lacks collector-status terminal artifact")
    status_ref = _livegraph_artifact(status, "LiveGraph P31 collector status")
    if Path(status_ref["path"]).resolve() != (run_dir / "collector-status.json").resolve():
        raise ContractError("LiveGraph P31 collector status path drift")
    result["collector_status"] = status_ref
    return result


def validate_livegraph_post_p31_store_seal(
    provenance: object,
    p31: dict[str, Any],
    seal: object,
) -> dict[str, Any]:
    """Validate the post-P31 seal without re-reading block/WAL payloads."""

    if not isinstance(provenance, dict) or provenance.get("schema_version") != "p10-livegraph-adapter-provenance-v2":
        raise ContractError("LiveGraph post-P31 seal requires formal v2 adapter provenance")
    seal_ref = _livegraph_artifact(seal, "LiveGraph post-P31 store seal")
    document = read_json(Path(seal_ref["path"]), "LiveGraph post-P31 store seal")
    if isinstance(seal, dict) and "content" in seal and seal["content"] != document:
        raise ContractError("LiveGraph post-P31 seal file/content mismatch")
    top_keys = (
        "schema_version", "state", "scope", "system_id", "suite_id", "run_id",
        "repeat_index", "sealed_at_utc", "adapter_provenance", "p31_terminal",
        "worker_terminal", "store",
    )
    value = require_keys(document, required=top_keys, allowed=top_keys, context="LiveGraph post-P31 seal")
    if (
        value["schema_version"] != "p10-livegraph-post-p31-store-seal-v1"
        or value["state"] != "PASS"
        or value["scope"] != "block-wal-full-sha256-after-p31-v1"
        or value["system_id"] != "livegraph"
        or value["suite_id"] != provenance.get("suite_id")
        or value["run_id"] != provenance.get("run_id")
        or value["repeat_index"] != provenance.get("repeat_index")
    ):
        raise ContractError("LiveGraph post-P31 seal identity/classification drift")
    nonempty_string(value["sealed_at_utc"], "LiveGraph post-P31 seal timestamp")

    provenance_path = Path(str(provenance.get("worker_lifecycle", {}).get("exit", {}).get("path", ""))).resolve().parent / "adapter-provenance.json"
    expected_provenance_ref = _artifact_ref(provenance_path)
    if read_json(provenance_path, "LiveGraph adapter provenance") != provenance:
        raise ContractError("LiveGraph post-P31 seal provenance file/content drift")
    if value["adapter_provenance"] != expected_provenance_ref:
        raise ContractError("LiveGraph post-P31 seal provenance reference drift")
    expected_p31 = _livegraph_p31_terminal_refs(p31)
    if value["p31_terminal"] != {"run_dir": str(Path(p31["run_dir"]).resolve()), **expected_p31}:
        raise ContractError("LiveGraph post-P31 seal P31 terminal chain drift")

    lifecycle = provenance.get("worker_lifecycle")
    identity = lifecycle.get("identity") if isinstance(lifecycle, dict) else None
    if not isinstance(lifecycle, dict) or not isinstance(identity, dict):
        raise ContractError("LiveGraph post-P31 seal lacks worker lifecycle")
    identity_keys = (
        "schema_version", "state", "pid", "proc_start_ticks", "process_group_id",
        "started_at_utc", "ended_at_utc", "started_monotonic_ns",
        "ended_monotonic_ns", "returncode", "same_process_alive_after_wait",
        "process_group_members_after_wait",
    )
    identity = require_keys(
        identity,
        required=identity_keys,
        allowed=identity_keys,
        context="LiveGraph post-P31 worker exit identity",
    )
    worker = require_keys(
        value["worker_terminal"],
        required=("start", "exit", "pid", "proc_start_ticks", "process_group_id", "same_process_alive", "process_group_members"),
        allowed=("start", "exit", "pid", "proc_start_ticks", "process_group_id", "same_process_alive", "process_group_members"),
        context="LiveGraph post-P31 worker terminal",
    )
    lifecycle_refs = {
        label: _livegraph_artifact(lifecycle[label], f"LiveGraph worker {label}")
        for label in ("start", "exit")
    }
    start_receipt = read_json(
        Path(lifecycle_refs["start"]["path"]), "LiveGraph post-P31 worker start"
    )
    exit_receipt = read_json(
        Path(lifecycle_refs["exit"]["path"]), "LiveGraph post-P31 worker exit"
    )
    if (
        identity["schema_version"] != "p10-livegraph-worker-pid-v2"
        or identity["state"] != "EXITED"
        or identity["returncode"] != 0
        or identity["same_process_alive_after_wait"] is not False
        or identity["process_group_id"] != identity["pid"]
        or identity["process_group_members_after_wait"] != []
        or exit_receipt != identity
        or start_receipt.get("schema_version") != "p10-livegraph-worker-pid-v2"
        or start_receipt.get("state") != "RUNNING"
        or start_receipt.get("pid") != identity["pid"]
        or start_receipt.get("proc_start_ticks") != identity["proc_start_ticks"]
        or start_receipt.get("process_group_id") != identity["pid"]
    ):
        raise ContractError("LiveGraph post-P31 seal lacks one clean v2 worker lifecycle")
    expected_worker = {
        "start": lifecycle_refs["start"], "exit": lifecycle_refs["exit"],
        "pid": identity.get("pid"), "proc_start_ticks": identity.get("proc_start_ticks"),
        "process_group_id": identity.get("process_group_id"),
        "same_process_alive": False, "process_group_members": [],
    }
    if worker != expected_worker:
        raise ContractError("LiveGraph post-P31 seal worker terminal chain drift")
    pid = integer(worker["pid"], "LiveGraph sealed worker PID", 1)
    start_ticks = integer(worker["proc_start_ticks"], "LiveGraph sealed worker start ticks", 1)
    pgrp = integer(worker["process_group_id"], "LiveGraph sealed worker process group", 1)
    _current_pgrp, current_ticks = _livegraph_proc_identity(pid)
    if current_ticks == start_ticks or _livegraph_process_group_members(pgrp):
        raise ContractError("LiveGraph sealed worker/process group became live")

    store = provenance.get("store")
    sealed_store = require_keys(
        value["store"],
        required=("root", "hash_policy", "read_only_policy", "root_pre_hash_identity", "root_post_hash_identity", "final_entries", "block", "wal"),
        allowed=("root", "hash_policy", "read_only_policy", "root_pre_hash_identity", "root_post_hash_identity", "final_entries", "block", "wal"),
        context="LiveGraph post-P31 sealed store",
    )
    if not isinstance(store, dict):
        raise ContractError("LiveGraph provenance store is malformed")
    root = Path(str(store.get("root", ""))).resolve()
    if (
        Path(str(sealed_store["root"])).resolve() != root
        or sealed_store["hash_policy"] != "external-post-p31-full-sha256-fd-bracketed-v1"
        or sealed_store["read_only_policy"] != "root-0555-files-0444-v1"
        or sealed_store["final_entries"] != ["livegraph-block", "livegraph-wal"]
        or sealed_store["root_pre_hash_identity"] != store.get("root_identity_after_worker_exit")
        or sealed_store["root_post_hash_identity"] != store.get("root_identity_after_worker_exit")
    ):
        raise ContractError("LiveGraph post-P31 sealed store root/policy identity drift")
    _validate_livegraph_node_identity(
        sealed_store["root_post_hash_identity"], root, "LiveGraph sealed store root",
        kind="directory", expected_mode=0o555,
    )
    for role, basename in (("block", "livegraph-block"), ("wal", "livegraph-wal")):
        sealed_file = require_keys(
            sealed_store[role],
            required=("role", "path", "size_bytes", "sha256", "adapter_terminal_identity", "pre_hash_identity", "post_hash_identity"),
            allowed=("role", "path", "size_bytes", "sha256", "adapter_terminal_identity", "pre_hash_identity", "post_hash_identity"),
            context=f"LiveGraph post-P31 {role}",
        )
        path = Path(str(sealed_file["path"])).resolve()
        adapter_identity = store.get(role, {}).get("identity_after_worker_exit") if isinstance(store.get(role), dict) else None
        if not isinstance(adapter_identity, dict):
            raise ContractError(f"LiveGraph provenance lacks {role} terminal identity")
        if (
            sealed_file["role"] != role
            or path != root / basename
            or sealed_file["adapter_terminal_identity"] != adapter_identity
            or sealed_file["pre_hash_identity"] != adapter_identity
            or sealed_file["post_hash_identity"] != adapter_identity
            or sealed_file["size_bytes"] != adapter_identity.get("size_bytes")
        ):
            raise ContractError(f"LiveGraph post-P31 {role} identity closure drift")
        normalize_sha(sealed_file["sha256"], f"LiveGraph post-P31 {role} SHA", required=True)
        _validate_livegraph_node_identity(
            sealed_file["post_hash_identity"], path, f"LiveGraph sealed {role}",
            kind="regular-file", expected_mode=0o444,
        )
    return {**seal_ref, "content": document}


def publish_livegraph_post_p31_store_seal(
    provenance_path: Path,
    provenance: dict[str, Any],
    p31: dict[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    """Hash the frozen block/WAL once, only after P31 has published its terminal DONE."""

    if output_path.exists() or output_path.is_symlink():
        raise ContractError(f"refusing to overwrite LiveGraph post-P31 seal: {output_path}")
    if not provenance_path.is_file() or provenance_path.is_symlink():
        raise ContractError("LiveGraph adapter provenance is missing before post-P31 seal")
    provenance_ref = _artifact_ref(provenance_path)
    if read_json(provenance_path, "LiveGraph adapter provenance") != provenance:
        raise ContractError("LiveGraph adapter provenance path/content drift before external seal")
    p31_refs = _livegraph_p31_terminal_refs(p31)
    lifecycle = provenance.get("worker_lifecycle")
    identity = lifecycle.get("identity") if isinstance(lifecycle, dict) else None
    if not isinstance(lifecycle, dict) or not isinstance(identity, dict):
        raise ContractError("LiveGraph provenance lacks worker terminal identity")
    pid = integer(identity.get("pid"), "LiveGraph post-P31 worker PID", 1)
    start_ticks = integer(identity.get("proc_start_ticks"), "LiveGraph post-P31 worker start ticks", 1)
    pgrp = integer(identity.get("process_group_id"), "LiveGraph post-P31 worker process group", 1)
    _observed_pgrp, observed_ticks = _livegraph_proc_identity(pid)
    same_alive = observed_ticks == start_ticks
    group_members = _livegraph_process_group_members(pgrp)
    if same_alive or group_members or pgrp != pid:
        raise ContractError("LiveGraph worker/process group is still alive before external store seal")

    store = provenance.get("store")
    if not isinstance(store, dict) or provenance.get("schema_version") != "p10-livegraph-adapter-provenance-v2":
        raise ContractError("LiveGraph external store seal requires formal v2 provenance")
    root = Path(nonempty_string(store.get("root"), "LiveGraph formal store root")).resolve()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(root, directory_flags)
    file_fds: dict[str, int] = {}
    try:
        root_pre = _livegraph_node_identity_from_stat(os.fstat(directory_fd), kind="directory")
        if root_pre != store.get("root_identity_after_worker_exit") or root_pre["mode_bits"] != 0o555:
            raise ContractError("LiveGraph store root changed/remained writable before external seal")
        if sorted(os.listdir(directory_fd)) != ["livegraph-block", "livegraph-wal"]:
            raise ContractError("LiveGraph store entries changed before external seal")
        pre: dict[str, dict[str, Any]] = {}
        for role, basename in (("block", "livegraph-block"), ("wal", "livegraph-wal")):
            descriptor = os.open(basename, file_flags, dir_fd=directory_fd)
            file_fds[role] = descriptor
            current = _livegraph_node_identity_from_stat(os.fstat(descriptor), kind="regular-file")
            expected = store.get(role, {}).get("identity_after_worker_exit") if isinstance(store.get(role), dict) else None
            if current != expected or current["mode_bits"] != 0o444 or current["nlink"] != 1:
                raise ContractError(f"LiveGraph {role} changed/remained writable before external seal")
            pre[role] = current
        digests = {role: _sha256_fd(file_fds[role]) for role in ("block", "wal")}
        post = {
            role: _livegraph_node_identity_from_stat(os.fstat(file_fds[role]), kind="regular-file")
            for role in ("block", "wal")
        }
        root_post = _livegraph_node_identity_from_stat(os.fstat(directory_fd), kind="directory")
        if pre != post or root_pre != root_post or sorted(os.listdir(directory_fd)) != ["livegraph-block", "livegraph-wal"]:
            raise ContractError("LiveGraph store identity changed during external payload hashing")
    finally:
        for descriptor in file_fds.values():
            os.close(descriptor)
        os.close(directory_fd)

    document = {
        "schema_version": "p10-livegraph-post-p31-store-seal-v1",
        "state": "PASS",
        "scope": "block-wal-full-sha256-after-p31-v1",
        "system_id": "livegraph",
        "suite_id": provenance.get("suite_id"),
        "run_id": provenance.get("run_id"),
        "repeat_index": provenance.get("repeat_index"),
        "sealed_at_utc": _utc_now(),
        "adapter_provenance": provenance_ref,
        "p31_terminal": {"run_dir": str(Path(p31["run_dir"]).resolve()), **p31_refs},
        "worker_terminal": {
            "start": _livegraph_artifact(lifecycle["start"], "LiveGraph worker start"),
            "exit": _livegraph_artifact(lifecycle["exit"], "LiveGraph worker exit"),
            "pid": pid, "proc_start_ticks": start_ticks, "process_group_id": pgrp,
            "same_process_alive": False, "process_group_members": [],
        },
        "store": {
            "root": str(root),
            "hash_policy": "external-post-p31-full-sha256-fd-bracketed-v1",
            "read_only_policy": "root-0555-files-0444-v1",
            "root_pre_hash_identity": root_pre,
            "root_post_hash_identity": root_post,
            "final_entries": ["livegraph-block", "livegraph-wal"],
            **{
                role: {
                    "role": role,
                    "path": str((root / ("livegraph-block" if role == "block" else "livegraph-wal")).resolve()),
                    "size_bytes": pre[role]["size_bytes"],
                    "sha256": digests[role],
                    "adapter_terminal_identity": store[role]["identity_after_worker_exit"],
                    "pre_hash_identity": pre[role],
                    "post_hash_identity": post[role],
                }
                for role in ("block", "wal")
            },
        },
    }
    _exclusive_json(output_path, document)
    return validate_livegraph_post_p31_store_seal(
        provenance, p31, {**_artifact_ref(output_path), "content": document}
    )


def validate_adapter_p31_binding(
    provenance: object,
    p31: dict[str, Any],
    *,
    system_id: str,
) -> None:
    """Cross-bind an embedded adapter's internal lineage to enclosing P31."""

    if system_id not in {"seml0", "aster"}:
        raise ContractError(f"unsupported P31 provenance binding for {system_id!r}")
    display = "SemL0" if system_id == "seml0" else "Aster"

    if not isinstance(provenance, dict):
        raise ContractError(f"formal {display} result lacks parsed adapter provenance")
    expected_refs = {
        "binary": provenance.get("binary"),
        "truth": provenance.get("truth"),
        "p31_wrapper": provenance.get("p31_wrapper"),
    }
    if system_id == "aster":
        expected_refs["dataset"] = provenance.get("dataset")
    for label, ref in expected_refs.items():
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or not isinstance(ref.get("sha256"), str):
            raise ContractError(f"{display} provenance lacks {label} path/SHA binding")
    harness = p31.get("harness")
    inputs = p31.get("inputs")
    repo = p31.get("repo")
    disk_roots = p31.get("disk_roots")
    if not isinstance(harness, dict) or not isinstance(inputs, dict) or not isinstance(repo, dict) or not isinstance(disk_roots, list):
        raise ContractError(f"P31 manifest lacks lineage objects required by {display}")

    def same_ref(observed: object, expected: dict[str, Any], label: str) -> None:
        if not isinstance(observed, dict):
            raise ContractError(f"P31 lacks {label} artifact reference")
        if Path(str(observed.get("path", ""))).resolve() != Path(expected["path"]).resolve():
            raise ContractError(f"P31 {label} path differs from {display} provenance")
        if observed.get("sha256") != expected["sha256"]:
            raise ContractError(f"P31 {label} SHA-256 differs from {display} provenance")

    same_ref(harness.get("wrapper"), expected_refs["p31_wrapper"], "wrapper")
    same_ref(inputs.get("binary"), expected_refs["binary"], "binary")
    if system_id == "aster":
        same_ref(inputs.get("dataset"), expected_refs["dataset"], "dataset")
    same_ref(inputs.get("truth"), expected_refs["truth"], "truth")
    same_ref(inputs.get("query_or_trace"), expected_refs["truth"], "query_or_trace")
    provenance_repo = provenance.get("repo")
    if not isinstance(provenance_repo, dict) or repo.get("git_sha") != provenance_repo.get("head"):
        raise ContractError(f"P31 Git SHA differs from {display} provenance")
    if repo.get("dirty") is not False:
        raise ContractError(f"formal {display} P31 manifest reports a dirty repository")
    store = provenance.get("store")
    if not isinstance(store, dict) or not isinstance(store.get("path"), str):
        raise ContractError(f"{display} provenance lacks store path")
    store_path = Path(store["path"]).resolve()
    matching_roots = [
        root
        for root in disk_roots
        if isinstance(root, dict)
        and root.get("role") == "store"
        and Path(str(root.get("path", ""))).resolve() == store_path
    ]
    if len(matching_roots) != 1:
        raise ContractError(f"P31 does not monitor exactly the {display} provenance store")


def validate_seml0_p31_binding(provenance: object, p31: dict[str, Any]) -> None:
    """Backward-compatible SemL0-specific entry point."""

    validate_adapter_p31_binding(provenance, p31, system_id="seml0")


def validate_livegraph_p31_binding(provenance: object, p31: dict[str, Any]) -> None:
    """Cross-bind the native LiveGraph worker and every formal gate to P31."""

    if not isinstance(provenance, dict) or provenance.get("schema_version") != "p10-livegraph-adapter-provenance-v2":
        raise ContractError("formal LiveGraph result lacks parsed adapter provenance")
    inputs = p31.get("inputs")
    disk_roots = p31.get("disk_roots")
    collector = p31.get("collector")
    collector_result = p31.get("collector_result")
    repo = p31.get("repo")
    if not isinstance(inputs, dict) or not isinstance(disk_roots, list) or not isinstance(collector, dict) or not isinstance(collector_result, dict) or not isinstance(repo, dict):
        raise ContractError("P31 manifest lacks LiveGraph input/disk/process/repo lineage")
    if collector.get("containers") != [] or collector.get("extra_pids") != []:
        raise ContractError("formal LiveGraph P31 must cover only its wrapped native process tree")

    def same_ref(observed: object, expected: object, label: str) -> None:
        if not isinstance(observed, dict) or not isinstance(expected, dict):
            raise ContractError(f"P31 lacks LiveGraph {label} artifact reference")
        if Path(str(observed.get("path", ""))).resolve() != Path(str(expected.get("path", ""))).resolve():
            raise ContractError(f"P31 {label} path differs from LiveGraph provenance")
        if observed.get("sha256") != expected.get("sha256"):
            raise ContractError(f"P31 {label} SHA-256 differs from LiveGraph provenance")

    artifacts = provenance.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ContractError("LiveGraph provenance lacks artifact lineage")
    same_ref(inputs.get("binary"), artifacts.get("binary"), "binary")
    same_ref(inputs.get("dataset"), artifacts.get("dataset"), "dataset")
    same_ref(inputs.get("truth"), artifacts.get("truth"), "truth")
    same_ref(inputs.get("query_or_trace"), artifacts.get("truth"), "query_or_trace")
    dataset_input = inputs.get("dataset")
    if not isinstance(dataset_input, dict) or dataset_input.get("content_sha256_mode") != "declared-no-read-v1":
        raise ContractError("formal LiveGraph P31 re-entered content-hash mode for the dense dataset")

    build = provenance.get("formal_build")
    p02b = provenance.get("p02b_admission")
    seal = provenance.get("dataset_seal")
    if not isinstance(build, dict) or not isinstance(p02b, dict) or not isinstance(seal, dict):
        raise ContractError("LiveGraph provenance lacks formal build/P02B/dataset-seal lineage")
    admission = p02b.get("admission")
    if not isinstance(admission, dict):
        raise ContractError("LiveGraph provenance lacks parsed P02B admission")
    named_refs = {
        "livegraph_build_receipt": build.get("receipt"),
        "livegraph_build_marker": build.get("marker"),
        "livegraph_source_library": build.get("source_library"),
        "livegraph_runtime_library": build.get("liblivegraph"),
        "livegraph_p02b_result": p02b.get("result"),
        "livegraph_p02b_validator": p02b.get("validator"),
        "livegraph_p02b_pass_marker": {
            "path": admission.get("pass_marker"), "sha256": admission.get("pass_marker_sha256")
        },
        "livegraph_p02b_provenance": {
            "path": admission.get("provenance"), "sha256": admission.get("provenance_sha256")
        },
        "livegraph_dataset_seal": seal,
    }
    for label, expected in named_refs.items():
        same_ref(inputs.get(label), expected, label)
    same_ref(artifacts.get("runtime_library"), build.get("liblivegraph"), "runtime library/build")

    expected_roots = {
        ("store", Path(str(provenance.get("store", {}).get("root", ""))).resolve()),
        ("temp", Path(str(provenance.get("temp", {}).get("root", ""))).resolve()),
    }
    for root in expected_roots:
        matches = [
            item for item in disk_roots
            if isinstance(item, dict)
            and item.get("role") == root[0]
            and Path(str(item.get("path", ""))).resolve() == root[1]
        ]
        if len(matches) != 1:
            raise ContractError(f"P31 does not monitor the exact LiveGraph {root[0]} root")

    identities = collector_result.get("process_identity_unique_set")
    if collector_result.get("process_identity_schema_version") != "cidr-process-identity-v1" or not isinstance(identities, list):
        raise ContractError("P31 lacks LiveGraph PID/start-ticks evidence")
    lifecycle = provenance.get("worker_lifecycle")
    identity = lifecycle.get("identity") if isinstance(lifecycle, dict) else None
    if not isinstance(identity, dict):
        raise ContractError("LiveGraph provenance lacks worker PID/start-ticks identity")
    if (
        identity.get("process_group_id") != identity.get("pid")
        or identity.get("process_group_members_after_wait") != []
        or identity.get("same_process_alive_after_wait") is not False
    ):
        raise ContractError("LiveGraph provenance lacks a clean detached worker process-group exit")
    worker_matches = [
        item for item in identities
        if isinstance(item, dict)
        and item.get("pid") == identity.get("pid")
        and item.get("start_ticks") == identity.get("proc_start_ticks")
    ]
    if len(worker_matches) != 1:
        raise ContractError("P31 did not sample exactly the LiveGraph worker PID/start-ticks identity")
    if worker_matches[0].get("pgrp") != identity.get("pid"):
        raise ContractError("P31 LiveGraph worker was not the recorded isolated worker session leader")

    integration = build.get("integration")
    if not isinstance(integration, dict) or repo.get("git_sha") != integration.get("head") or repo.get("dirty") is not False:
        raise ContractError("P31 Git state differs from LiveGraph build integration identity")
    p31_host = p31.get("host")
    admission_host = admission.get("host")
    if (
        not isinstance(p31_host, dict) or not isinstance(admission_host, dict)
        or p31_host.get("hostname") != admission_host.get("hostname")
        or p31_host.get("fingerprint_sha256") != admission_host.get("fingerprint_sha256")
    ):
        raise ContractError("P31 host differs from LiveGraph P02B admission host")


def validate_neo4j_p31_binding(provenance: object, p31: dict[str, Any]) -> None:
    """Cross-bind one stable external Neo4j container and its store to P31."""

    if not isinstance(provenance, dict) or provenance.get("schema_version") != "p10-neo4j-adapter-provenance-v4":
        raise ContractError("formal Neo4j result lacks v4 adapter provenance")
    collector = p31.get("collector")
    inputs = p31.get("inputs")
    disk_roots = p31.get("disk_roots")
    repo = p31.get("repo")
    if not all(isinstance(value, expected) for value, expected in (
        (collector, dict),
        (inputs, dict),
        (disk_roots, list),
        (repo, dict),
    )):
        raise ContractError("P31 manifest lacks Neo4j collector/input/disk/repo lineage")

    lifecycle = provenance.get("container_lifecycle")
    before = lifecycle.get("before") if isinstance(lifecycle, dict) else None
    after = lifecycle.get("after") if isinstance(lifecycle, dict) else None
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or lifecycle.get("stable") is not True
        or before.get("name") != after.get("name")
    ):
        raise ContractError("Neo4j provenance lacks one stable container lifecycle")
    container_name = before["name"]
    stable_fields = ("name", "container_id", "pid", "started_at", "restart_count")
    if any(before.get(key) != after.get(key) for key in stable_fields):
        raise ContractError("Neo4j provenance container identity changed during P31 collection")
    if before.get("restart_count") != 0:
        raise ContractError("Neo4j provenance container restarted during P31 collection")
    if collector.get("containers") != [container_name] or collector.get("extra_pids") != []:
        raise ContractError("P31 container coverage differs from Neo4j provenance")
    collector_result = p31.get("collector_result")
    if not isinstance(collector_result, dict):
        raise ContractError("P31 lacks observed Neo4j container identity evidence")
    seen = collector_result.get("containers_seen")
    unique = collector_result.get("container_identity_unique_set")
    history = collector_result.get("container_identity_history")
    ready = collector_result.get("ready")
    if not all(isinstance(value, dict) for value in (seen, unique, history, ready)):
        raise ContractError("P31 observed Neo4j container identity evidence is malformed")
    expected_identity = {
        "container_id": before.get("container_id"),
        "pid": before.get("pid"),
        "started_at": before.get("started_at"),
        "restart_count": before.get("restart_count"),
    }
    launch = provenance.get("launch")
    launch_receipt = launch.get("receipt") if isinstance(launch, dict) else None
    launch_docker = launch_receipt.get("docker") if isinstance(launch_receipt, dict) else None
    launch_running = launch_docker.get("running") if isinstance(launch_docker, dict) else None
    launch_config = launch_running.get("config") if isinstance(launch_running, dict) else None
    launch_runtime = launch_running.get("runtime") if isinstance(launch_running, dict) else None
    launch_contract = launch_receipt.get("runtime_contract") if isinstance(launch_receipt, dict) else None
    if not all(isinstance(value, dict) for value in (launch_config, launch_runtime, launch_contract)):
        raise ContractError("Neo4j provenance lacks launch identity evidence")
    launch_identity = {
        "container_id": launch_config.get("container_id"),
        "pid": launch_runtime.get("pid"),
        "started_at": launch_runtime.get("started_at"),
        "restart_count": launch_runtime.get("restart_count"),
    }
    if (
        launch_config.get("container_name") != container_name
        or launch_identity != expected_identity
        or launch_contract.get("store_root") != before.get("data_mount")
        or launch_contract.get("logs_root") != before.get("logs_mount")
        or launch_contract.get("bolt_port") != before.get("bolt_port")
        or launch_contract.get("container_user") != before.get("container_user")
    ):
        raise ContractError("Neo4j launch identity differs from adapter lifecycle")
    if seen != {container_name: before.get("pid")}:
        raise ContractError("P31 containers_seen PID differs from Neo4j adapter lifecycle")
    declared_identities = unique.get(container_name)
    if not isinstance(declared_identities, list) or len(declared_identities) != 1:
        raise ContractError("P31 unique container identity differs from Neo4j adapter lifecycle")
    p31_identity = declared_identities[0]
    if not isinstance(p31_identity, dict) or set(p31_identity) != {
        "container_id", "pid", "process_start_ticks", "started_at", "restart_count"
    }:
        raise ContractError("P31 Neo4j identity lacks PID start ticks")
    if {key: p31_identity.get(key) for key in expected_identity} != expected_identity:
        raise ContractError("P31 unique container identity differs from Neo4j adapter lifecycle")
    integer(p31_identity.get("process_start_ticks"), "P31 Neo4j process start ticks", 1)
    ready_containers = ready.get("containers")
    if ready_containers != {container_name: p31_identity}:
        raise ContractError("P31 READY identity differs from Neo4j adapter lifecycle")
    observed_history = history.get(container_name)
    if not isinstance(observed_history, list) or not observed_history:
        raise ContractError("P31 lacks Neo4j container identity history")
    for item in observed_history:
        if not isinstance(item, dict):
            raise ContractError("P31 Neo4j container identity history is malformed")
        if {key: item.get(key) for key in expected_identity} != expected_identity:
            raise ContractError("P31 Neo4j history identity differs from adapter lifecycle")
        if item.get("process_start_ticks") != p31_identity["process_start_ticks"]:
            raise ContractError("P31 Neo4j process identity changed during collection")
        integer(item.get("before_sample_index"), "P31 Neo4j identity sample index", 0)
        nonempty_string(item.get("observed_at_utc"), "P31 Neo4j identity observed_at_utc")

    def same_ref(observed: object, expected: object, label: str) -> None:
        if not isinstance(observed, dict) or not isinstance(expected, dict):
            raise ContractError(f"P31 lacks Neo4j {label} artifact reference")
        if Path(str(observed.get("path", ""))).resolve() != Path(str(expected.get("path", ""))).resolve():
            raise ContractError(f"P31 {label} path differs from Neo4j provenance")
        if observed.get("sha256") != expected.get("sha256"):
            raise ContractError(f"P31 {label} SHA-256 differs from Neo4j provenance")

    same_ref(inputs.get("binary"), provenance.get("python_binary"), "binary")
    same_ref(inputs.get("dataset"), provenance.get("dataset_input"), "dataset")
    same_ref(inputs.get("truth"), provenance.get("truth"), "truth")
    same_ref(inputs.get("query_or_trace"), provenance.get("truth"), "query_or_trace")

    store = provenance.get("store")
    lineage = store.get("lineage") if isinstance(store, dict) else None
    store_root = lineage.get("store_root") if isinstance(lineage, dict) else None
    if not isinstance(store_root, str):
        raise ContractError("Neo4j provenance lacks the runtime store root")
    matching = [
        root
        for root in disk_roots
        if isinstance(root, dict)
        and root.get("role") == "store"
        and Path(str(root.get("path", ""))).resolve() == Path(store_root).resolve()
    ]
    if len(matching) != 1:
        raise ContractError("P31 does not monitor exactly the Neo4j runtime store")
    logs_root = before.get("logs_mount")
    matching_logs = [
        root
        for root in disk_roots
        if isinstance(root, dict)
        and root.get("role") == "temp"
        and Path(str(root.get("path", ""))).resolve() == Path(str(logs_root)).resolve()
    ]
    if len(matching_logs) != 1:
        raise ContractError("P31 does not monitor exactly the Neo4j /logs root")

    provenance_repo = provenance.get("repo")
    if (
        not isinstance(provenance_repo, dict)
        or repo.get("git_sha") != provenance_repo.get("head")
        or repo.get("dirty") is not False
    ):
        raise ContractError("P31 Git state differs from Neo4j provenance")
    runtime = provenance.get("runtime_contract")
    runtime_host = runtime.get("host") if isinstance(runtime, dict) else None
    p31_host = p31.get("host")
    if (
        not isinstance(runtime_host, dict)
        or not isinstance(p31_host, dict)
        or runtime_host.get("fingerprint_sha256") != p31_host.get("fingerprint_sha256")
        or runtime_host.get("mem_total_bytes") != p31_host.get("mem_total_bytes")
    ):
        raise ContractError("P31 host/memory facts differ from Neo4j runtime provenance")


def validate_nebulagraph_p31_binding(provenance: object, p31: dict[str, Any]) -> None:
    """Cross-bind formal NebulaGraph client/server lineage to P31 coverage."""

    if not isinstance(provenance, dict):
        raise ContractError("formal NebulaGraph result lacks parsed adapter provenance")
    collector = p31.get("collector")
    inputs = p31.get("inputs")
    disk_roots = p31.get("disk_roots")
    if not isinstance(collector, dict) or not isinstance(inputs, dict) or not isinstance(disk_roots, list):
        raise ContractError("P31 manifest lacks NebulaGraph collector/input/disk lineage")
    containers = provenance.get("container_names")
    if (
        not isinstance(containers, list)
        or collector.get("containers") != containers
        or collector.get("extra_pids") != []
    ):
        raise ContractError("P31 container coverage differs from NebulaGraph provenance")
    lifecycle = provenance.get("cluster_lifecycle")
    preflight = lifecycle.get("preflight") if isinstance(lifecycle, dict) else None
    start = lifecycle.get("start") if isinstance(lifecycle, dict) else None
    preflight_receipt = preflight.get("receipt") if isinstance(preflight, dict) else None
    start_receipt = start.get("receipt") if isinstance(start, dict) else None
    if not isinstance(preflight_receipt, dict) or not isinstance(start_receipt, dict):
        raise ContractError("NebulaGraph P31 binding lacks launch receipts")
    spec = preflight_receipt.get("spec")
    launched = start_receipt.get("containers")
    if not isinstance(spec, dict) or not isinstance(launched, dict):
        raise ContractError("NebulaGraph launch receipts lack spec/container identity")
    roles = ("graphd", "metad", "storaged")
    if containers != [launched.get(role, {}).get("name") for role in roles]:
        raise ContractError("NebulaGraph launch names differ from P31 container order")
    runtime = provenance.get("container_runtime")
    if runtime != [launched.get(role) for role in roles]:
        raise ContractError("NebulaGraph launch IDs/PIDs differ from adapter runtime")
    collector_result = p31.get("collector_result")
    if not isinstance(collector_result, dict):
        raise ContractError("P31 lacks observed NebulaGraph container identity evidence")
    seen = collector_result.get("containers_seen")
    unique = collector_result.get("container_identity_unique_set")
    history = collector_result.get("container_identity_history")
    ready = collector_result.get("ready")
    if not all(isinstance(value, dict) for value in (seen, unique, history, ready)):
        raise ContractError("P31 NebulaGraph container identity evidence is malformed")
    expected_seen: dict[str, int] = {}
    expected_unique: dict[str, list[dict[str, Any]]] = {}
    for role in roles:
        snapshot = launched[role]
        name = snapshot.get("name")
        identity = {
            "container_id": snapshot.get("container_id"),
            "pid": snapshot.get("pid"),
            "process_start_ticks": snapshot.get("process_start_ticks"),
            "started_at": snapshot.get("started_at_utc"),
            "restart_count": snapshot.get("restart_count"),
        }
        if (
            not isinstance(name, str)
            or not isinstance(identity["container_id"], str)
            or len(identity["container_id"]) != 64
            or not isinstance(identity["pid"], int)
            or identity["pid"] <= 0
            or not isinstance(identity["process_start_ticks"], int)
            or isinstance(identity["process_start_ticks"], bool)
            or identity["process_start_ticks"] <= 0
            or not isinstance(identity["started_at"], str)
            or identity["restart_count"] != 0
        ):
            raise ContractError(f"NebulaGraph launch identity is malformed: {role}")
        expected_seen[name] = identity["pid"]
        expected_unique[name] = [identity]
        observed_history = history.get(name)
        if not isinstance(observed_history, list) or not observed_history:
            raise ContractError(f"P31 lacks NebulaGraph identity history: {role}")
        for item in observed_history:
            if not isinstance(item, dict) or {
                key: item.get(key) for key in identity
            } != identity:
                raise ContractError(f"P31 NebulaGraph identity drift: {role}")
            integer(item.get("before_sample_index"), "P31 NebulaGraph sample index", 0)
            nonempty_string(
                item.get("observed_at_utc"), "P31 NebulaGraph observed_at_utc"
            )
    if seen != expected_seen or unique != expected_unique:
        raise ContractError("P31 NebulaGraph observed IDs/PIDs differ from start receipt")
    if ready.get("containers") != {
        name: values[0] for name, values in expected_unique.items()
    }:
        raise ContractError("P31 READY NebulaGraph identity differs from start receipt")

    def same_ref(observed: object, expected: object, label: str) -> None:
        if not isinstance(observed, dict) or not isinstance(expected, dict):
            raise ContractError(f"P31 lacks NebulaGraph {label} artifact reference")
        if Path(str(observed.get("path", ""))).resolve() != Path(str(expected.get("path", ""))).resolve():
            raise ContractError(f"P31 {label} path differs from NebulaGraph provenance")
        if observed.get("sha256") != expected.get("sha256"):
            raise ContractError(f"P31 {label} SHA-256 differs from NebulaGraph provenance")

    same_ref(inputs.get("binary"), provenance.get("binary"), "binary")
    same_ref(inputs.get("dataset"), provenance.get("dataset"), "dataset")
    same_ref(inputs.get("truth"), provenance.get("truth"), "truth")
    same_ref(inputs.get("query_or_trace"), provenance.get("truth"), "query_or_trace")
    store = provenance.get("store")
    if not isinstance(store, dict) or not isinstance(store.get("path"), str):
        raise ContractError("NebulaGraph provenance lacks store path")
    matching = [
        root
        for root in disk_roots
        if isinstance(root, dict)
        and root.get("role") == "store"
        and Path(str(root.get("path", ""))).resolve() == Path(store["path"]).resolve()
    ]
    if len(matching) != 1:
        raise ContractError("P31 does not monitor exactly the NebulaGraph provenance store")
    logs_root = preflight_receipt.get("spec", {}).get("logs_root")
    matching_logs = [
        root
        for root in disk_roots
        if isinstance(root, dict)
        and root.get("role") == "temp"
        and Path(str(root.get("path", ""))).resolve() == Path(str(logs_root)).resolve()
    ]
    if len(matching_logs) != 1:
        raise ContractError("P31 does not monitor exactly the NebulaGraph role logs root")
    store_root = Path(store["path"]).resolve()
    spec_roles = spec.get("roles")
    if not isinstance(spec_roles, dict):
        raise ContractError("NebulaGraph launch spec lacks role mounts")
    for role in roles:
        role_spec = spec_roles.get(role)
        mounts = role_spec.get("mounts") if isinstance(role_spec, dict) else None
        if not isinstance(mounts, list) or len(mounts) != 2:
            raise ContractError(f"NebulaGraph launch spec lacks exact mounts: {role}")
        data_source = Path(str(mounts[0].get("source", ""))).resolve()
        log_source = Path(str(mounts[1].get("source", ""))).resolve()
        if data_source != store_root and store_root not in data_source.parents:
            raise ContractError(f"NebulaGraph {role} data mount escapes P31 store root")
        if log_source != Path(str(logs_root)).resolve() and Path(str(logs_root)).resolve() not in log_source.parents:
            raise ContractError(f"NebulaGraph {role} log mount escapes P31 temp root")
