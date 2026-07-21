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
from pathlib import Path
from typing import Any, Iterable


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


def _validate_protocol(value: object) -> dict[str, Any]:
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


def load_suite_manifest(
    manifest_path: Path,
    *,
    repo_root: Path,
    run_root: Path,
    mode: str,
) -> tuple[dict[str, Any], list[dict[str, int]]]:
    if mode not in {"fixture", "formal"}:
        raise ContractError(f"unsupported mode: {mode}")
    formal = mode == "formal"
    manifest_path = manifest_path.resolve()
    manifest_dir = manifest_path.parent
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
    protocol = _validate_protocol(manifest["protocol"])
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
        system = require_keys(raw_system, required=system_keys, allowed=system_keys, context=context)
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
    elif system["id"] == "seml0" and not system.get("fixture_only", False):
        raise ContractError("formal SemL0 adapter omitted adapter-provenance.json")
    elif system["id"] == "aster" and not system.get("fixture_only", False):
        raise ContractError("formal Aster adapter omitted adapter-provenance.json")
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


def read_p31_summary(run_dir: Path, *, performance_eligible: bool) -> dict[str, Any]:
    done = run_dir / "DONE"
    failed = run_dir / "FAILED"
    manifest_path = run_dir / "run-manifest.json"
    if not done.is_file() or failed.exists():
        raise ContractError(f"P31 did not publish an exclusive DONE marker: {run_dir}")
    done_value = read_json(done, "P31 DONE")
    if done_value.get("state") != "PASS":
        raise ContractError("P31 DONE state is not PASS")
    manifest = read_json(manifest_path, "P31 run manifest")
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
    return {
        "run_dir": str(run_dir.resolve()),
        "manifest_sha256": sha256_file(manifest_path),
        "done_sha256": sha256_file(done),
        "resources": summary["resources"],
        "disk": summary["disk"],
        "repo": manifest.get("repo"),
        "harness": manifest.get("harness"),
        "inputs": manifest.get("inputs"),
        "disk_roots": manifest.get("disk_roots"),
    }


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
