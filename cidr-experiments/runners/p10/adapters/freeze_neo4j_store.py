#!/usr/bin/env python3
"""Freeze an offline Neo4j runtime copy into the P10 lineage manifest.

This helper is read-only with respect to the store.  It refuses every running
container whose bind mount overlaps the store and never copies, deletes,
starts, or stops a database service.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))

from adapters.neo4j_store_contract import (  # noqa: E402
    CANONICAL_SENTINELS,
    KNOWN_MUTABLE_PATTERNS,
    OFFLINE_PROOF_METHOD,
    TREE_HASH_METHOD,
    canonical_sentinel_records,
    stable_tree_manifest,
    validate_controlled_import_receipt,
)
from p10_contract import ContractError, atomic_json, read_json, sha256_file  # noqa: E402

P31_DIR = P10_DIR.parent / "p31"
sys.path.insert(0, str(P31_DIR))
from run_manifest import host_facts as p31_host_facts  # noqa: E402

SCHEMA_VERSION = "p10-neo4j-store-manifest-v3"
RELATIONSHIP_MODEL = "dense-edge-type-as-outgoing-relationship-type-v1"
SNAPSHOT_PHASE = "offline-prestart-v1"
DATABASE_NAME = "neo4j"
INDEX_NAME = "v_id"
NODE_LABEL = "V"
ID_PROPERTY = "id"
IMAGE_REF = "neo4j:5.26.24"
OWNER_ONLY_MODE = 0o700
STORE_LOCK_MODE = 0o600
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--neo4j-version", default="5.26.24")
    parser.add_argument("--runtime-image-ref", default=IMAGE_REF)
    parser.add_argument(
        "--runtime-image-digest",
        "--image-digest",
        dest="runtime_image_digest",
        required=True,
    )
    parser.add_argument("--import-receipt", type=Path)
    parser.add_argument("--import-receipt-sha256")
    parser.add_argument("--historical-import-image-ref", default=IMAGE_REF)
    parser.add_argument("--assert-container-stopped")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def docker_json(command: list[str], context: str) -> object:
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    require(completed.returncode == 0, f"{context}: {completed.stderr.strip()}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{context}: Docker returned malformed JSON") from exc


def paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left in right.parents or right in left.parents


def _root_identity(root: Path) -> dict[str, object]:
    metadata = root.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and not root.is_symlink(), "store root is not a real directory")
    mode = stat.S_IMODE(metadata.st_mode)
    require(mode == OWNER_ONLY_MODE, "formal store root must have exact owner-only mode 0700")
    require(metadata.st_uid == os.geteuid(), "formal store root must be owned by the effective UID")
    require(metadata.st_gid == os.getegid(), "formal store root must be owned by the effective GID")
    return {
        "path": str(root),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "uid": int(metadata.st_uid),
        "gid": int(metadata.st_gid),
        "mode": format(mode, "04o"),
    }


def _lock_identity(root: Path) -> tuple[Path, dict[str, object]]:
    lock_path = root / "databases" / "store_lock"
    metadata = lock_path.lstat()
    require(
        stat.S_ISREG(metadata.st_mode) and not lock_path.is_symlink(),
        "Neo4j databases/store_lock is not a real regular file",
    )
    mode = stat.S_IMODE(metadata.st_mode)
    require(mode == STORE_LOCK_MODE, "formal Neo4j store_lock must have exact mode 0600")
    require(metadata.st_uid == os.geteuid(), "formal Neo4j store_lock must be owned by the effective UID")
    require(metadata.st_gid == os.getegid(), "formal Neo4j store_lock must be owned by the effective GID")
    return lock_path, {
        "path": str(lock_path),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "uid": int(metadata.st_uid),
        "gid": int(metadata.st_gid),
        "mode": format(mode, "04o"),
        "size_bytes": int(metadata.st_size),
    }


def _docker_mount_audit(root: Path, named_container: str | None) -> dict[str, object]:
    """Reject every running Docker bind mount that overlaps the guarded tree."""

    if named_container:
        completed = subprocess.run(
            ["docker", "container", "inspect", named_container],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        if completed.returncode == 0:
            try:
                values = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise ContractError("named-container inspect returned malformed JSON") from exc
            require(
                isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict),
                "named-container inspect returned an unexpected result",
            )
            require(
                values[0].get("State", {}).get("Running") is not True,
                f"refusing to hash a store while container {named_container!r} is running",
            )

    listed = subprocess.run(
        ["docker", "container", "ls", "--quiet", "--no-trunc"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    require(listed.returncode == 0, f"cannot enumerate running containers: {listed.stderr.strip()}")
    identifiers = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    require(
        all(CONTAINER_ID_RE.fullmatch(identifier) is not None for identifier in identifiers),
        "running-container enumeration returned a non-full container ID",
    )
    inspected_ids: list[str] = []
    if identifiers:
        inspected = docker_json(
            ["docker", "container", "inspect", *identifiers],
            "running-container inspect",
        )
        require(isinstance(inspected, list), "running-container inspect returned a non-array")
        for container in inspected:
            require(isinstance(container, dict), "running-container inspect entry is malformed")
            container_id = container.get("Id")
            require(
                isinstance(container_id, str)
                and CONTAINER_ID_RE.fullmatch(container_id) is not None
                and container_id in identifiers,
                "running-container inspect identity differs from the enumerated full IDs",
            )
            inspected_ids.append(container_id)
            name = str(container.get("Name", "")).lstrip("/") or str(container.get("Id", ""))
            for mount in container.get("Mounts", []) or []:
                if not isinstance(mount, dict) or mount.get("Type") != "bind":
                    continue
                source = mount.get("Source")
                if not isinstance(source, str) or not source.startswith("/"):
                    continue
                if paths_overlap(root, Path(source)):
                    raise ContractError(
                        f"refusing to hash store while running container {name!r} "
                        f"has overlapping bind mount {source!r}"
                    )
    require(
        sorted(inspected_ids) == sorted(identifiers),
        "running-container inspect did not return every enumerated full ID exactly once",
    )
    return {
        "running_container_count": len(identifiers),
        "inspected_container_ids": sorted(inspected_ids),
        "overlapping_mounts": [],
    }


def _offline_snapshot(
    root: Path,
    named_container: str | None,
    lock_identity: dict[str, object],
) -> dict[str, object]:
    current_lock_path, current_lock = _lock_identity(root)
    require(current_lock_path == Path(str(lock_identity["path"])), "Neo4j store_lock path changed")
    require(current_lock == lock_identity, "Neo4j store_lock identity changed while guarded")
    return {
        "root_identity": _root_identity(root),
        "docker_mount_audit": _docker_mount_audit(root, named_container),
        "store_lock": {
            **current_lock,
            "method": "fcntl-lockf-exclusive-nonblocking-v1",
            "acquired_exclusive": True,
        },
    }


@contextmanager
def store_offline_guard(
    root: Path,
    named_container: str | None,
) -> Iterator[dict[str, object]]:
    """Hold Neo4j's exclusive lock across the full hash and both stat audits.

    Formal proof intentionally does not scan other users' unreadable ``/proc``
    entries.  It instead relies on an owner-only 0700 root, exact owner/mode,
    absence of overlapping Docker bind mounts, and the database's exclusive
    ``store_lock`` held continuously across hashing.
    """

    root = root.resolve()
    before_root = _root_identity(root)
    lock_path, lock_identity = _lock_identity(root)
    flags = os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags)
    acquired = False
    gate: dict[str, object] = {
        "proof_method": OFFLINE_PROOF_METHOD,
        "before_hash": None,
        "after_hash": None,
    }
    try:
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ContractError(f"Neo4j store_lock is not exclusively acquirable: {exc}") from exc
        acquired = True
        gate["before_hash"] = _offline_snapshot(root, named_container, lock_identity)
        yield gate
        gate["after_hash"] = _offline_snapshot(root, named_container, lock_identity)
        require(
            gate["before_hash"]["root_identity"] == gate["after_hash"]["root_identity"],
            "formal store root identity changed across hashing",
        )
        require(before_root == gate["after_hash"]["root_identity"], "formal store root changed")
    finally:
        if acquired:
            fcntl.lockf(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def assert_store_offline(root: Path, named_container: str | None) -> dict[str, object]:
    """One-shot owner-only proof retained for callers that do not hash."""

    with store_offline_guard(root, named_container) as gate:
        pass
    return dict(gate["after_hash"])


def image_digest(value: object, context: str) -> str:
    require(
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:]),
        f"{context} must be sha256:<64 lowercase hex>",
    )
    return str(value)


def run(args: argparse.Namespace) -> None:
    require(args.store_root.is_dir(), f"store root does not exist: {args.store_root}")
    require(not args.store_root.is_symlink(), "store root must not be a symlink")
    root = args.store_root.resolve()
    output = args.output.resolve()
    require(not output.exists(), f"refusing to overwrite output: {output}")
    require(root not in output.parents, "manifest output must be outside the mutable runtime store")
    require(
        (args.import_receipt is None) == (args.import_receipt_sha256 is None),
        "--import-receipt and --import-receipt-sha256 must be provided together",
    )
    dataset_path = args.dataset_manifest.resolve()
    truth_path = args.truth.resolve()
    require(dataset_path.is_file(), f"dataset manifest is missing: {dataset_path}")
    require(truth_path.is_file(), f"truth TSV is missing: {truth_path}")
    dataset = read_json(dataset_path, "dataset manifest")
    require(dataset.get("schema_version") == "p02b-dataset-manifest-v1", "dataset manifest schema mismatch")
    dataset_sha = dataset.get("dataset_sha256")
    require(isinstance(dataset_sha, str) and len(dataset_sha) == 64, "dataset lineage SHA is invalid")
    runtime_digest = image_digest(args.runtime_image_digest, "runtime image digest")
    require(args.neo4j_version == "5.26.24", "Neo4j version must be '5.26.24'")
    require(args.runtime_image_ref == IMAGE_REF, f"runtime image ref must be {IMAGE_REF!r}")

    with store_offline_guard(root, args.assert_container_stopped) as offline_gate:
        tree = stable_tree_manifest(root, attempts=2)
    sentinel_files = canonical_sentinel_records(tree["files"])
    dataset_ref = {
        "path": str(dataset_path),
        "sha256": sha256_file(dataset_path),
        "size_bytes": dataset_path.stat().st_size,
    }
    current_host = p31_host_facts()
    host = {
        "hostname": current_host["hostname"],
        "fingerprint_sha256": current_host["fingerprint_sha256"],
    }
    if args.import_receipt is not None:
        receipt = validate_controlled_import_receipt(
            args.import_receipt,
            args.import_receipt_sha256,
            store_root=root,
            dataset_manifest=dataset_ref,
            dataset_sha256=dataset_sha,
            image_ref=args.runtime_image_ref,
            image_digest=runtime_digest,
            offline_tree=tree,
            importer_path=Path(__file__).with_name("import_neo4j_store.py"),
            current_host=host,
            verify_input_files=True,
        )
        import_provenance = {
            "status": "controlled-import-receipt-v3",
            **receipt,
        }
    else:
        require(
            args.historical_import_image_ref == IMAGE_REF,
            f"historical import image ref must be {IMAGE_REF!r}",
        )
        import_provenance = {
            "status": "unverified-historical-store",
            "reference": None,
            "producer": None,
            "host": None,
            "image": {
                "configured_ref": args.historical_import_image_ref,
                "image_id": None,
                "repo_digests": [],
                "selected_repo_digest": None,
            },
            "input": None,
            "stages": None,
            "database_contract": None,
            "final_store": None,
        }

    database_contract = {
        "database_name": DATABASE_NAME,
        "node_label": NODE_LABEL,
        "id_property": ID_PROPERTY,
        "required_index": {
            "name": INDEX_NAME,
            "state": "ONLINE",
            "type": "RANGE",
            "entityType": "NODE",
            "labelsOrTypes": [NODE_LABEL],
            "properties": [ID_PROPERTY],
        },
    }

    atomic_json(
        output,
        {
            "schema_version": SCHEMA_VERSION,
            "store_root": str(root),
            **{key: tree[key] for key in (
                "store_sha256", "file_count", "total_bytes",
                "immutable_store_sha256", "immutable_file_count", "immutable_total_bytes",
                "files",
            )},
            "hash_method": TREE_HASH_METHOD,
            "snapshot_phase": SNAPSHOT_PHASE,
            "known_mutable_patterns": list(KNOWN_MUTABLE_PATTERNS),
            "canonical_sentinels": list(CANONICAL_SENTINELS),
            "runtime_compatibility": {
                "neo4j_version": args.neo4j_version,
                "image_ref": args.runtime_image_ref,
                "image_digest": runtime_digest,
            },
            "import_provenance": import_provenance,
            "database_contract": database_contract,
            "dataset_manifest_sha256": dataset_ref["sha256"],
            "dataset_sha256": dataset_sha,
            "truth_sha256": sha256_file(truth_path),
            "relationship_model": RELATIONSHIP_MODEL,
            "sentinel_files": sentinel_files,
            "offline_audit": {
                **offline_gate,
                "per_file_stat_stability": True,
                "owner_only_root": True,
                "exclusive_store_lock_held_across_hash": True,
                "docker_mount_rescan": True,
                "proc_scan_used": False,
            },
        },
    )


def main() -> int:
    try:
        run(parse_args())
        return 0
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        print(f"freeze_neo4j_store: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
