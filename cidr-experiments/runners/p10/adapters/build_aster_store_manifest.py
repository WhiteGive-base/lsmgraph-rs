#!/usr/bin/env python3
"""Freeze the stable, query-relevant portion of an Aster RocksGraph store."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "p10-aster-store-manifest-v1"
TREE_HASH_METHOD = "sha256-tree-v1(relative-path,size,file-sha256;exclude=LOCK,LOG,LOG.old.*)"
STORE_HASH_METHOD = "sha256-aster-query-store-v1(key-map,store-metadata,GraphMeta,default-column-family-kv)"
KEY_MAP_NAME = "p10-aster-key-map.tsv"
STORE_METADATA_NAME = "p10-aster-store.json"


class ManifestError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_volatile(relative: str) -> bool:
    name = Path(relative).name
    return name in {"LOCK", "LOG"} or name.startswith("LOG.old.")


def stable_tree_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    require(root.is_dir(), f"store directory does not exist: {root}")
    files: list[Path] = []
    excluded: list[str] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in sorted(dirnames):
            require(not (directory_path / name).is_symlink(), "store tree rejects symlink directories")
        for name in sorted(filenames):
            path = directory_path / name
            require(not path.is_symlink() and path.is_file(), f"store tree found non-regular file: {path}")
            relative = path.relative_to(root).as_posix()
            if is_volatile(relative):
                excluded.append(relative)
            else:
                files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix().encode("utf-8"))
    excluded.sort(key=lambda value: value.encode("utf-8"))
    require(bool(files), "stable Aster store tree must not be empty")
    digest = hashlib.sha256()
    total_bytes = 0
    entries: list[dict[str, Any]] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        file_sha = sha256_file(path)
        digest.update(f"file\0{relative}\0{size}\0{file_sha}\n".encode("utf-8"))
        total_bytes += size
        entries.append({"path": relative, "size_bytes": size, "sha256": file_sha})
    return {
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "hash_method": TREE_HASH_METHOD,
        "excluded_runtime_files": excluded,
        "files": entries,
    }


def artifact_ref(path: Path) -> dict[str, Any]:
    path = path.resolve()
    require(path.is_file(), f"artifact does not exist: {path}")
    return {"path": str(path), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}


def logical_store_sha256(binary: Path, store: Path) -> str:
    binary = binary.resolve()
    store = store.resolve()
    require(binary.is_file() and os.access(binary, os.X_OK), f"Aster worker is not executable: {binary}")
    process = subprocess.Popen(
        [str(binary), "--dump-store-digest-stream", "--store", str(store)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    require(process.stdout is not None and process.stderr is not None, "cannot capture Aster digest worker")
    digest = hashlib.sha256()
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
        digest.update(chunk)
    stderr = process.stderr.read().decode("utf-8", errors="replace")
    returncode = process.wait(timeout=3600)
    require(returncode == 0, f"Aster logical store digest failed ({returncode}): {stderr.strip()}")
    return digest.hexdigest()


def git_state(root: Path) -> dict[str, Any]:
    root = root.resolve()
    require(root.is_dir(), f"source root does not exist: {root}")
    try:
        head = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT, timeout=20
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=normal"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManifestError(f"cannot audit Aster source repository: {exc}") from exc
    require(len(head) == 40, "Aster source HEAD is not a full Git commit")
    return {
        "root": str(root),
        "head": head,
        "clean": status == "",
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run(args: argparse.Namespace) -> None:
    store = args.store.resolve()
    output = args.output.resolve()
    require(output != store and store not in output.parents, "manifest output must be outside the frozen store")
    source = git_state(args.source_root)
    require(source["head"] == args.source_commit.lower(), "Aster source HEAD differs from --source-commit")
    key_map = artifact_ref(store / KEY_MAP_NAME)
    metadata = artifact_ref(store / STORE_METADATA_NAME)
    try:
        metadata_value = json.loads(Path(metadata["path"]).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot parse Aster store metadata: {exc}") from exc
    require(metadata_value.get("schema_version") == "p10-aster-rocksgraph-store-v1", "store metadata schema mismatch")
    require(metadata_value.get("source_commit") == source["head"], "store metadata source commit mismatch")
    binary = artifact_ref(args.binary)
    dataset = artifact_ref(args.dataset)
    require(metadata_value.get("dataset_sha256") == dataset["sha256"], "store metadata dataset SHA-256 mismatch")
    require(metadata_value.get("builder_binary_sha256") == binary["sha256"], "store metadata binary SHA-256 mismatch")
    tree = stable_tree_manifest(store)
    logical_sha = logical_store_sha256(args.binary, store)
    document = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "store_path": str(store),
        "store_sha256": logical_sha,
        "hash_method": STORE_HASH_METHOD,
        "file_count": tree["file_count"],
        "total_bytes": tree["total_bytes"],
        "excluded_runtime_files": tree["excluded_runtime_files"],
        "files": tree["files"],
        "snapshot_tree_sha256": tree["sha256"],
        "snapshot_tree_hash_method": tree["hash_method"],
        "source": source,
        "binary": binary,
        "dataset": dataset,
        "key_map": key_map,
        "store_metadata": metadata,
    }
    atomic_json(output, document)
    print(json.dumps({"state": "PASS", "output": str(output), "store_sha256": logical_sha}, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    args = parser.parse_args()
    try:
        run(args)
        return 0
    except (ManifestError, OSError, UnicodeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
