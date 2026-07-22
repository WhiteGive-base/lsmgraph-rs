#!/usr/bin/env python3
"""Shared, fail-closed Neo4j store snapshot and runtime-delta contract."""

from __future__ import annotations

import hashlib
import os
import stat
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from p10_contract import ContractError, read_json, sha256_file

TREE_HASH_METHOD = "sha256-tree-v1(relative-path,size,file-sha256)"
OFFLINE_PROOF_METHOD = "owner-only-root-exclusive-store-lock-no-docker-mount-v1"
CANONICAL_SENTINELS = (
    "databases/neo4j/neostore.nodestore.db",
    "databases/neo4j/neostore.relationshipstore.db",
)
EXPECTED_ENTRYPOINT = ["tini", "-g", "--", "/startup/docker-entrypoint.sh"]
EXPECTED_COMMAND = ["neo4j"]

# Neo4j Community cannot make the complete DBMS tree immutable while it is
# online: the system database is always writable, transaction logs and lock
# files are runtime state, and a server identifier can be created at startup.
# The query database itself stays immutable except for its database_lock.
KNOWN_MUTABLE_PATTERNS = (
    "server_id",
    "logs/**",
    "transactions/**",
    "databases/store_lock",
    "databases/*/database_lock",
    "databases/system/**",
)


class TreeChangedError(ContractError):
    """A store path or stat identity changed while it was being hashed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def is_known_mutable(relative_path: str) -> bool:
    return any(fnmatchcase(relative_path, pattern) for pattern in KNOWN_MUTABLE_PATTERNS)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _enumerate_regular_files(root: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in sorted(dirnames):
            child = directory_path / name
            require(not child.is_symlink(), f"store tree rejects symlink directory: {child}")
        for name in sorted(filenames):
            path = directory_path / name
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise TreeChangedError(f"store file disappeared during enumeration: {path}") from exc
            require(
                stat.S_ISREG(metadata.st_mode) and not path.is_symlink(),
                f"store tree found non-regular file: {path}",
            )
            files.append((path.relative_to(root).as_posix(), path))
    files.sort(key=lambda item: item[0].encode())
    require(files, "store tree is empty")
    return files


def _digest_records(records: list[dict[str, Any]]) -> dict[str, int | str]:
    digest = hashlib.sha256()
    total_bytes = 0
    for record in records:
        relative = str(record["path"])
        size = int(record["size_bytes"])
        file_sha = str(record["sha256"])
        total_bytes += size
        digest.update(f"file\0{relative}\0{size}\0{file_sha}\n".encode())
    return {
        "tree_sha256": digest.hexdigest(),
        "file_count": len(records),
        "total_bytes": total_bytes,
    }


def summarize_records(records: list[dict[str, Any]]) -> dict[str, int | str]:
    """Return full and immutable summaries for sorted file records."""

    full = _digest_records(records)
    immutable_records = [record for record in records if not is_known_mutable(str(record["path"]))]
    require(immutable_records, "Neo4j store has no immutable query-database files")
    immutable = _digest_records(immutable_records)
    return {
        "store_sha256": full["tree_sha256"],
        "file_count": full["file_count"],
        "total_bytes": full["total_bytes"],
        "immutable_store_sha256": immutable["tree_sha256"],
        "immutable_file_count": immutable["file_count"],
        "immutable_total_bytes": immutable["total_bytes"],
    }


def _hash_file(path: Path, *, evict_cache: bool) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        while True:
            block = handle.read(8 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
        if evict_cache:
            require(
                hasattr(os, "posix_fadvise") and hasattr(os, "POSIX_FADV_DONTNEED"),
                "formal store audit requires POSIX_FADV_DONTNEED cache eviction",
            )
            try:
                os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError as exc:
                raise ContractError(f"cannot evict audit pages for {path}: {exc}") from exc
    return digest.hexdigest()


def stable_tree_manifest(
    root: Path,
    *,
    attempts: int = 2,
    evict_cache: bool = False,
    allow_known_mutable_changes: bool = False,
) -> dict[str, Any]:
    """Hash every store file while proving path/stat stability around reads."""

    root = root.resolve()
    require(root.is_dir(), f"store root does not exist: {root}")
    require(attempts >= 1, "stable tree attempts must be positive")
    last_error: TreeChangedError | None = None
    for _attempt in range(attempts):
        try:
            initial = _enumerate_regular_files(root)
            initial_paths = [relative for relative, _path in initial]
            records: list[dict[str, Any]] = []
            initial_stats: dict[str, tuple[int, int, int, int, int, int]] = {}
            for relative, path in initial:
                file_attempts = 3 if allow_known_mutable_changes and is_known_mutable(relative) else 1
                before: os.stat_result | None = None
                before_identity: tuple[int, int, int, int, int, int] | None = None
                file_sha = ""
                for _file_attempt in range(file_attempts):
                    before = path.lstat()
                    before_identity = _stat_identity(before)
                    file_sha = _hash_file(path, evict_cache=evict_cache)
                    after = path.lstat()
                    if _stat_identity(after) == before_identity:
                        break
                else:
                    raise TreeChangedError(f"store file changed while hashing: {relative}")
                assert before is not None and before_identity is not None
                initial_stats[relative] = before_identity
                records.append(
                    {
                        "path": relative,
                        "size_bytes": before.st_size,
                        "sha256": file_sha,
                    }
                )
            final = _enumerate_regular_files(root)
            final_paths = [relative for relative, _path in final]
            if final_paths != initial_paths:
                raise TreeChangedError("store path set changed while hashing")
            for relative, path in final:
                if _stat_identity(path.lstat()) != initial_stats[relative] and not (
                    allow_known_mutable_changes and is_known_mutable(relative)
                ):
                    raise TreeChangedError(f"store file stat changed after tree hash: {relative}")
            return {
                **summarize_records(records),
                "files": records,
                "cache_eviction": "posix-fadvise-dontneed-v1" if evict_cache else "none",
                "known_mutable_changes_tolerated": allow_known_mutable_changes,
            }
        except TreeChangedError as exc:
            last_error = exc
    raise ContractError(f"Neo4j store never reached a stable hash snapshot: {last_error}")


def validate_recorded_tree(value: object, context: str) -> dict[str, Any]:
    """Validate manifest records and recompute all declared summaries."""

    require(isinstance(value, dict), f"{context}: expected object")
    files = value.get("files")
    require(isinstance(files, list) and files, f"{context}.files must be non-empty")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(files):
        item_context = f"{context}.files[{index}]"
        require(isinstance(raw, dict), f"{item_context}: expected object")
        require(set(raw) == {"path", "size_bytes", "sha256"}, f"{item_context}: wrong keys")
        relative = raw.get("path")
        require(
            isinstance(relative, str)
            and relative
            and not Path(relative).is_absolute()
            and ".." not in Path(relative).parts,
            f"{item_context}.path is unsafe",
        )
        require(relative not in seen, f"{item_context}.path is duplicated")
        seen.add(relative)
        size = raw.get("size_bytes")
        require(isinstance(size, int) and not isinstance(size, bool) and size >= 0, f"{item_context}.size_bytes invalid")
        file_sha = raw.get("sha256")
        require(
            isinstance(file_sha, str)
            and len(file_sha) == 64
            and all(character in "0123456789abcdef" for character in file_sha),
            f"{item_context}.sha256 invalid",
        )
        normalized.append({"path": relative, "size_bytes": size, "sha256": file_sha})
    normalized.sort(key=lambda item: str(item["path"]).encode())
    require(normalized == files, f"{context}.files are not bytewise-path sorted")
    summary = summarize_records(normalized)
    for key, expected in summary.items():
        require(value.get(key) == expected, f"{context}.{key} differs from file records")
    return {**summary, "files": normalized}


def validate_owner_only_offline_gate(
    value: object,
    expected_root: Path,
    context: str,
) -> dict[str, Any]:
    """Validate the process-independent offline proof held across a tree hash."""

    expected_root = expected_root.resolve()

    def exact_mapping(raw: object, keys: tuple[str, ...], label: str) -> dict[str, Any]:
        require(isinstance(raw, dict) and set(raw) == set(keys), f"{label}: wrong keys")
        return raw

    def integer(raw: object, label: str, minimum: int) -> int:
        require(isinstance(raw, int) and not isinstance(raw, bool) and raw >= minimum, f"{label}: invalid integer")
        return raw

    gate = exact_mapping(value, ("proof_method", "before_hash", "after_hash"), context)
    require(gate["proof_method"] == OFFLINE_PROOF_METHOD, f"{context}: proof method mismatch")
    snapshots: list[dict[str, Any]] = []
    for phase in ("before_hash", "after_hash"):
        phase_context = f"{context}.{phase}"
        snapshot = exact_mapping(
            gate[phase],
            ("root_identity", "docker_mount_audit", "store_lock"),
            phase_context,
        )
        root = exact_mapping(
            snapshot["root_identity"],
            ("path", "device", "inode", "uid", "gid", "mode"),
            f"{phase_context}.root_identity",
        )
        require(Path(str(root["path"])).resolve() == expected_root, f"{context}: root path mismatch")
        for key, minimum in (("device", 0), ("inode", 1), ("uid", 0), ("gid", 0)):
            integer(root[key], f"{phase_context}.root_identity.{key}", minimum)
        require(root["mode"] == "0700", f"{context}: root mode is not 0700")

        docker = exact_mapping(
            snapshot["docker_mount_audit"],
            ("running_container_count", "inspected_container_ids", "overlapping_mounts"),
            f"{phase_context}.docker_mount_audit",
        )
        count = integer(
            docker["running_container_count"],
            f"{phase_context}.docker_mount_audit.running_container_count",
            0,
        )
        identifiers = docker["inspected_container_ids"]
        require(
            isinstance(identifiers, list)
            and identifiers == sorted(set(identifiers))
            and len(identifiers) == count
            and all(
                isinstance(identifier, str)
                and len(identifier) == 64
                and all(character in "0123456789abcdef" for character in identifier)
                for identifier in identifiers
            ),
            f"{phase_context}: Docker identity audit malformed",
        )
        require(docker["overlapping_mounts"] == [], f"{phase_context}: overlapping Docker mount recorded")

        lock = exact_mapping(
            snapshot["store_lock"],
            (
                "path", "device", "inode", "uid", "gid", "mode", "size_bytes",
                "method", "acquired_exclusive",
            ),
            f"{phase_context}.store_lock",
        )
        require(
            Path(str(lock["path"])).resolve() == expected_root / "databases" / "store_lock",
            f"{context}: store_lock path mismatch",
        )
        for key, minimum in (
            ("device", 0), ("inode", 1), ("uid", 0), ("gid", 0), ("size_bytes", 0),
        ):
            integer(lock[key], f"{phase_context}.store_lock.{key}", minimum)
        require(lock["uid"] == root["uid"] and lock["gid"] == root["gid"], f"{context}: root/lock owner mismatch")
        require(lock["mode"] == "0600", f"{context}: store_lock mode is not 0600")
        require(
            lock["method"] == "fcntl-lockf-exclusive-nonblocking-v1"
            and lock["acquired_exclusive"] is True,
            f"{context}: exclusive store_lock proof missing",
        )
        snapshots.append(snapshot)
    require(snapshots[0]["root_identity"] == snapshots[1]["root_identity"], f"{context}: root identity changed")
    require(snapshots[0]["store_lock"] == snapshots[1]["store_lock"], f"{context}: store_lock identity changed")
    return gate


def canonical_sentinel_records(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_path = {str(item["path"]): item for item in files}
    missing = [path for path in CANONICAL_SENTINELS if path not in by_path]
    require(not missing, f"Neo4j store lacks canonical sentinels: {missing!r}")
    return [dict(by_path[path]) for path in CANONICAL_SENTINELS]


def _exact_sha(value: object, context: str) -> str:
    require(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value),
        f"{context}: expected 64 lowercase hex characters",
    )
    return str(value)


def _exact_image_digest(value: object, context: str) -> str:
    require(
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:]),
        f"{context}: expected sha256:<64 lowercase hex>",
    )
    return str(value)


def _artifact(
    value: object,
    context: str,
    *,
    verify_file: bool,
) -> dict[str, Any]:
    require(isinstance(value, dict), f"{context}: expected artifact object")
    require(set(value) == {"path", "sha256", "size_bytes"}, f"{context}: wrong keys")
    raw_path = value.get("path")
    require(isinstance(raw_path, str) and raw_path, f"{context}.path invalid")
    path = Path(raw_path).resolve()
    sha = _exact_sha(value.get("sha256"), f"{context}.sha256")
    size = value.get("size_bytes")
    require(isinstance(size, int) and not isinstance(size, bool) and size >= 0, f"{context}.size_bytes invalid")
    if verify_file:
        require(path.is_file(), f"{context}: file is missing: {path}")
        require(path.stat().st_size == size, f"{context}: file size mismatch")
        require(sha256_file(path) == sha, f"{context}: file SHA-256 mismatch")
    return {"path": str(path), "sha256": sha, "size_bytes": size}


def validate_controlled_import_receipt(
    receipt_path: Path,
    expected_receipt_sha256: str,
    *,
    store_root: Path,
    dataset_manifest: dict[str, Any],
    dataset_sha256: str,
    image_ref: str,
    image_digest: str,
    offline_tree: dict[str, Any],
    importer_path: Path,
    current_host: dict[str, Any],
    verify_input_files: bool,
) -> dict[str, Any]:
    """Validate a receipt emitted by the pinned, create-from-empty importer."""

    receipt_path = receipt_path.resolve()
    expected_receipt_sha256 = _exact_sha(expected_receipt_sha256, "import receipt SHA-256")
    require(receipt_path.is_file(), f"controlled import receipt is missing: {receipt_path}")
    require(sha256_file(receipt_path) == expected_receipt_sha256, "controlled import receipt SHA-256 mismatch")
    receipt = read_json(receipt_path, "controlled import receipt")
    top_keys = {
        "schema_version",
        "producer",
        "host",
        "image",
        "docker_config_baseline",
        "logs_root",
        "filesystem_identity",
        "input",
        "stages",
        "database_contract",
        "final_store",
        "known_mutable_patterns",
        "canonical_sentinels",
        "store_created_from_empty",
        "outcome",
        "neo4j_driver_version",
    }
    require(isinstance(receipt, dict) and set(receipt) == top_keys, "controlled import receipt has wrong keys")
    require(
        receipt["schema_version"] == "p10-neo4j-controlled-import-receipt-v3",
        "controlled import receipt schema mismatch",
    )
    producer = _artifact(receipt["producer"], "controlled import producer", verify_file=True)
    importer_path = importer_path.resolve()
    require(Path(producer["path"]) == importer_path, "controlled import producer path mismatch")
    require(producer["sha256"] == sha256_file(importer_path), "controlled import producer is not current")
    require(receipt["host"] == current_host, "controlled import receipt was produced on a different host")

    image = receipt["image"]
    require(isinstance(image, dict), "controlled import image identity is malformed")
    require(
        set(image) == {"configured_ref", "image_id", "repo_digests", "selected_repo_digest"},
        "controlled import image identity has wrong keys",
    )
    require(image["configured_ref"] == image_ref, "controlled import image ref mismatch")
    _exact_image_digest(image["image_id"], "controlled import image ID")
    require(image["selected_repo_digest"] == image_digest, "controlled import RepoDigest mismatch")
    repo_digests = image["repo_digests"]
    require(isinstance(repo_digests, list) and repo_digests == sorted(repo_digests), "controlled import RepoDigests malformed")
    resolved = {
        value.split("@", 1)[1]
        for value in repo_digests
        if isinstance(value, str) and "@" in value
    }
    require(image_digest in resolved, "controlled import RepoDigest is absent from image metadata")
    require(
        receipt["docker_config_baseline"]
        == {"entrypoint": EXPECTED_ENTRYPOINT, "command": EXPECTED_COMMAND},
        "controlled import Docker Config baseline mismatch",
    )
    logs_root = Path(str(receipt["logs_root"])).resolve()
    require(logs_root.is_dir() and not logs_root.is_symlink(), "controlled import logs root is invalid")
    require(
        logs_root != store_root.resolve()
        and logs_root not in store_root.resolve().parents
        and store_root.resolve() not in logs_root.parents,
        "controlled import /data and /logs roots overlap",
    )
    filesystem_identity = receipt["filesystem_identity"]
    require(
        isinstance(filesystem_identity, dict)
        and set(filesystem_identity) == {"container_user", "store_root", "logs_root"},
        "controlled import filesystem identity is malformed",
    )
    identity_keys = {"path", "device", "inode", "uid", "gid", "mode"}
    expected_paths = {"store_root": store_root.resolve(), "logs_root": logs_root}
    identities: dict[str, dict[str, Any]] = {}
    for role, expected_path in expected_paths.items():
        identity = filesystem_identity[role]
        require(
            isinstance(identity, dict) and set(identity) == identity_keys,
            f"controlled import {role} identity is malformed",
        )
        metadata = expected_path.stat()
        observed = {
            "path": str(expected_path),
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "uid": int(metadata.st_uid),
            "gid": int(metadata.st_gid),
            "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
        }
        require(identity == observed, f"controlled import {role} identity differs from the filesystem")
        require(identity["mode"] == "0700", f"controlled import {role} mode is not 0700")
        require(
            identity["uid"] == os.geteuid() and identity["gid"] == os.getegid(),
            f"controlled import {role} is not owned by the validating UID:GID",
        )
        identities[role] = identity
    expected_container_user = f"{identities['store_root']['uid']}:{identities['store_root']['gid']}"
    require(
        identities["store_root"]["uid"] == identities["logs_root"]["uid"]
        and identities["store_root"]["gid"] == identities["logs_root"]["gid"]
        and filesystem_identity["container_user"] == expected_container_user,
        "controlled import container user does not match the /data and /logs owner",
    )

    input_value = receipt["input"]
    require(isinstance(input_value, dict), "controlled import input is malformed")
    require(
        set(input_value) == {"root", "dataset_manifest", "dataset_sha256", "files", "input_sha256"},
        "controlled import input has wrong keys",
    )
    receipt_dataset = _artifact(
        input_value["dataset_manifest"],
        "controlled import dataset manifest",
        verify_file=True,
    )
    require(
        Path(receipt_dataset["path"]) == Path(dataset_manifest["path"]).resolve()
        and receipt_dataset["sha256"] == dataset_manifest["sha256"],
        "controlled import dataset manifest differs from store freezer input",
    )
    require(input_value["dataset_sha256"] == dataset_sha256, "controlled import dataset lineage mismatch")
    input_root = Path(str(input_value["root"])).resolve()
    require(input_root.is_dir(), "controlled import root is missing")
    input_files = input_value["files"]
    require(isinstance(input_files, list) and len(input_files) == 2, "controlled import requires nodes and relationships inputs")
    roles: list[str] = []
    digest = hashlib.sha256()
    normalized_inputs: list[dict[str, Any]] = []
    for index, raw in enumerate(input_files):
        context = f"controlled import input.files[{index}]"
        require(isinstance(raw, dict), f"{context}: expected object")
        require(
            set(raw) == {"role", "relative_path", "path", "sha256", "size_bytes"},
            f"{context}: wrong keys",
        )
        role = raw.get("role")
        relative = raw.get("relative_path")
        require(role in {"nodes", "relationships"}, f"{context}.role invalid")
        require(
            isinstance(relative, str)
            and relative
            and not Path(relative).is_absolute()
            and ".." not in Path(relative).parts,
            f"{context}.relative_path invalid",
        )
        ref = _artifact(
            {key: raw[key] for key in ("path", "sha256", "size_bytes")},
            context,
            verify_file=verify_input_files,
        )
        require(Path(ref["path"]) == (input_root / relative).resolve(), f"{context}.path differs from import root")
        roles.append(str(role))
        digest.update(
            f"{role}\0{relative}\0{ref['size_bytes']}\0{ref['sha256']}\n".encode()
        )
        normalized_inputs.append({"role": role, "relative_path": relative, **ref})
    require(roles == ["nodes", "relationships"], "controlled import input roles/order mismatch")
    require(input_value["input_sha256"] == digest.hexdigest(), "controlled import input digest mismatch")

    stages = receipt["stages"]
    require(isinstance(stages, list) and len(stages) == 2, "controlled import stages mismatch")
    require([stage.get("name") for stage in stages if isinstance(stage, dict)] == ["full-import", "schema-finalize"], "controlled import stage order mismatch")
    for stage in stages:
        name = stage["name"]
        expected_keys = {"name", "config", "runtime", "log", "lifecycle"}
        if name == "schema-finalize":
            expected_keys.add("database_contract")
        require(set(stage) == expected_keys, f"controlled import stage {name} has wrong keys")
        config = stage["config"]
        runtime = stage["runtime"]
        require(isinstance(config, dict) and isinstance(runtime, dict), f"controlled import stage {name} malformed")
        require(config.get("configured_image") == image_ref, f"controlled import stage {name} image ref mismatch")
        require(config.get("image_id") == image["image_id"], f"controlled import stage {name} image ID mismatch")
        require(config.get("restart_policy") == "no", f"controlled import stage {name} restart policy mismatch")
        require(
            config.get("entrypoint") == EXPECTED_ENTRYPOINT,
            f"controlled import stage {name} Entrypoint baseline mismatch",
        )
        require(
            config.get("container_user") == expected_container_user,
            f"controlled import stage {name} container user mismatch",
        )
        container_id = config.get("container_id")
        require(
            isinstance(container_id, str)
            and len(container_id) == 64
            and all(character in "0123456789abcdef" for character in container_id),
            f"controlled import stage {name} full container ID missing",
        )
        container_name = config.get("container_name")
        require(
            isinstance(container_name, str)
            and container_name
            and all(character.isalnum() or character in "_.-" for character in container_name),
            f"controlled import stage {name} container name malformed",
        )
        require(
            runtime.get("running") is False
            and runtime.get("exit_code") == 0
            and runtime.get("restart_count") == 0
            and isinstance(runtime.get("started_at"), str)
            and runtime.get("started_at")
            and isinstance(runtime.get("finished_at"), str)
            and runtime.get("finished_at"),
            f"controlled import stage {name} did not exit cleanly",
        )
        mounts = config.get("mounts")
        require(isinstance(mounts, list), f"controlled import stage {name} mounts malformed")
        data_mounts = [mount for mount in mounts if isinstance(mount, dict) and mount.get("destination") == "/data"]
        require(
            len(data_mounts) == 1
            and Path(str(data_mounts[0].get("source"))).resolve() == store_root.resolve()
            and data_mounts[0].get("rw") is True,
            f"controlled import stage {name} /data mount mismatch",
        )
        nested_data = [
            mount
            for mount in mounts
            if isinstance(mount, dict)
            and isinstance(mount.get("destination"), str)
            and mount.get("destination") != "/data"
            and str(mount.get("destination")).startswith("/data/")
        ]
        require(not nested_data, f"controlled import stage {name} has nested /data mounts")
        logs_mounts = [mount for mount in mounts if isinstance(mount, dict) and mount.get("destination") == "/logs"]
        require(
            len(logs_mounts) == 1
            and Path(str(logs_mounts[0].get("source"))).resolve() == logs_root
            and logs_mounts[0].get("rw") is True,
            f"controlled import stage {name} /logs mount mismatch",
        )
        if name == "full-import":
            require(
                {mount.get("destination") for mount in mounts if isinstance(mount, dict)}
                == {"/data", "/logs", "/import"},
                "full-import mount destination allowlist mismatch",
            )
            require(config.get("network_mode") == "none", "full-import must use network=none")
            import_mounts = [mount for mount in mounts if isinstance(mount, dict) and mount.get("destination") == "/import"]
            require(
                len(import_mounts) == 1
                and Path(str(import_mounts[0].get("source"))).resolve() == input_root
                and import_mounts[0].get("rw") is False,
                "full-import /import mount mismatch",
            )
            expected_command = [
                "neo4j-admin", "database", "import", "full",
                "--overwrite-destination=true", "--id-type=integer",
                f"--nodes=V=/import/{normalized_inputs[0]['relative_path']}",
                f"--relationships=/import/{normalized_inputs[1]['relative_path']}",
                "--", "neo4j",
            ]
            require(config.get("command") == expected_command, "full-import command mismatch")
        else:
            require(
                {mount.get("destination") for mount in mounts if isinstance(mount, dict)}
                == {"/data", "/logs"},
                "schema-finalize mount destination allowlist mismatch",
            )
            require(config.get("network_mode") in {"default", "bridge"}, "schema-finalize network mismatch")
            environment = config.get("environment")
            require(isinstance(environment, list) and "NEO4J_AUTH=none" in environment, "schema-finalize auth config mismatch")
            require(config.get("command") == EXPECTED_COMMAND, "schema-finalize command baseline mismatch")
            require(stage.get("database_contract") == receipt["database_contract"], "schema-finalize database contract mismatch")
        lifecycle = stage["lifecycle"]
        require(
            isinstance(lifecycle, dict)
            and set(lifecycle) == {"create", "start", "stop", "remove", "absence_probe"},
            f"controlled import stage {name} lifecycle malformed",
        )
        create = lifecycle["create"]
        create_command = create.get("command") if isinstance(create, dict) else None
        name_index = create_command.index("--name") if isinstance(create_command, list) and "--name" in create_command else -1
        user_index = create_command.index("--user") if isinstance(create_command, list) and create_command.count("--user") == 1 else -1
        require(
            isinstance(create, dict)
            and set(create) == {"command", "container_id"}
            and create["container_id"] == container_id
            and isinstance(create_command, list)
            and create_command[:3] == ["docker", "container", "create"]
            and 0 <= name_index < len(create_command) - 1
            and create_command[name_index + 1] == container_name
            and 0 <= user_index < len(create_command) - 1
            and create_command[user_index + 1] == expected_container_user,
            f"controlled import stage {name} create identity mismatch",
        )
        require("--entrypoint" not in create_command, f"controlled import stage {name} overrides Entrypoint")
        expected_start = ["docker", "container", "start"]
        if name == "full-import":
            expected_start.append("--attach")
        expected_start.append(container_id)
        require(
            lifecycle["start"] == {"command": expected_start},
            f"controlled import stage {name} start is not exact-ID bound",
        )
        if name == "full-import":
            require(lifecycle["stop"] is None, "full-import self-exit must not claim an explicit stop")
        else:
            require(
                lifecycle["stop"]
                == {
                    "command": ["docker", "container", "stop", "--time", "60", container_id],
                    "stdout": container_id,
                },
                "schema-finalize stop is not exact-ID bound",
            )
        require(
            lifecycle["remove"]
            == {"command": ["docker", "container", "rm", container_id], "stdout": container_id},
            f"controlled import stage {name} remove is not exact-ID bound",
        )
        require(
            lifecycle["absence_probe"]
            == {
                "command": [
                    "docker", "container", "ls", "--all",
                    "--filter", f"name=^/{container_name}$", "--format", "{{.ID}}",
                ],
                "stdout": "",
            },
            f"controlled import stage {name} removal absence proof mismatch",
        )
        _artifact(stage["log"], f"controlled import stage {name} log", verify_file=verify_input_files)

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
    require(receipt["database_contract"] == database_expected, "controlled import database/index contract mismatch")
    final_store = receipt["final_store"]
    require(isinstance(final_store, dict), "controlled import final_store malformed")
    summary_keys = {
        "root", "hash_method", "store_sha256", "file_count", "total_bytes",
        "immutable_store_sha256", "immutable_file_count", "immutable_total_bytes",
    }
    require(set(final_store) == summary_keys, "controlled import final_store has wrong keys")
    require(Path(str(final_store["root"])).resolve() == store_root.resolve(), "controlled import final store root mismatch")
    require(final_store["hash_method"] == TREE_HASH_METHOD, "controlled import final store hash method mismatch")
    for key in summary_keys - {"root", "hash_method"}:
        require(final_store[key] == offline_tree[key], f"controlled import final_store.{key} mismatch")
    require(receipt["known_mutable_patterns"] == list(KNOWN_MUTABLE_PATTERNS), "controlled import mutable paths mismatch")
    require(receipt["canonical_sentinels"] == list(CANONICAL_SENTINELS), "controlled import canonical sentinels mismatch")
    require(receipt["store_created_from_empty"] is True, "controlled import did not create store from empty")
    require(receipt["outcome"] == {"completed": True, "exit_code": 0}, "controlled import outcome is not successful")
    require(receipt["neo4j_driver_version"] == "5.28.3", "controlled import Neo4j driver version mismatch")
    return {
        "reference": {
            "path": str(receipt_path),
            "sha256": expected_receipt_sha256,
            "size_bytes": receipt_path.stat().st_size,
        },
        "producer": producer,
        "host": receipt["host"],
        "image": image,
        "logs_root": str(logs_root),
        "filesystem_identity": filesystem_identity,
        "input": input_value,
        "stages": stages,
        "database_contract": receipt["database_contract"],
        "final_store": final_store,
    }


def compare_runtime_tree(
    baseline_files: list[dict[str, Any]],
    current: dict[str, Any],
    *,
    context: str,
) -> dict[str, Any]:
    """Require every non-allowlisted file to equal the offline snapshot."""

    baseline = {str(item["path"]): item for item in baseline_files}
    observed_files = current.get("files")
    require(isinstance(observed_files, list), f"{context}.files missing")
    observed = {str(item["path"]): item for item in observed_files}
    deltas: list[dict[str, Any]] = []
    forbidden: list[str] = []
    for relative in sorted(set(baseline) | set(observed), key=lambda item: item.encode()):
        before = baseline.get(relative)
        after = observed.get(relative)
        before_identity = None if before is None else (before["size_bytes"], before["sha256"])
        after_identity = None if after is None else (after["size_bytes"], after["sha256"])
        if before_identity == after_identity:
            continue
        if not is_known_mutable(relative):
            forbidden.append(relative)
            continue
        deltas.append(
            {
                "path": relative,
                "before": before,
                "after": after,
            }
        )
    require(not forbidden, f"{context}: immutable store files changed: {forbidden!r}")
    baseline_summary = summarize_records(baseline_files)
    require(
        current.get("immutable_store_sha256") == baseline_summary["immutable_store_sha256"]
        and current.get("immutable_file_count") == baseline_summary["immutable_file_count"]
        and current.get("immutable_total_bytes") == baseline_summary["immutable_total_bytes"],
        f"{context}: immutable store summary differs from offline snapshot",
    )
    return {
        "tree": {key: current[key] for key in (
            "store_sha256",
            "file_count",
            "total_bytes",
            "immutable_store_sha256",
            "immutable_file_count",
            "immutable_total_bytes",
        )},
        "mutable_deltas": deltas,
    }
