#!/usr/bin/env python3
"""Launch and stop one receipt-bound Neo4j P10 repeat.

The launcher deliberately does not copy or hash the database.  A caller must
prepare an independent repeat-local clone, freeze its store manifest, and
publish the full pre-start audit before invoking ``launch``.  This keeps large
store reads outside the measured P31 window while still binding every runtime
choice to a small, fail-closed receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))

from adapters.neo4j_store_contract import (  # noqa: E402
    CANONICAL_SENTINELS,
    KNOWN_MUTABLE_PATTERNS,
    TREE_HASH_METHOD,
    is_known_mutable,
    stable_tree_manifest,
    validate_owner_only_offline_gate,
)
from p10_contract import ContractError, atomic_json, read_json, sha256_file  # noqa: E402

P31_DIR = P10_DIR.parent / "p31"
sys.path.insert(0, str(P31_DIR))
from run_manifest import host_facts as p31_host_facts  # noqa: E402

LAUNCH_SCHEMA = "p10-neo4j-runtime-launch-receipt-v2"
STOP_SCHEMA = "p10-neo4j-runtime-stop-receipt-v2"
IMAGE_REF = "neo4j:5.26.24"
IMAGE_REPO_DIGEST = "sha256:f66304b9511c60d33555a2c451f88e03d82d1ebc893f32d84c98a6b326096435"
CONTAINER_PORT = 7687
LOG_DRIVER = "none"
HEAP_INITIAL = "8G"
HEAP_MAX = "8G"
PAGECACHE = "16G"
EXPECTED_ENTRYPOINT = ["tini", "-g", "--", "/startup/docker-entrypoint.sh"]
EXPECTED_COMMAND = ["neo4j"]
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,80}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MEMORY_RE = re.compile(r"^[1-9][0-9]*(?:[kKmMgGtT]|[kKmMgGtT][bB])$")

MEMORY_ENV_KEYS = {
    "heap_initial": "NEO4J_server_memory_heap_initial__size",
    "heap_max": "NEO4J_server_memory_heap_max__size",
    "pagecache": "NEO4J_server_memory_pagecache_size",
}
READ_ONLY_ENV = "NEO4J_server_databases_default__to__read__only"
AUTH_ENV = "NEO4J_AUTH"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)

    launch = subparsers.add_parser("launch")
    launch.add_argument("--repeat-index", required=True, type=int)
    launch.add_argument("--clone-id", required=True)
    launch.add_argument("--repeat-root", required=True, type=Path)
    launch.add_argument("--source-store-root", required=True, type=Path)
    launch.add_argument("--store-root", required=True, type=Path)
    launch.add_argument("--logs-root", required=True, type=Path)
    launch.add_argument("--store-manifest", required=True, type=Path)
    launch.add_argument("--store-manifest-sha256", required=True)
    launch.add_argument("--store-preflight", required=True, type=Path)
    launch.add_argument("--store-preflight-sha256", required=True)
    launch.add_argument("--container-name", required=True)
    launch.add_argument("--bolt-port", required=True, type=int)
    launch.add_argument("--heap-initial", choices=(HEAP_INITIAL,), default=HEAP_INITIAL)
    launch.add_argument("--heap-max", choices=(HEAP_MAX,), default=HEAP_MAX)
    launch.add_argument("--pagecache", choices=(PAGECACHE,), default=PAGECACHE)
    launch.add_argument("--image-ref", default=IMAGE_REF)
    launch.add_argument("--image-digest", required=True)
    launch.add_argument("--log-driver", choices=(LOG_DRIVER,), default=LOG_DRIVER)
    launch.add_argument("--output", required=True, type=Path)

    stop = subparsers.add_parser("stop")
    stop.add_argument("--launch-receipt", required=True, type=Path)
    stop.add_argument("--launch-receipt-sha256", required=True)
    stop.add_argument("--timeout-s", type=int, default=60)
    stop.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def exact_keys(value: object, keys: Iterable[str], context: str) -> dict[str, Any]:
    expected = set(keys)
    require(isinstance(value, dict), f"{context}: expected object")
    require(set(value) == expected, f"{context}: wrong keys")
    return value


def exact_sha(value: object, context: str) -> str:
    require(isinstance(value, str) and HEX64_RE.fullmatch(value) is not None, f"{context}: invalid SHA-256")
    return str(value)


def exact_image_digest(value: object, context: str) -> str:
    require(
        isinstance(value, str) and IMAGE_DIGEST_RE.fullmatch(value) is not None,
        f"{context}: expected sha256:<64 lowercase hex>",
    )
    return str(value)


def positive_integer(value: object, context: str, *, minimum: int = 1) -> int:
    require(isinstance(value, int) and not isinstance(value, bool) and value >= minimum, f"{context}: invalid integer")
    return int(value)


def nonempty(value: object, context: str) -> str:
    require(isinstance(value, str) and value != "", f"{context}: expected non-empty string")
    return str(value)


def artifact_ref(path: Path) -> dict[str, Any]:
    require(not path.is_symlink(), f"artifact path must not be a symlink: {path}")
    path = path.resolve()
    require(path.is_file(), f"artifact is not a regular file: {path}")
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def validate_artifact(value: object, context: str, *, verify_file: bool) -> dict[str, Any]:
    item = exact_keys(value, ("path", "sha256", "size_bytes"), context)
    raw_path = nonempty(item["path"], f"{context}.path")
    path = Path(raw_path)
    require(path.is_absolute() and path == path.resolve(), f"{context}.path must be canonical and absolute")
    digest = exact_sha(item["sha256"], f"{context}.sha256")
    size = positive_integer(item["size_bytes"], f"{context}.size_bytes", minimum=0)
    if verify_file:
        require(path.is_file() and not path.is_symlink(), f"{context}: file is missing")
        require(path.stat().st_size == size, f"{context}: size mismatch")
        require(sha256_file(path) == digest, f"{context}: SHA-256 mismatch")
    return {"path": str(path), "sha256": digest, "size_bytes": size}


def same_artifact(left: dict[str, Any], right: dict[str, Any], context: str) -> None:
    require(left == right, f"{context}: artifact reference mismatch")


def canonical_directory(raw: Path, context: str) -> Path:
    require(raw.is_absolute(), f"{context} must be absolute")
    resolved = raw.resolve()
    require(raw == resolved, f"{context} must be canonical and contain no symlink components")
    metadata = raw.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and not raw.is_symlink(), f"{context} is not a real directory")
    return resolved


def inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def overlaps(left: Path, right: Path) -> bool:
    return inside(left, right) or inside(right, left)


def directory_identity(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    return {
        "path": str(path),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "uid": int(metadata.st_uid),
        "gid": int(metadata.st_gid),
        "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
    }


def regular_tree_stats(root: Path, context: str) -> dict[str, tuple[int, int, int]]:
    records: dict[str, tuple[int, int, int]] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in dirnames:
            child = directory_path / name
            require(not child.is_symlink(), f"{context} contains a symlink directory: {child}")
        for name in filenames:
            path = directory_path / name
            metadata = path.lstat()
            require(stat.S_ISREG(metadata.st_mode) and not path.is_symlink(), f"{context} contains a non-regular file: {path}")
            relative = path.relative_to(root).as_posix()
            records[relative] = (int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_size))
    require(records, f"{context} is empty")
    return records


def prove_independent_clone(source: Path, runtime: Path) -> dict[str, Any]:
    """Seal equal source/runtime content and reject hard-link clones pre-P31."""

    from adapters.freeze_neo4j_store import store_offline_guard

    # The exact stat/no-hardlink audit and both full hashes share one nested
    # owner/lock/mount guard, and all finish before P31 begins.
    with store_offline_guard(source, None) as source_offline:
        with store_offline_guard(runtime, None) as runtime_offline:
            source_files = regular_tree_stats(source, "source store")
            runtime_files = regular_tree_stats(runtime, "runtime clone")
            require(set(source_files) == set(runtime_files), "source store/runtime clone file sets differ")
            total_bytes = 0
            for relative in sorted(source_files, key=lambda value: value.encode()):
                _source_device, _source_inode, source_size = source_files[relative]
                _runtime_device, _runtime_inode, runtime_size = runtime_files[relative]
                require(source_size == runtime_size, f"source/runtime file size differs before launch: {relative}")
                total_bytes += runtime_size
            source_inode_paths: dict[tuple[int, int], list[str]] = {}
            runtime_inode_paths: dict[tuple[int, int], list[str]] = {}
            for relative, (device, inode, _size) in source_files.items():
                source_inode_paths.setdefault((device, inode), []).append(relative)
            for relative, (device, inode, _size) in runtime_files.items():
                runtime_inode_paths.setdefault((device, inode), []).append(relative)
            shared_inodes = sorted(set(source_inode_paths) & set(runtime_inode_paths))
            shared_examples = [
                {
                    "source_paths": sorted(source_inode_paths[identity]),
                    "runtime_paths": sorted(runtime_inode_paths[identity]),
                }
                for identity in shared_inodes[:8]
            ]
            require(
                not shared_inodes,
                f"runtime clone shares file inodes with source store: {shared_examples!r}",
            )
            source_tree = stable_tree_manifest(source, attempts=2, evict_cache=True)
            runtime_tree = stable_tree_manifest(runtime, attempts=2, evict_cache=True)
            source_after = regular_tree_stats(source, "source store rescan")
            runtime_after = regular_tree_stats(runtime, "runtime clone rescan")
            require(source_after == source_files, "source store changed during clone-independence audit")
            require(runtime_after == runtime_files, "runtime clone changed during clone-independence audit")
    summary_keys = ("store_sha256", "file_count", "total_bytes")
    source_summary = {key: source_tree[key] for key in summary_keys}
    runtime_summary = {key: runtime_tree[key] for key in summary_keys}
    require(
        source_summary == runtime_summary,
        "source store/runtime clone content trees differ before launch",
    )
    return {
        "method": "full-content-tree-sha256-stable-no-hardlinks-v2",
        "file_count": len(runtime_files),
        "total_bytes": total_bytes,
        "shared_inode_count": 0,
        "source_tree": source_summary,
        "runtime_tree": runtime_summary,
        "source_offline_gate": source_offline,
        "runtime_offline_gate": runtime_offline,
    }


def current_host() -> dict[str, str]:
    facts = p31_host_facts()
    return {
        "hostname": nonempty(facts.get("hostname"), "host hostname"),
        "fingerprint_sha256": exact_sha(facts.get("fingerprint_sha256"), "host fingerprint"),
    }


def docker_call(command: list[str], context: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(f"{context}: cannot execute Docker: {exc}") from exc
    require(completed.returncode == 0, f"{context}: {completed.stderr.strip()}")
    return completed


def docker_json(command: list[str], context: str) -> Any:
    completed = docker_call(command, context)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{context}: malformed Docker JSON") from exc


def image_identity(image_ref: str, expected_digest: str) -> dict[str, Any]:
    values = docker_json(["docker", "image", "inspect", image_ref], "inspect Neo4j image")
    require(isinstance(values, list) and len(values) == 1, "Docker returned an ambiguous Neo4j image")
    image = values[0]
    require(isinstance(image, dict), "Docker image inspection is malformed")
    image_id = exact_image_digest(image.get("Id"), "Neo4j image ID")
    repo_digests = image.get("RepoDigests") or []
    require(isinstance(repo_digests, list) and all(isinstance(item, str) for item in repo_digests), "Neo4j RepoDigests malformed")
    resolved = {item.split("@", 1)[1] for item in repo_digests if "@" in item}
    require(expected_digest in resolved, "requested Neo4j RepoDigest is not attached to the actual image")
    return {
        "configured_ref": image_ref,
        "image_id": image_id,
        "repo_digests": sorted(repo_digests),
        "selected_repo_digest": expected_digest,
    }


def ensure_container_absent(name: str) -> None:
    completed = docker_call(
        ["docker", "container", "ls", "--all", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"],
        "check repeat container name",
    )
    require(completed.stdout.strip() == "", f"container already exists: {name}")


def ensure_port_available(port: int) -> None:
    require(1024 <= port <= 65535, "Bolt host port must be in [1024, 65535]")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise ContractError(f"Bolt host port 127.0.0.1:{port} is unavailable: {exc}") from exc
    finally:
        probe.close()


def docker_inspect_container(identifier: str) -> dict[str, Any]:
    values = docker_json(["docker", "container", "inspect", identifier], f"inspect container {identifier}")
    require(isinstance(values, list) and len(values) == 1, f"Docker returned an ambiguous container: {identifier}")
    require(isinstance(values[0], dict), f"container inspection is malformed: {identifier}")
    return values[0]


def remove_failed_container(container_id: str) -> None:
    require(HEX64_RE.fullmatch(container_id) is not None, "failed-container cleanup requires a full ID")
    subprocess.run(
        ["docker", "container", "rm", "--force", container_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=60,
    )


def environment_map(values: object, context: str) -> dict[str, str]:
    require(isinstance(values, list), f"{context}: expected environment array")
    result: dict[str, str] = {}
    for raw in values:
        require(isinstance(raw, str) and "=" in raw, f"{context}: malformed entry")
        key, value = raw.split("=", 1)
        require(key and key not in result, f"{context}: duplicate environment key {key!r}")
        result[key] = value
    return result


def reject_directory_overrides(environment: dict[str, str], command: object, entrypoint: object) -> None:
    prefixes = ("neo4j_server_directories_", "neo4j_dbms_directories_")
    forbidden_keys = [key for key in environment if key.lower().startswith(prefixes)]
    require(not forbidden_keys, f"Neo4j directory override environment is forbidden: {forbidden_keys!r}")
    tokens: list[str] = []
    for value in (command, entrypoint):
        if isinstance(value, list):
            tokens.extend(str(item) for item in value)
        elif value is not None:
            tokens.append(str(value))
    forbidden_tokens = [
        token for token in tokens
        if "server.directories." in token.lower() or "dbms.directories." in token.lower()
    ]
    require(not forbidden_tokens, "Neo4j command/entrypoint contains a directory override")


def normalized_mounts(raw: object) -> list[dict[str, Any]]:
    require(isinstance(raw, list), "container mounts are malformed")
    mounts: list[dict[str, Any]] = []
    for item in raw:
        require(isinstance(item, dict), "container mount entry is malformed")
        destination = nonempty(item.get("Destination"), "container mount destination")
        mounts.append(
            {
                "type": item.get("Type"),
                "source": item.get("Source"),
                "destination": destination,
                "rw": item.get("RW"),
                "mode": item.get("Mode", ""),
                "propagation": item.get("Propagation", ""),
            }
        )
    return sorted(mounts, key=lambda item: str(item["destination"]).encode())


def required_environment(memory: dict[str, str]) -> dict[str, str]:
    return {
        AUTH_ENV: "none",
        READ_ONLY_ENV: "true",
        MEMORY_ENV_KEYS["heap_initial"]: memory["heap_initial"],
        MEMORY_ENV_KEYS["heap_max"]: memory["heap_max"],
        MEMORY_ENV_KEYS["pagecache"]: memory["pagecache"],
    }


def validate_memory(heap_initial: str, heap_max: str, pagecache: str) -> dict[str, str]:
    values = {"heap_initial": heap_initial, "heap_max": heap_max, "pagecache": pagecache}
    for key, value in values.items():
        require(MEMORY_RE.fullmatch(value) is not None, f"{key} must be a positive Neo4j byte-size such as 8G")
    require(
        values == {"heap_initial": HEAP_INITIAL, "heap_max": HEAP_MAX, "pagecache": PAGECACHE},
        "formal Neo4j memory settings must be exactly heap=8G/8G and pagecache=16G",
    )
    return values


def expected_labels(repeat_index: int, clone_id: str) -> dict[str, str]:
    return {
        "cidr.p10.system": "neo4j",
        "cidr.p10.repeat": str(repeat_index),
        "cidr.p10.clone-id": clone_id,
    }


def runtime_contract(
    *,
    name: str,
    repeat_index: int,
    clone_id: str,
    image: dict[str, Any],
    store_root: Path,
    logs_root: Path,
    bolt_port: int,
    log_driver: str,
    memory: dict[str, str],
) -> dict[str, Any]:
    store_metadata = store_root.stat()
    return {
        "container_name": name,
        "repeat_index": repeat_index,
        "clone_id": clone_id,
        "image_ref": image["configured_ref"],
        "image_id": image["image_id"],
        "store_root": str(store_root),
        "logs_root": str(logs_root),
        "bolt_host": "127.0.0.1",
        "bolt_port": bolt_port,
        "container_port": CONTAINER_PORT,
        "network_mode": "bridge",
        "restart_policy": "no",
        "log_driver": log_driver,
        "container_user": f"{store_metadata.st_uid}:{store_metadata.st_gid}",
        "memory_limit_bytes": 0,
        "memory_swap_limit_bytes": 0,
        "memory": memory,
        "environment": required_environment(memory),
        "labels": expected_labels(repeat_index, clone_id),
        "expected_entrypoint": list(EXPECTED_ENTRYPOINT),
        "expected_command": list(EXPECTED_COMMAND),
        "read_only_default": True,
        "directory_overrides_forbidden": True,
    }


def validate_container_inspect(
    container: dict[str, Any],
    contract: dict[str, Any],
    *,
    expected_running: bool,
    ever_started: bool = True,
) -> dict[str, Any]:
    name = contract["container_name"]
    require(container.get("Name") == f"/{name}", "Neo4j container name mismatch")
    container_id = nonempty(container.get("Id"), "Neo4j container ID")
    require(HEX64_RE.fullmatch(container_id) is not None, "Neo4j container ID must be 64 lowercase hex")
    config = container.get("Config") or {}
    host_config = container.get("HostConfig") or {}
    require(isinstance(config, dict) and isinstance(host_config, dict), "Neo4j container config is malformed")
    require(config.get("Image") == contract["image_ref"], "Neo4j configured image ref mismatch")
    require(config.get("User") == contract["container_user"], "Neo4j container user differs from the store owner")
    require(container.get("Image") == contract["image_id"], "Neo4j actual image ID mismatch")
    require(host_config.get("RestartPolicy", {}).get("Name") == "no", "Neo4j restart policy must be no")
    require(host_config.get("NetworkMode") == "bridge", "Neo4j network mode must be bridge")
    require(host_config.get("Memory") == 0, "Neo4j Docker Memory must be the frozen value 0")
    require(host_config.get("MemorySwap") == 0, "Neo4j Docker MemorySwap must be the frozen value 0")

    environment = environment_map(config.get("Env") or [], "Neo4j environment")
    for key, value in contract["environment"].items():
        require(environment.get(key) == value, f"Neo4j environment {key} differs from launch contract")
    require(
        config.get("Entrypoint") == contract["expected_entrypoint"],
        "Neo4j Docker Config.Entrypoint differs from the frozen image baseline",
    )
    require(
        config.get("Cmd") == contract["expected_command"],
        "Neo4j Docker Config.Cmd differs from the frozen image baseline",
    )
    reject_directory_overrides(environment, config.get("Cmd"), config.get("Entrypoint"))
    labels = config.get("Labels") or {}
    require(isinstance(labels, dict), "Neo4j container labels are malformed")
    for key, value in contract["labels"].items():
        require(labels.get(key) == value, f"Neo4j container label {key} mismatch")

    mounts = normalized_mounts(container.get("Mounts") or [])
    require({item["destination"] for item in mounts} == {"/data", "/logs"}, "Neo4j mounts must be exactly /data and /logs")
    by_destination = {item["destination"]: item for item in mounts}
    for destination, source in (("/data", contract["store_root"]), ("/logs", contract["logs_root"])):
        mount = by_destination[destination]
        require(mount["type"] == "bind", f"Neo4j {destination} must be a bind mount")
        require(Path(str(mount["source"])).resolve() == Path(source), f"Neo4j {destination} source mismatch")
        require(mount["rw"] is True, f"Neo4j {destination} must be writable for controlled runtime state")
    require(not any(item["destination"].startswith("/data/") for item in mounts), "nested /data mounts are forbidden")

    expected_binding = {"7687/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(contract["bolt_port"])}]}
    require(host_config.get("PortBindings") == expected_binding, "Neo4j host Bolt binding mismatch")
    log_config = host_config.get("LogConfig") or {}
    require(
        log_config.get("Type") == contract["log_driver"] and (log_config.get("Config") or {}) == {},
        "Neo4j Docker log driver/config mismatch",
    )
    if expected_running:
        ports = (container.get("NetworkSettings") or {}).get("Ports") or {}
        published = {key: value for key, value in ports.items() if value}
        require(published == expected_binding, "Neo4j running Bolt binding is not localhost-only/exact")

    state = container.get("State") or {}
    require(state.get("Running") is expected_running, "Neo4j running state mismatch")
    restart_count = positive_integer(container.get("RestartCount"), "Neo4j RestartCount", minimum=0)
    require(restart_count == 0, "Neo4j container restarted")
    pid = positive_integer(state.get("Pid"), "Neo4j PID", minimum=0)
    if expected_running:
        require(pid > 0, "running Neo4j container lacks a positive PID")
    else:
        require(pid == 0, "stopped Neo4j container still has a PID")
    started_at = nonempty(state.get("StartedAt"), "Neo4j StartedAt")
    if ever_started:
        require(not started_at.startswith("0001-01-01"), "Neo4j container was never started")
    else:
        require(started_at.startswith("0001-01-01"), "new Neo4j container was unexpectedly started")
    runtime = {
        "running": expected_running,
        "pid": pid,
        "started_at": started_at,
        "finished_at": nonempty(state.get("FinishedAt"), "Neo4j FinishedAt"),
        "exit_code": positive_integer(state.get("ExitCode"), "Neo4j ExitCode", minimum=0),
        "restart_count": restart_count,
    }
    snapshot = {
        "container_name": name,
        "container_id": container_id,
        "configured_image": config.get("Image"),
        "container_user": config.get("User"),
        "image_id": container.get("Image"),
        "entrypoint": config.get("Entrypoint"),
        "command": config.get("Cmd"),
        "environment": sorted(f"{key}={value}" for key, value in environment.items()),
        "labels": {key: labels[key] for key in sorted(contract["labels"])},
        "network_mode": host_config.get("NetworkMode"),
        "restart_policy": host_config.get("RestartPolicy", {}).get("Name"),
        "mounts": mounts,
        "port_bindings": expected_binding,
        "log_config": {"type": log_config.get("Type"), "config": log_config.get("Config") or {}},
        "resource_limits": {
            "memory_bytes": host_config.get("Memory"),
            "memory_swap_bytes": host_config.get("MemorySwap"),
        },
    }
    return {"config": snapshot, "runtime": runtime}


def validate_store_inputs(
    store_root: Path,
    manifest_path: Path,
    manifest_sha256: str,
    preflight_path: Path,
    preflight_sha256: str,
) -> dict[str, Any]:
    require(manifest_path.is_absolute() and preflight_path.is_absolute(), "store manifest/preflight paths must be absolute")
    manifest_expected_sha = exact_sha(manifest_sha256, "store manifest SHA-256")
    preflight_expected_sha = exact_sha(preflight_sha256, "store preflight SHA-256")
    manifest_ref = artifact_ref(manifest_path)
    preflight_ref = artifact_ref(preflight_path)
    require(manifest_ref["sha256"] == manifest_expected_sha, "store manifest SHA-256 mismatch")
    require(preflight_ref["sha256"] == preflight_expected_sha, "store preflight SHA-256 mismatch")
    manifest = read_json(Path(manifest_ref["path"]), "Neo4j store manifest")
    require(manifest.get("schema_version") == "p10-neo4j-store-manifest-v3", "runtime launch requires Neo4j store manifest v3")
    require(Path(str(manifest.get("store_root", ""))).resolve() == store_root, "store manifest root differs from runtime clone")
    immutable = {
        "store_sha256": exact_sha(manifest.get("immutable_store_sha256"), "manifest immutable store SHA-256"),
        "file_count": positive_integer(manifest.get("immutable_file_count"), "manifest immutable file count"),
        "total_bytes": positive_integer(manifest.get("immutable_total_bytes"), "manifest immutable bytes", minimum=0),
    }
    frozen_store = {
        "store_sha256": exact_sha(manifest.get("store_sha256"), "manifest full store SHA-256"),
        "file_count": positive_integer(manifest.get("file_count"), "manifest full file count"),
        "total_bytes": positive_integer(manifest.get("total_bytes"), "manifest full bytes", minimum=0),
    }
    for relative in CANONICAL_SENTINELS:
        sentinel = store_root / relative
        require(sentinel.is_file() and not sentinel.is_symlink(), f"runtime clone lacks canonical sentinel: {relative}")

    preflight = read_json(Path(preflight_ref["path"]), "Neo4j store preflight")
    required_top = (
        "schema_version", "stage", "request", "store_manifest", "store_root",
        "hash_method", "known_mutable_patterns", "cache_eviction",
        "known_mutable_changes_tolerated", "offline_gate", "stop_receipt", "audit",
    )
    exact_keys(preflight, required_top, "Neo4j store preflight")
    require(preflight["schema_version"] == "p10-neo4j-runtime-store-audit-v1", "store preflight schema mismatch")
    require(preflight["stage"] == "pre", "store preflight must be pre-start")
    require(Path(str(preflight["store_root"])).resolve() == store_root, "store preflight root mismatch")
    require(preflight["hash_method"] == TREE_HASH_METHOD, "store preflight hash method mismatch")
    require(preflight["known_mutable_patterns"] == list(KNOWN_MUTABLE_PATTERNS), "store preflight mutable patterns mismatch")
    require(preflight["cache_eviction"] == "posix-fadvise-dontneed-v1", "store preflight lacks cache eviction")
    require(preflight["known_mutable_changes_tolerated"] is False, "store preflight tolerated online mutable-file changes")
    require(preflight["stop_receipt"] is None, "store preflight unexpectedly binds a stop receipt")
    validate_offline_gate(preflight["offline_gate"], store_root, "store preflight offline gate")
    recorded_manifest = validate_artifact(preflight["store_manifest"], "store preflight manifest", verify_file=False)
    same_artifact(recorded_manifest, manifest_ref, "store preflight manifest")
    validate_artifact(preflight["request"], "store preflight request", verify_file=True)
    audit = exact_keys(preflight["audit"], ("tree", "mutable_deltas"), "store preflight audit")
    tree = exact_keys(
        audit["tree"],
        ("store_sha256", "file_count", "total_bytes", "immutable_store_sha256", "immutable_file_count", "immutable_total_bytes"),
        "store preflight tree",
    )
    require(tree["immutable_store_sha256"] == immutable["store_sha256"], "preflight immutable SHA differs from manifest")
    require(tree["immutable_file_count"] == immutable["file_count"], "preflight immutable file count differs from manifest")
    require(tree["immutable_total_bytes"] == immutable["total_bytes"], "preflight immutable bytes differ from manifest")
    exact_sha(tree["store_sha256"], "preflight store SHA-256")
    positive_integer(tree["file_count"], "preflight file count")
    positive_integer(tree["total_bytes"], "preflight total bytes", minimum=0)
    deltas = audit["mutable_deltas"]
    require(isinstance(deltas, list), "store preflight mutable_deltas must be an array")
    for index, delta in enumerate(deltas):
        item = exact_keys(delta, ("path", "before", "after"), f"store preflight delta[{index}]")
        require(is_known_mutable(nonempty(item["path"], f"store preflight delta[{index}].path")), "store preflight contains forbidden delta")
    require(not deltas, "store preflight differs from the frozen full-tree manifest")
    return {
        "manifest": manifest_ref,
        "preflight": preflight_ref,
        "immutable_store": immutable,
        "frozen_store": frozen_store,
    }


def build_create_command(contract: dict[str, Any]) -> list[str]:
    command = [
        "docker", "container", "create",
        "--name", contract["container_name"],
        "--restart", "no",
        "--network", "bridge",
        "--log-driver", contract["log_driver"],
        "--user", contract["container_user"],
        "--publish", f"127.0.0.1:{contract['bolt_port']}:{CONTAINER_PORT}",
        "--mount", f"type=bind,src={contract['store_root']},dst=/data",
        "--mount", f"type=bind,src={contract['logs_root']},dst=/logs",
    ]
    for key, value in sorted(contract["labels"].items()):
        command.extend(("--label", f"{key}={value}"))
    for key, value in sorted(contract["environment"].items()):
        command.extend(("--env", f"{key}={value}"))
    command.append(contract["image_ref"])
    return command


def prepare_clone_identity(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    require(args.repeat_index >= 1, "repeat index must be positive")
    require(NAME_RE.fullmatch(args.clone_id) is not None, "invalid clone ID")
    require(NAME_RE.fullmatch(args.container_name) is not None, "invalid container name")
    require(args.container_name.endswith(f"-r{args.repeat_index:02d}"), "container name must end with the repeat suffix")
    repeat_root = canonical_directory(args.repeat_root, "repeat root")
    source_root = canonical_directory(args.source_store_root, "source store root")
    store_root = canonical_directory(args.store_root, "runtime clone root")
    logs_root = canonical_directory(args.logs_root, "repeat logs root")
    require(inside(store_root, repeat_root), "runtime clone must be inside its repeat root")
    require(inside(logs_root, repeat_root), "logs root must be repeat-local")
    require(not overlaps(store_root, logs_root), "runtime clone and logs root overlap")
    require(not overlaps(source_root, store_root), "source store and runtime clone must be independent paths")
    require(not overlaps(source_root, logs_root), "source store and repeat logs overlap")
    source_identity = directory_identity(source_root)
    runtime_identity = directory_identity(store_root)
    require(
        (source_identity["device"], source_identity["inode"]) != (runtime_identity["device"], runtime_identity["inode"]),
        "runtime store is not an independent clone directory",
    )
    require(next(logs_root.iterdir(), None) is None, "repeat logs root must be empty before launch")
    for path, context in ((store_root, "runtime clone"), (logs_root, "repeat logs root")):
        metadata = path.stat()
        require(
            metadata.st_uid == os.geteuid() and metadata.st_gid == os.getegid(),
            f"{context} must be owned by the launching UID:GID",
        )
        require(stat.S_IMODE(metadata.st_mode) == 0o700, f"{context} must have exact owner-only mode 0700")
    store_inputs = validate_store_inputs(
        store_root,
        args.store_manifest,
        args.store_manifest_sha256,
        args.store_preflight,
        args.store_preflight_sha256,
    )
    independence_audit = prove_independent_clone(source_root, store_root)
    require(
        independence_audit["runtime_tree"] == store_inputs["frozen_store"],
        "runtime clone content proof differs from its frozen store manifest",
    )
    for reference in (store_inputs["manifest"], store_inputs["preflight"]):
        artifact_path = Path(reference["path"])
        require(
            not inside(artifact_path, store_root) and not inside(artifact_path, logs_root),
            "store manifest/preflight must be outside runtime /data and /logs roots",
        )
    clone = {
        "clone_id": args.clone_id,
        "repeat_index": args.repeat_index,
        "repeat_root": str(repeat_root),
        "source_store_root": source_identity,
        "runtime_store_root": runtime_identity,
        "logs_root": directory_identity(logs_root),
        "store_manifest": store_inputs["manifest"],
        "store_preflight": store_inputs["preflight"],
        "immutable_store": store_inputs["immutable_store"],
        "independence_audit": independence_audit,
        "independent_directory": True,
    }
    return clone, {"repeat_root": repeat_root, "source_root": source_root, "store_root": store_root, "logs_root": logs_root}


def validate_output_path(output: Path, repeat_root: Path, store_root: Path, logs_root: Path) -> Path:
    require(output.is_absolute(), "receipt output must be absolute")
    output = output.resolve()
    require(not output.exists(), f"refusing to overwrite receipt: {output}")
    require(output.parent.is_dir(), f"receipt parent is missing: {output.parent}")
    require(inside(output, repeat_root), "receipt output must be repeat-local")
    require(not inside(output, store_root) and not inside(output, logs_root), "receipt must be outside /data and /logs roots")
    return output


def run_launch(args: argparse.Namespace) -> dict[str, Any]:
    require(args.image_ref == IMAGE_REF, f"formal Neo4j image ref must be {IMAGE_REF}")
    image_digest = exact_image_digest(args.image_digest, "Neo4j RepoDigest")
    require(image_digest == IMAGE_REPO_DIGEST, "formal Neo4j RepoDigest differs from the frozen 5.26.24 image")
    require(args.log_driver == LOG_DRIVER, "formal Neo4j Docker log driver must be none")
    memory = validate_memory(args.heap_initial, args.heap_max, args.pagecache)
    clone, roots = prepare_clone_identity(args)
    output = validate_output_path(args.output, roots["repeat_root"], roots["store_root"], roots["logs_root"])
    image = image_identity(args.image_ref, image_digest)
    ensure_container_absent(args.container_name)
    ensure_port_available(args.bolt_port)
    contract = runtime_contract(
        name=args.container_name,
        repeat_index=args.repeat_index,
        clone_id=args.clone_id,
        image=image,
        store_root=roots["store_root"],
        logs_root=roots["logs_root"],
        bolt_port=args.bolt_port,
        log_driver=args.log_driver,
        memory=memory,
    )
    create_command = build_create_command(contract)
    created_id: str | None = None
    published = False
    try:
        create_started_at = utc_now()
        create_result = docker_call(create_command, "create Neo4j repeat container")
        create_finished_at = utc_now()
        candidate_id = create_result.stdout.strip()
        require(HEX64_RE.fullmatch(candidate_id) is not None, "Docker create did not return a full container ID")
        created_id = candidate_id
        created_snapshot = validate_container_inspect(
            docker_inspect_container(created_id),
            contract,
            expected_running=False,
            ever_started=False,
        )
        require(created_snapshot["config"]["container_id"] == created_id, "Docker create/inspect container ID mismatch")

        start_command = ["docker", "container", "start", created_id]
        start_started_at = utc_now()
        start_result = docker_call(start_command, "start Neo4j repeat container", timeout=120)
        start_finished_at = utc_now()
        running_snapshot = validate_container_inspect(
            docker_inspect_container(created_id), contract, expected_running=True
        )
        require(running_snapshot["config"] == created_snapshot["config"], "Neo4j container config changed during start")
        require(directory_identity(roots["store_root"]) == clone["runtime_store_root"], "runtime clone root identity changed during launch")
        require(directory_identity(roots["logs_root"]) == clone["logs_root"], "repeat logs root identity changed during launch")
        host = current_host()
        document = {
            "schema_version": LAUNCH_SCHEMA,
            "producer": artifact_ref(Path(__file__)),
            "host": host,
            "repeat": {
                "repeat_index": args.repeat_index,
                "clone_id": args.clone_id,
                "repeat_root": str(roots["repeat_root"]),
            },
            "clone": clone,
            "image": image,
            "runtime_contract": contract,
            "docker": {
                "create": {
                    "command": create_command,
                    "started_at": create_started_at,
                    "finished_at": create_finished_at,
                    "exit_code": create_result.returncode,
                    "stdout": created_id,
                },
                "created": created_snapshot,
                "start": {
                    "command": start_command,
                    "started_at": start_started_at,
                    "finished_at": start_finished_at,
                    "exit_code": start_result.returncode,
                    "stdout": start_result.stdout.strip(),
                },
                "running": running_snapshot,
            },
            "outcome": {"completed": True, "container_running": True, "exit_code": 0},
        }
        validate_launch_receipt(document, verify_artifacts=True, expected_host=host)
        atomic_json(output, document)
        published = True
        return document
    finally:
        if created_id is not None and not published:
            remove_failed_container(created_id)


def validate_operation(value: object, context: str) -> dict[str, Any]:
    operation = exact_keys(value, ("command", "started_at", "finished_at", "exit_code", "stdout"), context)
    require(isinstance(operation["command"], list) and all(isinstance(item, str) for item in operation["command"]), f"{context}.command invalid")
    nonempty(operation["started_at"], f"{context}.started_at")
    nonempty(operation["finished_at"], f"{context}.finished_at")
    require(operation["exit_code"] == 0, f"{context}.exit_code is not zero")
    require(isinstance(operation["stdout"], str), f"{context}.stdout invalid")
    return operation


def validate_root_identity(value: object, context: str) -> dict[str, Any]:
    item = exact_keys(value, ("path", "device", "inode", "uid", "gid", "mode"), context)
    path = Path(nonempty(item["path"], f"{context}.path"))
    require(path.is_absolute() and path == path.resolve(), f"{context}.path must be canonical")
    positive_integer(item["device"], f"{context}.device", minimum=0)
    positive_integer(item["inode"], f"{context}.inode", minimum=1)
    positive_integer(item["uid"], f"{context}.uid", minimum=0)
    positive_integer(item["gid"], f"{context}.gid", minimum=0)
    require(item["mode"] == "0700", f"{context}.mode must be 0700")
    return item


def validate_offline_gate(value: object, expected_root: Path, context: str) -> dict[str, Any]:
    return validate_owner_only_offline_gate(value, expected_root, context)


def validate_snapshot_record(value: object, context: str, *, running: bool) -> dict[str, Any]:
    record = exact_keys(value, ("config", "runtime"), context)
    config = exact_keys(
        record["config"],
        (
            "container_name", "container_id", "configured_image", "container_user", "image_id", "entrypoint",
            "command", "environment", "labels", "network_mode", "restart_policy", "mounts",
            "port_bindings", "log_config", "resource_limits",
        ),
        f"{context}.config",
    )
    runtime = exact_keys(
        record["runtime"],
        ("running", "pid", "started_at", "finished_at", "exit_code", "restart_count"),
        f"{context}.runtime",
    )
    require(runtime["running"] is running, f"{context}.runtime.running mismatch")
    pid = positive_integer(runtime["pid"], f"{context}.runtime.pid", minimum=0)
    require((pid > 0) is running, f"{context}.runtime.pid/running mismatch")
    require(runtime["restart_count"] == 0, f"{context}: container restarted")
    nonempty(runtime["started_at"], f"{context}.runtime.started_at")
    nonempty(runtime["finished_at"], f"{context}.runtime.finished_at")
    positive_integer(runtime["exit_code"], f"{context}.runtime.exit_code", minimum=0)
    require(isinstance(config["mounts"], list), f"{context}.config.mounts invalid")
    require(isinstance(config["environment"], list), f"{context}.config.environment invalid")
    return record


def validate_snapshot_against_contract(
    snapshot: dict[str, Any],
    contract: dict[str, Any],
    context: str,
) -> None:
    config = snapshot["config"]
    container_id = nonempty(config["container_id"], f"{context}.container_id")
    require(HEX64_RE.fullmatch(container_id) is not None, f"{context}.container_id invalid")
    require(config["container_name"] == contract["container_name"], f"{context}.container_name mismatch")
    require(config["configured_image"] == contract["image_ref"], f"{context}.configured_image mismatch")
    require(config["container_user"] == contract["container_user"], f"{context}.container_user mismatch")
    require(config["image_id"] == contract["image_id"], f"{context}.image_id mismatch")
    require(config["network_mode"] == "bridge" and config["restart_policy"] == "no", f"{context}.network/restart mismatch")
    environment = environment_map(config["environment"], f"{context}.environment")
    for key, expected in contract["environment"].items():
        require(environment.get(key) == expected, f"{context}.environment {key} mismatch")
    require(
        config["entrypoint"] == contract["expected_entrypoint"],
        f"{context}.entrypoint differs from the frozen image baseline",
    )
    require(
        config["command"] == contract["expected_command"],
        f"{context}.command differs from the frozen image baseline",
    )
    reject_directory_overrides(environment, config["command"], config["entrypoint"])
    require(config["labels"] == contract["labels"], f"{context}.labels mismatch")
    mounts = config["mounts"]
    require(isinstance(mounts, list), f"{context}.mounts malformed")
    require({item.get("destination") for item in mounts if isinstance(item, dict)} == {"/data", "/logs"}, f"{context}.mounts are not exact")
    by_destination = {item["destination"]: item for item in mounts}
    for destination, source in (("/data", contract["store_root"]), ("/logs", contract["logs_root"])):
        item = by_destination[destination]
        require(
            item.get("type") == "bind"
            and Path(str(item.get("source"))).resolve() == Path(source)
            and item.get("rw") is True,
            f"{context}.{destination} mount mismatch",
        )
    expected_binding = {"7687/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(contract["bolt_port"])}]}
    require(config["port_bindings"] == expected_binding, f"{context}.port binding mismatch")
    require(config["log_config"] == {"type": contract["log_driver"], "config": {}}, f"{context}.log config mismatch")
    require(
        config["resource_limits"]
        == {
            "memory_bytes": contract["memory_limit_bytes"],
            "memory_swap_bytes": contract["memory_swap_limit_bytes"],
        },
        f"{context}.Docker memory limits mismatch",
    )


def validate_launch_receipt(
    value: object,
    *,
    verify_artifacts: bool,
    expected_host: dict[str, str] | None = None,
) -> dict[str, Any]:
    document = exact_keys(
        value,
        ("schema_version", "producer", "host", "repeat", "clone", "image", "runtime_contract", "docker", "outcome"),
        "Neo4j launch receipt",
    )
    require(document["schema_version"] == LAUNCH_SCHEMA, "Neo4j launch receipt schema mismatch")
    producer = validate_artifact(document["producer"], "Neo4j launch producer", verify_file=verify_artifacts)
    if verify_artifacts:
        current = artifact_ref(Path(__file__))
        same_artifact(producer, current, "Neo4j launch producer")
    host = exact_keys(document["host"], ("hostname", "fingerprint_sha256"), "Neo4j launch host")
    nonempty(host["hostname"], "Neo4j launch hostname")
    exact_sha(host["fingerprint_sha256"], "Neo4j launch host fingerprint")
    if expected_host is not None:
        require(host == expected_host, "Neo4j launch receipt host mismatch")

    repeat = exact_keys(document["repeat"], ("repeat_index", "clone_id", "repeat_root"), "Neo4j launch repeat")
    repeat_index = positive_integer(repeat["repeat_index"], "Neo4j launch repeat index")
    clone_id = nonempty(repeat["clone_id"], "Neo4j launch clone ID")
    require(NAME_RE.fullmatch(clone_id) is not None, "Neo4j launch clone ID invalid")
    repeat_root = Path(nonempty(repeat["repeat_root"], "Neo4j launch repeat root"))
    require(repeat_root.is_absolute() and repeat_root == repeat_root.resolve(), "Neo4j launch repeat root is not canonical")

    clone = exact_keys(
        document["clone"],
        (
            "clone_id", "repeat_index", "repeat_root", "source_store_root", "runtime_store_root",
            "logs_root", "store_manifest", "store_preflight", "immutable_store", "independent_directory",
            "independence_audit",
        ),
        "Neo4j launch clone",
    )
    require(clone["clone_id"] == clone_id and clone["repeat_index"] == repeat_index and clone["repeat_root"] == str(repeat_root), "Neo4j launch clone/repeat mismatch")
    require(clone["independent_directory"] is True, "Neo4j runtime store is not marked independent")
    independence_audit = exact_keys(
        clone["independence_audit"],
        (
            "method", "file_count", "total_bytes", "shared_inode_count",
            "source_tree", "runtime_tree", "source_offline_gate", "runtime_offline_gate",
        ),
        "Neo4j clone independence audit",
    )
    require(independence_audit["method"] == "full-content-tree-sha256-stable-no-hardlinks-v2", "Neo4j clone independence method mismatch")
    positive_integer(independence_audit["file_count"], "Neo4j clone file count")
    positive_integer(independence_audit["total_bytes"], "Neo4j clone total bytes", minimum=0)
    require(independence_audit["shared_inode_count"] == 0, "Neo4j clone contains shared hard links")
    content_summaries: list[dict[str, Any]] = []
    for role in ("source_tree", "runtime_tree"):
        tree = exact_keys(
            independence_audit[role],
            ("store_sha256", "file_count", "total_bytes"),
            f"Neo4j clone {role}",
        )
        exact_sha(tree["store_sha256"], f"Neo4j clone {role} SHA-256")
        positive_integer(tree["file_count"], f"Neo4j clone {role} file count")
        positive_integer(tree["total_bytes"], f"Neo4j clone {role} bytes", minimum=0)
        content_summaries.append(tree)
    require(content_summaries[0] == content_summaries[1], "Neo4j source/runtime content proof differs")
    require(
        content_summaries[1]["file_count"] == independence_audit["file_count"]
        and content_summaries[1]["total_bytes"] == independence_audit["total_bytes"],
        "Neo4j clone stat/content summaries differ",
    )
    source_identity = validate_root_identity(clone["source_store_root"], "Neo4j source store identity")
    runtime_identity = validate_root_identity(clone["runtime_store_root"], "Neo4j runtime store identity")
    logs_identity = validate_root_identity(clone["logs_root"], "Neo4j logs identity")
    require((source_identity["device"], source_identity["inode"]) != (runtime_identity["device"], runtime_identity["inode"]), "Neo4j source/runtime directory identity reused")
    require(not overlaps(Path(source_identity["path"]), Path(runtime_identity["path"])), "Neo4j source/runtime paths overlap")
    require(not overlaps(Path(runtime_identity["path"]), Path(logs_identity["path"])), "Neo4j runtime/log paths overlap")
    require(inside(Path(runtime_identity["path"]), repeat_root) and inside(Path(logs_identity["path"]), repeat_root), "Neo4j clone/log roots are not repeat-local")
    source_offline_gate = validate_offline_gate(
        independence_audit["source_offline_gate"],
        Path(source_identity["path"]),
        "Neo4j golden source offline gate",
    )
    runtime_offline_gate = validate_offline_gate(
        independence_audit["runtime_offline_gate"],
        Path(runtime_identity["path"]),
        "Neo4j runtime clone offline gate",
    )
    for identity, gate, context in (
        (source_identity, source_offline_gate, "golden source"),
        (runtime_identity, runtime_offline_gate, "runtime clone"),
    ):
        owner = gate["before_hash"]["root_identity"]
        require(
            (identity["uid"], identity["gid"], identity["mode"])
            == (owner["uid"], owner["gid"], owner["mode"]),
            f"Neo4j {context} directory identity differs from its offline gate",
        )
    manifest_ref = validate_artifact(clone["store_manifest"], "Neo4j launch store manifest", verify_file=verify_artifacts)
    preflight_ref = validate_artifact(clone["store_preflight"], "Neo4j launch store preflight", verify_file=verify_artifacts)
    immutable = exact_keys(clone["immutable_store"], ("store_sha256", "file_count", "total_bytes"), "Neo4j launch immutable store")
    exact_sha(immutable["store_sha256"], "Neo4j launch immutable store SHA-256")
    positive_integer(immutable["file_count"], "Neo4j launch immutable file count")
    positive_integer(immutable["total_bytes"], "Neo4j launch immutable total bytes", minimum=0)
    if verify_artifacts:
        checked = validate_store_inputs(
            Path(runtime_identity["path"]),
            Path(manifest_ref["path"]), manifest_ref["sha256"],
            Path(preflight_ref["path"]), preflight_ref["sha256"],
        )
        require(checked["immutable_store"] == immutable, "Neo4j launch immutable store binding mismatch")
        require(checked["frozen_store"] == content_summaries[1], "Neo4j launch runtime tree differs from frozen manifest")

    image = exact_keys(document["image"], ("configured_ref", "image_id", "repo_digests", "selected_repo_digest"), "Neo4j launch image")
    require(image["configured_ref"] == IMAGE_REF, "Neo4j launch image ref mismatch")
    exact_image_digest(image["image_id"], "Neo4j launch image ID")
    selected_digest = exact_image_digest(image["selected_repo_digest"], "Neo4j launch RepoDigest")
    require(selected_digest == IMAGE_REPO_DIGEST, "Neo4j launch receipt RepoDigest is not frozen")
    require(
        isinstance(image["repo_digests"], list)
        and all(isinstance(item, str) for item in image["repo_digests"])
        and image["repo_digests"] == sorted(image["repo_digests"]),
        "Neo4j launch RepoDigests malformed",
    )
    require(any(isinstance(item, str) and item.endswith(f"@{selected_digest}") for item in image["repo_digests"]), "Neo4j selected RepoDigest missing")

    contract_keys = (
        "container_name", "repeat_index", "clone_id", "image_ref", "image_id", "store_root", "logs_root",
        "bolt_host", "bolt_port", "container_port", "network_mode", "restart_policy", "log_driver",
        "container_user", "memory_limit_bytes", "memory_swap_limit_bytes", "memory", "environment", "labels",
        "expected_entrypoint", "expected_command",
        "read_only_default", "directory_overrides_forbidden",
    )
    contract = exact_keys(document["runtime_contract"], contract_keys, "Neo4j runtime contract")
    require(contract["repeat_index"] == repeat_index and contract["clone_id"] == clone_id, "Neo4j runtime contract repeat mismatch")
    require(contract["image_ref"] == image["configured_ref"] and contract["image_id"] == image["image_id"], "Neo4j runtime contract image mismatch")
    require(contract["store_root"] == runtime_identity["path"] and contract["logs_root"] == logs_identity["path"], "Neo4j runtime contract root mismatch")
    require(
        (runtime_identity["uid"], runtime_identity["gid"], runtime_identity["mode"])
        == (logs_identity["uid"], logs_identity["gid"], logs_identity["mode"]),
        "Neo4j runtime store/log owner-mode identities differ",
    )
    require(contract["bolt_host"] == "127.0.0.1" and contract["container_port"] == CONTAINER_PORT, "Neo4j Bolt binding contract mismatch")
    positive_integer(contract["bolt_port"], "Neo4j Bolt host port", minimum=1024)
    require(contract["bolt_port"] <= 65535, "Neo4j Bolt host port is too large")
    require(contract["network_mode"] == "bridge" and contract["restart_policy"] == "no", "Neo4j network/restart contract mismatch")
    require(contract["log_driver"] == LOG_DRIVER, "Neo4j log driver contract mismatch")
    runtime_owner = runtime_offline_gate["before_hash"]["root_identity"]
    require(
        contract["container_user"] == f"{runtime_owner['uid']}:{runtime_owner['gid']}",
        "Neo4j container user is not the owner of the 0700 runtime store",
    )
    require(
        contract["memory_limit_bytes"] == 0 and contract["memory_swap_limit_bytes"] == 0,
        "Neo4j Docker memory limit contract mismatch",
    )
    require(contract["read_only_default"] is True and contract["directory_overrides_forbidden"] is True, "Neo4j read-only/directory contract mismatch")
    memory = exact_keys(contract["memory"], ("heap_initial", "heap_max", "pagecache"), "Neo4j memory contract")
    require(validate_memory(memory["heap_initial"], memory["heap_max"], memory["pagecache"]) == memory, "Neo4j memory contract invalid")
    require(contract["environment"] == required_environment(memory), "Neo4j required environment mismatch")
    require(contract["labels"] == expected_labels(repeat_index, clone_id), "Neo4j runtime labels mismatch")
    require(contract["expected_entrypoint"] == EXPECTED_ENTRYPOINT, "Neo4j expected Entrypoint baseline mismatch")
    require(contract["expected_command"] == EXPECTED_COMMAND, "Neo4j expected Cmd baseline mismatch")
    container_name = nonempty(contract["container_name"], "Neo4j runtime container name")
    require(container_name.endswith(f"-r{repeat_index:02d}"), "Neo4j runtime container repeat suffix mismatch")

    docker = exact_keys(document["docker"], ("create", "created", "start", "running"), "Neo4j launch Docker record")
    create = validate_operation(docker["create"], "Neo4j Docker create")
    start = validate_operation(docker["start"], "Neo4j Docker start")
    created = validate_snapshot_record(docker["created"], "Neo4j Docker created snapshot", running=False)
    running = validate_snapshot_record(docker["running"], "Neo4j Docker running snapshot", running=True)
    validate_snapshot_against_contract(created, contract, "Neo4j Docker created snapshot")
    validate_snapshot_against_contract(running, contract, "Neo4j Docker running snapshot")
    require(created["runtime"]["started_at"].startswith("0001-01-01"), "Neo4j created snapshot was already started")
    require(not running["runtime"]["started_at"].startswith("0001-01-01"), "Neo4j running snapshot lacks StartedAt")
    require(created["runtime"]["exit_code"] == 0 and running["runtime"]["exit_code"] == 0, "Neo4j launch snapshot has nonzero exit state")
    require(running["runtime"]["finished_at"].startswith("0001-01-01"), "running Neo4j has a FinishedAt timestamp")
    require(create["command"] == build_create_command(contract), "Neo4j Docker create command differs from frozen contract")
    created_id = created["config"]["container_id"]
    require(start["command"] == ["docker", "container", "start", created_id], "Neo4j Docker start command is not exact-ID bound")
    require(start["stdout"] == created_id, "Neo4j Docker start output mismatch")
    require(create["stdout"] == created["config"]["container_id"], "Neo4j Docker create ID mismatch")
    require(created["config"] == running["config"], "Neo4j Docker config changed across start")
    require(running["config"]["container_name"] == container_name, "Neo4j snapshot container name mismatch")
    require(running["config"]["configured_image"] == IMAGE_REF and running["config"]["image_id"] == image["image_id"], "Neo4j snapshot image mismatch")
    require(running["config"]["network_mode"] == "bridge" and running["config"]["restart_policy"] == "no", "Neo4j snapshot network/restart mismatch")
    require(running["config"]["log_config"] == {"type": contract["log_driver"], "config": {}}, "Neo4j snapshot log config mismatch")
    mounts = running["config"]["mounts"]
    require({item.get("destination") for item in mounts if isinstance(item, dict)} == {"/data", "/logs"}, "Neo4j snapshot mounts are not exact")
    require(document["outcome"] == {"completed": True, "container_running": True, "exit_code": 0}, "Neo4j launch outcome mismatch")
    return document


def read_pinned_launch_receipt(path: Path, expected_sha256: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path = path.resolve()
    expected = exact_sha(expected_sha256, "launch receipt SHA-256")
    require(path.is_file() and not path.is_symlink(), f"launch receipt is missing: {path}")
    require(sha256_file(path) == expected, "launch receipt SHA-256 mismatch")
    reference = artifact_ref(path)
    document = validate_launch_receipt(read_json(path, "Neo4j launch receipt"), verify_artifacts=True, expected_host=current_host())
    return document, reference


def contract_from_launch(document: dict[str, Any]) -> dict[str, Any]:
    return dict(document["runtime_contract"])


def run_stop(args: argparse.Namespace) -> dict[str, Any]:
    require(1 <= args.timeout_s <= 300, "graceful stop timeout must be in [1, 300]")
    launch, launch_ref = read_pinned_launch_receipt(args.launch_receipt, args.launch_receipt_sha256)
    repeat_root = Path(launch["repeat"]["repeat_root"])
    store_root = Path(launch["clone"]["runtime_store_root"]["path"])
    logs_root = Path(launch["clone"]["logs_root"]["path"])
    output = validate_output_path(args.output, repeat_root, store_root, logs_root)
    contract = contract_from_launch(launch)
    name = contract["container_name"]
    container_id = launch["docker"]["running"]["config"]["container_id"]
    before = validate_container_inspect(docker_inspect_container(container_id), contract, expected_running=True)
    require(before == launch["docker"]["running"], "Neo4j container identity/config changed after launch receipt")
    stop_command = ["docker", "container", "stop", "--time", str(args.timeout_s), container_id]
    started_at = utc_now()
    completed = docker_call(stop_command, "gracefully stop Neo4j repeat", timeout=args.timeout_s + 30)
    finished_at = utc_now()
    after = validate_container_inspect(docker_inspect_container(container_id), contract, expected_running=False)
    require(after["config"] == before["config"], "Neo4j container config changed during stop")
    require(after["runtime"]["started_at"] == before["runtime"]["started_at"], "Neo4j StartedAt changed during stop")
    require(after["runtime"]["exit_code"] == 0, "Neo4j did not exit cleanly after graceful stop")
    require(after["runtime"]["finished_at"] != before["runtime"]["finished_at"], "Neo4j FinishedAt did not advance after stop")
    remove_command = ["docker", "container", "rm", container_id]
    remove_started_at = utc_now()
    removed = docker_call(remove_command, "remove stopped Neo4j repeat")
    remove_finished_at = utc_now()
    absence_command = [
        "docker", "container", "ls", "--all",
        "--filter", f"name=^/{name}$", "--format", "{{.ID}}",
    ]
    absence_started_at = utc_now()
    absent = docker_call(absence_command, "prove removed Neo4j repeat absent")
    absence_finished_at = utc_now()
    require(absent.stdout.strip() == "", "Neo4j repeat still exists after exact-ID removal")
    post_stop_filesystem = {
        "store_root": directory_identity(store_root),
        "logs_root": directory_identity(logs_root),
    }
    require(
        post_stop_filesystem
        == {
            "store_root": launch["clone"]["runtime_store_root"],
            "logs_root": launch["clone"]["logs_root"],
        },
        "Neo4j store/log owner-mode identity changed during the repeat",
    )
    host = current_host()
    document = {
        "schema_version": STOP_SCHEMA,
        "producer": artifact_ref(Path(__file__)),
        "host": host,
        "launch_receipt": launch_ref,
        "launch_identity": {
            "repeat_index": launch["repeat"]["repeat_index"],
            "clone_id": launch["repeat"]["clone_id"],
            "container_name": name,
            "container_id": before["config"]["container_id"],
            "pid": before["runtime"]["pid"],
            "started_at": before["runtime"]["started_at"],
            "restart_count": before["runtime"]["restart_count"],
        },
        "before_stop": before,
        "stop": {
            "command": stop_command,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": completed.returncode,
            "stdout": completed.stdout.strip(),
        },
        "post_stop_inspect": after,
        "remove": {
            "command": remove_command,
            "started_at": remove_started_at,
            "finished_at": remove_finished_at,
            "exit_code": removed.returncode,
            "stdout": removed.stdout.strip(),
        },
        "absence_probe": {
            "command": absence_command,
            "started_at": absence_started_at,
            "finished_at": absence_finished_at,
            "exit_code": absent.returncode,
            "stdout": absent.stdout,
        },
        "post_stop_filesystem": post_stop_filesystem,
        "outcome": {
            "completed": True,
            "container_running": False,
            "container_absent": True,
            "exit_code": after["runtime"]["exit_code"],
            "restart_count": after["runtime"]["restart_count"],
        },
    }
    validate_stop_receipt(document, verify_artifacts=True, expected_host=host, launch_document=launch)
    atomic_json(output, document)
    return document


def validate_stop_receipt(
    value: object,
    *,
    verify_artifacts: bool,
    expected_host: dict[str, str] | None = None,
    launch_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document = exact_keys(
        value,
        (
            "schema_version", "producer", "host", "launch_receipt", "launch_identity",
            "before_stop", "stop", "post_stop_inspect", "remove", "absence_probe",
            "post_stop_filesystem", "outcome",
        ),
        "Neo4j stop receipt",
    )
    require(document["schema_version"] == STOP_SCHEMA, "Neo4j stop receipt schema mismatch")
    producer = validate_artifact(document["producer"], "Neo4j stop producer", verify_file=verify_artifacts)
    if verify_artifacts:
        same_artifact(producer, artifact_ref(Path(__file__)), "Neo4j stop producer")
    host = exact_keys(document["host"], ("hostname", "fingerprint_sha256"), "Neo4j stop host")
    nonempty(host["hostname"], "Neo4j stop hostname")
    exact_sha(host["fingerprint_sha256"], "Neo4j stop host fingerprint")
    if expected_host is not None:
        require(host == expected_host, "Neo4j stop receipt host mismatch")
    launch_ref = validate_artifact(document["launch_receipt"], "Neo4j stop launch receipt", verify_file=verify_artifacts)
    if launch_document is None and verify_artifacts:
        launch_document = validate_launch_receipt(
            read_json(Path(launch_ref["path"]), "Neo4j stop launch receipt"),
            verify_artifacts=True,
            expected_host=host,
        )
    elif launch_document is not None and verify_artifacts:
        recorded_launch = validate_launch_receipt(
            read_json(Path(launch_ref["path"]), "Neo4j stop launch receipt"),
            verify_artifacts=True,
            expected_host=host,
        )
        require(recorded_launch == launch_document, "Neo4j supplied launch document differs from pinned receipt")
    require(launch_document is not None, "Neo4j stop validation requires its launch document")
    launch = validate_launch_receipt(launch_document, verify_artifacts=False, expected_host=host)
    identity = exact_keys(
        document["launch_identity"],
        ("repeat_index", "clone_id", "container_name", "container_id", "pid", "started_at", "restart_count"),
        "Neo4j stop launch identity",
    )
    expected_identity = {
        "repeat_index": launch["repeat"]["repeat_index"],
        "clone_id": launch["repeat"]["clone_id"],
        "container_name": launch["runtime_contract"]["container_name"],
        "container_id": launch["docker"]["running"]["config"]["container_id"],
        "pid": launch["docker"]["running"]["runtime"]["pid"],
        "started_at": launch["docker"]["running"]["runtime"]["started_at"],
        "restart_count": launch["docker"]["running"]["runtime"]["restart_count"],
    }
    require(identity == expected_identity, "Neo4j stop launch identity mismatch")
    before = validate_snapshot_record(document["before_stop"], "Neo4j before-stop snapshot", running=True)
    require(before == launch["docker"]["running"], "Neo4j before-stop snapshot differs from launch")
    stop = validate_operation(document["stop"], "Neo4j Docker stop")
    require(
        len(stop["command"]) == 6
        and stop["command"][:4] == ["docker", "container", "stop", "--time"]
        and stop["command"][-1] == identity["container_id"],
        "Neo4j graceful stop command mismatch",
    )
    require(stop["command"][4].isdigit() and 1 <= int(stop["command"][4]) <= 300, "Neo4j graceful stop timeout invalid")
    require(stop["stdout"] == identity["container_id"], "Neo4j graceful stop output mismatch")
    after = validate_snapshot_record(document["post_stop_inspect"], "Neo4j post-stop inspect", running=False)
    require(after["config"] == before["config"], "Neo4j post-stop config mismatch")
    require(after["runtime"]["started_at"] == before["runtime"]["started_at"], "Neo4j post-stop StartedAt mismatch")
    require(after["runtime"]["exit_code"] == 0 and after["runtime"]["restart_count"] == 0, "Neo4j post-stop exit/restart contract failed")
    require(after["runtime"]["finished_at"] != before["runtime"]["finished_at"], "Neo4j post-stop FinishedAt did not advance")
    require(not after["runtime"]["finished_at"].startswith("0001-01-01"), "Neo4j post-stop FinishedAt is unset")
    remove = validate_operation(document["remove"], "Neo4j Docker remove")
    require(
        remove["command"] == ["docker", "container", "rm", identity["container_id"]]
        and remove["stdout"] == identity["container_id"],
        "Neo4j remove command is not exact-ID bound",
    )
    absence = validate_operation(document["absence_probe"], "Neo4j removal absence probe")
    require(
        absence["command"]
        == [
            "docker", "container", "ls", "--all",
            "--filter", f"name=^/{identity['container_name']}$", "--format", "{{.ID}}",
        ]
        and absence["stdout"] == "",
        "Neo4j removal absence proof mismatch",
    )
    filesystem = exact_keys(
        document["post_stop_filesystem"],
        ("store_root", "logs_root"),
        "Neo4j post-stop filesystem identity",
    )
    expected_filesystem = {
        "store_root": launch["clone"]["runtime_store_root"],
        "logs_root": launch["clone"]["logs_root"],
    }
    for role in ("store_root", "logs_root"):
        validate_root_identity(filesystem[role], f"Neo4j post-stop {role}")
    require(filesystem == expected_filesystem, "Neo4j post-stop store/log identity differs from launch")
    if verify_artifacts:
        for role, identity_value in filesystem.items():
            require(
                directory_identity(Path(identity_value["path"])) == identity_value,
                f"Neo4j post-stop {role} identity differs from the filesystem",
            )
    require(
        document["outcome"]
        == {
            "completed": True,
            "container_running": False,
            "container_absent": True,
            "exit_code": 0,
            "restart_count": 0,
        },
        "Neo4j stop outcome mismatch",
    )
    return document


def validate_launch_receipts_independent(values: list[dict[str, Any]]) -> None:
    require(values, "Neo4j launch receipt set is empty")
    seen: dict[str, set[object]] = {
        "repeat_index": set(),
        "clone_id": set(),
        "repeat_root": set(),
        "runtime_store_root": set(),
        "logs_root": set(),
        "container_name": set(),
        "container_id": set(),
        "bolt_port": set(),
    }
    golden_source_binding: tuple[object, ...] | None = None
    for index, raw in enumerate(values):
        receipt = validate_launch_receipt(raw, verify_artifacts=False)
        fields: dict[str, object] = {
            "repeat_index": receipt["repeat"]["repeat_index"],
            "clone_id": receipt["repeat"]["clone_id"],
            "repeat_root": receipt["repeat"]["repeat_root"],
            "runtime_store_root": receipt["clone"]["runtime_store_root"]["path"],
            "logs_root": receipt["clone"]["logs_root"]["path"],
            "container_name": receipt["runtime_contract"]["container_name"],
            "container_id": receipt["docker"]["running"]["config"]["container_id"],
            "bolt_port": receipt["runtime_contract"]["bolt_port"],
        }
        for key, value in fields.items():
            require(value not in seen[key], f"Neo4j launch receipts reuse {key}: repeat entry {index}")
            seen[key].add(value)
        source_tree = receipt["clone"]["independence_audit"]["source_tree"]
        source_binding = (
            receipt["clone"]["source_store_root"]["path"],
            source_tree["store_sha256"],
            source_tree["file_count"],
            source_tree["total_bytes"],
        )
        if golden_source_binding is None:
            golden_source_binding = source_binding
        else:
            require(
                source_binding == golden_source_binding,
                "Neo4j repeats do not bind one identical golden source lineage/tree",
            )


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.action == "launch":
            run_launch(args)
        else:
            run_stop(args)
        return 0
    except (ContractError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"launch_neo4j_runtime: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
