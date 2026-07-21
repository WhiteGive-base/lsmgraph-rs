#!/usr/bin/env python3
"""Fail-closed NebulaGraph adapter for the CIDR P10 typed-neighbor contract.

Fixture mode owns one uniquely named three-container cluster and removes it at
the end of the run.  Formal mode only attaches to an already running cluster;
it never starts, restarts, stops, or mutates its frozen store lineage.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence


P10_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(P10_DIR))

from p10_contract import (  # noqa: E402
    CLOCK_NAME,
    CONTRACT_VERSION,
    EXTERNAL_PROCESS_LIFETIME,
    FIXTURE_PROCESS_LIFETIME,
    INTERFACE_SCOPE,
    OBSERVATION_COLUMNS,
    REQUEST_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    TRUTH_DIGEST_ALGORITHM,
    ContractError,
    atomic_json,
    phase_digest,
    read_truth,
    sha256_file,
)


RUNTIME_SCHEMA = "cidr-p10-nebulagraph-runtime-v1"
STORE_SCHEMA = "cidr-p10-nebulagraph-store-v1"
PROVENANCE_SCHEMA = "p10-nebulagraph-adapter-provenance-v1"
TREE_HASH_METHOD = "sha256-tree-v1(relative-path,size,file-sha256)"
FORMAL_TRUTH_SHA256 = "876ec4be45bb7c220ce595b8d8efd5c79d13db285569cc0d6ae3e8bc19a1c788"
FORMAL_QUERY_COUNT = 1700
MASK = (1 << 64) - 1
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
EDGE_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EXPECTED_IMAGES = {
    "graphd": {
        "tag": "vesoft/nebula-graphd:v3.8.0",
        "digest": "sha256:1040573cc684ea6cc5e673b667422e9abab607c9d553c2c17c7bac6ad8a74e05",
    },
    "metad": {
        "tag": "vesoft/nebula-metad:v3.8.0",
        "digest": "sha256:ab687bd32d3e441d436842b41427ba0961a5e169c46997ef03fa4dcfd5388b35",
    },
    "storaged": {
        "tag": "vesoft/nebula-storaged:v3.8.0",
        "digest": "sha256:7142642ee69001a5b5c50520196538d58bb2ec3930707c067cb4c4e2be3890f9",
    },
}


def unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_strict_json(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except ContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{context}: cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{context}: top level must be an object")
    return value


def exact_keys(value: object, keys: Iterable[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{context}: expected object")
    expected = set(keys)
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        raise ContractError(f"{context}: key drift missing={missing}, unknown={unknown}")
    return value


def integer(value: object, context: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{context}: expected integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{context}: expected >= {minimum}")
    return value


def nonempty(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{context}: expected non-empty string")
    return value


def exact_sha(value: object, context: str) -> str:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        raise ContractError(f"{context}: expected lowercase SHA-256")
    return value


def canonical_path(value: object, context: str, *, file: bool = False, directory: bool = False) -> Path:
    raw = nonempty(value, context)
    if not Path(raw).is_absolute():
        raise ContractError(f"{context}: path must be absolute")
    path = Path(raw).resolve()
    if str(path) != raw:
        raise ContractError(f"{context}: path must be canonical")
    if file and not path.is_file():
        raise ContractError(f"{context}: file does not exist: {path}")
    if directory and not path.is_dir():
        raise ContractError(f"{context}: directory does not exist: {path}")
    return path


def checked_file(path: Path | None, expected_sha: str | None, context: str, *, executable: bool = False) -> Path:
    if path is None or expected_sha is None:
        raise ContractError(f"{context}: path and SHA-256 are required")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ContractError(f"{context}: file does not exist: {resolved}")
    if executable and not os.access(resolved, os.X_OK):
        raise ContractError(f"{context}: file is not executable")
    if sha256_file(resolved) != exact_sha(expected_sha, f"{context}.sha256"):
        raise ContractError(f"{context}: SHA-256 mismatch")
    return resolved


def tree_identity(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    total = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        file_sha = sha256_file(path)
        digest.update(f"{relative}\t{size}\t{file_sha}\n".encode())
        count += 1
        total += size
    return {
        "hash_method": TREE_HASH_METHOD,
        "sha256": digest.hexdigest(),
        "file_count": count,
        "total_bytes": total,
    }


def validate_tree_ref(value: object, context: str, *, recompute: bool) -> tuple[Path, dict[str, Any]]:
    item = exact_keys(value, ("path", "hash_method", "sha256", "file_count", "total_bytes"), context)
    path = canonical_path(item["path"], f"{context}.path", directory=True)
    if item["hash_method"] != TREE_HASH_METHOD:
        raise ContractError(f"{context}.hash_method drift")
    exact_sha(item["sha256"], f"{context}.sha256")
    integer(item["file_count"], f"{context}.file_count", 0)
    integer(item["total_bytes"], f"{context}.total_bytes", 0)
    if recompute:
        actual = tree_identity(path)
        for key in ("hash_method", "sha256", "file_count", "total_bytes"):
            if item[key] != actual[key]:
                raise ContractError(f"{context}.{key}: tree identity mismatch")
    return path, item


def validate_file_ref(value: object, context: str) -> Path:
    item = exact_keys(value, ("path", "sha256"), context)
    path = canonical_path(item["path"], f"{context}.path", file=True)
    if sha256_file(path) != exact_sha(item["sha256"], f"{context}.sha256"):
        raise ContractError(f"{context}: SHA-256 mismatch")
    return path


def validate_request(path: Path, store_label: str) -> tuple[dict[str, Any], list[dict[str, int]], Path, Path, Path]:
    request = exact_keys(
        read_strict_json(path, "NebulaGraph request"),
        (
            "schema_version", "contract_version", "suite_id", "run_id", "execution_mode",
            "system_id", "group", "system_version", "interface_scope", "repeat_index",
            "process_lifetime", "binary", "dataset", "runtime_libraries", "store_roots",
            "truth", "timing",
        ),
        "NebulaGraph request",
    )
    exact_values = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "system_id": "nebulagraph",
        "group": "client-server",
        "interface_scope": INTERFACE_SCOPE,
    }
    for key, expected in exact_values.items():
        if request[key] != expected:
            raise ContractError(f"request.{key}: {request[key]!r} != {expected!r}")
    mode = request["execution_mode"]
    if mode not in ("fixture", "formal"):
        raise ContractError("request.execution_mode must be fixture or formal")
    expected_lifetime = EXTERNAL_PROCESS_LIFETIME if mode == "formal" else FIXTURE_PROCESS_LIFETIME
    if request["process_lifetime"] != expected_lifetime:
        raise ContractError(f"request.process_lifetime must be {expected_lifetime!r}")
    if request["system_version"] != "NebulaGraph 3.8.0":
        raise ContractError("request.system_version must be 'NebulaGraph 3.8.0'")
    integer(request["repeat_index"], "request.repeat_index", 1)
    binary = validate_file_ref(request["binary"], "request.binary")
    if binary != Path(sys.executable).resolve():
        raise ContractError("request.binary must be the Python interpreter running the adapter")
    dataset = validate_file_ref(request["dataset"], "request.dataset")
    raw_libraries = request["runtime_libraries"]
    if not isinstance(raw_libraries, list):
        raise ContractError("request.runtime_libraries must be an array")
    for index, library in enumerate(raw_libraries):
        validate_file_ref(library, f"request.runtime_libraries[{index}]")
    stores = request["store_roots"]
    if not isinstance(stores, list) or not stores:
        raise ContractError("request.store_roots must be a non-empty array")
    selected: Path | None = None
    for index, raw in enumerate(stores):
        item = exact_keys(raw, ("label", "path", "sha256"), f"request.store_roots[{index}]")
        root = canonical_path(item["path"], f"request.store_roots[{index}].path", directory=True)
        if item["sha256"] not in ("", None):
            exact_sha(item["sha256"], f"request.store_roots[{index}].sha256")
        if item["label"] == store_label:
            selected = root
    if selected is None:
        raise ContractError(f"request.store_roots has no {store_label!r} label")
    truth_obj = exact_keys(request["truth"], ("path", "sha256", "query_count", "digest_algorithm"), "request.truth")
    truth = canonical_path(truth_obj["path"], "request.truth.path", file=True)
    if sha256_file(truth) != exact_sha(truth_obj["sha256"], "request.truth.sha256"):
        raise ContractError("request.truth SHA-256 mismatch")
    count = integer(truth_obj["query_count"], "request.truth.query_count", 1)
    if truth_obj["digest_algorithm"] != TRUTH_DIGEST_ALGORITHM:
        raise ContractError("request.truth.digest_algorithm drift")
    truth_rows = read_truth(truth, count)
    if mode == "formal" and (count != FORMAL_QUERY_COUNT or truth_obj["sha256"] != FORMAL_TRUTH_SHA256):
        raise ContractError("formal NebulaGraph requires the canonical 1700-query SF10 truth")
    timing = exact_keys(
        request["timing"],
        (
            "timing_boundary", "clock", "cache_policy", "process_reuse_between_phases",
            "warmup_passes", "measured_passes", "concurrency", "per_query_timeout_ms",
            "sequence_digest_algorithm",
        ),
        "request.timing",
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
        if timing[key] != expected:
            raise ContractError(f"request.timing.{key} drift")
    for key in ("warmup_passes", "measured_passes", "per_query_timeout_ms"):
        integer(timing[key], f"request.timing.{key}", 1)
    return request, truth_rows, dataset, truth, selected


def docker_json(docker: Path, image: str) -> dict[str, Any]:
    completed = subprocess.run(
        [str(docker), "image", "inspect", image], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=30, check=False,
    )
    if completed.returncode != 0:
        raise ContractError(f"docker image inspect failed for {image}: {completed.stderr.strip()}")
    try:
        values = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"docker image inspect returned malformed JSON for {image}") from exc
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise ContractError(f"docker image inspect returned an unexpected result for {image}")
    return values[0]


def validate_runtime_manifest(path: Path, request: dict[str, Any]) -> tuple[dict[str, Any], Path, Path]:
    value = exact_keys(
        read_strict_json(path, "NebulaGraph runtime manifest"),
        ("schema_version", "system_version", "client", "docker", "images"),
        "NebulaGraph runtime manifest",
    )
    if value["schema_version"] != RUNTIME_SCHEMA or value["system_version"] != request["system_version"]:
        raise ContractError("NebulaGraph runtime manifest schema/system version drift")
    client = exact_keys(value["client"], ("version", "tree", "metadata"), "runtime.client")
    if client["version"] != "3.8.3":
        raise ContractError("NebulaGraph Python client version must be 3.8.3")
    client_root, _ = validate_tree_ref(client["tree"], "runtime.client.tree", recompute=True)
    validate_file_ref(client["metadata"], "runtime.client.metadata")
    docker_obj = exact_keys(value["docker"], ("path", "sha256", "version"), "runtime.docker")
    docker = canonical_path(docker_obj["path"], "runtime.docker.path", file=True)
    if not os.access(docker, os.X_OK) or sha256_file(docker) != exact_sha(docker_obj["sha256"], "runtime.docker.sha256"):
        raise ContractError("runtime Docker client path/SHA is invalid")
    nonempty(docker_obj["version"], "runtime.docker.version")
    raw_images = value["images"]
    if not isinstance(raw_images, list) or len(raw_images) != 3:
        raise ContractError("runtime.images must contain exactly graphd/metad/storaged")
    seen: set[str] = set()
    for index, raw in enumerate(raw_images):
        image = exact_keys(raw, ("role", "tag", "repo_digest", "image_id"), f"runtime.images[{index}]")
        role = nonempty(image["role"], f"runtime.images[{index}].role")
        if role in seen or role not in EXPECTED_IMAGES:
            raise ContractError("runtime image role is missing, duplicate, or unknown")
        seen.add(role)
        expected = EXPECTED_IMAGES[role]
        if image["tag"] != expected["tag"]:
            raise ContractError(f"runtime {role} tag drift")
        repo_digest = nonempty(image["repo_digest"], f"runtime.images[{index}].repo_digest")
        if repo_digest != f"{expected['tag'].split(':', 1)[0]}@{expected['digest']}":
            raise ContractError(f"runtime {role} repository digest drift")
        if not IMAGE_RE.fullmatch(nonempty(image["image_id"], f"runtime.images[{index}].image_id")):
            raise ContractError(f"runtime {role} image ID is malformed")
        actual = docker_json(docker, image["tag"])
        if actual.get("Id") != image["image_id"] or repo_digest not in actual.get("RepoDigests", []):
            raise ContractError(f"local Docker image identity differs for {role}")
    if seen != set(EXPECTED_IMAGES):
        raise ContractError("runtime image role coverage drift")
    return value, client_root, docker


def validate_store_manifest(
    path: Path,
    request: dict[str, Any],
    dataset: Path,
    truth: Path,
    store: Path,
) -> tuple[dict[str, Any], dict[int, str]]:
    value = exact_keys(
        read_strict_json(path, "NebulaGraph store manifest"),
        (
            "schema_version", "system_version", "formal_eligible", "lineage_stage", "data_root",
            "dataset", "truth", "space", "graph_endpoint", "authentication", "edge_type_labels",
            "containers", "network", "import_provenance",
        ),
        "NebulaGraph store manifest",
    )
    if value["schema_version"] != STORE_SCHEMA or value["system_version"] != request["system_version"]:
        raise ContractError("NebulaGraph store manifest schema/system version drift")
    formal = request["execution_mode"] == "formal"
    if not isinstance(value["formal_eligible"], bool) or (formal and value["formal_eligible"] is not True):
        raise ContractError("formal NebulaGraph requires a formal-eligible store manifest")
    expected_stage = "pre-service-start-frozen-copy-v1" if formal else "empty-fixture-root-v1"
    if value["lineage_stage"] != expected_stage:
        raise ContractError(f"store.lineage_stage must be {expected_stage!r}")
    data_root, lineage = validate_tree_ref(value["data_root"], "store.data_root", recompute=not formal)
    if data_root != store:
        raise ContractError("store manifest data root differs from P10 request")
    requested = next(item for item in request["store_roots"] if item["label"] == "nebulagraph")
    if formal and requested["sha256"] != lineage["sha256"]:
        raise ContractError("formal request store lineage differs from store manifest")
    dataset_ref = exact_keys(value["dataset"], ("path", "sha256"), "store.dataset")
    if canonical_path(dataset_ref["path"], "store.dataset.path", file=True) != dataset:
        raise ContractError("store dataset path differs from request")
    if exact_sha(dataset_ref["sha256"], "store.dataset.sha256") != request["dataset"]["sha256"]:
        raise ContractError("store dataset SHA differs from request")
    truth_ref = exact_keys(value["truth"], ("path", "sha256", "query_count"), "store.truth")
    if canonical_path(truth_ref["path"], "store.truth.path", file=True) != truth:
        raise ContractError("store truth path differs from request")
    if exact_sha(truth_ref["sha256"], "store.truth.sha256") != request["truth"]["sha256"]:
        raise ContractError("store truth SHA differs from request")
    if integer(truth_ref["query_count"], "store.truth.query_count", 1) != request["truth"]["query_count"]:
        raise ContractError("store truth query count differs from request")
    space = nonempty(value["space"], "store.space")
    if not EDGE_LABEL_RE.fullmatch(space):
        raise ContractError("store.space is not a safe nGQL identifier")
    endpoint = exact_keys(value["graph_endpoint"], ("host", "port"), "store.graph_endpoint")
    host = nonempty(endpoint["host"], "store.graph_endpoint.host")
    if formal and host not in ("127.0.0.1", "localhost"):
        raise ContractError("formal graph endpoint must be localhost")
    port = integer(endpoint["port"], "store.graph_endpoint.port", 0)
    if formal and port == 0:
        raise ContractError("formal graph endpoint port cannot be zero")
    auth = exact_keys(value["authentication"], ("user", "password_env", "password_sha256"), "store.authentication")
    nonempty(auth["user"], "store.authentication.user")
    nonempty(auth["password_env"], "store.authentication.password_env")
    exact_sha(auth["password_sha256"], "store.authentication.password_sha256")
    labels: dict[int, str] = {}
    raw_labels = value["edge_type_labels"]
    if not isinstance(raw_labels, list) or not raw_labels:
        raise ContractError("store.edge_type_labels must be non-empty")
    for index, raw in enumerate(raw_labels):
        item = exact_keys(raw, ("edge_type", "label"), f"store.edge_type_labels[{index}]")
        edge_type = integer(item["edge_type"], f"store.edge_type_labels[{index}].edge_type")
        label = nonempty(item["label"], f"store.edge_type_labels[{index}].label")
        if edge_type in labels or not EDGE_LABEL_RE.fullmatch(label):
            raise ContractError("store edge-type map has duplicate/unsafe entry")
        labels[edge_type] = label
    truth_types = {row["edge_type"] for row in read_truth(truth, request["truth"]["query_count"])}
    if set(labels) != truth_types:
        raise ContractError("store edge-type map does not exactly cover truth")
    containers = exact_keys(value["containers"], ("metad", "storaged", "graphd"), "store.containers")
    names = list(containers.values())
    if any(not isinstance(name, str) or not NAME_RE.fullmatch(name) for name in names) or len(set(names)) != 3:
        raise ContractError("store container names are invalid or duplicate")
    network = nonempty(value["network"], "store.network")
    if not NAME_RE.fullmatch(network) or network in names:
        raise ContractError("store network name is invalid")
    provenance = exact_keys(value["import_provenance"], ("kind", "source", "result"), "store.import_provenance")
    nonempty(provenance["kind"], "store.import_provenance.kind")
    for key in ("source", "result"):
        if provenance[key] is not None:
            validate_file_ref(provenance[key], f"store.import_provenance.{key}")
    return value, labels


def consume_p02b(args: argparse.Namespace, formal: bool) -> dict[str, Any] | None:
    supplied = (args.p02b_result, args.p02b_validator, args.p02b_validator_sha256)
    if not formal:
        if any(item is not None for item in supplied):
            raise ContractError("fixture NebulaGraph run must not claim a P02B formal admission")
        return None
    if any(item is None for item in supplied):
        raise ContractError("formal NebulaGraph run requires P02B result/validator/SHA-256")
    validator = checked_file(args.p02b_validator, args.p02b_validator_sha256, "P02B validator", executable=True)
    result = args.p02b_result.resolve()
    if not result.is_file():
        raise ContractError("P02B result does not exist")
    completed = subprocess.run(
        [sys.executable, "-B", str(validator), "--result", str(result), "--consumer", "P10", "--require-formal"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
    )
    if completed.returncode != 0:
        raise ContractError("P02B formal admission rejected NebulaGraph P10: " + completed.stderr.strip())
    return {
        "state": "PASS",
        "formal_required": True,
        "consumer": "P10",
        "result": {"path": str(result), "sha256": sha256_file(result)},
        "validator": {"path": str(validator), "sha256": sha256_file(validator)},
    }


def docker_call(docker: Path, args: list[str], *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(docker), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    if check and completed.returncode != 0:
        raise ContractError(f"docker {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed


def inspect_cluster(
    docker: Path,
    runtime: dict[str, Any],
    store_manifest: dict[str, Any],
    graph_port: int,
) -> list[dict[str, Any]]:
    expected_images = {item["role"]: item for item in runtime["images"]}
    records: list[dict[str, Any]] = []
    for role in ("graphd", "metad", "storaged"):
        name = store_manifest["containers"][role]
        completed = docker_call(docker, ["container", "inspect", name], check=False)
        if completed.returncode != 0:
            raise ContractError(f"required NebulaGraph container is absent: {name}")
        try:
            values = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ContractError(f"container inspect is malformed for {name}") from exc
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise ContractError(f"container inspect shape is invalid for {name}")
        value = values[0]
        state = value.get("State")
        if not isinstance(state, dict) or state.get("Running") is not True:
            raise ContractError(f"NebulaGraph container is not running: {name}")
        image_id = expected_images[role]["image_id"]
        if value.get("Image") != image_id:
            raise ContractError(f"NebulaGraph container image differs for {name}")
        networks = value.get("NetworkSettings", {}).get("Networks", {})
        if store_manifest["network"] not in networks:
            raise ContractError(f"NebulaGraph container is outside the frozen network: {name}")
        restart_count = integer(value.get("RestartCount"), f"container {name}.RestartCount", 0)
        if restart_count != 0:
            raise ContractError(f"NebulaGraph container has restarted: {name}")
        pid = integer(state.get("Pid"), f"container {name}.State.Pid", 1)
        if role == "graphd":
            bindings = value.get("NetworkSettings", {}).get("Ports", {}).get("9669/tcp")
            if not isinstance(bindings, list) or len(bindings) != 1 or not isinstance(bindings[0], dict):
                raise ContractError("graphd must publish exactly one localhost port")
            if bindings[0].get("HostIp") != "127.0.0.1" or bindings[0].get("HostPort") != str(graph_port):
                raise ContractError("graphd published endpoint differs from frozen localhost endpoint")
        records.append(
            {
                "role": role,
                "name": name,
                "container_id": value.get("Id"),
                "image_id": image_id,
                "pid": pid,
                "restart_count": restart_count,
            }
        )
    return records


class ManagedFixtureCluster:
    def __init__(self, docker: Path, runtime: dict[str, Any], store_manifest: dict[str, Any], data_root: Path):
        self.docker = docker
        self.images = {item["role"]: item for item in runtime["images"]}
        self.names = store_manifest["containers"]
        self.network = store_manifest["network"]
        self.data_root = data_root
        self.instance_root = data_root / f"fixture-{self.network}"
        self.port = 0

    def _absent(self, kind: str, name: str) -> None:
        probe = docker_call(self.docker, [kind, "inspect", name], check=False)
        if probe.returncode == 0:
            raise ContractError(f"managed fixture refuses to reuse existing Docker {kind}: {name}")

    def start(self) -> int:
        for name in self.names.values():
            self._absent("container", name)
        self._absent("network", self.network)
        if self.instance_root.exists():
            raise ContractError("managed fixture instance root already exists")
        for sub in ("meta", "storage", "graph", "logs"):
            (self.instance_root / sub).mkdir(parents=True, exist_ok=False)
        docker_call(self.docker, ["network", "create", self.network])
        meta = self.names["metad"]
        storage = self.names["storaged"]
        graph = self.names["graphd"]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = int(reservation.getsockname()[1])
        docker_call(self.docker, [
            "run", "-d", "--name", meta, "--network", self.network,
            "-v", f"{self.instance_root / 'meta'}:/data/meta",
            "-v", f"{self.instance_root / 'logs'}:/logs",
            self.images["metad"]["repo_digest"],
            f"--meta_server_addrs={meta}:9559", f"--local_ip={meta}", "--ws_ip=0.0.0.0",
            "--port=9559", "--ws_http_port=19559", "--data_path=/data/meta", "--log_dir=/logs",
        ])
        time.sleep(3)
        docker_call(self.docker, [
            "run", "-d", "--name", storage, "--network", self.network,
            "-v", f"{self.instance_root / 'storage'}:/data/storage",
            "-v", f"{self.instance_root / 'logs'}:/logs",
            self.images["storaged"]["repo_digest"],
            f"--meta_server_addrs={meta}:9559", f"--local_ip={storage}", "--ws_ip=0.0.0.0",
            "--port=9779", "--ws_http_port=19779", "--data_path=/data/storage", "--log_dir=/logs",
        ])
        docker_call(self.docker, [
            "run", "-d", "--name", graph, "--network", self.network,
            "-p", f"127.0.0.1:{self.port}:9669",
            "-v", f"{self.instance_root / 'graph'}:/data/graph",
            "-v", f"{self.instance_root / 'logs'}:/logs",
            self.images["graphd"]["repo_digest"],
            f"--meta_server_addrs={meta}:9559", f"--local_ip={graph}", "--ws_ip=0.0.0.0",
            "--port=9669", "--ws_http_port=19669", "--log_dir=/logs",
        ])
        deadline = time.monotonic() + 30
        last_socket_error: OSError | None = None
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return self.port
            except OSError as exc:
                last_socket_error = exc
            state = docker_call(
                self.docker,
                ["inspect", "-f", "{{.State.Status}}|{{.State.ExitCode}}|{{.State.Error}}", graph],
                check=False,
            ).stdout.strip()
            if state.startswith(("exited|", "dead|")):
                break
            time.sleep(1)
        diagnostics = []
        for name in (meta, storage, graph):
            state = docker_call(
                self.docker,
                ["inspect", "-f", "{{.State.Status}}|{{.State.ExitCode}}|{{.State.Error}}", name],
                check=False,
            ).stdout.strip()
            log_result = docker_call(self.docker, ["logs", "--tail", "40", name], check=False)
            logs = (log_result.stdout + log_result.stderr).strip()
            diagnostics.append(f"{name} state={state!r} logs={logs!r}")
        raise ContractError(
            f"managed graphd did not listen on localhost:{self.port}: {last_socket_error}; "
            + "; ".join(diagnostics)
        )

    def cleanup(self) -> None:
        errors: list[str] = []
        for name in (self.names["graphd"], self.names["storaged"], self.names["metad"]):
            completed = docker_call(self.docker, ["rm", "-f", name], check=False)
            if completed.returncode not in (0, 1):
                errors.append(f"container {name}: {completed.stderr.strip()}")
        completed = docker_call(self.docker, ["network", "rm", self.network], check=False)
        if completed.returncode not in (0, 1):
            errors.append(f"network {self.network}: {completed.stderr.strip()}")
        if self.instance_root.exists():
            uid = os.getuid()
            gid = os.getgid()
            docker_call(
                self.docker,
                ["run", "--rm", "--network", "none", "--entrypoint", "sh", "-v", f"{self.instance_root}:/target", self.images["graphd"]["repo_digest"], "-c", f"chown -R {uid}:{gid} /target"],
                check=False,
            )
            shutil.rmtree(self.instance_root)
        if errors:
            raise ContractError("managed fixture cleanup failed: " + "; ".join(errors))


def import_client(client_root: Path) -> str:
    sys.path.insert(0, str(client_root))
    try:
        import nebula3  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise ContractError(f"cannot import frozen nebula3 client: {exc}") from exc
    versions = [dist.version for dist in importlib.metadata.distributions(path=[str(client_root)]) if dist.metadata.get("Name", "").lower() == "nebula3-python"]
    if versions != ["3.8.3"]:
        raise ContractError(f"frozen NebulaGraph client version drift: {versions!r}")
    return versions[0]


def open_session(host: str, port: int, user: str, password: str, timeout_s: int = 180) -> tuple[Any, Any]:
    from nebula3.Config import Config
    from nebula3.gclient.net import ConnectionPool

    deadline = time.monotonic() + timeout_s
    last_error: object = "not attempted"
    while time.monotonic() < deadline:
        pool = None
        session = None
        try:
            config = Config()
            config.max_connection_pool_size = 2
            pool = ConnectionPool()
            if not pool.init([(host, port)], config):
                raise RuntimeError("ConnectionPool.init returned false")
            session = pool.get_session(user, password)
            result = session.execute("SHOW HOSTS")
            if result.is_succeeded():
                return pool, session
            last_error = result.error_msg()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        if session is not None:
            session.release()
        if pool is not None:
            pool.close()
        time.sleep(1)
    raise ContractError(f"NebulaGraph did not become ready: {last_error}")


def execute_ok(session: Any, statement: str) -> Any:
    result = session.execute(statement)
    if not result.is_succeeded():
        raise ContractError(f"nGQL failed: {statement}; {result.error_msg()}")
    return result


def load_fixture(session: Any, manifest: dict[str, Any], dataset: Path, labels: dict[int, str]) -> None:
    storage = manifest["containers"]["storaged"]
    result = session.execute(f'ADD HOSTS "{storage}":9779')
    if not result.is_succeeded() and "exist" not in str(result.error_msg()).lower():
        raise ContractError(f"ADD HOSTS failed: {result.error_msg()}")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        hosts = session.execute("SHOW HOSTS")
        if hosts.is_succeeded() and storage in str(hosts) and "ONLINE" in str(hosts).upper():
            break
        time.sleep(1)
    else:
        raise ContractError("fixture storaged did not become ONLINE")
    space = manifest["space"]
    execute_ok(session, f"DROP SPACE IF EXISTS {space}")
    execute_ok(session, f"CREATE SPACE {space}(partition_num=1, replica_factor=1, vid_type=INT64)")
    time.sleep(10)
    execute_ok(session, f"USE {space}")
    for label in labels.values():
        execute_ok(session, f"CREATE EDGE {label}()")
    time.sleep(10)
    buffers: dict[int, list[tuple[int, int]]] = {edge_type: [] for edge_type in labels}
    with dataset.open("r", encoding="utf-8") as handle:
        first = handle.readline()
        if not first.strip().isdigit():
            raise ContractError("fixture dataset must use dense-edges header format")
        for line_no, line in enumerate(handle, 2):
            fields = line.split()
            if len(fields) != 3:
                raise ContractError(f"fixture dataset line {line_no}: expected src edge_type dst")
            src, edge_type, dst = map(int, fields)
            if edge_type not in labels:
                raise ContractError(f"fixture dataset line {line_no}: edge type outside truth")
            buffers[edge_type].append((src, dst))
    for edge_type, pairs in buffers.items():
        values = ",".join(f"{src}->{dst}:()" for src, dst in pairs)
        if values:
            execute_ok(session, f"INSERT EDGE {labels[edge_type]}() VALUES {values}")
    time.sleep(2)


def mix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & MASK
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK
    return (value ^ (value >> 31)) & MASK


def query_digest(session: Any, row: dict[str, int], label: str) -> tuple[int, int, int]:
    result = session.execute(f"GO FROM {row['src']} OVER {label} YIELD dst(edge) AS dst")
    if not result.is_succeeded():
        raise ContractError(f"typed-neighbor nGQL failed: {result.error_msg()}")
    count = 0
    sum_hash = 0
    xor_hash = 0
    for record in result:
        dst = record.values()[0].as_int()
        count += 1
        sum_hash = (sum_hash + mix64(dst)) & MASK
        xor_hash ^= mix64(dst ^ 0xD6E8FEB86659FD93)
    return count, sum_hash, xor_hash


def write_contract_outputs(
    output_dir: Path,
    request: dict[str, Any],
    truth_rows: list[dict[str, int]],
    labels: dict[int, str],
    session: Any,
) -> None:
    observations: list[dict[str, str]] = []
    summaries: dict[str, dict[str, Any]] = {}
    timeout_ns = request["timing"]["per_query_timeout_ms"] * 1_000_000
    for phase in ("warmup", "measured"):
        passes = request["timing"][f"{phase}_passes"]
        phase_start = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        for pass_index in range(passes):
            for truth in truth_rows:
                started = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
                actual = query_digest(session, truth, labels[truth["edge_type"]])
                latency = time.clock_gettime_ns(time.CLOCK_MONOTONIC) - started
                timeout = latency > timeout_ns
                observations.append({
                    "contract_version": CONTRACT_VERSION,
                    "system_id": "nebulagraph",
                    "group": "client-server",
                    "repeat_index": str(request["repeat_index"]),
                    "phase": phase,
                    "pass_index": str(pass_index),
                    "query_index": str(truth["query_index"]),
                    "edge_type": str(truth["edge_type"]),
                    "src": str(truth["src"]),
                    "expected_count": str(truth["count"]),
                    "actual_count": "" if timeout else str(actual[0]),
                    "expected_sum_hash": str(truth["sum_hash"]),
                    "actual_sum_hash": "" if timeout else str(actual[1]),
                    "expected_xor_hash": str(truth["xor_hash"]),
                    "actual_xor_hash": "" if timeout else str(actual[2]),
                    "status": "timeout" if timeout else "ok",
                    "latency_ns": str(latency),
                })
        phase_end = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        rows = [row for row in observations if row["phase"] == phase]
        summaries[phase] = {
            "passes": passes,
            "requested_queries": len(rows),
            "completed_queries": sum(row["status"] == "ok" for row in rows),
            "timeout_queries": sum(row["status"] == "timeout" for row in rows),
            "mismatch_queries": sum(
                row["status"] == "ok" and (
                    row["actual_count"], row["actual_sum_hash"], row["actual_xor_hash"]
                ) != (row["expected_count"], row["expected_sum_hash"], row["expected_xor_hash"])
                for row in rows
            ),
            "started_monotonic_ns": phase_start,
            "ended_monotonic_ns": phase_end,
            "elapsed_ns": phase_end - phase_start,
            "expected_digest_sha256": phase_digest(phase, passes, truth_rows),
            "actual_digest_sha256": phase_digest(phase, passes, truth_rows, rows),
        }
    observations_tmp = output_dir / "query-observations.tsv.tmp"
    with observations_tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OBSERVATION_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(observations)
    os.replace(observations_tmp, output_dir / "query-observations.tsv")
    events: list[dict[str, Any]] = []
    for phase in ("warmup", "measured"):
        events.extend((
            {"contract_version": CONTRACT_VERSION, "phase": phase, "event": "start", "monotonic_ns": summaries[phase]["started_monotonic_ns"]},
            {"contract_version": CONTRACT_VERSION, "phase": phase, "event": "end", "monotonic_ns": summaries[phase]["ended_monotonic_ns"]},
        ))
    events_tmp = output_dir / "phase-events.jsonl.tmp"
    events_tmp.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in events), encoding="utf-8")
    os.replace(events_tmp, output_dir / "phase-events.jsonl")
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "system_id": "nebulagraph",
        "group": "client-server",
        "system_version": request["system_version"],
        "interface_scope": INTERFACE_SCOPE,
        "repeat_index": request["repeat_index"],
        "process_lifetime": request["process_lifetime"],
        "truth_sha256": request["truth"]["sha256"],
        "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "concurrency": 1,
        "per_query_timeout_ms": request["timing"]["per_query_timeout_ms"],
        "warmup": summaries["warmup"],
        "measured": summaries["measured"],
    }
    atomic_json(output_dir / "adapter-result.json", result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-label", default="nebulagraph")
    parser.add_argument("--service-mode", required=True, choices=("managed-fixture", "external-prestarted"))
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    parser.add_argument("--runtime-manifest-sha256", required=True)
    parser.add_argument("--store-manifest", required=True, type=Path)
    parser.add_argument("--store-manifest-sha256", required=True)
    parser.add_argument("--p02b-result", type=Path)
    parser.add_argument("--p02b-validator", type=Path)
    parser.add_argument("--p02b-validator-sha256")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def run(args: argparse.Namespace) -> None:
    runtime_path = checked_file(args.runtime_manifest, args.runtime_manifest_sha256, "runtime manifest")
    store_manifest_path = checked_file(args.store_manifest, args.store_manifest_sha256, "store manifest")
    request, truth_rows, dataset, truth, store = validate_request(args.request.resolve(), args.store_label)
    formal = request["execution_mode"] == "formal"
    expected_service = "external-prestarted" if formal else "managed-fixture"
    if args.service_mode != expected_service:
        raise ContractError(f"{request['execution_mode']} mode requires --service-mode={expected_service}")
    runtime, client_root, docker = validate_runtime_manifest(runtime_path, request)
    store_manifest, labels = validate_store_manifest(store_manifest_path, request, dataset, truth, store)
    p02b = consume_p02b(args, formal)
    client_version = import_client(client_root)
    password_env = store_manifest["authentication"]["password_env"]
    password = os.environ.get(password_env)
    if not password:
        raise ContractError(f"{password_env} is required")
    if hashlib.sha256(password.encode()).hexdigest() != store_manifest["authentication"]["password_sha256"]:
        raise ContractError("NebulaGraph password does not match store manifest")
    output_dir = args.output_dir.resolve()
    if not output_dir.is_dir() or any(output_dir.iterdir()):
        raise ContractError("--output-dir must be an existing empty directory")
    if output_dir == store or output_dir in store.parents or store in output_dir.parents:
        raise ContractError("adapter output and store roots must not overlap")

    managed: ManagedFixtureCluster | None = None
    pool = None
    session = None
    cleanup_error: Exception | None = None
    port = store_manifest["graph_endpoint"]["port"]
    initial_containers: list[dict[str, Any]] = []
    try:
        if not formal:
            managed = ManagedFixtureCluster(docker, runtime, store_manifest, store)
            port = managed.start()
        initial_containers = inspect_cluster(docker, runtime, store_manifest, port)
        pool, session = open_session(
            store_manifest["graph_endpoint"]["host"],
            port,
            store_manifest["authentication"]["user"],
            password,
        )
        if not formal:
            load_fixture(session, store_manifest, dataset, labels)
        execute_ok(session, f"USE {store_manifest['space']}")
        write_contract_outputs(output_dir, request, truth_rows, labels, session)
        final_containers = inspect_cluster(docker, runtime, store_manifest, port)
        for before, after in zip(initial_containers, final_containers):
            for key in ("role", "name", "container_id", "image_id", "pid", "restart_count"):
                if before[key] != after[key]:
                    raise ContractError(f"NebulaGraph container lifecycle changed during warmup/measured: {before['name']}")
        provenance = {
            "schema_version": PROVENANCE_SCHEMA,
            "execution_mode": request["execution_mode"],
            "group": "client-server",
            "process_lifetime": request["process_lifetime"],
            "service_mode": args.service_mode,
            "system_version": request["system_version"],
            "binary": request["binary"],
            "dataset": request["dataset"],
            "truth": request["truth"],
            "store": {
                "path": str(store),
                "sha256": next(item["sha256"] for item in request["store_roots"] if item["label"] == args.store_label),
            },
            "store_manifest": {"path": str(store_manifest_path), "sha256": sha256_file(store_manifest_path)},
            "runtime_manifest": {"path": str(runtime_path), "sha256": sha256_file(runtime_path)},
            "client": {
                "version": client_version,
                "tree": runtime["client"]["tree"],
                "metadata": runtime["client"]["metadata"],
            },
            "images": runtime["images"],
            "image_digests": [EXPECTED_IMAGES[role]["digest"] for role in ("graphd", "metad", "storaged")],
            "container_names": [store_manifest["containers"][role] for role in ("graphd", "metad", "storaged")],
            "container_runtime": final_containers,
            "network": store_manifest["network"],
            "graph_endpoint": {"host": store_manifest["graph_endpoint"]["host"], "port": port},
            "service_session": {
                "session_open_count": 1,
                "restart_count": 0,
                "warmup_measured_same_session": True,
                "cleanup_owned_fixture": not formal,
            },
            "p02b_admission": p02b,
        }
        atomic_json(output_dir / "adapter-provenance.json", provenance)
    finally:
        if session is not None:
            session.release()
        if pool is not None:
            pool.close()
        if managed is not None:
            try:
                managed.cleanup()
            except Exception as exc:  # noqa: BLE001
                cleanup_error = exc
    if cleanup_error is not None:
        raise ContractError(str(cleanup_error))


def main() -> int:
    try:
        run(parse_args())
        return 0
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
