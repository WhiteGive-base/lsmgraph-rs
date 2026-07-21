#!/usr/bin/env python3
"""Freeze an offline Neo4j runtime copy into the P10 lineage manifest.

This helper is read-only with respect to the store.  It refuses a running
container name when supplied and never copies, deletes, starts, or stops a
database service.
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

SCHEMA_VERSION = "p10-neo4j-store-manifest-v1"
TREE_HASH_METHOD = "sha256-tree-v1(relative-path,size,file-sha256)"
RELATIONSHIP_MODEL = "dense-edge-type-as-outgoing-relationship-type-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--neo4j-version", default="5.26.24")
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--sentinel", action="append", required=True)
    parser.add_argument("--assert-container-stopped")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def assert_container_stopped(name: str | None) -> None:
    if not name:
        return
    completed = subprocess.run(
        ["docker", "container", "inspect", name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        return
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("docker inspect returned malformed JSON") from exc
    require(
        not value or value[0].get("State", {}).get("Running") is not True,
        f"refusing to hash a store while container {name!r} is running",
    )


def tree_manifest(root: Path) -> dict[str, int | str]:
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
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total_bytes += size
        digest.update(f"file\0{relative}\0{size}\0{sha256_file(path)}\n".encode())
    return {
        "store_sha256": digest.hexdigest(),
        "file_count": len(files),
        "total_bytes": total_bytes,
    }


def run(args: argparse.Namespace) -> None:
    root = args.store_root.resolve()
    require(root.is_dir(), f"store root does not exist: {root}")
    require(not root.is_symlink(), "store root must not be a symlink")
    output = args.output.resolve()
    require(not output.exists(), f"refusing to overwrite output: {output}")
    require(root not in output.parents, "manifest output must be outside the mutable runtime store")
    assert_container_stopped(args.assert_container_stopped)

    dataset_path = args.dataset_manifest.resolve()
    truth_path = args.truth.resolve()
    require(dataset_path.is_file(), f"dataset manifest is missing: {dataset_path}")
    require(truth_path.is_file(), f"truth TSV is missing: {truth_path}")
    dataset = read_json(dataset_path, "dataset manifest")
    require(dataset.get("schema_version") == "p02b-dataset-manifest-v1", "dataset manifest schema mismatch")
    dataset_sha = dataset.get("dataset_sha256")
    require(isinstance(dataset_sha, str) and len(dataset_sha) == 64, "dataset lineage SHA is invalid")
    image_digest = args.image_digest
    require(
        image_digest.startswith("sha256:")
        and len(image_digest) == 71
        and all(character in "0123456789abcdef" for character in image_digest[7:]),
        "image digest must be sha256:<64 hex>",
    )

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
        sentinel_files.append(
            {
                "path": relative_text,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )

    tree = tree_manifest(root)
    atomic_json(
        output,
        {
            "schema_version": SCHEMA_VERSION,
            "store_root": str(root),
            **tree,
            "hash_method": TREE_HASH_METHOD,
            "neo4j_version": args.neo4j_version,
            "image_digest": image_digest,
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
