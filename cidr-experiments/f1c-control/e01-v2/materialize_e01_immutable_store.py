#!/usr/bin/env python3
"""Materialize and seal one E01 immutable store without mutating its source.

The production path is deliberately one-variant-at-a-time:

1. validate the frozen lifecycle row and the old small P02B manifest;
2. freshly hash the source and require the frozen SHA/count/bytes;
3. reflink-copy into the exact staging root;
4. freshly hash the staging copy, make only that copy immutable, and atomically
   publish it at the exact immutable root;
5. publish receipt-bound manifests and a PASS seal outside the hashed tree.

Failed attempts and staging roots are retained.  No retry overwrites evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


PLAN_SCHEMA = "cidr-e01-store-lifecycle-plan-v1"
MANIFEST_SCHEMA = "p02b-store-manifest-v1"
SEAL_SCHEMA = "cidr-e01-immutable-store-seal-v1"
HASH_METHOD = "sha256-tree-v1(relative-path,size,file-sha256)"
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}
MIN_DATA_FREE_BYTES = 200 * 1024**3
MAX_SMALL_BYTES = 16 * 1024 * 1024


class SealError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SealError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    require(path.is_file() and not path.is_symlink(), f"{label}: missing/symlink")
    require(path.stat().st_size <= MAX_SMALL_BYTES, f"{label}: too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{label}: object required")
    return value


def file_ref(path: Path) -> dict[str, Any]:
    require(path.is_file() and not path.is_symlink(), f"{path}: missing/symlink")
    size = path.stat().st_size
    require(0 < size <= MAX_SMALL_BYTES, f"{path}: small evidence size")
    return {"path": str(path.resolve()), "sha256": sha256_file(path), "size_bytes": size}


def atomic_json_new(path: Path, value: dict[str, Any]) -> None:
    require(not path.exists(), f"{path}: overwrite forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    require(len(payload) <= MAX_SMALL_BYTES, f"{path}: receipt too large")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def tree_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    require(root.is_dir() and not root.is_symlink(), "tree root missing/symlink")
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in sorted(dirnames):
            path = directory_path / name
            require(not path.is_symlink(), f"symlink directory forbidden: {path}")
        for name in sorted(filenames):
            path = directory_path / name
            require(not path.is_symlink(), f"symlink file forbidden: {path}")
            require(path.is_file(), f"non-regular file forbidden: {path}")
            files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix().encode("utf-8"))
    require(files, "empty store root")
    digest = hashlib.sha256()
    total = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        file_sha = sha256_file(path)
        total += size
        digest.update(f"file\0{relative}\0{size}\0{file_sha}\n".encode("utf-8"))
    return {
        "schema_version": MANIFEST_SCHEMA,
        "store_path": str(root),
        "store_sha256": digest.hexdigest(),
        "hash_method": HASH_METHOD,
        "file_count": len(files),
        "total_bytes": total,
    }


def nearest_existing_parent(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists():
        require(candidate.parent != candidate, "no existing target ancestor")
        candidate = candidate.parent
    return candidate


def free_bytes(path: Path) -> int:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def _inside(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((str(child.resolve()), str(parent.resolve()))) == str(
            parent.resolve()
        )
    except ValueError:
        return False


def preflight(
    plan_path: Path,
    *,
    variant: str,
    attempt_root: Path,
    minimum_free_bytes: int = MIN_DATA_FREE_BYTES,
) -> dict[str, Any]:
    require(variant in {"budg-b64", "naive"}, "unsupported variant")
    plan = load_json(plan_path.resolve(), "lifecycle plan")
    require(plan.get("schema_version") == PLAN_SCHEMA, "lifecycle schema")
    require(plan.get("state") == "HOLD", "lifecycle must remain HOLD")
    require(plan.get("strict_serial") is True, "STRICT_SERIAL required")
    require(plan.get("max_live_mutable_clones") == 1, "one live clone contract")
    rows = [row for row in plan.get("stores", []) if row.get("variant") == variant]
    require(len(rows) == 1, "exactly one variant row")
    row = rows[0]
    source = Path(row["source_root"]).resolve()
    staging = Path(row["staging_root"]).resolve()
    target = Path(row["immutable_root"]).resolve()
    immutable_parent = Path(plan["immutable_asset_root"]).resolve()
    require(source.is_dir() and not source.is_symlink(), "source store missing/symlink")
    require(_inside(staging, immutable_parent), "staging escapes immutable root")
    require(_inside(target, immutable_parent), "target escapes immutable root")
    require(not _inside(staging, source) and not _inside(target, source), "target inside source")
    require(not staging.exists(), "staging root exists")
    require(not target.exists(), "immutable target exists")
    require(attempt_root.is_absolute() and not attempt_root.exists(), "new absolute attempt root")
    require(
        str(attempt_root).startswith("/data/WorkSpace/results/E01-F1-ASSET-SEAL-")
        or str(attempt_root).startswith("/tmp/"),
        "attempt root prefix",
    )
    source_manifest_ref = row.get("source_manifest")
    require(isinstance(source_manifest_ref, dict), "source manifest reference")
    source_manifest_path = Path(source_manifest_ref["path"]).resolve()
    require(file_ref(source_manifest_path) == source_manifest_ref, "source manifest ref drift")
    source_manifest = load_json(source_manifest_path, "source manifest")
    require(source_manifest.get("schema_version") == MANIFEST_SCHEMA, "source manifest schema")
    require(source_manifest.get("hash_method") == HASH_METHOD, "source hash method")
    require(Path(source_manifest["store_root"]).resolve() == source, "source manifest root")
    expected = {
        "store_sha256": row["expected_tree_sha256"],
        "file_count": row["expected_file_count"],
        "total_bytes": row["expected_total_bytes"],
    }
    require(source_manifest["store_sha256"] == expected["store_sha256"], "expected SHA drift")
    require(source_manifest["file_count"] == expected["file_count"], "expected file count drift")
    require(source_manifest["total_bytes"] == expected["total_bytes"], "expected bytes drift")
    ancestor = nearest_existing_parent(immutable_parent)
    require(ancestor.stat().st_dev == source.stat().st_dev, "reflink source/target device drift")
    available = free_bytes(ancestor)
    require(available >= minimum_free_bytes, "capacity below immutable-seal gate")
    return {
        "schema_version": "cidr-e01-immutable-store-preflight-v1",
        "state": "PASS",
        "variant": variant,
        "plan": file_ref(plan_path.resolve()),
        "source_manifest": source_manifest_ref,
        "source_root": str(source),
        "staging_root": str(staging),
        "immutable_root": str(target),
        "attempt_root": str(attempt_root.resolve()),
        "source_dev": source.stat().st_dev,
        "source_inode": source.stat().st_ino,
        "available_bytes": available,
        "minimum_free_bytes": minimum_free_bytes,
        "expected": expected,
        "source_mutation_authorized": False,
        "copy_or_hash_performed": False,
        **FALSE_ELIGIBILITY,
    }


def reflink_copy(source: Path, staging: Path, stdout_path: Path, stderr_path: Path) -> None:
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=False, exist_ok=False)
    argv = ["cp", "--archive", "--reflink=always", "--", f"{source}/.", f"{staging}/"]
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        result = subprocess.run(argv, stdout=stdout, stderr=stderr, check=False)
    require(result.returncode == 0, f"reflink copy failed rc={result.returncode}")


def make_immutable(root: Path, *, include_root: bool = True) -> None:
    for directory, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        directory_path = Path(directory)
        for name in filenames:
            path = directory_path / name
            require(path.is_file() and not path.is_symlink(), f"immutable file invalid: {path}")
            path.chmod(0o444)
        for name in dirnames:
            path = directory_path / name
            require(path.is_dir() and not path.is_symlink(), f"immutable dir invalid: {path}")
            path.chmod(0o555)
        if include_root or directory_path != root:
            directory_path.chmod(0o555)


def execute(
    plan_path: Path,
    *,
    variant: str,
    attempt_root: Path,
    minimum_free_bytes: int = MIN_DATA_FREE_BYTES,
    copier: Callable[[Path, Path, Path, Path], None] = reflink_copy,
) -> dict[str, Any]:
    checked = preflight(
        plan_path,
        variant=variant,
        attempt_root=attempt_root,
        minimum_free_bytes=minimum_free_bytes,
    )
    attempt_root.mkdir(parents=True, exist_ok=False)
    atomic_json_new(attempt_root / "PREFLIGHT.json", checked)
    source = Path(checked["source_root"])
    staging = Path(checked["staging_root"])
    target = Path(checked["immutable_root"])
    try:
        source_manifest = tree_manifest(source)
        expected = checked["expected"]
        require(source_manifest["store_sha256"] == expected["store_sha256"], "fresh source SHA mismatch")
        require(source_manifest["file_count"] == expected["file_count"], "fresh source count mismatch")
        require(source_manifest["total_bytes"] == expected["total_bytes"], "fresh source bytes mismatch")
        atomic_json_new(attempt_root / "SOURCE-MANIFEST.json", source_manifest)
        copier(
            source,
            staging,
            attempt_root / "COPY.stdout",
            attempt_root / "COPY.stderr",
        )
        staging_manifest = tree_manifest(staging)
        require(staging_manifest["store_sha256"] == expected["store_sha256"], "copy SHA mismatch")
        require(staging_manifest["file_count"] == expected["file_count"], "copy count mismatch")
        require(staging_manifest["total_bytes"] == expected["total_bytes"], "copy bytes mismatch")
        # Keep only the staging root writable until its atomic rename. Some
        # filesystems reject renaming a read-only source directory even when
        # both parents are writable; all descendants are already immutable.
        make_immutable(staging, include_root=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        require(not target.exists(), "target appeared before publish")
        staging.rename(target)
        target.chmod(0o555)
        final_manifest = dict(staging_manifest)
        final_manifest["store_path"] = str(target)
        atomic_json_new(attempt_root / "IMMUTABLE-MANIFEST.json", final_manifest)
        seal = {
            "schema_version": SEAL_SCHEMA,
            "state": "PASS",
            "variant": variant,
            "hash_method": HASH_METHOD,
            "tree_sha256": final_manifest["store_sha256"],
            "file_count": final_manifest["file_count"],
            "total_bytes": final_manifest["total_bytes"],
            "source_root": str(source),
            "source_dev": checked["source_dev"],
            "source_inode": checked["source_inode"],
            "immutable_root": str(target),
            "immutable_dev": target.stat().st_dev,
            "immutable_inode": target.stat().st_ino,
            "source_manifest_fresh": file_ref(attempt_root / "SOURCE-MANIFEST.json"),
            "immutable_manifest": file_ref(attempt_root / "IMMUTABLE-MANIFEST.json"),
            "copy_method": "cp --archive --reflink=always",
            "source_modified": False,
            "immutable_directory_mode": "0555",
            "immutable_file_mode": "0444",
            "formal_data_collected": False,
            **FALSE_ELIGIBILITY,
        }
        atomic_json_new(attempt_root / "SEAL-DONE.json", seal)
        return seal
    except BaseException as error:
        if not (attempt_root / "FAILED.json").exists():
            atomic_json_new(
                attempt_root / "FAILED.json",
                {
                    "schema_version": "cidr-e01-immutable-store-failed-v1",
                    "state": "FAILED_RETAINED",
                    "variant": variant,
                    "reason": str(error),
                    "staging_root": str(staging),
                    "staging_retained": staging.exists(),
                    "source_root": str(source),
                    "source_modified": False,
                    **FALSE_ELIGIBILITY,
                },
            )
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("preflight", "run"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--variant", choices=("budg-b64", "naive"), required=True)
    parser.add_argument("--attempt-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            value = preflight(args.plan, variant=args.variant, attempt_root=args.attempt_root)
        else:
            value = execute(args.plan, variant=args.variant, attempt_root=args.attempt_root)
    except (SealError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"E01 IMMUTABLE STORE BLOCKED: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"state": value["state"], "variant": value["variant"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
