#!/usr/bin/env python3
"""Create a fresh Neo4j store and an auditable controlled-import receipt.

This is the only producer accepted by the formal P10 Neo4j store freezer.  It
refuses a pre-existing store, resolves the actual Docker image identity, runs
the full import, builds the required ``:V(id)`` index, stops Neo4j cleanly, and
only then records the complete offline tree.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))

from adapters.neo4j_store_contract import (  # noqa: E402
    CANONICAL_SENTINELS,
    KNOWN_MUTABLE_PATTERNS,
    TREE_HASH_METHOD,
    canonical_sentinel_records,
    stable_tree_manifest,
)
from p10_contract import ContractError, atomic_json, read_json, sha256_file  # noqa: E402

P31_DIR = P10_DIR.parent / "p31"
sys.path.insert(0, str(P31_DIR))
from run_manifest import host_facts as p31_host_facts  # noqa: E402

SCHEMA_VERSION = "p10-neo4j-controlled-import-receipt-v3"
IMAGE_REF = "neo4j:5.26.24"
DATABASE_NAME = "neo4j"
INDEX_NAME = "v_id"
NODE_LABEL = "V"
ID_PROPERTY = "id"
EXPECTED_ENTRYPOINT = ["tini", "-g", "--", "/startup/docker-entrypoint.sh"]
EXPECTED_COMMAND = ["neo4j"]
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--import-root", required=True, type=Path)
    parser.add_argument("--logs-root", required=True, type=Path)
    parser.add_argument("--nodes-file", default="nodes.csv")
    parser.add_argument("--relationships-file", default="rels.csv")
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--image-ref", default=IMAGE_REF)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--container-prefix", required=True)
    parser.add_argument("--import-timeout-s", type=int, default=21600)
    parser.add_argument("--readiness-timeout-s", type=int, default=300)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def exact_image_digest(value: object, context: str) -> str:
    require(
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:]),
        f"{context} must be sha256:<64 lowercase hex>",
    )
    return str(value)


def artifact_ref(path: Path) -> dict[str, Any]:
    path = path.resolve()
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def safe_input(root: Path, raw: str, role: str) -> dict[str, Any]:
    relative = Path(raw)
    require(
        raw
        and not relative.is_absolute()
        and ".." not in relative.parts
        and relative.as_posix() == raw,
        f"{role} must be one normalized relative path",
    )
    path = (root / relative).resolve()
    require(root in path.parents and path.is_file() and not path.is_symlink(), f"invalid {role}: {raw}")
    return {
        "role": role,
        "relative_path": raw,
        **artifact_ref(path),
    }


def docker_json(command: list[str], context: str) -> Any:
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    require(completed.returncode == 0, f"{context}: {completed.stderr.strip()}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{context}: Docker returned malformed JSON") from exc


def docker_call(command: list[str], context: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    require(completed.returncode == 0, f"{context}: {completed.stderr.strip()}")
    return completed


def image_identity(image_ref: str, expected_digest: str) -> dict[str, Any]:
    values = docker_json(["docker", "image", "inspect", image_ref], "Docker image inspect")
    require(isinstance(values, list) and len(values) == 1, "Docker returned an ambiguous image")
    image = values[0]
    image_id = exact_image_digest(image.get("Id"), "Docker image ID")
    image_config = image.get("Config") or {}
    require(isinstance(image_config, dict), "Docker image Config is malformed")
    require(
        image_config.get("Entrypoint") == EXPECTED_ENTRYPOINT,
        "Docker image Config.Entrypoint differs from the frozen Neo4j baseline",
    )
    require(
        image_config.get("Cmd") == EXPECTED_COMMAND,
        "Docker image Config.Cmd differs from the frozen Neo4j baseline",
    )
    repo_digests = image.get("RepoDigests") or []
    require(isinstance(repo_digests, list), "Docker image RepoDigests is malformed")
    resolved = {
        value.split("@", 1)[1]
        for value in repo_digests
        if isinstance(value, str) and "@" in value
    }
    require(expected_digest in resolved, "requested RepoDigest is not attached to the actual image")
    return {
        "configured_ref": image_ref,
        "image_id": image_id,
        "repo_digests": sorted(repo_digests),
        "selected_repo_digest": expected_digest,
    }


def normalized_mounts(container: dict[str, Any]) -> list[dict[str, Any]]:
    mounts = container.get("Mounts") or []
    require(isinstance(mounts, list), "container mounts are malformed")
    result = []
    for mount in mounts:
        require(isinstance(mount, dict), "container mount entry is malformed")
        result.append(
            {
                "type": mount.get("Type"),
                "source": mount.get("Source"),
                "destination": mount.get("Destination"),
                "rw": mount.get("RW"),
            }
        )
    return sorted(result, key=lambda item: str(item["destination"]))


def root_identity(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    require(stat.S_ISDIR(metadata.st_mode) and not path.is_symlink(), f"not a real directory: {path}")
    require(
        metadata.st_uid == os.geteuid() and metadata.st_gid == os.getegid(),
        f"directory must be owned by the importing UID:GID: {path}",
    )
    require(stat.S_IMODE(metadata.st_mode) == 0o700, f"directory must have exact owner-only mode 0700: {path}")
    return {
        "path": str(path.resolve()),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "uid": int(metadata.st_uid),
        "gid": int(metadata.st_gid),
        "mode": "0700",
    }


def inspect_stage(
    identifier: str,
    *,
    expected_name: str,
    expected_image: dict[str, Any],
    expected_command: list[str],
    expected_mounts: list[dict[str, Any]],
    expected_network: str,
    expected_user: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    require(CONTAINER_ID_RE.fullmatch(identifier) is not None, "container inspect requires a full ID")
    values = docker_json(["docker", "container", "inspect", identifier], f"inspect {identifier}")
    require(isinstance(values, list) and len(values) == 1, f"ambiguous container inspect: {identifier}")
    container = values[0]
    require(container.get("Id") == identifier, "container inspect returned a different ID")
    require(container.get("Name") == f"/{expected_name}", f"container name mismatch: {expected_name}")
    require(container.get("Config", {}).get("Image") == expected_image["configured_ref"], "container image ref mismatch")
    require(container.get("Config", {}).get("User") == expected_user, "container user differs from controlled store owner")
    require(container.get("Image") == expected_image["image_id"], "container image ID mismatch")
    require(
        container.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") == "no",
        "controlled import containers require restart=no",
    )
    require(container.get("HostConfig", {}).get("NetworkMode") == expected_network, "container network mode mismatch")
    mounts = normalized_mounts(container)
    require(mounts == sorted(expected_mounts, key=lambda item: str(item["destination"])), "container mounts differ from controlled command")
    entrypoint = container.get("Config", {}).get("Entrypoint")
    configured_cmd = container.get("Config", {}).get("Cmd")
    require(entrypoint == EXPECTED_ENTRYPOINT, "container Entrypoint differs from the frozen Neo4j baseline")
    require(configured_cmd == expected_command, "container command differs from controlled command")
    state = container.get("State") or {}
    snapshot = {
        "container_name": expected_name,
        "container_id": identifier,
        "configured_image": container.get("Config", {}).get("Image"),
        "container_user": container.get("Config", {}).get("User"),
        "image_id": container.get("Image"),
        "entrypoint": entrypoint,
        "command": configured_cmd,
        "environment": sorted(container.get("Config", {}).get("Env") or []),
        "network_mode": container.get("HostConfig", {}).get("NetworkMode"),
        "restart_policy": container.get("HostConfig", {}).get("RestartPolicy", {}).get("Name"),
        "mounts": mounts,
        "ports": container.get("NetworkSettings", {}).get("Ports") or {},
    }
    runtime = {
        "running": state.get("Running"),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "exit_code": state.get("ExitCode"),
        "restart_count": container.get("RestartCount"),
    }
    return snapshot, runtime


def start_attached(container_id: str, log_path: Path, timeout_s: int) -> None:
    require(CONTAINER_ID_RE.fullmatch(container_id) is not None, "attached start requires a full ID")
    with log_path.open("xb") as handle:
        completed = subprocess.run(
            ["docker", "container", "start", "--attach", container_id],
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout_s,
        )
    require(completed.returncode == 0, f"controlled import container exited {completed.returncode}")


def bolt_port(container_id: str) -> int:
    require(CONTAINER_ID_RE.fullmatch(container_id) is not None, "port lookup requires a full ID")
    values = docker_json(["docker", "container", "inspect", container_id], f"inspect {container_id} port")
    require(values[0].get("Id") == container_id, "port lookup returned a different container ID")
    bindings = values[0].get("NetworkSettings", {}).get("Ports", {}).get("7687/tcp")
    require(isinstance(bindings, list) and len(bindings) == 1, "schema stage lacks one Bolt binding")
    require(bindings[0].get("HostIp") == "127.0.0.1", "schema Bolt port is not localhost-only")
    value = bindings[0].get("HostPort")
    require(isinstance(value, str) and value.isdigit(), "schema Bolt host port is invalid")
    return int(value)


def build_index(container_id: str, timeout_s: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_error = "not attempted"
    while True:
        remaining = deadline - time.monotonic()
        require(remaining > 0, f"schema-finalize readiness timed out: {last_error}")
        driver = None
        try:
            uri = f"bolt://127.0.0.1:{bolt_port(container_id)}"
            driver = GraphDatabase.driver(
                uri,
                auth=None,
                connection_timeout=max(0.001, min(30.0, remaining)),
                max_connection_pool_size=1,
            )
            driver.verify_connectivity()
            with driver.session(database=DATABASE_NAME) as session:
                session.run("CREATE INDEX v_id IF NOT EXISTS FOR (v:V) ON (v.id)").consume()
                session.run("CALL db.awaitIndexes() ").consume()
                rows = [
                    record.data()
                    for record in session.run(
                        "SHOW INDEXES YIELD name, state, type, entityType, labelsOrTypes, properties "
                        "WHERE name = $name RETURN name, state, type, entityType, labelsOrTypes, properties",
                        name=INDEX_NAME,
                    )
                ]
            require(len(rows) == 1, "schema-finalize did not create exactly one v_id index")
            expected = {
                "name": INDEX_NAME,
                "state": "ONLINE",
                "type": "RANGE",
                "entityType": "NODE",
                "labelsOrTypes": [NODE_LABEL],
                "properties": [ID_PROPERTY],
            }
            require(rows[0] == expected, "schema-finalize v_id index contract mismatch")
            return {
                "database_name": DATABASE_NAME,
                "node_label": NODE_LABEL,
                "id_property": ID_PROPERTY,
                "required_index": expected,
            }
        except Exception as exc:
            if isinstance(exc, ContractError):
                raise
            last_error = f"{exc.__class__.__name__}: {exc}"
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        finally:
            if driver is not None:
                driver.close()


def remove_container(container_id: str, *, force: bool) -> None:
    require(CONTAINER_ID_RE.fullmatch(container_id) is not None, "container cleanup requires a full ID")
    command = ["docker", "container", "rm"]
    if force:
        command.append("--force")
    command.append(container_id)
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=60)


def prove_container_absent(name: str) -> list[str]:
    command = [
        "docker", "container", "ls", "--all",
        "--filter", f"name=^/{name}$", "--format", "{{.ID}}",
    ]
    completed = docker_call(command, f"prove container absent: {name}")
    require(completed.stdout.strip() == "", f"container still exists after removal: {name}")
    return command


def run(args: argparse.Namespace) -> None:
    require(args.image_ref == IMAGE_REF, f"controlled importer image must be {IMAGE_REF!r}")
    image_digest = exact_image_digest(args.image_digest, "image digest")
    require(NAME_RE.fullmatch(args.container_prefix) is not None, "invalid container prefix")
    require(args.import_timeout_s > 0, "import timeout must be positive")
    require(1 <= args.readiness_timeout_s <= 900, "readiness timeout must be in [1, 900]")
    store = args.store_root.absolute()
    require(not store.exists(), f"controlled importer refuses a pre-existing store: {store}")
    require(store.parent.is_dir(), f"store parent is missing: {store.parent}")
    output = args.output.resolve()
    require(not output.exists(), f"refusing to overwrite receipt: {output}")
    require(output.parent.is_dir(), f"receipt parent is missing: {output.parent}")
    require(store not in output.parents, "receipt must be outside the store")
    import_root = args.import_root.resolve()
    require(import_root.is_dir() and not args.import_root.is_symlink(), "import root is invalid")
    logs_root = args.logs_root.absolute()
    require(not logs_root.exists(), f"controlled importer refuses a pre-existing logs root: {logs_root}")
    require(logs_root.parent.is_dir(), f"logs parent is missing: {logs_root.parent}")
    require(
        logs_root != store and logs_root not in store.parents and store not in logs_root.parents,
        "controlled import /data and /logs roots must be independent",
    )
    dataset_path = args.dataset_manifest.resolve()
    dataset = read_json(dataset_path, "dataset manifest")
    require(dataset.get("schema_version") == "p02b-dataset-manifest-v1", "dataset manifest schema mismatch")
    dataset_ref = artifact_ref(dataset_path)
    inputs = [
        safe_input(import_root, args.nodes_file, "nodes"),
        safe_input(import_root, args.relationships_file, "relationships"),
    ]
    input_digest = hashlib.sha256()
    for item in inputs:
        input_digest.update(
            f"{item['role']}\0{item['relative_path']}\0{item['size_bytes']}\0{item['sha256']}\n".encode()
        )
    image = image_identity(args.image_ref, image_digest)
    import_name = f"{args.container_prefix}-import"
    schema_name = f"{args.container_prefix}-schema"
    import_log = output.with_name(output.name + ".import.log")
    schema_log = output.with_name(output.name + ".schema.log")
    for path in (import_log, schema_log):
        require(not path.exists(), f"refusing to overwrite log: {path}")

    store.mkdir(mode=0o700)
    logs_root.mkdir(mode=0o700)
    initial_store_identity = root_identity(store)
    initial_logs_identity = root_identity(logs_root)
    container_user = f"{initial_store_identity['uid']}:{initial_store_identity['gid']}"
    require(
        (initial_store_identity["uid"], initial_store_identity["gid"])
        == (initial_logs_identity["uid"], initial_logs_identity["gid"]),
        "controlled import /data and /logs owners differ",
    )
    import_command = [
        "neo4j-admin",
        "database",
        "import",
        "full",
        "--overwrite-destination=true",
        "--id-type=integer",
        f"--nodes=V=/import/{args.nodes_file}",
        f"--relationships=/import/{args.relationships_file}",
        "--",
        DATABASE_NAME,
    ]
    data_mount = {
        "type": "bind",
        "source": str(store.resolve()),
        "destination": "/data",
        "rw": True,
    }
    import_mount = {
        "type": "bind",
        "source": str(import_root),
        "destination": "/import",
        "rw": False,
    }
    logs_mount = {
        "type": "bind",
        "source": str(logs_root.resolve()),
        "destination": "/logs",
        "rw": True,
    }
    stages: list[dict[str, Any]] = []
    active: set[str] = set()
    try:
        import_create_command = [
            "docker", "container", "create", "--pull", "never", "--name", import_name,
            "--restart", "no", "--network", "none",
            "--user", container_user,
            "--mount", f"type=bind,src={store.resolve()},dst=/data",
            "--mount", f"type=bind,src={logs_root.resolve()},dst=/logs",
            "--mount", f"type=bind,src={import_root},dst=/import,readonly",
            args.image_ref,
            *import_command,
        ]
        created = docker_call(import_create_command, "create controlled import container")
        import_id = created.stdout.strip()
        require(CONTAINER_ID_RE.fullmatch(import_id) is not None, "Docker did not return a full import container ID")
        active.add(import_id)
        import_config, _created_runtime = inspect_stage(
            import_id,
            expected_name=import_name,
            expected_image=image,
            expected_command=import_command,
            expected_mounts=[data_mount, logs_mount, import_mount],
            expected_network="none",
            expected_user=container_user,
        )
        import_start_command = ["docker", "container", "start", "--attach", import_id]
        start_attached(import_id, import_log, args.import_timeout_s)
        import_final_config, import_runtime = inspect_stage(
            import_id,
            expected_name=import_name,
            expected_image=image,
            expected_command=import_command,
            expected_mounts=[data_mount, logs_mount, import_mount],
            expected_network="none",
            expected_user=container_user,
        )
        require(import_final_config == import_config, "full-import container config changed after create")
        require(import_runtime["running"] is False, "import container is still running")
        require(import_runtime["exit_code"] == 0, "import container exit code is not zero")
        require(import_runtime["restart_count"] == 0, "import container restarted")
        import_remove_command = ["docker", "container", "rm", import_id]
        removed = docker_call(import_remove_command, "remove import container")
        require(removed.stdout.strip() == import_id, "import removal did not return its exact ID")
        active.remove(import_id)
        import_absence_command = prove_container_absent(import_name)
        stages.append(
            {
                "name": "full-import",
                "config": import_config,
                "runtime": import_runtime,
                "log": artifact_ref(import_log),
                "lifecycle": {
                    "create": {"command": import_create_command, "container_id": import_id},
                    "start": {"command": import_start_command},
                    "stop": None,
                    "remove": {"command": import_remove_command, "stdout": removed.stdout.strip()},
                    "absence_probe": {"command": import_absence_command, "stdout": ""},
                },
            }
        )

        schema_create_command = [
            "docker", "container", "create", "--pull", "never", "--name", schema_name,
            "--restart", "no", "--publish", "127.0.0.1::7687",
            "--user", container_user,
            "--mount", f"type=bind,src={store.resolve()},dst=/data",
            "--mount", f"type=bind,src={logs_root.resolve()},dst=/logs",
            "--env", "NEO4J_AUTH=none",
            args.image_ref,
        ]
        created = docker_call(schema_create_command, "create schema-finalize container")
        schema_id = created.stdout.strip()
        require(CONTAINER_ID_RE.fullmatch(schema_id) is not None, "Docker did not return a full schema container ID")
        active.add(schema_id)
        schema_config, _created_runtime = inspect_stage(
            schema_id,
            expected_name=schema_name,
            expected_image=image,
            expected_command=EXPECTED_COMMAND,
            expected_mounts=[data_mount, logs_mount],
            expected_network="default",
            expected_user=container_user,
        )
        schema_start_command = ["docker", "container", "start", schema_id]
        started = docker_call(schema_start_command, "start schema-finalize container")
        require(started.stdout.strip() == schema_id, "schema start did not return its exact ID")
        database_contract = build_index(schema_id, args.readiness_timeout_s)
        schema_stop_command = ["docker", "container", "stop", "--time", "60", schema_id]
        stopped = docker_call(
            schema_stop_command,
            "stop schema-finalize container",
            timeout=90,
        )
        require(stopped.stdout.strip() == schema_id, "schema stop did not return its exact ID")
        logs = docker_call(["docker", "container", "logs", schema_id], "read schema-finalize logs")
        schema_log.write_text(logs.stdout + logs.stderr, encoding="utf-8")
        schema_final_config, schema_runtime = inspect_stage(
            schema_id,
            expected_name=schema_name,
            expected_image=image,
            expected_command=EXPECTED_COMMAND,
            expected_mounts=[data_mount, logs_mount],
            expected_network="default",
            expected_user=container_user,
        )
        require(schema_final_config == schema_config, "schema-finalize container config changed after create")
        require(schema_runtime["running"] is False, "schema-finalize container is still running")
        require(schema_runtime["exit_code"] == 0, "schema-finalize container exit code is not zero")
        require(schema_runtime["restart_count"] == 0, "schema-finalize container restarted")
        schema_remove_command = ["docker", "container", "rm", schema_id]
        removed = docker_call(schema_remove_command, "remove schema-finalize container")
        require(removed.stdout.strip() == schema_id, "schema removal did not return its exact ID")
        active.remove(schema_id)
        schema_absence_command = prove_container_absent(schema_name)
        stages.append(
            {
                "name": "schema-finalize",
                "config": schema_config,
                "runtime": schema_runtime,
                "database_contract": database_contract,
                "log": artifact_ref(schema_log),
                "lifecycle": {
                    "create": {"command": schema_create_command, "container_id": schema_id},
                    "start": {"command": schema_start_command},
                    "stop": {"command": schema_stop_command, "stdout": stopped.stdout.strip()},
                    "remove": {"command": schema_remove_command, "stdout": removed.stdout.strip()},
                    "absence_probe": {"command": schema_absence_command, "stdout": ""},
                },
            }
        )

        store.chmod(0o700)
        store_lock = store / "databases" / "store_lock"
        require(store_lock.is_file() and not store_lock.is_symlink(), "controlled import lacks databases/store_lock")
        store_lock.chmod(0o600)
        final_store_identity = root_identity(store)
        final_logs_identity = root_identity(logs_root)
        require(final_store_identity == initial_store_identity, "controlled import changed the store root identity")
        require(final_logs_identity == initial_logs_identity, "controlled import changed the logs root identity")
        tree = stable_tree_manifest(store, attempts=2)
        canonical_sentinel_records(tree["files"])
        current_host = p31_host_facts()
        producer = artifact_ref(Path(__file__))
        atomic_json(
            output,
            {
                "schema_version": SCHEMA_VERSION,
                "producer": producer,
                "host": {
                    "hostname": current_host["hostname"],
                    "fingerprint_sha256": current_host["fingerprint_sha256"],
                },
                "image": image,
                "docker_config_baseline": {
                    "entrypoint": list(EXPECTED_ENTRYPOINT),
                    "command": list(EXPECTED_COMMAND),
                },
                "logs_root": str(logs_root.resolve()),
                "filesystem_identity": {
                    "container_user": container_user,
                    "store_root": final_store_identity,
                    "logs_root": final_logs_identity,
                },
                "input": {
                    "root": str(import_root),
                    "dataset_manifest": dataset_ref,
                    "dataset_sha256": dataset.get("dataset_sha256"),
                    "files": inputs,
                    "input_sha256": input_digest.hexdigest(),
                },
                "stages": stages,
                "database_contract": database_contract,
                "final_store": {
                    "root": str(store.resolve()),
                    "hash_method": TREE_HASH_METHOD,
                    **{key: tree[key] for key in (
                        "store_sha256", "file_count", "total_bytes",
                        "immutable_store_sha256", "immutable_file_count", "immutable_total_bytes",
                    )},
                },
                "known_mutable_patterns": list(KNOWN_MUTABLE_PATTERNS),
                "canonical_sentinels": list(CANONICAL_SENTINELS),
                "store_created_from_empty": True,
                "outcome": {"completed": True, "exit_code": 0},
                "neo4j_driver_version": importlib.metadata.version("neo4j"),
            },
        )
    finally:
        for name in sorted(active):
            remove_container(name, force=True)


def main() -> int:
    try:
        run(parse_args())
        return 0
    except (
        ContractError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
        importlib.metadata.PackageNotFoundError,
    ) as exc:
        print(f"import_neo4j_store: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
