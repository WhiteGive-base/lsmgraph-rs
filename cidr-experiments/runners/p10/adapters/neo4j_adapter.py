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
import subprocess
import sys
import time
from dataclasses import dataclass
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
)

P31_DIR = P10_DIR.parent / "p31"
sys.path.insert(0, str(P31_DIR))
from run_manifest import host_facts as p31_host_facts  # noqa: E402

PROVENANCE_SCHEMA_VERSION = "p10-neo4j-adapter-provenance-v2"
STORE_MANIFEST_SCHEMA_VERSION = "p10-neo4j-store-manifest-v2"
TREE_HASH_METHOD = "sha256-tree-v1(relative-path,size,file-sha256)"
RELATIONSHIP_MODEL = "dense-edge-type-as-outgoing-relationship-type-v1"
SNAPSHOT_PHASE = "offline-prestart-v1"
DATABASE_NAME = "neo4j"
INDEX_NAME = "v_id"
NODE_LABEL = "V"
ID_PROPERTY = "id"
EXPECTED_IMAGE_REF = "neo4j:5.26.24"
EXPECTED_DRIVER_VERSION = "5.28.3"
EXPECTED_SERVER_AGENT = "Neo4j/5.26.24"
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
    parser.add_argument("--expected-server-agent", default=EXPECTED_SERVER_AGENT)
    parser.add_argument("--store-label", default="neo4j-runtime")
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--dataset-manifest-sha256", required=True)
    parser.add_argument("--store-manifest", required=True, type=Path)
    parser.add_argument("--store-manifest-sha256", required=True)
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
    if dataset_path.is_file():
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
) -> dict[str, Any]:
    reference = resolve_file(path, expected_sha, "Neo4j store manifest")
    required = (
        "schema_version",
        "store_root",
        "store_sha256",
        "hash_method",
        "file_count",
        "total_bytes",
        "snapshot_phase",
        "mutable_runtime_paths",
        "runtime_compatibility",
        "import_image_identity",
        "database_contract",
        "dataset_manifest_sha256",
        "dataset_sha256",
        "truth_sha256",
        "relationship_model",
        "sentinel_files",
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
    integer(manifest["file_count"], "Neo4j store manifest.file_count", 1)
    integer(manifest["total_bytes"], "Neo4j store manifest.total_bytes", 1)
    mutable_paths = manifest["mutable_runtime_paths"]
    require(
        mutable_paths == ["logs/**", "server_id", "transactions/**"],
        "Neo4j store manifest.mutable_runtime_paths mismatch",
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
    import_identity = require_keys(
        manifest["import_image_identity"],
        required=("status", "image_ref", "image_digest"),
        allowed=("status", "image_ref", "image_digest"),
        context="Neo4j store manifest.import_image_identity",
    )
    require(import_identity["image_ref"] == expected_image_ref, "Neo4j import image ref mismatch")
    require(
        import_identity["status"] in {"verified-repodigest", "unverified-tag-only"},
        "Neo4j import image identity has unknown status",
    )
    if import_identity["status"] == "verified-repodigest":
        require(
            import_identity["image_digest"] == image_digest,
            "Neo4j verified import image digest differs from runtime digest",
        )
    else:
        require(
            import_identity["image_digest"] is None,
            "Neo4j unverified import identity must not claim a digest",
        )
        require(
            not formal,
            "formal Neo4j runs require a verified import image RepoDigest",
        )
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
    sentinels = manifest["sentinel_files"]
    require(isinstance(sentinels, list) and sentinels, "Neo4j store manifest requires sentinel_files")
    validated: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, raw in enumerate(sentinels):
        context = f"Neo4j store manifest.sentinel_files[{index}]"
        item = require_keys(
            raw,
            required=("path", "size_bytes", "sha256"),
            allowed=("path", "size_bytes", "sha256"),
            context=context,
        )
        relative = Path(nonempty_string(item["path"], f"{context}.path"))
        require(not relative.is_absolute() and ".." not in relative.parts, f"{context}.path escapes store root")
        relative_text = relative.as_posix()
        require(relative_text not in names, f"{context}.path is duplicated")
        names.add(relative_text)
        target = (store / relative).resolve()
        require(store == target or store in target.parents, f"{context}.path escapes store root")
        reference_item = resolve_file(target, item["sha256"], context)
        expected_size = integer(item["size_bytes"], f"{context}.size_bytes", 1)
        require(reference_item["size_bytes"] == expected_size, f"{context}.size_bytes mismatch")
        validated.append(
            {
                "path": relative_text,
                "absolute_path": reference_item["path"],
                "sha256": reference_item["sha256"],
                "size_bytes": reference_item["size_bytes"],
            }
        )
    return {"reference": reference, "lineage": manifest, "validated_sentinels": validated}


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


def validate_container(
    name: str,
    expected_image_ref: str,
    expected_image_digest: str,
    store: Path,
    uri: str,
    formal: bool,
) -> dict[str, Any]:
    parsed = urlparse(uri)
    require(parsed.scheme in ("bolt", "neo4j"), "Neo4j URI scheme must be bolt or neo4j")
    require(parsed.hostname in ("127.0.0.1", "localhost"), "Neo4j URI must use localhost")
    require(parsed.port is not None, "Neo4j URI must contain an explicit port")
    raw_container = run_json(["docker", "container", "inspect", name], "Docker container inspect")
    require(isinstance(raw_container, list) and len(raw_container) == 1, "Docker returned an ambiguous container")
    container = raw_container[0]
    require(container.get("Name") == f"/{name}", "Docker container name mismatch")
    state = container.get("State", {})
    require(state.get("Running") is True, "Neo4j container is not running")
    pid = integer(state.get("Pid"), "Neo4j container PID", 1)
    started_at = nonempty_string(state.get("StartedAt"), "Neo4j container StartedAt")
    restart_count = integer(container.get("RestartCount"), "Neo4j container RestartCount", 0)
    require(restart_count == 0, "Neo4j container must have RestartCount=0")
    require(container.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") == "no", "Neo4j container restart policy must be 'no'")
    require(container.get("Config", {}).get("Image") == expected_image_ref, "Neo4j container image tag mismatch")
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

    mounts = [
        mount
        for mount in container.get("Mounts", [])
        if mount.get("Destination") == "/data"
    ]
    require(len(mounts) == 1, "Neo4j container must have exactly one /data mount")
    mount = mounts[0]
    same_path(mount.get("Source"), store, "Neo4j container /data mount")
    require(mount.get("RW") is True, "Neo4j /data mount unexpectedly is not writable")

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

    environment = container.get("Config", {}).get("Env") or []
    require("NEO4J_AUTH=none" in environment, "Neo4j adapter currently requires NEO4J_AUTH=none")
    require(
        "NEO4J_server_databases_default__to__read__only=true" in environment,
        "Neo4j service must default databases to read-only",
    )
    return {
        "name": name,
        "container_id": nonempty_string(container.get("Id"), "Docker container ID"),
        "pid": pid,
        "started_at": started_at,
        "restart_count": restart_count,
        "image_id": image_id,
        "configured_image": expected_image_ref,
        "repo_digests": repo_digests,
        "expected_repo_digest": expected_image_digest,
        "data_mount": str(store),
        "bolt_host": "127.0.0.1",
        "bolt_port": parsed.port,
        "restart_policy": "no",
        "read_only_default": True,
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
        "expected_repo_digest",
        "data_mount",
        "bolt_host",
        "bolt_port",
        "restart_policy",
        "read_only_default",
    )
    for key in stable_keys:
        require(before.get(key) == after.get(key), f"Neo4j container {key} changed during the repeat")
    require(before.get("restart_count") == 0, "Neo4j container restarted before the repeat")
    require(after.get("restart_count") == 0, "Neo4j container restarted during the repeat")
    return {"stable": True, "before": before, "after": after}


def driver_binding(expected_version: str) -> dict[str, Any]:
    actual_version = importlib.metadata.version("neo4j")
    require(actual_version == expected_version, f"Neo4j Python driver {actual_version!r} != {expected_version!r}")
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
    return {
        "version": actual_version,
        "package_tree_sha256": digest.hexdigest(),
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
        require(
            args.expected_server_agent == EXPECTED_SERVER_AGENT,
            f"formal Neo4j server agent must be {EXPECTED_SERVER_AGENT!r}",
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
    require(
        output_dir != store and store not in output_dir.parents and output_dir not in store.parents,
        "Neo4j adapter output and store roots must not overlap",
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
    )
    driver = driver_binding(args.expected_driver_version)
    external = request["external_service"]
    container_before = validate_container(
        external["containers"][0],
        args.expected_image_ref,
        image_digest,
        store,
        args.uri,
        formal,
    )

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
        observations, phases = execute_queries(
            request, truth_rows, query_driver, args.database
        )
    finally:
        if query_driver is not None:
            query_driver.close()
    container_after = validate_container(
        external["containers"][0],
        args.expected_image_ref,
        image_digest,
        store,
        args.uri,
        formal,
    )
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
        "container_lifecycle": container_lifecycle,
        "readiness": readiness,
        "database_contract": database_contract,
        "dataset_input": request["dataset"],
        "dataset": dataset,
        "truth": artifact_ref(Path(request["truth"]["path"])),
        "store": store_manifest,
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
