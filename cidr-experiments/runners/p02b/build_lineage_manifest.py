#!/usr/bin/env python3
"""Build a deterministic SHA-256 tree manifest for a dataset or immutable store."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from p02b_common import GateError, atomic_write_json, resolved_existing_dir, sha256_file


def build_tree_manifest(root: Path, kind: str) -> Dict[str, Any]:
    root = resolved_existing_dir(root, "{} root".format(kind))
    files: List[Path] = []
    for directory, dirnames, filenames in os.walk(str(root), followlinks=False):
        directory_path = Path(directory)
        for name in sorted(dirnames):
            path = directory_path / name
            if path.is_symlink():
                raise GateError("tree manifest rejects symlink directory: {}".format(path))
        for name in sorted(filenames):
            path = directory_path / name
            if path.is_symlink():
                raise GateError("tree manifest rejects symlink file: {}".format(path))
            if not path.is_file():
                raise GateError("tree manifest found non-regular file: {}".format(path))
            files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix().encode("utf-8"))
    if not files:
        raise GateError("tree manifest refuses an empty {} root".format(kind))

    tree_digest = hashlib.sha256()
    total_bytes = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        digest = sha256_file(path)
        total_bytes += size
        record = "file\0{}\0{}\0{}\n".format(relative, size, digest).encode("utf-8")
        tree_digest.update(record)
    # The formal P02B contract intentionally names the selected store
    # `store_path` (while datasets use `dataset_root`).  Keep the builder and
    # both producer/consumer validators on that same v1 schema.
    root_key = "store_path" if kind == "store" else "dataset_root"
    digest_key = "{}_sha256".format(kind)
    return {
        "schema_version": "p02b-{}-manifest-v1".format(kind),
        root_key: str(root),
        digest_key: tree_digest.hexdigest(),
        "hash_method": "sha256-tree-v1(relative-path,size,file-sha256)",
        "file_count": len(files),
        "total_bytes": total_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=("dataset", "store"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        output = args.output.resolve()
        if output == root or root in output.parents:
            raise GateError("manifest output must be outside the hashed root")
        atomic_write_json(output, build_tree_manifest(root, args.kind))
        return 0
    except GateError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
