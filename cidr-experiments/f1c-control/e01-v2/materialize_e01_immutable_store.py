#!/usr/bin/env python3
"""Materialize and seal one E01 immutable store without mutating its source.

The production path is deliberately one-variant-at-a-time:

1. validate the frozen lifecycle row and the old small P02B manifest;
2. freshly hash the source and require the frozen SHA/count/bytes;
3. use the exact copy method frozen by the lifecycle plan (reflink-only for
   v1, or an explicitly evidenced full-copy fallback for v2);
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
from typing import Any, Callable, Optional


PLAN_SCHEMA_V1 = "cidr-e01-store-lifecycle-plan-v1"
PLAN_SCHEMA_V2 = "cidr-e01-store-lifecycle-plan-v2"
PLAN_SCHEMA = PLAN_SCHEMA_V1
MANIFEST_SCHEMA = "p02b-store-manifest-v1"
SEAL_SCHEMA = "cidr-e01-immutable-store-seal-v1"
HASH_METHOD = "sha256-tree-v1(relative-path,size,file-sha256)"
REFLINK_COPY_METHOD = "cp-archive-reflink-always"
FULL_COPY_METHOD = "cp-archive-reflink-never"
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


def stat_identity(path: Path) -> dict[str, Any]:
    value = path.stat()
    return {
        "dev": value.st_dev,
        "inode": value.st_ino,
        "mode": value.st_mode & 0o7777,
        "uid": value.st_uid,
        "gid": value.st_gid,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


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
    plan_schema = plan.get("schema_version")
    require(plan_schema in {PLAN_SCHEMA_V1, PLAN_SCHEMA_V2}, "lifecycle schema")
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

    if plan_schema == PLAN_SCHEMA_V1:
        copy_method = REFLINK_COPY_METHOD
        require(
            row.get("copy_policy", {}).get("preferred_method") == "cp-reflink-always",
            "v1 reflink copy policy drift",
        )
        fallback_evidence = None
        required_available = minimum_free_bytes
    else:
        copy_method = row.get("copy_method")
        require(copy_method == FULL_COPY_METHOD, "v2 explicit full-copy method required")
        fallback = row.get("full_copy_fallback")
        require(isinstance(fallback, dict), "v2 full-copy fallback contract required")
        require(fallback.get("enabled") is True, "v2 full-copy fallback not enabled")
        require(
            fallback.get("reason") == "reflink_operation_not_supported",
            "v2 fallback reason drift",
        )
        fallback_evidence = fallback.get("failure_receipt")
        require(isinstance(fallback_evidence, dict), "fallback failure receipt reference")
        for key in (
            "prior_preflight_receipt",
            "prior_fresh_source_manifest",
            "prior_copy_stderr",
        ):
            evidence = fallback.get(key)
            require(isinstance(evidence, dict), f"{key} reference")
            require(
                file_ref(Path(evidence["path"]).resolve()) == evidence,
                f"{key} drift",
            )
        failure_path = Path(fallback_evidence["path"]).resolve()
        require(file_ref(failure_path) == fallback_evidence, "fallback receipt drift")
        failure = load_json(failure_path, "fallback failure receipt")
        require(failure.get("state") == "FAILED_RETAINED", "fallback evidence state")
        require(failure.get("source_modified") is False, "fallback source mutation")
        require(failure.get("staging_retained") is True, "fallback staging retention")
        require(failure.get("variant") == "budg-b64", "fallback evidence variant")
        require(
            failure.get("reason") == "reflink copy failed rc=1",
            "fallback evidence reason",
        )
        failed_staging = Path(failure["staging_root"]).resolve()
        require(failed_staging.exists(), "failed staging evidence missing")
        require(staging != failed_staging, "new attempt must use a new staging root")
        budg_rows = [
            item
            for item in plan.get("stores", [])
            if isinstance(item, dict) and item.get("variant") == "budg-b64"
        ]
        require(len(budg_rows) == 1, "fallback budg-b64 source row")
        require(
            Path(failure["source_root"]).resolve()
            == Path(budg_rows[0]["source_root"]).resolve(),
            "fallback evidence source drift",
        )
        required_available = minimum_free_bytes + expected["total_bytes"]

    ancestor = nearest_existing_parent(immutable_parent)
    if copy_method == REFLINK_COPY_METHOD:
        require(
            ancestor.stat().st_dev == source.stat().st_dev,
            "reflink source/target device drift",
        )
    available = free_bytes(ancestor)
    require(available >= required_available, "capacity below immutable-seal gate")
    source_stat = stat_identity(source)
    return {
        "schema_version": "cidr-e01-immutable-store-preflight-v2",
        "state": "PASS",
        "variant": variant,
        "copy_method": copy_method,
        "fallback_evidence": fallback_evidence,
        "plan": file_ref(plan_path.resolve()),
        "source_manifest": source_manifest_ref,
        "source_root": str(source),
        "staging_root": str(staging),
        "immutable_root": str(target),
        "attempt_root": str(attempt_root.resolve()),
        "source_dev": source.stat().st_dev,
        "source_inode": source.stat().st_ino,
        "source_preflight_stat": source_stat,
        "available_bytes": available,
        "minimum_free_bytes": minimum_free_bytes,
        "copy_reserve_bytes": (
            expected["total_bytes"] if copy_method == FULL_COPY_METHOD else 0
        ),
        "required_available_bytes": required_available,
        "expected": expected,
        "source_mutation_authorized": False,
        "copy_or_hash_performed": False,
        **FALSE_ELIGIBILITY,
    }


def copy_argv(method: str, source: Path, staging: Path) -> list[str]:
    reflink = {
        REFLINK_COPY_METHOD: "always",
        FULL_COPY_METHOD: "never",
    }.get(method)
    require(reflink is not None, "unsupported copy method")
    return [
        "cp",
        "--archive",
        f"--reflink={reflink}",
        "--",
        f"{source}/.",
        f"{staging}/",
    ]


def reflink_copy(source: Path, staging: Path, stdout_path: Path, stderr_path: Path) -> None:
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=False, exist_ok=False)
    argv = copy_argv(REFLINK_COPY_METHOD, source, staging)
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        result = subprocess.run(argv, stdout=stdout, stderr=stderr, check=False)
    require(result.returncode == 0, f"reflink copy failed rc={result.returncode}")


def full_copy(source: Path, staging: Path, stdout_path: Path, stderr_path: Path) -> None:
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=False, exist_ok=False)
    argv = copy_argv(FULL_COPY_METHOD, source, staging)
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        result = subprocess.run(argv, stdout=stdout, stderr=stderr, check=False)
    require(result.returncode == 0, f"full copy failed rc={result.returncode}")


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
    copier: Optional[Callable[[Path, Path, Path, Path], None]] = None,
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
    target_ancestor = None
    phase = "SOURCE_PRE_HASH_STAT"
    capacity_before_copy = None
    capacity_after_copy = None
    try:
        source_pre_stat = stat_identity(source)
        require(
            source_pre_stat == checked["source_preflight_stat"],
            "source stat drift after preflight",
        )
        phase = "SOURCE_FRESH_HASH"
        source_manifest = tree_manifest(source)
        expected = checked["expected"]
        require(source_manifest["store_sha256"] == expected["store_sha256"], "fresh source SHA mismatch")
        require(source_manifest["file_count"] == expected["file_count"], "fresh source count mismatch")
        require(source_manifest["total_bytes"] == expected["total_bytes"], "fresh source bytes mismatch")
        atomic_json_new(attempt_root / "SOURCE-MANIFEST.json", source_manifest)
        phase = "PRE_COPY_GATE"
        source_pre_copy_stat = stat_identity(source)
        require(
            source_pre_copy_stat == source_pre_stat,
            "source root stat changed before copy",
        )
        target_ancestor = nearest_existing_parent(Path(checked["immutable_root"]))
        capacity_before_copy = free_bytes(target_ancestor)
        require(
            capacity_before_copy >= checked["required_available_bytes"],
            "capacity dropped below copy gate",
        )
        copy_impl = copier
        if copy_impl is None:
            copy_impl = (
                reflink_copy
                if checked["copy_method"] == REFLINK_COPY_METHOD
                else full_copy
            )
        phase = "COPY"
        copy_impl(
            source,
            staging,
            attempt_root / "COPY.stdout",
            attempt_root / "COPY.stderr",
        )
        phase = "TARGET_FRESH_HASH"
        staging_manifest = tree_manifest(staging)
        require(staging_manifest["store_sha256"] == expected["store_sha256"], "copy SHA mismatch")
        require(staging_manifest["file_count"] == expected["file_count"], "copy count mismatch")
        require(staging_manifest["total_bytes"] == expected["total_bytes"], "copy bytes mismatch")
        source_post_stat = stat_identity(source)
        require(source_post_stat == source_pre_stat, "source root stat changed during copy")
        capacity_after_copy = free_bytes(target_ancestor)
        # Keep only the staging root writable until its atomic rename. Some
        # filesystems reject renaming a read-only source directory even when
        # both parents are writable; all descendants are already immutable.
        phase = "IMMUTABLE_PUBLISH"
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
            "source_pre_stat": source_pre_stat,
            "source_pre_copy_stat": source_pre_copy_stat,
            "source_post_stat": source_post_stat,
            "immutable_root": str(target),
            "immutable_dev": target.stat().st_dev,
            "immutable_inode": target.stat().st_ino,
            "source_manifest_fresh": file_ref(attempt_root / "SOURCE-MANIFEST.json"),
            "immutable_manifest": file_ref(attempt_root / "IMMUTABLE-MANIFEST.json"),
            "copy_method": checked["copy_method"],
            "copy_argv": copy_argv(checked["copy_method"], source, staging),
            "fallback_evidence": checked["fallback_evidence"],
            "fresh_source_tree_sha256": source_manifest["store_sha256"],
            "fresh_target_tree_sha256": final_manifest["store_sha256"],
            "capacity_gate": {
                "available_bytes": checked["available_bytes"],
                "minimum_free_bytes": checked["minimum_free_bytes"],
                "copy_reserve_bytes": checked["copy_reserve_bytes"],
                "required_available_bytes": checked["required_available_bytes"],
                "available_before_copy_bytes": capacity_before_copy,
                "available_after_copy_bytes": capacity_after_copy,
            },
            "source_modified": False,
            "immutable_directory_mode": "0555",
            "immutable_file_mode": "0444",
            "formal_data_collected": False,
            **FALSE_ELIGIBILITY,
        }
        phase = "SEAL"
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
                    "phase": phase,
                    "reason": str(error),
                    "copy_method": checked["copy_method"],
                    "copy_argv": copy_argv(checked["copy_method"], source, staging),
                    "staging_root": str(staging),
                    "staging_retained": staging.exists(),
                    "target_root": str(target),
                    "target_exists": target.exists(),
                    "source_root": str(source),
                    "source_pre_stat": locals().get("source_pre_stat"),
                    "source_pre_copy_stat": locals().get("source_pre_copy_stat"),
                    "source_post_stat": (
                        stat_identity(source) if source.exists() else None
                    ),
                    "capacity_gate": {
                        "available_at_preflight_bytes": checked["available_bytes"],
                        "required_available_bytes": checked["required_available_bytes"],
                        "available_before_copy_bytes": capacity_before_copy,
                        "available_after_copy_bytes": capacity_after_copy,
                    },
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
