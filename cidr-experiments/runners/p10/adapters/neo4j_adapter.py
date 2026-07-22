#!/usr/bin/env python3
"""Fail-closed Neo4j Community Bolt adapter for the CIDR P10 contract.

The database service is deliberately external and pre-started.  The P10/P31
orchestrator owns resource observation, while this process binds the exact
container/image, Python driver, dataset/truth/store lineage and (for formal
runs) the canonical P02B admission before issuing any Cypher query.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import posixpath
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import neo4j
from neo4j import GraphDatabase, Query

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))

from p10_contract import (  # noqa: E402
    CLOCK_NAME,
    CONTRACT_VERSION,
    EXTERNAL_PROCESS_LIFETIME,
    INTERFACE_SCOPE,
    OBSERVATION_COLUMNS,
    REQUEST_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    TRUTH_DIGEST_ALGORITHM,
    ContractError,
    atomic_json,
    boolean,
    integer,
    nonempty_string,
    phase_digest,
    read_json,
    read_truth,
    require_keys,
    sha256_file,
    validate_adapter_outputs,
    validate_neo4j_store_audit,
)

from adapters.neo4j_store_contract import (  # noqa: E402
    CANONICAL_SENTINELS,
    KNOWN_MUTABLE_PATTERNS,
    TREE_HASH_METHOD,
    canonical_sentinel_records,
    validate_controlled_import_receipt,
    validate_owner_only_offline_gate,
    validate_recorded_tree,
)
from adapters.launch_neo4j_runtime import validate_launch_receipt  # noqa: E402

P31_DIR = P10_DIR.parent / "p31"
sys.path.insert(0, str(P31_DIR))
from run_manifest import host_facts as p31_host_facts  # noqa: E402

PROVENANCE_SCHEMA_VERSION = "p10-neo4j-adapter-provenance-v4"
STORE_MANIFEST_SCHEMA_VERSION = "p10-neo4j-store-manifest-v3"
RELATIONSHIP_MODEL = "dense-edge-type-as-outgoing-relationship-type-v1"
SNAPSHOT_PHASE = "offline-prestart-v1"
DATABASE_NAME = "neo4j"
INDEX_NAME = "v_id"
NODE_LABEL = "V"
ID_PROPERTY = "id"
EXPECTED_IMAGE_REF = "neo4j:5.26.24"
EXPECTED_DRIVER_VERSION = "5.28.3"
EXPECTED_SERVER_AGENT = "Neo4j/5.26.24"
EXPECTED_DOCKER_LOG_CONFIG = {"type": "none", "config": {}}
EXPECTED_ENTRYPOINT = ["tini", "-g", "--", "/startup/docker-entrypoint.sh"]
EXPECTED_COMMAND = ["neo4j"]
MEMORY_SETTING_NAMES = {
    "heap_initial": "server.memory.heap.initial_size",
    "heap_max": "server.memory.heap.max_size",
    "pagecache": "server.memory.pagecache.size",
}
MEMORY_ENV_NAMES = {
    "heap_initial": "NEO4J_server_memory_heap_initial__size",
    "heap_max": "NEO4J_server_memory_heap_max__size",
    "pagecache": "NEO4J_server_memory_pagecache_size",
}
MASK = (1 << 64) - 1
SHA256_CHARS = frozenset("0123456789abcdef")


@dataclass
class Digest:
    count: int = 0
    sum_hash: int = 0
    xor_hash: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("fixture", "formal"), required=True)
    parser.add_argument("--uri", required=True)
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--readiness-timeout-s", type=int, default=180)
    parser.add_argument("--expected-image-ref", default=EXPECTED_IMAGE_REF)
    parser.add_argument("--expected-driver-version", required=True)
    parser.add_argument("--expected-driver-tree-sha256", required=True)
    parser.add_argument("--expected-server-agent", default=EXPECTED_SERVER_AGENT)
    parser.add_argument(
        "--expected-entrypoint-json",
        default=json.dumps(EXPECTED_ENTRYPOINT, separators=(",", ":")),
    )
    parser.add_argument(
        "--expected-command-json",
        default=json.dumps(EXPECTED_COMMAND, separators=(",", ":")),
    )
    parser.add_argument("--expected-heap-initial-size", required=True)
    parser.add_argument("--expected-heap-max-size", required=True)
    parser.add_argument("--expected-pagecache-size", required=True)
    parser.add_argument("--store-label", default="neo4j-runtime")
    parser.add_argument("--logs-root", required=True, type=Path)
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--dataset-manifest-sha256", required=True)
    parser.add_argument("--store-manifest", required=True, type=Path)
    parser.add_argument("--store-manifest-sha256", required=True)
    parser.add_argument("--store-preflight", type=Path)
    parser.add_argument("--store-preflight-sha256")
    parser.add_argument("--import-receipt", type=Path)
    parser.add_argument("--import-receipt-sha256")
    parser.add_argument("--launch-receipt", type=Path)
    parser.add_argument("--launch-receipt-sha256")
    parser.add_argument("--p02b-result", type=Path)
    parser.add_argument("--p02b-result-sha256")
    parser.add_argument("--p02b-validator", type=Path)
    parser.add_argument("--p02b-validator-sha256")
    parser.add_argument("--p02b-binary", type=Path)
    parser.add_argument("--p02b-binary-sha256")
    parser.add_argument("--p02b-max-age-seconds", type=int, default=21600)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def exact_sha(value: object, context: str) -> str:
    require(
        isinstance(value, str)
        and len(value) == 64
        and all(character in SHA256_CHARS for character in value),
        f"{context}: expected 64 lowercase hexadecimal characters",
    )
    return str(value)


def exact_string_vector(value: str, context: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{context}: expected a JSON string array") from exc
    require(
        isinstance(parsed, list)
        and parsed
        and all(isinstance(item, str) and item for item in parsed),
        f"{context}: expected a nonempty JSON string array",
    )
    return list(parsed)


def exact_image_digest(value: object, context: str) -> str:
    require(
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in SHA256_CHARS for character in value[7:]),
        f"{context}: expected sha256:<64 lowercase hexadecimal characters>",
    )
    return str(value)


def resolve_file(
    path: Path, expected_sha: object, context: str, *, executable: bool = False
) -> dict[str, Any]:
    path = path.resolve()
    require(path.is_file(), f"{context}: missing file: {path}")
    if executable:
        require(os.access(path, os.X_OK), f"{context}: file is not executable: {path}")
    expected = exact_sha(expected_sha, f"{context}.sha256")
    actual = sha256_file(path)
    require(actual == expected, f"{context}: SHA-256 mismatch for {path}")
    return {"path": str(path), "sha256": actual, "size_bytes": path.stat().st_size}


def artifact_ref(path: Path) -> dict[str, Any]:
    path = path.resolve()
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def same_path(value: object, expected: Path, context: str) -> None:
    require(isinstance(value, str), f"{context}: expected path string")
    require(Path(str(value)).resolve() == expected.resolve(), f"{context}: path mismatch")


def git_state(repo_root: Path) -> dict[str, Any]:
    try:
        head = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(repo_root), "status", "--porcelain=v1", "--untracked-files=normal"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(f"cannot audit Git repository: {exc}") from exc
    require(len(head) == 40, "Git HEAD is not a full commit SHA")
    return {
        "root": str(repo_root),
        "head": head,
        "clean": status == "",
        "status_sha256": hashlib.sha256(status.encode()).hexdigest(),
    }


def checked_request_file(value: object, context: str, *, executable: bool = False) -> dict[str, Any]:
    item = require_keys(
        value, required=("path", "sha256"), allowed=("path", "sha256"), context=context
    )
    return resolve_file(Path(nonempty_string(item["path"], f"{context}.path")), item["sha256"], context, executable=executable)


def validate_request(
    request_path: Path, mode: str, store_label: str
) -> tuple[dict[str, Any], list[dict[str, int]], Path, str, str]:
    request = require_keys(
        read_json(request_path, "Neo4j adapter request"),
        required=(
            "schema_version",
            "contract_version",
            "suite_id",
            "run_id",
            "execution_mode",
            "system_id",
            "group",
            "system_version",
            "interface_scope",
            "repeat_index",
            "process_lifetime",
            "binary",
            "dataset",
            "runtime_libraries",
            "store_roots",
            "truth",
            "timing",
            "external_service",
        ),
        allowed=(
            "schema_version",
            "contract_version",
            "suite_id",
            "run_id",
            "execution_mode",
            "system_id",
            "group",
            "system_version",
            "interface_scope",
            "repeat_index",
            "process_lifetime",
            "binary",
            "dataset",
            "runtime_libraries",
            "store_roots",
            "truth",
            "timing",
            "external_service",
        ),
        context="Neo4j adapter request",
    )
    exact_values = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "execution_mode": mode,
        "system_id": "neo4j",
        "group": "client-server",
        "interface_scope": INTERFACE_SCOPE,
        "process_lifetime": EXTERNAL_PROCESS_LIFETIME,
    }
    for key, expected in exact_values.items():
        require(request[key] == expected, f"request.{key}: {request[key]!r} != {expected!r}")
    nonempty_string(request["suite_id"], "request.suite_id")
    nonempty_string(request["run_id"], "request.run_id")
    nonempty_string(request["system_version"], "request.system_version")
    integer(request["repeat_index"], "request.repeat_index", 1)
    request["binary"] = checked_request_file(request["binary"], "request.binary", executable=True)

    dataset_obj = require_keys(
        request["dataset"],
        required=("path", "sha256"),
        allowed=("path", "sha256"),
        context="request.dataset",
    )
    dataset_path = Path(nonempty_string(dataset_obj["path"], "request.dataset.path")).resolve()
    require(dataset_path.exists(), f"request.dataset does not exist: {dataset_path}")
    dataset_sha = exact_sha(dataset_obj["sha256"], "request.dataset.sha256")
    if mode == "formal":
        require(
            dataset_path.is_dir(),
            "formal Neo4j request.dataset must be the sealed P02B dataset root directory",
        )
    elif dataset_path.is_file():
        require(sha256_file(dataset_path) == dataset_sha, "request.dataset SHA-256 mismatch")
    request["dataset"] = {"path": str(dataset_path), "sha256": dataset_sha}

    libraries = request["runtime_libraries"]
    require(isinstance(libraries, list), "request.runtime_libraries must be an array")
    request["runtime_libraries"] = [
        checked_request_file(item, f"request.runtime_libraries[{index}]")
        for index, item in enumerate(libraries)
    ]
    require(
        not request["runtime_libraries"],
        "Neo4j Python adapter binds its installed package tree, not native runtime_libraries",
    )

    truth_obj = require_keys(
        request["truth"],
        required=("path", "sha256", "query_count", "digest_algorithm"),
        allowed=("path", "sha256", "query_count", "digest_algorithm"),
        context="request.truth",
    )
    truth_ref = resolve_file(Path(str(truth_obj["path"])), truth_obj["sha256"], "request.truth")
    query_count = integer(truth_obj["query_count"], "request.truth.query_count", 1)
    require(
        truth_obj["digest_algorithm"] == TRUTH_DIGEST_ALGORITHM,
        f"request.truth.digest_algorithm must be {TRUTH_DIGEST_ALGORITHM!r}",
    )
    request["truth"]["path"] = truth_ref["path"]
    truth_rows = read_truth(Path(truth_ref["path"]), query_count)
    if mode == "formal":
        require(query_count == 1700, "formal Neo4j P10 requires the frozen 1,700-query truth")

    stores = request["store_roots"]
    require(isinstance(stores, list) and stores, "request.store_roots must be a non-empty array")
    selected_store: Path | None = None
    selected_lineage = ""
    labels: set[str] = set()
    for index, raw in enumerate(stores):
        context = f"request.store_roots[{index}]"
        item = require_keys(
            raw,
            required=("label", "path", "sha256"),
            allowed=("label", "path", "sha256"),
            context=context,
        )
        label = nonempty_string(item["label"], f"{context}.label")
        require(label not in labels, f"{context}.label is duplicated")
        labels.add(label)
        path = Path(nonempty_string(item["path"], f"{context}.path")).resolve()
        require(path.is_dir(), f"{context}.path is not an existing directory: {path}")
        lineage = exact_sha(item["sha256"], f"{context}.sha256")
        if label == store_label:
            selected_store = path
            selected_lineage = lineage
    require(selected_store is not None, f"request.store_roots has no label {store_label!r}")

    timing = require_keys(
        request["timing"],
        required=(
            "timing_boundary",
            "clock",
            "cache_policy",
            "process_reuse_between_phases",
            "warmup_passes",
            "measured_passes",
            "concurrency",
            "per_query_timeout_ms",
            "sequence_digest_algorithm",
        ),
        allowed=(
            "timing_boundary",
            "clock",
            "cache_policy",
            "process_reuse_between_phases",
            "warmup_passes",
            "measured_passes",
            "concurrency",
            "per_query_timeout_ms",
            "sequence_digest_algorithm",
        ),
        context="request.timing",
    )
    expected_timing = {
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
        "process_reuse_between_phases": True,
        "concurrency": 1,
        "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
    }
    for key, expected in expected_timing.items():
        require(timing[key] == expected, f"request.timing.{key}: {timing[key]!r} != {expected!r}")
    boolean(timing["process_reuse_between_phases"], "request.timing.process_reuse_between_phases")
    integer(timing["warmup_passes"], "request.timing.warmup_passes", 1)
    integer(timing["measured_passes"], "request.timing.measured_passes", 1)
    integer(timing["concurrency"], "request.timing.concurrency", 1)
    integer(timing["per_query_timeout_ms"], "request.timing.per_query_timeout_ms", 1)

    external = require_keys(
        request["external_service"],
        required=("service_lifecycle", "containers", "extra_pids", "image_digests"),
        allowed=("service_lifecycle", "containers", "extra_pids", "image_digests"),
        context="request.external_service",
    )
    require(
        external["service_lifecycle"] == "external-prestarted",
        "Neo4j requires service_lifecycle=external-prestarted",
    )
    containers = external["containers"]
    require(
        isinstance(containers, list)
        and len(containers) == 1
        and isinstance(containers[0], str)
        and containers[0],
        "Neo4j requires exactly one P31-visible container",
    )
    pids = external["extra_pids"]
    require(
        isinstance(pids, list) and not pids,
        "Neo4j requires an empty extra_pids array; P31 must observe the one container",
    )
    image_digests = external["image_digests"]
    require(
        isinstance(image_digests, list) and len(image_digests) == 1,
        "Neo4j requires exactly one pinned image digest",
    )
    image_digest = exact_image_digest(image_digests[0], "request.external_service.image_digests[0]")
    return request, truth_rows, selected_store, selected_lineage, image_digest


def validate_dataset_manifest(
    path: Path, expected_sha: str, request: dict[str, Any]
) -> dict[str, Any]:
    reference = resolve_file(path, expected_sha, "dataset manifest")
    manifest = require_keys(
        read_json(path, "dataset manifest"),
        required=(
            "schema_version",
            "dataset_root",
            "dataset_sha256",
            "hash_method",
            "file_count",
            "total_bytes",
        ),
        allowed=(
            "schema_version",
            "dataset_root",
            "dataset_sha256",
            "hash_method",
            "file_count",
            "total_bytes",
        ),
        context="dataset manifest",
    )
    require(manifest["schema_version"] == "p02b-dataset-manifest-v1", "dataset manifest schema mismatch")
    same_path(manifest["dataset_root"], Path(request["dataset"]["path"]), "dataset manifest.dataset_root")
    require(manifest["dataset_sha256"] == request["dataset"]["sha256"], "dataset manifest/request lineage mismatch")
    require(manifest["hash_method"] == TREE_HASH_METHOD, "dataset manifest hash method mismatch")
    integer(manifest["file_count"], "dataset manifest.file_count", 1)
    integer(manifest["total_bytes"], "dataset manifest.total_bytes", 1)
    return {"reference": reference, "lineage": manifest}


def validate_store_manifest(
    path: Path,
    expected_sha: str,
    store: Path,
    store_lineage: str,
    dataset: dict[str, Any],
    request: dict[str, Any],
    image_digest: str,
    expected_image_ref: str,
    expected_server_version: str,
    formal: bool,
    import_receipt_path: Path | None,
    import_receipt_sha256: str | None,
) -> dict[str, Any]:
    reference = resolve_file(path, expected_sha, "Neo4j store manifest")
    required = (
        "schema_version",
        "store_root",
        "store_sha256",
        "file_count",
        "total_bytes",
        "immutable_store_sha256",
        "immutable_file_count",
        "immutable_total_bytes",
        "files",
        "hash_method",
        "snapshot_phase",
        "known_mutable_patterns",
        "canonical_sentinels",
        "runtime_compatibility",
        "import_provenance",
        "database_contract",
        "dataset_manifest_sha256",
        "dataset_sha256",
        "truth_sha256",
        "relationship_model",
        "sentinel_files",
        "offline_audit",
    )
    manifest = require_keys(
        read_json(path, "Neo4j store manifest"),
        required=required,
        allowed=required,
        context="Neo4j store manifest",
    )
    exact = {
        "schema_version": STORE_MANIFEST_SCHEMA_VERSION,
        "store_sha256": store_lineage,
        "hash_method": TREE_HASH_METHOD,
        "snapshot_phase": SNAPSHOT_PHASE,
        "dataset_manifest_sha256": dataset["reference"]["sha256"],
        "dataset_sha256": request["dataset"]["sha256"],
        "truth_sha256": request["truth"]["sha256"],
        "relationship_model": RELATIONSHIP_MODEL,
    }
    for key, expected in exact.items():
        require(manifest[key] == expected, f"Neo4j store manifest.{key} mismatch")
    same_path(manifest["store_root"], store, "Neo4j store manifest.store_root")
    recorded_tree = validate_recorded_tree(manifest, "Neo4j store manifest")
    require(
        manifest["known_mutable_patterns"] == list(KNOWN_MUTABLE_PATTERNS),
        "Neo4j store manifest.known_mutable_patterns mismatch",
    )
    require(
        manifest["canonical_sentinels"] == list(CANONICAL_SENTINELS),
        "Neo4j store manifest.canonical_sentinels mismatch",
    )
    runtime = require_keys(
        manifest["runtime_compatibility"],
        required=("neo4j_version", "image_ref", "image_digest"),
        allowed=("neo4j_version", "image_ref", "image_digest"),
        context="Neo4j store manifest.runtime_compatibility",
    )
    require(runtime["neo4j_version"] == expected_server_version, "Neo4j runtime version mismatch")
    require(runtime["image_ref"] == expected_image_ref, "Neo4j runtime image ref mismatch")
    require(runtime["image_digest"] == image_digest, "Neo4j runtime image digest mismatch")
    require(
        (import_receipt_path is None) == (import_receipt_sha256 is None),
        "--import-receipt and --import-receipt-sha256 must be provided together",
    )
    import_provenance = manifest["import_provenance"]
    require(isinstance(import_provenance, dict), "Neo4j import provenance is malformed")
    import_status = import_provenance.get("status")
    if import_status == "controlled-import-receipt-v3":
        require(
            import_receipt_path is not None and import_receipt_sha256 is not None,
            "controlled Neo4j store requires its import receipt path/SHA",
        )
        current = p31_host_facts()
        receipt = validate_controlled_import_receipt(
            import_receipt_path,
            import_receipt_sha256,
            store_root=store,
            dataset_manifest=dataset["reference"],
            dataset_sha256=request["dataset"]["sha256"],
            image_ref=expected_image_ref,
            image_digest=image_digest,
            offline_tree=recorded_tree,
            importer_path=Path(__file__).with_name("import_neo4j_store.py"),
            current_host={
                "hostname": current["hostname"],
                "fingerprint_sha256": current["fingerprint_sha256"],
            },
            # The prelaunch importer sealed the two multi-GB CSVs into input_sha256.
            # P31 consumes that pinned receipt and only rehashes its small artifacts.
            verify_input_files=False,
        )
        require(
            import_provenance == {"status": "controlled-import-receipt-v3", **receipt},
            "Neo4j store import provenance differs from the controlled receipt",
        )
    elif import_status == "unverified-historical-store":
        require(not formal, "formal Neo4j runs reject every unverified historical store")
        require(import_receipt_path is None, "historical store cannot be upgraded by attaching a receipt")
        expected_historical = {
            "status": "unverified-historical-store",
            "reference": None,
            "producer": None,
            "host": None,
            "image": {
                "configured_ref": expected_image_ref,
                "image_id": None,
                "repo_digests": [],
                "selected_repo_digest": None,
            },
            "input": None,
            "stages": None,
            "database_contract": None,
            "final_store": None,
        }
        require(import_provenance == expected_historical, "historical Neo4j import marker is not canonical")
    else:
        raise ContractError("Neo4j store import provenance has unknown status")
    database = require_keys(
        manifest["database_contract"],
        required=("database_name", "node_label", "id_property", "required_index"),
        allowed=("database_name", "node_label", "id_property", "required_index"),
        context="Neo4j store manifest.database_contract",
    )
    require(database["database_name"] == DATABASE_NAME, "Neo4j database name mismatch")
    require(database["node_label"] == NODE_LABEL, "Neo4j node label mismatch")
    require(database["id_property"] == ID_PROPERTY, "Neo4j ID property mismatch")
    required_index = require_keys(
        database["required_index"],
        required=("name", "type", "state"),
        allowed=("name", "type", "state"),
        context="Neo4j store manifest.database_contract.required_index",
    )
    require(
        required_index == {"name": INDEX_NAME, "type": "RANGE", "state": "ONLINE"},
        "Neo4j required index contract mismatch",
    )
    expected_sentinels = canonical_sentinel_records(recorded_tree["files"])
    require(
        manifest["sentinel_files"] == expected_sentinels,
        "Neo4j store sentinel metadata differs from the complete offline inventory",
    )
    validated = [
        {**item, "absolute_path": str((store / item["path"]).resolve())}
        for item in expected_sentinels
    ]
    offline_audit = require_keys(
        manifest["offline_audit"],
        required=(
            "proof_method", "before_hash", "after_hash", "per_file_stat_stability",
            "owner_only_root", "exclusive_store_lock_held_across_hash",
            "docker_mount_rescan", "proc_scan_used",
        ),
        allowed=(
            "proof_method", "before_hash", "after_hash", "per_file_stat_stability",
            "owner_only_root", "exclusive_store_lock_held_across_hash",
            "docker_mount_rescan", "proc_scan_used",
        ),
        context="Neo4j store manifest.offline_audit",
    )
    require(
        offline_audit["per_file_stat_stability"] is True
        and offline_audit["owner_only_root"] is True
        and offline_audit["exclusive_store_lock_held_across_hash"] is True
        and offline_audit["docker_mount_rescan"] is True
        and offline_audit["proc_scan_used"] is False,
        "Neo4j offline store audit lacks stability/rescan proof",
    )
    validate_owner_only_offline_gate(
        {key: offline_audit[key] for key in ("proof_method", "before_hash", "after_hash")},
        store,
        "Neo4j store manifest.offline_audit",
    )
    return {
        "reference": reference,
        "lineage": manifest,
        "validated_sentinels": validated,
        "import_receipt": None if import_status == "unverified-historical-store" else import_provenance["reference"],
    }


def validate_store_preflight(
    path: Path | None,
    expected_sha256: str | None,
    *,
    request_ref: dict[str, Any],
    store: Path,
    store_manifest: dict[str, Any],
    formal: bool,
) -> dict[str, Any] | None:
    """Validate the small orchestrator receipt; never reread the runtime store."""

    require(
        (path is None) == (expected_sha256 is None),
        "--store-preflight and --store-preflight-sha256 must be provided together",
    )
    if path is None:
        require(not formal, "formal Neo4j runs require an external pre-P31 full-store audit")
        return None
    reference = resolve_file(path, expected_sha256, "Neo4j store preflight")
    document = validate_neo4j_store_audit(
        read_json(Path(reference["path"]), "Neo4j store preflight"),
        stage="pre",
        request_ref=request_ref,
        store_root=store,
        store_manifest_ref=store_manifest["reference"],
    )
    return {"reference": reference, "audit": document}


def _same_artifact_ref(observed: object, expected: object, context: str) -> None:
    require(isinstance(observed, dict) and isinstance(expected, dict), f"{context}: malformed artifact reference")
    require(
        Path(str(observed.get("path", ""))).resolve()
        == Path(str(expected.get("path", ""))).resolve(),
        f"{context}: artifact path mismatch",
    )
    require(observed.get("sha256") == expected.get("sha256"), f"{context}: artifact SHA-256 mismatch")


def validate_runtime_launch(
    path: Path | None,
    expected_sha256: str | None,
    *,
    formal: bool,
    request: dict[str, Any],
    store: Path,
    logs_root: Path,
    store_manifest: dict[str, Any],
    preflight: dict[str, Any] | None,
    uri: str,
    image_digest: str,
) -> dict[str, Any] | None:
    """Validate and cross-bind the orchestrator's launch receipt before queries."""

    require(
        (path is None) == (expected_sha256 is None),
        "--launch-receipt and --launch-receipt-sha256 must be provided together",
    )
    if not formal:
        require(path is None, "fixture Neo4j execution must not carry a formal launch receipt")
        return None
    require(path is not None and expected_sha256 is not None, "formal Neo4j execution requires a launch receipt")
    require(preflight is not None, "formal Neo4j launch requires the pre-launch store audit")
    reference = resolve_file(path, expected_sha256, "Neo4j launch receipt")
    host = p31_host_facts()
    receipt = validate_launch_receipt(
        read_json(Path(reference["path"]), "Neo4j launch receipt"),
        verify_artifacts=True,
        expected_host={
            "hostname": host["hostname"],
            "fingerprint_sha256": host["fingerprint_sha256"],
        },
    )
    repeat = receipt["repeat"]
    require(repeat["repeat_index"] == request["repeat_index"], "Neo4j launch repeat index differs from request")
    clone = receipt["clone"]
    same_path(clone["runtime_store_root"]["path"], store, "Neo4j launch runtime store")
    same_path(clone["logs_root"]["path"], logs_root, "Neo4j launch logs root")
    _same_artifact_ref(clone["store_manifest"], store_manifest["reference"], "Neo4j launch store manifest")
    _same_artifact_ref(clone["store_preflight"], preflight["reference"], "Neo4j launch preflight")
    external = request["external_service"]
    contract = receipt["runtime_contract"]
    require(
        external["containers"] == [contract["container_name"]],
        "Neo4j launch container differs from request",
    )
    require(
        external["image_digests"] == [receipt["image"]["selected_repo_digest"]]
        and receipt["image"]["selected_repo_digest"] == image_digest,
        "Neo4j launch image RepoDigest differs from request",
    )
    parsed_uri = urlparse(uri)
    require(
        parsed_uri.hostname in {"127.0.0.1", "localhost"}
        and parsed_uri.port == contract["bolt_port"]
        and contract["bolt_host"] == "127.0.0.1",
        "Neo4j launch Bolt endpoint differs from request",
    )
    same_path(contract["store_root"], store, "Neo4j launch contract store root")
    same_path(contract["logs_root"], logs_root, "Neo4j launch contract logs root")
    return {"reference": reference, "receipt": receipt}


def validate_container_against_launch(
    container: dict[str, Any], launch: dict[str, Any] | None
) -> None:
    if launch is None:
        return
    running = launch["receipt"]["docker"]["running"]
    config = running["config"]
    runtime = running["runtime"]
    exact = {
        "name": config["container_name"],
        "container_id": config["container_id"],
        "pid": runtime["pid"],
        "started_at": runtime["started_at"],
        "restart_count": runtime["restart_count"],
        "image_id": config["image_id"],
        "configured_image": config["configured_image"],
        "container_user": config["container_user"],
        "bolt_port": launch["receipt"]["runtime_contract"]["bolt_port"],
        "restart_policy": config["restart_policy"],
        "docker_log_config": config["log_config"],
        "docker_memory_limit_bytes": config["resource_limits"]["memory_bytes"],
        "docker_memory_swap_bytes": config["resource_limits"]["memory_swap_bytes"],
        "entrypoint": config["entrypoint"],
        "command": config["command"],
    }
    for key, expected in exact.items():
        require(container.get(key) == expected, f"Neo4j container {key} differs from launch receipt")
    by_destination = {item["destination"]: item for item in config["mounts"]}
    same_path(container["data_mount"], Path(str(by_destination["/data"]["source"])), "Neo4j launch /data")
    same_path(container["logs_mount"], Path(str(by_destination["/logs"]["source"])), "Neo4j launch /logs")


def run_json(command: list[str], context: str) -> Any:
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(f"{context}: cannot execute command: {exc}") from exc
    require(completed.returncode == 0, f"{context}: command failed: {completed.stderr.strip()}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{context}: command returned invalid JSON") from exc


def memory_size_bytes(value: object, context: str) -> int:
    require(isinstance(value, str) and value.strip(), f"{context}: expected memory size string")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]i?B?|B)", value.strip(), re.IGNORECASE)
    require(match is not None, f"{context}: unsupported memory size {value!r}")
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ContractError(f"{context}: invalid memory size {value!r}") from exc
    unit = match.group(2).lower()
    factors = {
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gib": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
        "tib": 1024**4,
    }
    result = amount * factors[unit]
    require(result == result.to_integral_value() and result > 0, f"{context}: memory size is not positive integral bytes")
    return int(result)


def expected_memory_contract(heap_initial: str, heap_max: str, pagecache: str) -> dict[str, Any]:
    configured = {
        "heap_initial": heap_initial,
        "heap_max": heap_max,
        "pagecache": pagecache,
    }
    bytes_by_role = {
        role: memory_size_bytes(value, f"expected {role}")
        for role, value in configured.items()
    }
    require(
        bytes_by_role["heap_initial"] == bytes_by_role["heap_max"],
        "Neo4j formal heap initial/max sizes must be equal",
    )
    return {
        "configured": configured,
        "bytes": bytes_by_role,
        "settings": dict(MEMORY_SETTING_NAMES),
        "environment_names": dict(MEMORY_ENV_NAMES),
    }


def _normalized_container_destination(value: object) -> str:
    require(isinstance(value, str) and value.startswith("/"), "container mount destination must be absolute")
    normalized = posixpath.normpath(value)
    require(normalized == value, f"container mount destination is not canonical: {value!r}")
    return normalized


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left in right.parents or right in left.parents


def _environment_map(values: object) -> dict[str, str]:
    require(isinstance(values, list), "Neo4j container environment is malformed")
    result: dict[str, str] = {}
    for raw in values:
        require(isinstance(raw, str) and "=" in raw, "Neo4j container environment entry is malformed")
        name, value = raw.split("=", 1)
        require(name and name not in result, f"Neo4j container environment duplicates {name!r}")
        result[name] = value
    return result


def _reject_directory_overrides(environment: dict[str, str], container: dict[str, Any]) -> None:
    forbidden_tokens = (
        "directories_data",
        "directories_databases",
        "directories_transaction__logs__root",
        "directories_transaction_logs_root",
        "directories_logs",
        "directories_run",
        "directories_home",
        "dbms_directories_data",
        "dbms_directories_databases",
        "dbms_directories_tx_log",
    )
    forbidden_env = [
        name for name in environment
        if name.startswith("NEO4J_") and any(token in name.lower() for token in forbidden_tokens)
    ]
    require(not forbidden_env, f"Neo4j container overrides storage directories: {forbidden_env!r}")
    command_values = []
    for key in ("Entrypoint", "Cmd"):
        raw = container.get("Config", {}).get(key)
        if isinstance(raw, str):
            command_values.append(raw)
        elif isinstance(raw, list):
            require(all(isinstance(item, str) for item in raw), f"Neo4j container {key} is malformed")
            command_values.extend(raw)
        elif raw is not None:
            raise ContractError(f"Neo4j container {key} is malformed")
    lowered = "\n".join(command_values).lower().replace(".", "_")
    require(
        not any(token in lowered for token in forbidden_tokens),
        "Neo4j container command/entrypoint overrides a storage directory",
    )


def validate_container(
    inspect_target: str,
    expected_name: str,
    expected_image_ref: str,
    expected_image_digest: str,
    store: Path,
    logs_root: Path,
    uri: str,
    formal: bool,
    expected_memory: dict[str, Any],
    expected_entrypoint: list[str] = EXPECTED_ENTRYPOINT,
    expected_command: list[str] = EXPECTED_COMMAND,
) -> dict[str, Any]:
    parsed = urlparse(uri)
    require(parsed.scheme in ("bolt", "neo4j"), "Neo4j URI scheme must be bolt or neo4j")
    require(parsed.hostname in ("127.0.0.1", "localhost"), "Neo4j URI must use localhost")
    require(parsed.port is not None, "Neo4j URI must contain an explicit port")
    require(
        not formal or re.fullmatch(r"[0-9a-f]{64}", inspect_target) is not None,
        "formal Docker container inspect requires the launch-bound full ID",
    )
    raw_container = run_json(
        ["docker", "container", "inspect", inspect_target],
        "Docker container inspect",
    )
    require(isinstance(raw_container, list) and len(raw_container) == 1, "Docker returned an ambiguous container")
    container = raw_container[0]
    container_id = nonempty_string(container.get("Id"), "Docker container ID")
    require(container.get("Name") == f"/{expected_name}", "Docker container name mismatch")
    if formal:
        require(container_id == inspect_target, "Docker inspect returned a different launch-bound ID")
    state = container.get("State", {})
    require(state.get("Running") is True, "Neo4j container is not running")
    pid = integer(state.get("Pid"), "Neo4j container PID", 1)
    started_at = nonempty_string(state.get("StartedAt"), "Neo4j container StartedAt")
    restart_count = integer(container.get("RestartCount"), "Neo4j container RestartCount", 0)
    require(restart_count == 0, "Neo4j container must have RestartCount=0")
    host_config = container.get("HostConfig", {})
    require(host_config.get("RestartPolicy", {}).get("Name") == "no", "Neo4j container restart policy must be 'no'")
    raw_config = container.get("Config", {})
    require(isinstance(raw_config, dict), "Neo4j container config is malformed")
    require(raw_config.get("Image") == expected_image_ref, "Neo4j container image tag mismatch")
    require(
        raw_config.get("Entrypoint") == expected_entrypoint,
        "Neo4j Docker Config.Entrypoint differs from the preregistered baseline",
    )
    require(
        raw_config.get("Cmd") == expected_command,
        "Neo4j Docker Config.Cmd differs from the preregistered baseline",
    )
    container_user = nonempty_string(raw_config.get("User"), "Neo4j container user")
    require(re.fullmatch(r"[0-9]+:[0-9]+", container_user) is not None, "Neo4j container user must be numeric UID:GID")
    image_id = exact_image_digest(container.get("Image"), "Docker container image ID")
    raw_image = run_json(["docker", "image", "inspect", image_id], "Docker image inspect")
    require(isinstance(raw_image, list) and len(raw_image) == 1, "Docker returned an ambiguous image")
    image = raw_image[0]
    repo_digests = image.get("RepoDigests") or []
    resolved_digests = sorted(
        item.split("@", 1)[1]
        for item in repo_digests
        if isinstance(item, str) and "@" in item
    )
    require(expected_image_digest in resolved_digests, "Neo4j image RepoDigest mismatch")

    logs_root = logs_root.resolve()
    require(logs_root.is_dir() and not logs_root.is_symlink(), f"Neo4j logs root is invalid: {logs_root}")
    require(not _paths_overlap(store, logs_root), "Neo4j /data and /logs roots overlap")
    if formal:
        store_metadata = store.stat()
        logs_metadata = logs_root.stat()
        expected_user = f"{store_metadata.st_uid}:{store_metadata.st_gid}"
        require(container_user == expected_user, "Neo4j container user differs from the formal store owner")
        for metadata, context in ((store_metadata, "store"), (logs_metadata, "logs")):
            require(
                metadata.st_uid == os.geteuid() and metadata.st_gid == os.getegid(),
                f"formal Neo4j {context} root is not owned by the current UID:GID",
            )
            require(stat.S_IMODE(metadata.st_mode) == 0o700, f"formal Neo4j {context} root mode is not 0700")
        require(
            (store_metadata.st_uid, store_metadata.st_gid)
            == (logs_metadata.st_uid, logs_metadata.st_gid),
            "formal Neo4j store/log owners differ",
        )
    raw_mounts = container.get("Mounts", [])
    require(isinstance(raw_mounts, list), "Neo4j container mounts are malformed")
    mounts_by_destination: dict[str, dict[str, Any]] = {}
    for raw_mount in raw_mounts:
        require(isinstance(raw_mount, dict), "Neo4j container mount entry is malformed")
        destination = _normalized_container_destination(raw_mount.get("Destination"))
        require(destination not in mounts_by_destination, f"Neo4j container duplicates mount {destination}")
        require(
            destination in {"/data", "/logs"},
            f"Neo4j container mount is outside the exact allowlist: {destination}",
        )
        require(raw_mount.get("Type") == "bind", f"Neo4j {destination} must be a bind mount")
        source = raw_mount.get("Source")
        require(isinstance(source, str) and source.startswith("/"), f"Neo4j {destination} source is invalid")
        mounts_by_destination[destination] = raw_mount
    require(set(mounts_by_destination) == {"/data", "/logs"}, "Neo4j requires exactly /data and /logs bind mounts")
    data_mount = mounts_by_destination["/data"]
    logs_mount = mounts_by_destination["/logs"]
    same_path(data_mount.get("Source"), store, "Neo4j container /data mount")
    same_path(logs_mount.get("Source"), logs_root, "Neo4j container /logs mount")
    require(data_mount.get("RW") is True, "Neo4j /data mount unexpectedly is not writable")
    require(logs_mount.get("RW") is True, "Neo4j /logs mount unexpectedly is not writable")
    for destination, raw_mount in mounts_by_destination.items():
        source = Path(str(raw_mount["Source"])).resolve()
        if destination != "/data":
            require(not _paths_overlap(source, store), "Neo4j nested/aliased /data mount is forbidden")

    raw_log_config = host_config.get("LogConfig") or {}
    log_config = {
        "type": raw_log_config.get("Type"),
        "config": raw_log_config.get("Config") or {},
    }
    require(log_config == EXPECTED_DOCKER_LOG_CONFIG, "Neo4j Docker log driver/options mismatch")
    memory_limit = integer(host_config.get("Memory", 0), "Neo4j container memory limit", 0)
    memory_swap = integer(host_config.get("MemorySwap", 0), "Neo4j container memory swap limit", -1)
    require(memory_limit == 0, "Neo4j formal template must explicitly record an unlimited Docker memory limit")
    require(memory_swap == 0, "Neo4j formal template must record the default unlimited Docker swap limit")

    all_ports = container.get("NetworkSettings", {}).get("Ports", {})
    published = {
        container_port: bindings
        for container_port, bindings in all_ports.items()
        if bindings
    }
    require(set(published) == {"7687/tcp"}, "Neo4j must not publish ports other than Bolt")
    ports = published.get("7687/tcp")
    require(isinstance(ports, list) and len(ports) == 1, "Neo4j must publish exactly one Bolt port")
    require(ports[0].get("HostIp") == "127.0.0.1", "Neo4j Bolt port must bind only to 127.0.0.1")
    require(int(ports[0].get("HostPort", "0")) == parsed.port, "Neo4j Bolt host port differs from URI")

    environment = _environment_map(container.get("Config", {}).get("Env") or [])
    _reject_directory_overrides(environment, container)
    require(environment.get("NEO4J_AUTH") == "none", "Neo4j adapter currently requires NEO4J_AUTH=none")
    require(
        environment.get("NEO4J_server_databases_default__to__read__only") == "true",
        "Neo4j service must default databases to read-only",
    )
    for role, env_name in MEMORY_ENV_NAMES.items():
        require(
            environment.get(env_name) == expected_memory["configured"][role],
            f"Neo4j container {role} setting differs from the preregistered value",
        )
    return {
        "name": expected_name,
        "container_id": container_id,
        "pid": pid,
        "started_at": started_at,
        "restart_count": restart_count,
        "image_id": image_id,
        "configured_image": expected_image_ref,
        "container_user": container_user,
        "repo_digests": repo_digests,
        "expected_repo_digest": expected_image_digest,
        "data_mount": str(store),
        "logs_mount": str(logs_root),
        "bolt_host": "127.0.0.1",
        "bolt_port": parsed.port,
        "restart_policy": "no",
        "read_only_default": True,
        "docker_log_config": log_config,
        "docker_memory_limit_bytes": memory_limit,
        "docker_memory_swap_bytes": memory_swap,
        "memory_configuration": expected_memory,
        "entrypoint": container.get("Config", {}).get("Entrypoint"),
        "command": container.get("Config", {}).get("Cmd"),
    }


def validate_container_stability(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    stable_keys = (
        "name",
        "container_id",
        "pid",
        "started_at",
        "restart_count",
        "image_id",
        "configured_image",
        "container_user",
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
    for key in stable_keys:
        require(before.get(key) == after.get(key), f"Neo4j container {key} changed during the repeat")
    require(before.get("restart_count") == 0, "Neo4j container restarted before the repeat")
    require(after.get("restart_count") == 0, "Neo4j container restarted during the repeat")
    return {"stable": True, "before": before, "after": after}


def driver_binding(expected_version: str, expected_tree_sha256: str) -> dict[str, Any]:
    actual_version = importlib.metadata.version("neo4j")
    require(actual_version == expected_version, f"Neo4j Python driver {actual_version!r} != {expected_version!r}")
    expected_tree_sha256 = exact_sha(
        expected_tree_sha256,
        "expected Neo4j Python driver tree SHA-256",
    )
    distribution = importlib.metadata.distribution("neo4j")
    files: list[tuple[str, Path]] = []
    for item in distribution.files or []:
        path = Path(distribution.locate_file(item)).resolve()
        if path.is_file():
            files.append((str(item).replace(os.sep, "/"), path))
    files.sort(key=lambda pair: pair[0].encode())
    require(files, "Neo4j Python distribution has no auditable files")
    digest = hashlib.sha256()
    total_bytes = 0
    for relative, path in files:
        size = path.stat().st_size
        total_bytes += size
        digest.update(f"file\0{relative}\0{size}\0{sha256_file(path)}\n".encode())
    module_path = Path(str(neo4j.__file__)).resolve()
    actual_tree_sha256 = digest.hexdigest()
    require(
        actual_tree_sha256 == expected_tree_sha256,
        "Neo4j Python driver package tree differs from the prebound SHA-256",
    )
    return {
        "version": actual_version,
        "expected_package_tree_sha256": expected_tree_sha256,
        "package_tree_sha256": actual_tree_sha256,
        "hash_method": TREE_HASH_METHOD,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "module": artifact_ref(module_path),
    }


def validate_p02b(
    args: argparse.Namespace,
    git: dict[str, Any],
    request: dict[str, Any],
    dataset_manifest: dict[str, Any],
) -> dict[str, Any]:
    require(args.p02b_max_age_seconds > 0, "P02B max age must be positive")
    required_args = (
        "p02b_result",
        "p02b_result_sha256",
        "p02b_validator",
        "p02b_validator_sha256",
        "p02b_binary",
        "p02b_binary_sha256",
    )
    for name in required_args:
        require(getattr(args, name) not in (None, ""), f"formal Neo4j adapter requires --{name.replace('_', '-')}")
    result_ref = resolve_file(args.p02b_result, args.p02b_result_sha256, "P02B result")
    validator_ref = resolve_file(
        args.p02b_validator,
        args.p02b_validator_sha256,
        "P02B validator",
        executable=True,
    )
    binary_ref = resolve_file(args.p02b_binary, args.p02b_binary_sha256, "P02B binary", executable=True)
    command = [
        validator_ref["path"],
        "--result",
        result_ref["path"],
        "--consumer",
        "P10",
        "--expected-repo-root",
        git["root"],
        "--expected-repo-head",
        git["head"],
        "--expected-binary-sha256",
        binary_ref["sha256"],
        "--require-formal",
        "--max-age-seconds",
        str(args.p02b_max_age_seconds),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=60)
    require(completed.returncode == 0, f"P02B admission failed: {completed.stderr.strip()}")
    try:
        admission = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("P02B validator emitted invalid JSON") from exc
    require(admission.get("state") == "PASS", "P02B validator returned a non-PASS admission")
    require(admission.get("consumer") == "P10", "P02B admission consumer mismatch")
    require(admission.get("formal_required") is True, "P02B admission is not formal")
    require(admission.get("fixture_only") is False, "P02B admission is fixture-only")
    require(admission.get("scale") == "sf10", "P02B admission scale mismatch")
    same_path(admission.get("sentinel_result"), Path(result_ref["path"]), "P02B admission result")
    same_path(admission.get("repo_root"), Path(git["root"]), "P02B admission repo root")
    require(admission.get("repo_head") == git["head"], "P02B admission repo HEAD mismatch")
    require(admission.get("binary_sha256") == binary_ref["sha256"], "P02B admission binary mismatch")
    require(admission.get("sentinel_result_sha256") == result_ref["sha256"], "P02B admission result SHA mismatch")
    current_host = p31_host_facts()
    require(
        admission.get("host")
        == {
            "hostname": current_host["hostname"],
            "fingerprint_sha256": current_host["fingerprint_sha256"],
        },
        "P02B admission was produced on a different host",
    )
    marker = resolve_file(
        Path(str(admission.get("pass_marker", ""))),
        admission.get("pass_marker_sha256"),
        "P02B PASS marker",
    )
    provenance_ref = resolve_file(
        Path(str(admission.get("provenance", ""))),
        admission.get("provenance_sha256"),
        "P02B provenance",
    )
    result = read_json(args.p02b_result, "P02B result")
    provenance = result.get("provenance", {})
    require(provenance.get("repo_head") == git["head"], "P02B result repo HEAD mismatch")
    require(provenance.get("binary_sha256") == binary_ref["sha256"], "P02B result binary mismatch")
    require(provenance.get("truth_sha256") == request["truth"]["sha256"], "P02B/P10 truth mismatch")
    require(provenance.get("dataset_sha256") == request["dataset"]["sha256"], "P02B/P10 dataset mismatch")
    canonical = read_json(Path(provenance_ref["path"]), "P02B canonical provenance")
    files = canonical.get("files", {})
    dataset_file = files.get("dataset_manifest", {})
    truth_file = files.get("truth", {})
    same_path(dataset_file.get("path"), Path(dataset_manifest["reference"]["path"]), "P02B dataset manifest")
    require(dataset_file.get("sha256") == dataset_manifest["reference"]["sha256"], "P02B dataset manifest SHA mismatch")
    same_path(truth_file.get("path"), Path(request["truth"]["path"]), "P02B truth")
    require(truth_file.get("sha256") == request["truth"]["sha256"], "P02B truth SHA mismatch")
    return {
        "result": result_ref,
        "validator": validator_ref,
        "binary": binary_ref,
        "pass_marker": marker,
        "provenance": provenance_ref,
        "admission": admission,
        "validator_argv_sha256": hashlib.sha256(
            json.dumps(command, separators=(",", ":")).encode()
        ).hexdigest(),
        "max_age_seconds": args.p02b_max_age_seconds,
        "current_host": {
            "hostname": current_host["hostname"],
            "fingerprint_sha256": current_host["fingerprint_sha256"],
        },
    }


def mix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & MASK
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK
    return (value ^ (value >> 31)) & MASK


def digest_add(digest: Digest, destination: int) -> None:
    digest.count += 1
    digest.sum_hash = (digest.sum_hash + mix64(destination)) & MASK
    digest.xor_hash ^= mix64(destination ^ 0xD6E8FEB86659FD93)


def relationship_type(edge_type: int) -> str:
    return f"E_P{edge_type}" if edge_type > 0 else f"E_N{abs(edge_type)}"


def timeout_exception(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = " ".join(
            str(value)
            for value in (
                current.__class__.__name__,
                getattr(current, "code", ""),
                current,
            )
        ).lower()
        if "timeout" in text or "timed out" in text or "deadline exceeded" in text:
            return True
        current = current.__cause__ or current.__context__
    return False


def clock_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def run_query(
    session: Any,
    truth: dict[str, int],
    timeout_ms: int,
) -> tuple[str, int, Digest | None]:
    relationship = relationship_type(truth["edge_type"])
    cypher = f"MATCH (s:V {{id: $src}})-[:{relationship}]->(d:V) RETURN d.id AS dst"
    started = clock_ns()
    digest = Digest()
    try:
        result = session.run(Query(cypher, timeout=timeout_ms / 1000.0), src=truth["src"])
        for record in result:
            digest_add(digest, int(record["dst"]))
        result.consume()
    except Exception as exc:  # Neo4j exposes several timeout exception classes.
        if not timeout_exception(exc):
            raise ContractError(
                f"Neo4j query {truth['query_index']} failed: {exc.__class__.__name__}: {exc}"
            ) from exc
        deadline = started + timeout_ms * 1_000_000
        remaining = deadline - clock_ns()
        if remaining > 0:
            time.sleep(remaining / 1_000_000_000)
        return "timeout", clock_ns() - started, None
    ended = clock_ns()
    latency = ended - started
    require(latency > 0, "Neo4j query latency must be positive")
    if latency > timeout_ms * 1_000_000:
        raise ContractError(
            f"Neo4j query {truth['query_index']} exceeded deadline without a timeout error"
        )
    return "ok", latency, digest


def observation_row(
    request: dict[str, Any],
    truth: dict[str, int],
    phase: str,
    pass_index: int,
    status: str,
    latency_ns: int,
    digest: Digest | None,
) -> dict[str, str]:
    return {
        "contract_version": CONTRACT_VERSION,
        "system_id": "neo4j",
        "group": "client-server",
        "repeat_index": str(request["repeat_index"]),
        "phase": phase,
        "pass_index": str(pass_index),
        "query_index": str(truth["query_index"]),
        "edge_type": str(truth["edge_type"]),
        "src": str(truth["src"]),
        "expected_count": str(truth["count"]),
        "actual_count": "" if digest is None else str(digest.count),
        "expected_sum_hash": str(truth["sum_hash"]),
        "actual_sum_hash": "" if digest is None else str(digest.sum_hash),
        "expected_xor_hash": str(truth["xor_hash"]),
        "actual_xor_hash": "" if digest is None else str(digest.xor_hash),
        "status": status,
        "latency_ns": str(latency_ns),
    }


def open_ready_driver(
    uri: str,
    expected_agent: str,
    timeout_s: int,
) -> tuple[Any, str, dict[str, Any]]:
    require(1 <= timeout_s <= 900, "Neo4j readiness timeout must be in [1, 900] seconds")
    started = clock_ns()
    deadline = time.monotonic() + timeout_s
    attempts = 0
    last_error = "not attempted"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ContractError(
                f"Neo4j readiness timed out after {attempts} attempts: {last_error}"
            )
        attempts += 1
        driver = None
        try:
            driver = GraphDatabase.driver(
                uri,
                auth=None,
                max_connection_pool_size=1,
                connection_timeout=max(0.001, min(30.0, remaining)),
            )
            driver.verify_connectivity()
            server_agent = driver.get_server_info().agent
        except Exception as exc:  # Neo4j exposes several transient startup errors.
            if driver is not None:
                driver.close()
            last_error = f"{exc.__class__.__name__}: {exc}"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ContractError(
                    f"Neo4j readiness timed out after {attempts} attempts: {last_error}"
                ) from exc
            time.sleep(min(1.0, remaining))
            continue
        if server_agent != expected_agent:
            driver.close()
            raise ContractError(
                f"Neo4j server agent {server_agent!r} != {expected_agent!r}"
            )
        elapsed_ns = clock_ns() - started
        if time.monotonic() > deadline or elapsed_ns > timeout_s * 1_000_000_000:
            driver.close()
            raise ContractError(
                f"Neo4j readiness exceeded its {timeout_s}-second deadline"
            )
        return (
            driver,
            server_agent,
            {
                "timeout_s": timeout_s,
                "attempts": attempts,
                "elapsed_ns": elapsed_ns,
            },
        )


def verify_database_contract(driver: Any, database: str) -> dict[str, Any]:
    require(database == DATABASE_NAME, f"Neo4j database must be {DATABASE_NAME!r}")
    statement = (
        "SHOW INDEXES YIELD name, state, type, entityType, labelsOrTypes, properties "
        "WHERE name = $name "
        "RETURN name, state, type, entityType, labelsOrTypes, properties"
    )
    with driver.session(database=database, default_access_mode=neo4j.READ_ACCESS) as session:
        records = [record.data() for record in session.run(statement, name=INDEX_NAME)]
    require(len(records) == 1, f"Neo4j requires exactly one {INDEX_NAME!r} index")
    index = records[0]
    expected = {
        "name": INDEX_NAME,
        "state": "ONLINE",
        "type": "RANGE",
        "entityType": "NODE",
        "labelsOrTypes": [NODE_LABEL],
        "properties": [ID_PROPERTY],
    }
    for key, value in expected.items():
        require(index.get(key) == value, f"Neo4j index {INDEX_NAME!r} {key} mismatch")
    return {
        "database_name": database,
        "node_label": NODE_LABEL,
        "id_property": ID_PROPERTY,
        "required_index": expected,
    }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _plan_tree(plan: Any) -> dict[str, Any]:
    operator = getattr(plan, "operator_type", None)
    require(isinstance(operator, str) and operator, "Neo4j EXPLAIN plan node lacks operator_type")
    raw_children = getattr(plan, "children", []) or []
    require(isinstance(raw_children, (list, tuple)), "Neo4j EXPLAIN plan children are malformed")
    identifiers = getattr(plan, "identifiers", []) or []
    arguments = getattr(plan, "arguments", {}) or {}
    return {
        "operator_type": operator,
        "identifiers": _jsonable(identifiers),
        "arguments": _jsonable(arguments),
        "children": [_plan_tree(child) for child in raw_children],
    }


def _plan_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [plan]
    for child in plan["children"]:
        nodes.extend(_plan_nodes(child))
    return nodes


def verify_live_runtime_contract(
    driver: Any,
    database: str,
    expected_memory: dict[str, Any],
    representative_truth: dict[str, int],
) -> dict[str, Any]:
    require(database == DATABASE_NAME, f"Neo4j database must be {DATABASE_NAME!r}")
    with driver.session(database="system", default_access_mode=neo4j.READ_ACCESS) as session:
        database_rows = [
            record.data()
            for record in session.run(
                "SHOW DATABASES YIELD name, currentStatus, requestedStatus, access "
                "WHERE name = $name RETURN name, currentStatus, requestedStatus, access",
                name=database,
            )
        ]
        setting_rows = [
            record.data()
            for record in session.run(
                "SHOW SETTINGS YIELD name, value WHERE name IN $names RETURN name, value",
                names=list(MEMORY_SETTING_NAMES.values()),
            )
        ]
    require(len(database_rows) == 1, "Neo4j SHOW DATABASES did not return exactly the query database")
    database_status = database_rows[0]
    require(database_status.get("name") == database, "Neo4j live database name mismatch")
    require(str(database_status.get("currentStatus", "")).lower() == "online", "Neo4j query database is not online")
    require(str(database_status.get("requestedStatus", "")).lower() == "online", "Neo4j query database is not requested online")
    require(str(database_status.get("access", "")).lower() == "read-only", "Neo4j query database is not live read-only")

    by_name = {
        row.get("name"): row.get("value")
        for row in setting_rows
        if isinstance(row, dict)
    }
    require(set(by_name) == set(MEMORY_SETTING_NAMES.values()), "Neo4j SHOW SETTINGS memory set mismatch")
    live_memory: dict[str, Any] = {}
    for role, name in MEMORY_SETTING_NAMES.items():
        raw = by_name[name]
        live_bytes = memory_size_bytes(raw, f"Neo4j live setting {name}")
        require(
            live_bytes == expected_memory["bytes"][role],
            f"Neo4j live setting {name} differs from the preregistered value",
        )
        live_memory[role] = {"name": name, "value": raw, "bytes": live_bytes}

    relationship = relationship_type(representative_truth["edge_type"])
    cypher = f"EXPLAIN MATCH (s:V {{id: $src}})-[:{relationship}]->(d:V) RETURN d.id AS dst"
    with driver.session(database=database, default_access_mode=neo4j.READ_ACCESS) as session:
        summary = session.run(cypher, src=representative_truth["src"]).consume()
    raw_plan = getattr(summary, "plan", None)
    require(raw_plan is not None, "Neo4j EXPLAIN did not publish a logical plan")
    plan = _plan_tree(raw_plan)
    plan_nodes = _plan_nodes(plan)
    index_seeks = [node for node in plan_nodes if node["operator_type"] == "NodeIndexSeek"]
    require(index_seeks, "Neo4j representative plan lacks NodeIndexSeek")
    seek_text = json.dumps(index_seeks, sort_keys=True, separators=(",", ":"))
    compact_seek_text = seek_text.replace("`", "").replace(" ", "")
    require(
        "v_id" in seek_text or "V(id)" in compact_seek_text,
        "Neo4j NodeIndexSeek is not bound to the frozen :V(id) index",
    )
    return {
        "database_status": database_status,
        "memory": {
            "preregistered": expected_memory,
            "live": live_memory,
        },
        "representative_plan": {
            "cypher": cypher,
            "parameters": {"src": representative_truth["src"]},
            "required_operator": "NodeIndexSeek",
            "plan": plan,
        },
    }


def jvm_process_contract(container_pid: int) -> dict[str, Any]:
    pending = [container_pid]
    seen: set[int] = set()
    candidates: list[tuple[int, bytes, list[str]]] = []
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        proc = Path("/proc") / str(pid)
        try:
            raw = (proc / "cmdline").read_bytes()
            argv = [item.decode("utf-8", errors="strict") for item in raw.split(b"\0") if item]
        except (OSError, UnicodeError) as exc:
            raise ContractError(f"cannot read Neo4j container process {pid}: {exc}") from exc
        if argv and Path(argv[0]).name == "java":
            candidates.append((pid, raw, argv))
        try:
            children_text = (proc / "task" / str(pid) / "children").read_text(encoding="ascii")
        except OSError as exc:
            raise ContractError(f"cannot enumerate Neo4j container descendants for PID {pid}: {exc}") from exc
        children = [int(value) for value in children_text.split()]
        pending.extend(children)
    require(len(candidates) == 1, "Neo4j container must have exactly one JVM descendant")
    pid, raw, argv = candidates[0]
    require(any("-XX:+UseG1GC" == item for item in argv), "Neo4j JVM is not explicitly using G1GC")
    return {
        "container_pid": container_pid,
        "pid": pid,
        "java_executable": argv[0],
        "gc": "G1GC",
        "argv": argv,
        "argv_sha256": hashlib.sha256(raw).hexdigest(),
    }


def execute_queries(
    request: dict[str, Any], truth_rows: list[dict[str, int]], driver: Any, database: str
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    timing = request["timing"]
    observations: list[dict[str, str]] = []
    phases: dict[str, Any] = {}
    with driver.session(database=database, default_access_mode=neo4j.READ_ACCESS) as session:
        for phase in ("warmup", "measured"):
            passes = timing[f"{phase}_passes"]
            started = clock_ns()
            for pass_index in range(passes):
                for truth in truth_rows:
                    status, latency_ns, digest = run_query(
                        session, truth, timing["per_query_timeout_ms"]
                    )
                    observations.append(
                        observation_row(
                            request,
                            truth,
                            phase,
                            pass_index,
                            status,
                            latency_ns,
                            digest,
                        )
                    )
            ended = clock_ns()
            phase_rows = [row for row in observations if row["phase"] == phase]
            timeouts = sum(row["status"] == "timeout" for row in phase_rows)
            mismatches = 0
            for row in phase_rows:
                if row["status"] == "ok" and (
                    row["actual_count"] != row["expected_count"]
                    or row["actual_sum_hash"] != row["expected_sum_hash"]
                    or row["actual_xor_hash"] != row["expected_xor_hash"]
                ):
                    mismatches += 1
            phases[phase] = {
                "passes": passes,
                "requested_queries": passes * len(truth_rows),
                "completed_queries": len(phase_rows) - timeouts,
                "timeout_queries": timeouts,
                "mismatch_queries": mismatches,
                "started_monotonic_ns": started,
                "ended_monotonic_ns": ended,
                "elapsed_ns": ended - started,
                "expected_digest_sha256": phase_digest(phase, passes, truth_rows),
                "actual_digest_sha256": phase_digest(phase, passes, truth_rows, phase_rows),
            }
    return observations, phases


def write_observations(path: Path, rows: list[dict[str, str]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OBSERVATION_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def write_phase_events(path: Path, phases: dict[str, Any]) -> None:
    lines = []
    for phase in ("warmup", "measured"):
        lines.append(
            json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "phase": phase,
                    "event": "start",
                    "monotonic_ns": phases[phase]["started_monotonic_ns"],
                },
                sort_keys=True,
            )
        )
        lines.append(
            json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "phase": phase,
                    "event": "end",
                    "monotonic_ns": phases[phase]["ended_monotonic_ns"],
                },
                sort_keys=True,
            )
        )
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run(args: argparse.Namespace) -> None:
    formal = args.mode == "formal"
    expected_entrypoint = exact_string_vector(
        args.expected_entrypoint_json, "expected Neo4j Docker Config.Entrypoint"
    )
    expected_command = exact_string_vector(
        args.expected_command_json, "expected Neo4j Docker Config.Cmd"
    )
    memory_contract = expected_memory_contract(
        args.expected_heap_initial_size,
        args.expected_heap_max_size,
        args.expected_pagecache_size,
    )
    if formal:
        require(
            memory_contract["configured"]
            == {"heap_initial": "8G", "heap_max": "8G", "pagecache": "16G"},
            "formal Neo4j memory configuration must be the preregistered 8G/8G/16G profile",
        )
    require(args.database == DATABASE_NAME, f"Neo4j database must be {DATABASE_NAME!r}")
    require(
        1 <= args.readiness_timeout_s <= 900,
        "Neo4j readiness timeout must be in [1, 900] seconds",
    )
    if formal:
        require(
            args.expected_image_ref == EXPECTED_IMAGE_REF,
            f"formal Neo4j image must be {EXPECTED_IMAGE_REF!r}",
        )
        require(
            args.expected_driver_version == EXPECTED_DRIVER_VERSION,
            f"formal Neo4j Python driver must be {EXPECTED_DRIVER_VERSION!r}",
        )
        exact_sha(
            args.expected_driver_tree_sha256,
            "formal Neo4j Python driver tree SHA-256",
        )
        require(
            args.expected_server_agent == EXPECTED_SERVER_AGENT,
            f"formal Neo4j server agent must be {EXPECTED_SERVER_AGENT!r}",
        )
        require(
            expected_entrypoint == EXPECTED_ENTRYPOINT,
            "formal Neo4j Docker Config.Entrypoint baseline mismatch",
        )
        require(
            expected_command == EXPECTED_COMMAND,
            "formal Neo4j Docker Config.Cmd baseline mismatch",
        )
    request_path = args.request.resolve()
    request_ref = artifact_ref(request_path)
    request, truth_rows, store, store_lineage, image_digest = validate_request(
        request_path, args.mode, args.store_label
    )
    same_path(
        request["binary"]["path"],
        Path(sys.executable),
        "request.binary must identify the running Python interpreter",
    )
    output_dir = args.output_dir.resolve()
    logs_root = args.logs_root.resolve()
    require(
        output_dir != store and store not in output_dir.parents and output_dir not in store.parents,
        "Neo4j adapter output and store roots must not overlap",
    )
    require(
        not _paths_overlap(output_dir, logs_root) and not _paths_overlap(store, logs_root),
        "Neo4j adapter output, /data, and /logs roots must be independent",
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "adapter-result.json",
        "query-observations.tsv",
        "phase-events.jsonl",
        "adapter-provenance.json",
    ):
        require(not (output_dir / name).exists(), f"Neo4j adapter refuses to overwrite {name}")

    dataset = validate_dataset_manifest(
        args.dataset_manifest.resolve(), args.dataset_manifest_sha256, request
    )
    server_version = args.expected_server_agent.split("/", 1)[-1]
    store_manifest = validate_store_manifest(
        args.store_manifest.resolve(),
        args.store_manifest_sha256,
        store,
        store_lineage,
        dataset,
        request,
        image_digest,
        args.expected_image_ref,
        server_version,
        formal,
        None if args.import_receipt is None else args.import_receipt.resolve(),
        args.import_receipt_sha256,
    )
    preflight = validate_store_preflight(
        None if args.store_preflight is None else args.store_preflight.resolve(),
        args.store_preflight_sha256,
        request_ref=request_ref,
        store=store,
        store_manifest=store_manifest,
        formal=formal,
    )
    launch = validate_runtime_launch(
        None if args.launch_receipt is None else args.launch_receipt.resolve(),
        args.launch_receipt_sha256,
        formal=formal,
        request=request,
        store=store,
        logs_root=logs_root,
        store_manifest=store_manifest,
        preflight=preflight,
        uri=args.uri,
        image_digest=image_digest,
    )
    driver = driver_binding(
        args.expected_driver_version,
        args.expected_driver_tree_sha256,
    )
    external = request["external_service"]
    container_name = external["containers"][0]
    container_inspect_target = container_name
    if launch is not None:
        container_inspect_target = launch["receipt"]["docker"]["running"]["config"]["container_id"]
    container_before = validate_container(
        container_inspect_target,
        container_name,
        args.expected_image_ref,
        image_digest,
        store,
        logs_root,
        args.uri,
        formal,
        memory_contract,
        expected_entrypoint,
        expected_command,
    )
    validate_container_against_launch(container_before, launch)
    jvm_before = jvm_process_contract(container_before["pid"])

    git: dict[str, Any] | None = None
    p02b: dict[str, Any] | None = None
    if formal:
        require(args.repo_root is not None, "formal Neo4j adapter requires --repo-root")
        git = git_state(args.repo_root.resolve())
        require(git["clean"], "formal Neo4j adapter requires a clean Git worktree")
        p02b = validate_p02b(args, git, request, dataset)

    query_driver = None
    try:
        query_driver, server_agent, readiness = open_ready_driver(
            args.uri,
            args.expected_server_agent,
            args.readiness_timeout_s,
        )
        database_contract = verify_database_contract(query_driver, args.database)
        runtime_contract = verify_live_runtime_contract(
            query_driver,
            args.database,
            memory_contract,
            truth_rows[0],
        )
        current_host = p31_host_facts()
        runtime_contract["host"] = {
            "hostname": current_host["hostname"],
            "fingerprint_sha256": current_host["fingerprint_sha256"],
            "mem_total_bytes": current_host["mem_total_bytes"],
        }
        observations, phases = execute_queries(
            request, truth_rows, query_driver, args.database
        )
    finally:
        if query_driver is not None:
            query_driver.close()
    container_after = validate_container(
        container_inspect_target,
        container_name,
        args.expected_image_ref,
        image_digest,
        store,
        logs_root,
        args.uri,
        formal,
        memory_contract,
        expected_entrypoint,
        expected_command,
    )
    validate_container_against_launch(container_after, launch)
    jvm_after = jvm_process_contract(container_after["pid"])
    require(jvm_before == jvm_after, "Neo4j JVM PID/argv changed during the repeat")
    container_lifecycle = validate_container_stability(container_before, container_after)
    write_observations(output_dir / "query-observations.tsv", observations)
    write_phase_events(output_dir / "phase-events.jsonl", phases)
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "system_id": "neo4j",
        "group": "client-server",
        "system_version": request["system_version"],
        "interface_scope": INTERFACE_SCOPE,
        "repeat_index": request["repeat_index"],
        "process_lifetime": EXTERNAL_PROCESS_LIFETIME,
        "truth_sha256": request["truth"]["sha256"],
        "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "concurrency": 1,
        "per_query_timeout_ms": request["timing"]["per_query_timeout_ms"],
        "warmup": phases["warmup"],
        "measured": phases["measured"],
    }
    atomic_json(output_dir / "adapter-result.json", result)
    provenance = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "performance_eligible": formal,
        "execution_mode": args.mode,
        "group": "client-server",
        "system_version": request["system_version"],
        "process_lifetime": EXTERNAL_PROCESS_LIFETIME,
        "request": request_ref,
        "repo": git,
        "p02b": p02b,
        "python_binary": request["binary"],
        "python_driver": driver,
        "server_agent": server_agent,
        "launch": launch,
        "container_lifecycle": container_lifecycle,
        "readiness": readiness,
        "database_contract": database_contract,
        "runtime_contract": runtime_contract,
        "jvm_lifecycle": {"stable": True, "before": jvm_before, "after": jvm_after},
        "dataset_input": request["dataset"],
        "dataset": dataset,
        "truth": artifact_ref(Path(request["truth"]["path"])),
        "store": {**store_manifest, "preflight": preflight},
        "query_contract": {
            "cypher_shape": "MATCH (s:V {id: $src})-[:E_{P|N}<type>]->(d:V) RETURN d.id AS dst",
            "relationship_model": RELATIONSHIP_MODEL,
            "database_name": DATABASE_NAME,
            "required_index": database_contract["required_index"],
            "clock": CLOCK_NAME,
            "timing_boundary": TIMING_BOUNDARY,
            "warmup_before_measured": True,
            "process_reuse_between_phases": True,
            "concurrency": 1,
        },
    }
    atomic_json(output_dir / "adapter-provenance.json", provenance)
    validate_adapter_outputs(
        output_dir=output_dir,
        request=request,
        system={
            "id": "neo4j",
            "group": "client-server",
            "system_version": request["system_version"],
            "display_name": "Neo4j Community",
            "fixture_only": not formal,
        },
        truth_rows=truth_rows,
        max_timeouts=0,
    )


def main() -> int:
    args = parse_args()
    try:
        run(args)
        return 0
    except (
        ContractError,
        OSError,
        UnicodeError,
        ValueError,
        subprocess.SubprocessError,
        importlib.metadata.PackageNotFoundError,
    ) as exc:
        print(f"neo4j_adapter: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
