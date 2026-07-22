#!/usr/bin/env python3
"""Build or verify a full ``p20-store-inventory-v1`` manifest.

The digest algorithm is the one consumed by P20's ``run_single_profile.py``:
sort the per-file objects by POSIX relative path, serialize the list as compact
JSON with sorted object keys plus one LF, then SHA-256 the UTF-8 bytes.

This script is deliberately Python 3.8 compatible and fail-closed.  It rejects
links and non-regular files, detects a file changing while it is hashed, and
compares a quick file-set/size/mtime inventory before and after the full pass.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InventoryError(ValueError):
    pass


def sha256_file(path):
    """Hash one stable, regular, non-symlink file.

    ``lstat(); open()`` is racy: a path can be replaced with a symlink between
    the two calls.  Linux supplies ``O_NOFOLLOW``; the descriptor ``fstat``
    checks also make replacement/truncation during the read fail closed.
    """
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise InventoryError("hash input must be a regular file: {}".format(path))
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    identity_before = (
        before.st_dev, before.st_ino, before.st_size,
        before.st_mtime_ns, before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev, after.st_ino, after.st_size,
        after.st_mtime_ns, after.st_ctime_ns,
    )
    if identity_before != identity_after:
        raise InventoryError("file changed while hashing: {}".format(path))
    return digest.hexdigest()


def normalize_sha(value, label):
    value = value.strip().lower()
    if not SHA256_RE.fullmatch(value):
        raise InventoryError("{} must be a lowercase SHA-256".format(label))
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InventoryError("duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def load_json(path, label):
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object
        )
    except (OSError, json.JSONDecodeError, InventoryError) as exc:
        raise InventoryError("cannot read {}: {}".format(label, exc))
    if not isinstance(value, dict):
        raise InventoryError("{} must be a JSON object".format(label))
    return value


def require_absolute(path, label):
    path = Path(path)
    if not path.is_absolute():
        raise InventoryError("{} must be absolute: {}".format(label, path))
    # Preserve the final path component so callers can still detect a symlink.
    # ``Path.resolve`` here would make ``path.is_symlink()`` always false.
    return Path(os.path.abspath(str(path)))


def _directory_identity(path):
    info = os.lstat(str(path))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InventoryError("store must be a non-link directory: {}".format(path))
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)


def _regular_file_stat(path):
    before = os.lstat(str(path))
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise InventoryError("inventory requires regular non-link files: {}".format(path))
    return before


def quick_inventory(root):
    root = Path(root)
    rows = []
    for directory, directory_names, file_names in os.walk(str(root), followlinks=False):
        directory_path = Path(directory)
        directory_names.sort()
        file_names.sort()
        for name in directory_names:
            path = directory_path / name
            info = os.lstat(str(path))
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise InventoryError("inventory forbids linked/non-directory entry: {}".format(path))
        for name in file_names:
            path = directory_path / name
            info = _regular_file_stat(path)
            relative = path.relative_to(root).as_posix()
            rows.append(
                {
                    "path": relative,
                    "size_bytes": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                    "ctime_ns": info.st_ctime_ns,
                    "device": info.st_dev,
                    "inode": info.st_ino,
                }
            )
    rows.sort(key=lambda item: item["path"])
    return rows


def canonical_inventory_sha(files):
    normalized = [
        {
            "path": item["path"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
        for item in sorted(files, key=lambda value: value["path"])
    ]
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_file_rows(root, quick_before):
    root = Path(root)
    result = []
    for expected in quick_before:
        relative = expected["path"]
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not relative or "." in pure.parts or ".." in pure.parts:
            raise InventoryError("unsafe relative path: {!r}".format(relative))
        path = root / pure
        before = _regular_file_stat(path)
        digest = sha256_file(path)
        after = _regular_file_stat(path)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        expected_identity = (
            expected["device"],
            expected["inode"],
            expected["size_bytes"],
            expected["mtime_ns"],
            expected["ctime_ns"],
        )
        if identity_before != expected_identity or identity_after != expected_identity:
            raise InventoryError("store file changed while hashing: {}".format(path))
        result.append(
            {"path": relative, "size_bytes": after.st_size, "sha256": digest}
        )
    return result


def validate_manifest(manifest, store, scale, dataset_sha, binary_sha):
    required = {
        "schema_version",
        "state",
        "inventory_schema",
        "store_path",
        "scale",
        "l0_layout",
        "dataset_sha256",
        "binary_sha256",
        "inventory_sha256",
        "files",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise InventoryError("manifest missing keys: {}".format(", ".join(missing)))
    if manifest.get("schema_version") != 1:
        raise InventoryError("schema_version must be exactly 1")
    if manifest.get("state") != "FROZEN":
        raise InventoryError("state must be FROZEN")
    if manifest.get("inventory_schema") != "p20-store-inventory-v1":
        raise InventoryError("inventory_schema drift")
    try:
        manifest_store = Path(manifest.get("store_path", "")).resolve()
    except (TypeError, OSError):
        raise InventoryError("invalid store_path")
    if manifest_store != store:
        raise InventoryError("store_path drift")
    if manifest.get("scale") != scale:
        raise InventoryError("scale drift")
    if manifest.get("l0_layout") != "semantic-budgeted":
        raise InventoryError("l0_layout must be semantic-budgeted")
    if manifest.get("dataset_sha256") != dataset_sha:
        raise InventoryError("dataset_sha256 drift")
    if manifest.get("binary_sha256") != binary_sha:
        raise InventoryError("binary_sha256 drift")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise InventoryError("files must be a non-empty array")
    seen = set()
    previous = None
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size_bytes", "sha256"}:
            raise InventoryError("every file row must contain exactly path,size_bytes,sha256")
        relative = item.get("path")
        pure = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath("/")
        if pure.is_absolute() or not relative or "." in pure.parts or ".." in pure.parts:
            raise InventoryError("unsafe file path")
        if relative in seen or (previous is not None and relative <= previous):
            raise InventoryError("file rows must be unique and strictly path-sorted")
        seen.add(relative)
        previous = relative
        size = item.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise InventoryError("invalid size_bytes")
        normalize_sha(item.get("sha256", ""), "file sha256")
    actual_inventory_sha = canonical_inventory_sha(files)
    if manifest.get("inventory_sha256") != actual_inventory_sha:
        raise InventoryError("inventory_sha256 does not match canonical file rows")
    if "file_count" in manifest and manifest["file_count"] != len(files):
        raise InventoryError("file_count drift")
    total_bytes = sum(item["size_bytes"] for item in files)
    if "total_bytes" in manifest and manifest["total_bytes"] != total_bytes:
        raise InventoryError("total_bytes drift")
    return files


def scan_full_manifest(store, scale, dataset_sha, binary_sha):
    root_before = _directory_identity(store)
    before = quick_inventory(store)
    if not before:
        raise InventoryError("store is empty")
    files = build_file_rows(store, before)
    after = quick_inventory(store)
    root_after = _directory_identity(store)
    if root_before != root_after:
        raise InventoryError("store root changed during inventory")
    if before != after:
        raise InventoryError("store file set or identity changed during inventory")
    return {
        "schema_version": 1,
        "state": "FROZEN",
        "inventory_schema": "p20-store-inventory-v1",
        "store_path": str(store),
        "scale": scale,
        "l0_layout": "semantic-budgeted",
        "dataset_sha256": dataset_sha,
        "binary_sha256": binary_sha,
        "inventory_sha256": canonical_inventory_sha(files),
        "file_count": len(files),
        "total_bytes": sum(item["size_bytes"] for item in files),
        "hash_method": "sha256(canonical-json(files[path,size_bytes,sha256])+LF)",
        "files": files,
    }


def verify_content(store, manifest):
    expected_files = validate_manifest(
        manifest,
        store,
        manifest.get("scale"),
        manifest.get("dataset_sha256"),
        manifest.get("binary_sha256"),
    )
    rebuilt = scan_full_manifest(
        store,
        manifest["scale"],
        manifest["dataset_sha256"],
        manifest["binary_sha256"],
    )
    if rebuilt["files"] != expected_files:
        raise InventoryError("live store content differs from manifest")
    if rebuilt["inventory_sha256"] != manifest["inventory_sha256"]:
        raise InventoryError("live store inventory SHA-256 differs from manifest")
    return rebuilt


def write_json_new(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary_identity = None
    published = False
    try:
        temporary_fd = os.open(
            str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        info = os.fstat(temporary_fd)
        temporary_identity = (info.st_dev, info.st_ino)
        with os.fdopen(temporary_fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # link(2) is an atomic O_EXCL-style publication: it never overwrites a
        # path created after the caller's initial absent-path check.
        os.link(str(temporary), str(path))
        published = True
        os.unlink(str(temporary))
    except Exception:
        for candidate in (temporary, path if published else None):
            if candidate is None or temporary_identity is None:
                continue
            try:
                current = os.lstat(str(candidate))
                if (
                    stat.S_ISREG(current.st_mode)
                    and (current.st_dev, current.st_ino) == temporary_identity
                ):
                    os.unlink(str(candidate))
            except OSError:
                pass
        raise


def run(args):
    store_input = require_absolute(args.store, "store")
    if store_input.is_symlink():
        raise InventoryError("store must be a non-link directory")
    store = store_input.resolve()
    if store.is_symlink() or not store.is_dir():
        raise InventoryError("store must be a non-link directory")
    if args.verify:
        manifest_path = require_absolute(args.verify, "manifest")
        manifest = load_json(manifest_path, "manifest")
        rebuilt = verify_content(store, manifest)
        print(json.dumps({
            "state": "PASS",
            "manifest": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "store_path": str(store),
            "inventory_sha256": rebuilt["inventory_sha256"],
            "file_count": rebuilt["file_count"],
            "total_bytes": rebuilt["total_bytes"],
        }, sort_keys=True))
        return 0

    output_input = require_absolute(args.output, "output")
    if os.path.lexists(str(output_input)):
        raise InventoryError("output must be absent: {}".format(output_input))
    output = output_input.resolve()
    if output == store or store in output.parents:
        raise InventoryError("output cannot be inside the inventoried store")
    dataset_sha = normalize_sha(args.dataset_sha256, "dataset_sha256")
    binary_sha = normalize_sha(args.binary_sha256, "binary_sha256")
    manifest = scan_full_manifest(store, args.scale, dataset_sha, binary_sha)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json_new(output, manifest)
    print(json.dumps({
        "state": "FROZEN",
        "manifest": str(output),
        "manifest_sha256": sha256_file(output),
        "inventory_sha256": manifest["inventory_sha256"],
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
    }, sort_keys=True))
    return 0


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--store", required=True, type=Path)
    action = value.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path)
    action.add_argument("--verify", type=Path)
    value.add_argument("--scale", choices=("sf10", "sf30"))
    value.add_argument("--dataset-sha256")
    value.add_argument("--binary-sha256")
    return value


def main():
    args = parser().parse_args()
    if not args.verify and not (args.scale and args.dataset_sha256 and args.binary_sha256):
        print("FAIL: build mode requires --scale, --dataset-sha256, and --binary-sha256", file=sys.stderr)
        return 2
    try:
        return run(args)
    except (InventoryError, OSError) as exc:
        print("FAIL: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
