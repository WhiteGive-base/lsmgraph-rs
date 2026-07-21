#!/usr/bin/env python3
"""Freeze an offline Neo4j runtime copy into the P10 lineage manifest.

This helper is read-only with respect to the store.  It refuses every running
container whose bind mount overlaps the store and never copies, deletes,
starts, or stops a database service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))

from p10_contract import ContractError, atomic_json, read_json, sha256_file  # noqa: E402

SCHEMA_VERSION = "p10-neo4j-store-manifest-v2"
TREE_HASH_METHOD = "sha256-tree-v1(relative-path,size,file-sha256)"
RELATIONSHIP_MODEL = "dense-edge-type-as-outgoing-relationship-type-v1"
SNAPSHOT_PHASE = "offline-prestart-v1"
DATABASE_NAME = "neo4j"
INDEX_NAME = "v_id"
NODE_LABEL = "V"
ID_PROPERTY = "id"
MUTABLE_RUNTIME_PATHS = [
    "logs/**",
    "server_id",
    "transactions/**",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--neo4j-version", default="5.26.24")
    parser.add_argument("--runtime-image-ref", default="neo4j:5.26.24")
    parser.add_argument(
        "--runtime-image-digest",
        "--image-digest",
        dest="runtime_image_digest",
        required=True,
    )
    parser.add_argument("--import-image-ref", default="neo4j:5.26.24")
    parser.add_argument(
        "--import-image-identity",
        choices=("verified-repodigest", "unverified-tag-only"),
        default="unverified-tag-only",
    )
    parser.add_argument("--import-image-digest")
    parser.add_argument("--database-name", default=DATABASE_NAME)
    parser.add_argument("--index-name", default=INDEX_NAME)
    parser.add_argument("--node-label", default=NODE_LABEL)
    parser.add_argument("--id-property", default=ID_PROPERTY)
    parser.add_argument("--sentinel", action="append", required=True)
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


def assert_store_offline(root: Path, named_container: str | None) -> None:
    """Reject any live container that could mutate the snapshot tree."""

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
    if not identifiers:
        return
    inspected = docker_json(
        ["docker", "container", "inspect", *identifiers],
        "running-container inspect",
    )
    require(isinstance(inspected, list), "running-container inspect returned a non-array")
    for container in inspected:
        require(isinstance(container, dict), "running-container inspect entry is malformed")
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


def tree_manifest(root: Path) -> tuple[dict[str, int | str], dict[str, dict[str, int | str]]]:
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in sorted(dirnames):
            require(not (directory_path / name).is_symlink(), "store tree rejects symlink directory")
        for name in sorted(filenames):
            path = directory_path / name
            require(path.is_file() and not path.is_symlink(), f"store tree found non-regular file: {path}")
            files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix().encode())
    require(files, "store tree is empty")
    digest = hashlib.sha256()
    total_bytes = 0
    records: dict[str, dict[str, int | str]] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        file_sha = sha256_file(path)
        total_bytes += size
        digest.update(f"file\0{relative}\0{size}\0{file_sha}\n".encode())
        records[relative] = {"size_bytes": size, "sha256": file_sha}
    return (
        {
            "store_sha256": digest.hexdigest(),
            "file_count": len(files),
            "total_bytes": total_bytes,
        },
        records,
    )


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
    assert_store_offline(root, args.assert_container_stopped)

    dataset_path = args.dataset_manifest.resolve()
    truth_path = args.truth.resolve()
    require(dataset_path.is_file(), f"dataset manifest is missing: {dataset_path}")
    require(truth_path.is_file(), f"truth TSV is missing: {truth_path}")
    dataset = read_json(dataset_path, "dataset manifest")
    require(dataset.get("schema_version") == "p02b-dataset-manifest-v1", "dataset manifest schema mismatch")
    dataset_sha = dataset.get("dataset_sha256")
    require(isinstance(dataset_sha, str) and len(dataset_sha) == 64, "dataset lineage SHA is invalid")
    runtime_digest = image_digest(args.runtime_image_digest, "runtime image digest")
    require(args.database_name == DATABASE_NAME, f"database name must be {DATABASE_NAME!r}")
    require(args.index_name == INDEX_NAME, f"index name must be {INDEX_NAME!r}")
    require(args.node_label == NODE_LABEL, f"node label must be {NODE_LABEL!r}")
    require(args.id_property == ID_PROPERTY, f"ID property must be {ID_PROPERTY!r}")
    if args.import_image_identity == "verified-repodigest":
        import_digest: str | None = image_digest(
            args.import_image_digest, "verified import image digest"
        )
    else:
        require(
            args.import_image_digest is None,
            "unverified-tag-only import identity must not claim an image digest",
        )
        import_digest = None

    tree, file_records = tree_manifest(root)
    sentinel_files = []
    names: set[str] = set()
    for raw in args.sentinel:
        relative = Path(raw)
        require(not relative.is_absolute() and ".." not in relative.parts, "sentinel must be a safe relative path")
        relative_text = relative.as_posix()
        require(relative_text not in names, f"duplicate sentinel: {relative_text}")
        names.add(relative_text)
        path = (root / relative).resolve()
        require(root in path.parents and path.is_file() and not path.is_symlink(), f"invalid sentinel: {relative_text}")
        record = file_records.get(relative_text)
        require(record is not None, f"sentinel missing from frozen tree: {relative_text}")
        sentinel_files.append(
            {
                "path": relative_text,
                **record,
            }
        )

    atomic_json(
        output,
        {
            "schema_version": SCHEMA_VERSION,
            "store_root": str(root),
            **tree,
            "hash_method": TREE_HASH_METHOD,
            "snapshot_phase": SNAPSHOT_PHASE,
            "mutable_runtime_paths": MUTABLE_RUNTIME_PATHS,
            "runtime_compatibility": {
                "neo4j_version": args.neo4j_version,
                "image_ref": args.runtime_image_ref,
                "image_digest": runtime_digest,
            },
            "import_image_identity": {
                "status": args.import_image_identity,
                "image_ref": args.import_image_ref,
                "image_digest": import_digest,
            },
            "database_contract": {
                "database_name": args.database_name,
                "node_label": args.node_label,
                "id_property": args.id_property,
                "required_index": {
                    "name": args.index_name,
                    "type": "RANGE",
                    "state": "ONLINE",
                },
            },
            "dataset_manifest_sha256": sha256_file(dataset_path),
            "dataset_sha256": dataset_sha,
            "truth_sha256": sha256_file(truth_path),
            "relationship_model": RELATIONSHIP_MODEL,
            "sentinel_files": sentinel_files,
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
