#!/usr/bin/env python3
"""Receipt-bound phase executor for the four-cell E01 incremental campaign.

The executor deliberately separates clone/request preparation, the P31-wrapped
storage-bench process, post-timing conversion/validation, and exact cleanup.
It never upgrades eligibility; every receipt remains false for formal,
performance, and paper-claim eligibility.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Optional, Sequence


PLAN_SCHEMA = "cidr-e01-incremental-backend-plan-v3"
DRYRUN_SCHEMA = "cidr-e01-mutable-clone-dry-run-v3"
PHASES = ("prepare", "p31", "finalize", "cleanup")
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}
MAX_SMALL_FILE_BYTES = 16 * 1024 * 1024
FULL_COPY_RESERVE_BYTES = 14_400_000_000
TARGET_P02B_SCHEMA = "cidr-e01-target-specific-p02b-v1"
TARGETS = {
    "budg-b64": {"layout": "semantic-budgeted", "hint": True},
    "naive": {"layout": "naive", "hint": False},
}
CELL_VARIANTS = {
    "seml0:bridge-canary": "budg-b64",
    "seml0-naive:r1": "naive",
    "seml0-naive:r2": "naive",
    "seml0-naive:r3": "naive",
}
CLONE_DRY_RUN_BLOCKERS = ["mutable clone lifecycle dry-run receipt absent"]
FULL_COPY_ARGV_TEMPLATE = (
    "/bin/cp",
    "--archive",
    "--reflink=never",
    "--one-file-system",
    "--",
    "{SOURCE}",
    "{TARGET}",
)


class PhaseError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PhaseError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PhaseError(f"{label}: invalid JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    require(len(payload) <= MAX_SMALL_FILE_BYTES, f"{path}: receipt too large")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def replace_state_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace the sole lifecycle marker after RUNNING."""
    require(path.name == "STATE.json" and path.is_file(), "active STATE marker missing")
    current = load_json(path, "active lifecycle state")
    require(current.get("state") == "RUNNING", "lifecycle state is not RUNNING")
    temporary = path.with_name(f".STATE.{os.getpid()}.tmp")
    require(not os.path.lexists(temporary), "state temporary path exists")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def verify_ref(value: Any, label: str) -> dict[str, Any]:
    require(type(value) is dict, f"{label}: reference required")
    require(set(value) == {"path", "sha256", "size_bytes"}, f"{label}: keys drift")
    path = Path(value["path"]).resolve()
    require(path.is_file() and not path.is_symlink(), f"{label}: regular file required")
    require(0 < path.stat().st_size <= MAX_SMALL_FILE_BYTES, f"{label}: size invalid")
    actual = {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }
    require(actual == value, f"{label}: path/size/SHA drift")
    return actual


def metadata_manifest(root: Path) -> dict[str, Any]:
    """Hash names/types/sizes/modes only; never read file contents."""
    root = root.resolve()
    require(root.is_dir() and not root.is_symlink(), f"invalid tree root: {root}")
    digest = hashlib.sha256()
    files = directories = total = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            path = base / name
            require(not path.is_symlink(), f"symlink directory forbidden: {path}")
            relative = path.relative_to(root).as_posix()
            mode = os.stat(path, follow_symlinks=False).st_mode & 0o7777
            digest.update(f"d\0{relative}\0{mode:o}\n".encode())
            directories += 1
        for name in filenames:
            path = base / name
            require(path.is_file() and not path.is_symlink(), f"non-regular file: {path}")
            relative = path.relative_to(root).as_posix()
            stat = os.stat(path, follow_symlinks=False)
            mode = stat.st_mode & 0o7777
            digest.update(f"f\0{relative}\0{stat.st_size}\0{mode:o}\n".encode())
            files += 1
            total += stat.st_size
    require(files > 0, "tree is empty")
    return {
        "method": "sha256-tree-metadata-v1(relative-path,type,size,mode;no-content-read)",
        "sha256": digest.hexdigest(),
        "file_count": files,
        "directory_count": directories,
        "total_bytes": total,
        "content_hashed": False,
    }


def content_tree_manifest(root: Path) -> dict[str, Any]:
    """Recompute the sealed store content tree contract."""
    root = root.resolve()
    require(root.is_dir() and not root.is_symlink(), f"invalid content tree root: {root}")
    files: list[Path] = []
    directory_count = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        directory_count += 1
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            require(not (base / name).is_symlink(), "symlink directory forbidden")
        for name in filenames:
            path = base / name
            require(path.is_file() and not path.is_symlink(), f"non-regular file: {path}")
            files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix().encode())
    require(files, "content tree is empty")
    digest = hashlib.sha256()
    total = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total += size
        digest.update(f"file\0{relative}\0{size}\0{sha256_file(path)}\n".encode())
    root_stat = os.stat(root, follow_symlinks=False)
    return {
        "method": "sha256-tree-v1(relative-path,size,content)",
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "directory_count": directory_count,
        "total_bytes": total,
        "dev": root_stat.st_dev,
        "inode": root_stat.st_ino,
        "full_tree_hash_performed": True,
    }


def identity_permission_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    require(root.is_dir() and not root.is_symlink(), f"invalid identity root: {root}")
    digest = hashlib.sha256()
    files = directories = 0
    writable: list[str] = []
    entries: list[dict[str, Any]] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        dirnames.sort()
        filenames.sort()
        for path, kind in [(base, "d"), *[(base / name, "f") for name in filenames]]:
            require(not path.is_symlink(), f"symlink forbidden: {path}")
            entry_stat = os.stat(path, follow_symlinks=False)
            relative = path.relative_to(root).as_posix() or "."
            mode = stat.S_IMODE(entry_stat.st_mode)
            if mode & 0o222:
                writable.append(relative)
            digest.update(
                f"{kind}\0{relative}\0{entry_stat.st_dev}\0{entry_stat.st_ino}\0"
                f"{entry_stat.st_uid}\0{entry_stat.st_gid}\0{mode:o}\n".encode()
            )
            entries.append(
                {
                    "path": relative,
                    "kind": "directory" if kind == "d" else "file",
                    "dev": entry_stat.st_dev,
                    "inode": entry_stat.st_ino,
                    "uid": entry_stat.st_uid,
                    "gid": entry_stat.st_gid,
                    "mode": f"{mode:04o}",
                }
            )
            if kind == "d":
                directories += 1
            else:
                files += 1
        for name in dirnames:
            require(not (base / name).is_symlink(), "symlink directory forbidden")
    root_stat = os.stat(root, follow_symlinks=False)
    return {
        "method": "sha256-tree-identity-permissions-v1(path,type,dev,inode,uid,gid,mode)",
        "sha256": digest.hexdigest(),
        "root_dev": root_stat.st_dev,
        "root_inode": root_stat.st_ino,
        "root_uid": root_stat.st_uid,
        "root_gid": root_stat.st_gid,
        "root_mode": f"{stat.S_IMODE(root_stat.st_mode):04o}",
        "file_count": files,
        "directory_count": directories,
        "writable_entries": writable,
        "entries": entries,
    }


def regular_file_identity_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    require(root.is_dir() and not root.is_symlink(), f"invalid file identity root: {root}")
    rows: list[dict[str, Any]] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            require(not (base / name).is_symlink(), "symlink directory forbidden")
        for name in filenames:
            path = base / name
            require(path.is_file() and not path.is_symlink(), f"non-regular file: {path}")
            entry_stat = os.stat(path, follow_symlinks=False)
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "dev": entry_stat.st_dev,
                    "inode": entry_stat.st_ino,
                    "size_bytes": entry_stat.st_size,
                }
            )
    rows.sort(key=lambda row: row["path"].encode())
    require(rows, "regular file identity manifest is empty")
    digest = hashlib.sha256(
        "".join(
            f'{row["path"]}\0{row["dev"]}\0{row["inode"]}\0{row["size_bytes"]}\n'
            for row in rows
        ).encode()
    ).hexdigest()
    return {
        "method": "sha256-regular-file-identity-v1(path,dev,inode,size)",
        "sha256": digest,
        "file_count": len(rows),
        "files": rows,
    }


def validate_file_identity_separation(
    source: Mapping[str, Any], clone: Mapping[str, Any]
) -> None:
    source_rows = source.get("files")
    clone_rows = clone.get("files")
    require(type(source_rows) is list and type(clone_rows) is list, "file identity rows required")
    source_by_path = {row["path"]: row for row in source_rows}
    clone_by_path = {row["path"]: row for row in clone_rows}
    require(len(source_by_path) == len(source_rows), "duplicate source file path")
    require(len(clone_by_path) == len(clone_rows), "duplicate clone file path")
    require(set(source_by_path) == set(clone_by_path), "source/clone relative file paths differ")
    for path in source_by_path:
        require(
            source_by_path[path]["size_bytes"] == clone_by_path[path]["size_bytes"],
            f"source/clone file size differs: {path}",
        )
    source_inodes = {(row["dev"], row["inode"]) for row in source_rows}
    clone_inodes = {(row["dev"], row["inode"]) for row in clone_rows}
    require(len(source_inodes) == len(source_rows), "duplicate source dev/inode")
    require(len(clone_inodes) == len(clone_rows), "duplicate clone dev/inode")
    require(not source_inodes.intersection(clone_inodes), "source/clone inode overlap")


def tree_space(root: Path) -> dict[str, Any]:
    root = root.resolve()
    logical = allocated = entries = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        for path in [base, *[base / name for name in filenames]]:
            entry_stat = os.stat(path, follow_symlinks=False)
            logical += entry_stat.st_size if path.is_file() else 0
            allocated += entry_stat.st_blocks * 512
            entries += 1
        for name in dirnames:
            require(not (base / name).is_symlink(), "symlink directory forbidden")
    return {
        "logical_file_bytes": logical,
        "allocated_bytes": allocated,
        "entry_count": entries,
    }


def full_copy_capacity_evidence(source: Path, target_parent: Path) -> dict[str, Any]:
    source_space = tree_space(source)
    fs_stat = os.statvfs(target_parent)
    free_bytes = fs_stat.f_bavail * fs_stat.f_frsize
    minimum_bytes = source_space["allocated_bytes"] + FULL_COPY_RESERVE_BYTES
    require(
        free_bytes >= minimum_bytes,
        f"full-copy capacity below source-allocated+14.4GB reserve: {free_bytes} < {minimum_bytes}",
    )
    return {
        "free_bytes_before": free_bytes,
        "source_allocated_bytes": source_space["allocated_bytes"],
        "source_logical_bytes": source_space["logical_file_bytes"],
        "reserve_bytes": FULL_COPY_RESERVE_BYTES,
        "minimum_bytes": minimum_bytes,
        "state": "PASS",
    }


def filesystem_evidence(source: Path, target_parent: Path) -> dict[str, Any]:
    rows = {}
    for label, path in (("source", source.resolve()), ("target_parent", target_parent.resolve())):
        completed = subprocess.run(
            ["/usr/bin/findmnt", "-n", "-o", "TARGET,SOURCE,FSTYPE", "-T", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        require(completed.returncode == 0 and completed.stdout.strip(), f"{label}: findmnt failed")
        fields = completed.stdout.strip().split()
        require(len(fields) == 3, f"{label}: unexpected findmnt output")
        entry_stat = os.stat(path, follow_symlinks=False)
        rows[label] = {
            "path": str(path),
            "mount_target": fields[0],
            "mount_source": fields[1],
            "filesystem_type": fields[2],
            "dev": entry_stat.st_dev,
        }
    require(rows["source"]["filesystem_type"] == "ext4", "source filesystem is not ext4")
    require(rows["target_parent"]["filesystem_type"] == "ext4", "target filesystem is not ext4")
    require(rows["source"]["dev"] == rows["target_parent"]["dev"], "source/target device drift")
    return rows


def thaw_owner_writable(root: Path) -> list[dict[str, Any]]:
    root = root.resolve()
    require(root.is_dir() and not root.is_symlink(), "thaw root invalid")
    paths: list[Path] = [root]
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        dirnames.sort()
        filenames.sort()
        paths.extend(base / name for name in dirnames)
        paths.extend(base / name for name in filenames)
    unique = sorted(set(paths), key=lambda path: path.relative_to(root).as_posix().encode())
    rows: list[dict[str, Any]] = []
    for path in unique:
        require(not path.is_symlink(), f"thaw symlink forbidden: {path}")
        before_stat = os.stat(path, follow_symlinks=False)
        require(before_stat.st_uid == os.geteuid(), f"thaw owner drift: {path}")
        before = stat.S_IMODE(before_stat.st_mode)
        after = before | stat.S_IWUSR
        os.chmod(path, after)
        actual = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
        require(actual == after, f"thaw mode apply failed: {path}")
        require(actual ^ before == stat.S_IWUSR, f"thaw changed bits beyond owner write: {path}")
        rows.append(
            {
                "path": path.relative_to(root).as_posix() or ".",
                "kind": "directory" if path.is_dir() else "file",
                "uid": before_stat.st_uid,
                "gid": before_stat.st_gid,
                "dev": before_stat.st_dev,
                "inode": before_stat.st_ino,
                "mode_before": f"{before:04o}",
                "mode_after": f"{after:04o}",
            }
        )
    return rows


def validate_thaw_manifest(
    source_identity: Mapping[str, Any],
    clone_identity: Mapping[str, Any],
    thaw_manifest: Sequence[Mapping[str, Any]],
) -> None:
    source_entries = source_identity.get("entries")
    clone_entries = clone_identity.get("entries")
    require(type(source_entries) is list and type(clone_entries) is list, "identity entries required")
    source_by_path = {row["path"]: row for row in source_entries}
    clone_by_path = {row["path"]: row for row in clone_entries}
    thaw_by_path = {row["path"]: row for row in thaw_manifest}
    require(len(source_by_path) == len(source_entries), "duplicate source identity path")
    require(len(clone_by_path) == len(clone_entries), "duplicate clone identity path")
    require(len(thaw_by_path) == len(thaw_manifest), "duplicate thaw path")
    require(
        set(source_by_path) == set(clone_by_path) == set(thaw_by_path),
        "thaw manifest does not cover every file and directory",
    )
    for path, thaw in thaw_by_path.items():
        source = source_by_path[path]
        clone = clone_by_path[path]
        before = int(thaw["mode_before"], 8)
        after = int(thaw["mode_after"], 8)
        require(thaw["kind"] == source["kind"] == clone["kind"], f"thaw kind drift: {path}")
        require(thaw["uid"] == source["uid"] == clone["uid"], f"thaw uid drift: {path}")
        require(thaw["gid"] == source["gid"] == clone["gid"], f"thaw gid drift: {path}")
        require(before == int(source["mode"], 8), f"thaw before-mode drift: {path}")
        require(after == int(clone["mode"], 8), f"thaw after-mode drift: {path}")
        require(after ^ before == stat.S_IWUSR, f"thaw changed bits beyond owner write: {path}")
        require(
            thaw["dev"] == clone["dev"] and thaw["inode"] == clone["inode"],
            f"thaw clone identity drift: {path}",
        )


def full_copy_argv(policy: Mapping[str, Any], source: Path, target: Path) -> list[str]:
    configured = policy.get("copy_argv")
    require(
        type(configured) is list and tuple(configured) == FULL_COPY_ARGV_TEMPLATE,
        "full-copy argv contract drift",
    )
    return [
        str(source.resolve()) + "/." if item == "{SOURCE}"
        else str(target.absolute()) if item == "{TARGET}"
        else item
        for item in FULL_COPY_ARGV_TEMPLATE
    ]


def _safe_clone_roots(source: Path, target: Path, allowed_parent: Path) -> tuple[Path, Path]:
    source = source.resolve()
    target = target.absolute()
    allowed_parent = allowed_parent.resolve()
    require(source.is_dir() and not source.is_symlink(), "clone source invalid")
    require(not os.path.lexists(target), "clone target already exists, including dangling symlink")
    require(target.parent.resolve() == allowed_parent, "clone target parent drift")
    require(not os.path.ismount(source), "clone source must not be a mount point")
    require(not os.path.ismount(target.parent), "clone target parent must not be a mount point")
    require(
        source != target and source not in target.parents and target not in source.parents,
        "clone overlap",
    )
    return source, target


def copy_clone(
    source: Path,
    target: Path,
    allowed_parent: Path,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    source, target = _safe_clone_roots(source, target, allowed_parent)
    command = full_copy_argv(policy, source, target)
    source_meta = metadata_manifest(source)
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    require(completed.returncode == 0, f"clone command failed rc={completed.returncode}: {completed.stderr.strip()}")
    require(target.is_dir() and not target.is_symlink(), "clone target missing")
    target_meta = metadata_manifest(target)
    require(source_meta == target_meta, "source/clone metadata drift")
    stat = os.stat(target, follow_symlinks=False)
    return {
        "copy_argv": command,
        "source": str(source),
        "target": str(target.resolve()),
        "target_dev": stat.st_dev,
        "target_inode": stat.st_ino,
        "metadata_manifest": target_meta,
    }


def verified_full_copy_and_thaw(
    source: Path,
    target: Path,
    allowed_parent: Path,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Create and fully verify an inode-independent mutable clone outside timing."""
    source, target = _safe_clone_roots(source, target, allowed_parent)
    require(policy.get("copy_mode") == "explicit-full-copy-ext4-v1", "full-copy mode required")
    require(
        policy.get("filesystem_contract", {}).get("filesystem_type") == "ext4"
        and policy.get("filesystem_contract", {}).get("reflink_supported") is False,
        "full-copy filesystem contract drift",
    )
    expected_tree = policy.get("tree_sha256")
    require(type(expected_tree) is str and len(expected_tree) == 64, "sealed tree SHA required")
    expanded_argv = full_copy_argv(policy, source, target)
    fs = filesystem_evidence(source, allowed_parent)
    capacity = full_copy_capacity_evidence(source, allowed_parent)
    source_tree_pre = content_tree_manifest(source)
    require(source_tree_pre["sha256"] == expected_tree, "source pre-copy tree SHA drift")
    source_identity_pre = identity_permission_manifest(source)
    require(not source_identity_pre["writable_entries"], "source became writable before copy")
    source_files_pre = regular_file_identity_manifest(source)

    clone = copy_clone(source, target, allowed_parent, policy)
    require(clone["copy_argv"] == expanded_argv, "executed copy argv drift")
    clone_files = regular_file_identity_manifest(target)
    validate_file_identity_separation(source_files_pre, clone_files)
    clone_identity_before_thaw = identity_permission_manifest(target)
    require(
        set(source_identity_pre["writable_entries"])
        == set(clone_identity_before_thaw["writable_entries"]),
        "clone permissions differ before thaw",
    )
    thaw = thaw_owner_writable(target)
    clone_identity_after_thaw = identity_permission_manifest(target)
    validate_thaw_manifest(source_identity_pre, clone_identity_after_thaw, thaw)
    clone_tree = content_tree_manifest(target)
    require(clone_tree["sha256"] == expected_tree, "clone content tree SHA drift")
    require(
        clone_tree["file_count"] == source_tree_pre["file_count"]
        and clone_tree["total_bytes"] == source_tree_pre["total_bytes"],
        "clone count/byte drift",
    )
    clone_space = tree_space(target)
    require(
        clone_space["logical_file_bytes"] == clone_tree["total_bytes"]
        and clone_space["allocated_bytes"] > 0,
        "clone space evidence drift",
    )

    source_tree_post = content_tree_manifest(source)
    source_identity_post = identity_permission_manifest(source)
    source_files_post = regular_file_identity_manifest(source)
    require(source_tree_pre == source_tree_post, "source tree drift across full copy")
    require(source_identity_pre == source_identity_post, "source identity/permissions drift across full copy")
    require(source_files_pre == source_files_post, "source regular-file identity drift across full copy")
    require(not source_identity_post["writable_entries"], "source became writable after copy")
    return {
        "copy": clone,
        "copy_argv": expanded_argv,
        "filesystem_evidence": fs,
        "capacity_evidence": capacity,
        "source_tree_pre": source_tree_pre,
        "source_tree_post": source_tree_post,
        "source_identity_pre": source_identity_pre,
        "source_identity_post": source_identity_post,
        "source_files_pre": source_files_pre,
        "source_files_post": source_files_post,
        "clone_files": clone_files,
        "clone_identity_before_thaw": clone_identity_before_thaw,
        "clone_identity_after_thaw": clone_identity_after_thaw,
        "clone_tree": clone_tree,
        "clone_space": clone_space,
        "thaw_manifest": thaw,
        "full_content_hash_performed": True,
        "hash_outside_p31": True,
    }


def revalidate_prepared_clone(
    cell: Mapping[str, Any],
    cwd: Path,
) -> dict[str, Any]:
    receipt = load_json(cwd / "receipts/store-clone.json", "store clone receipt")
    policy = cell["runtime"]["clone_policy"]
    source = Path(policy["source_root"]).resolve()
    target = Path(policy["mutable_clone"]).absolute()
    require(target == (cwd / "mutable-store").absolute(), "prepared clone target drift")
    require(receipt.get("source") == str(source), "prepared clone source drift")
    require(receipt.get("target") == str(target), "prepared clone receipt target drift")
    require(target.is_dir() and not target.is_symlink(), "prepared clone missing")
    target_stat = os.stat(target, follow_symlinks=False)
    require(
        target_stat.st_dev == receipt.get("target_dev")
        and target_stat.st_ino == receipt.get("target_inode"),
        "prepared clone root identity drift",
    )
    verification = receipt.get("verification")
    require(type(verification) is dict, "prepared clone verification missing")
    require(
        verification.get("copy_argv") == full_copy_argv(policy, source, target),
        "prepared clone copy argv drift",
    )
    source_tree = content_tree_manifest(source)
    source_identity = identity_permission_manifest(source)
    source_files = regular_file_identity_manifest(source)
    clone_tree = content_tree_manifest(target)
    clone_identity = identity_permission_manifest(target)
    clone_files = regular_file_identity_manifest(target)
    require(source_tree == verification.get("source_tree_post"), "prepared source tree drift")
    require(source_identity == verification.get("source_identity_post"), "prepared source identity drift")
    require(source_files == verification.get("source_files_post"), "prepared source files drift")
    require(clone_tree == verification.get("clone_tree"), "prepared clone tree drift")
    require(clone_identity == verification.get("clone_identity_after_thaw"), "prepared clone identity drift")
    require(clone_files == verification.get("clone_files"), "prepared clone files drift")
    validate_file_identity_separation(source_files, clone_files)
    validate_thaw_manifest(source_identity, clone_identity, verification.get("thaw_manifest", []))
    require(clone_tree["sha256"] == policy["tree_sha256"], "prepared clone sealed SHA drift")
    return receipt


def exact_cleanup(target: Path, allowed_parent: Path, expected_dev: int, expected_inode: int) -> None:
    target = target.absolute()
    allowed_parent = allowed_parent.resolve()
    require(os.path.lexists(target), "cleanup target absent")
    require(not target.is_symlink(), "cleanup target symlink forbidden")
    require(target.parent.resolve() == allowed_parent, "cleanup parent drift")
    require(target.is_dir() and not target.is_symlink(), "cleanup target invalid")
    require(not os.path.ismount(target), "cleanup target is a mount point")
    target_stat = os.stat(target, follow_symlinks=False)
    require(
        target_stat.st_dev == expected_dev and target_stat.st_ino == expected_inode,
        "cleanup dev/inode drift",
    )
    shutil.rmtree(target)
    require(not os.path.lexists(target), "cleanup target still exists, including dangling symlink")


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("e01_seml0_adapter_bound", path)
    require(spec is not None and spec.loader is not None, "cannot load adapter module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replace_tokens(argv: list[str], tokens: Mapping[str, str]) -> list[str]:
    require(argv and Path(argv[0]).is_absolute(), "absolute argv required")
    result = []
    for item in argv:
        for token, replacement in tokens.items():
            item = item.replace(token, replacement)
        require("{" not in item and "}" not in item, f"unresolved argv token: {item}")
        result.append(item)
    return result


def _expected_request(cell: Mapping[str, Any], mutable_clone: Path) -> dict[str, Any]:
    return json.loads(
        json.dumps(cell["runtime"]["request"]).replace("{MUTABLE_CLONE}", str(mutable_clone.resolve()))
    )


def _base_receipt(cell: Mapping[str, Any], plan_sha: str, role: str) -> dict[str, Any]:
    return {
        "schema_version": f"cidr-e01-incremental-{role}-receipt-v1",
        "state": "PASS",
        "mode": "production",
        "synthetic_test_only": False,
        "fixture_only": False,
        "cell_key": cell["cell_key"],
        "ordinal": cell["ordinal"],
        "backend_plan_sha256": plan_sha,
        **FALSE_ELIGIBILITY,
    }


def _cell(plan: Mapping[str, Any], key: str) -> dict[str, Any]:
    matches = [row for row in plan.get("cells", []) if row.get("cell_key") == key]
    require(len(matches) == 1, "cell key missing/duplicate")
    cell = matches[0]
    require(type(cell.get("runtime")) is dict, "cell runtime contract missing")
    return cell


def _iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def revalidate_target(cell: Mapping[str, Any]) -> dict[str, Any]:
    runtime = cell["runtime"]
    variant = runtime.get("variant")
    require(variant in TARGETS, "target variant drift")
    ref = verify_ref(runtime.get("target_p02b"), f"{variant} target P02B")
    bundle = load_json(Path(ref["path"]), f"{variant} target P02B")
    require(bundle.get("schema_version") == TARGET_P02B_SCHEMA, "target P02B schema drift")
    require(bundle.get("state") == "PASS" and bundle.get("variant") == variant, "target P02B state/variant drift")
    require(bundle.get("target") == TARGETS[variant], "target layout/hint drift")
    require(bundle.get("store_unchanged") is True and bundle.get("store_pre") == bundle.get("store_post"), "target store integrity drift")
    require(bundle["store_pre"].get("full_tree_hash_performed") is True, "full-tree P02B seal required")
    policy = runtime["clone_policy"]
    require(bundle["store_pre"].get("sha256") == policy["tree_sha256"], "target P02B/store SHA mismatch")
    seal_ref = verify_ref(policy["source_seal"], "source store seal")
    seal = load_json(Path(seal_ref["path"]), "source store seal")
    source = Path(policy["source_root"]).resolve()
    stat_now = os.stat(source, follow_symlinks=False)
    require(stat_now.st_dev == seal.get("immutable_dev") and stat_now.st_ino == seal.get("immutable_inode"), "immutable root dev/inode drift")
    require(stat_now.st_mode & 0o222 == 0, "immutable root became writable")
    for directory, _, filenames in os.walk(source, followlinks=False):
        base = Path(directory)
        require(os.stat(base, follow_symlinks=False).st_mode & 0o222 == 0, "immutable directory became writable")
        for name in filenames:
            path = base / name
            require(path.is_file() and not path.is_symlink(), "immutable non-regular file")
            require(os.stat(path, follow_symlinks=False).st_mode & 0o222 == 0, "immutable file became writable")
    require(bundle.get("static_inputs", {}).get("query_plan") == runtime.get("target_query_plan"), "target query-plan ref drift")
    require(bundle.get("lease") == runtime.get("target_lease"), "target lease ref drift")
    verify_ref(runtime["target_query_plan"], "target query plan")
    verify_ref(bundle["static_inputs"]["config"], "target P02B config")
    if variant == "naive":
        equivalence_ref = verify_ref(bundle["static_inputs"].get("naive_plan_equivalence"), "naive plan equivalence")
        equivalence = load_json(Path(equivalence_ref["path"]), "naive plan equivalence")
        require(equivalence.get("source_plan", {}).get("sha256") == "4520c88eb594903eb6e3f282838e6cd885e205ee5b0b1790565112d5940f8ea3", "naive source-plan SHA drift")
        require(equivalence.get("target_plan") == runtime["target_query_plan"], "naive equivalence target drift")
    lease_ref = verify_ref(runtime["target_lease"], "target lease")
    lease = load_json(Path(lease_ref["path"]), "target lease")
    expires = lease.get("expires_at_utc") or lease.get("expires_at")
    require(type(expires) is str and _iso(expires) > dt.datetime.now(dt.timezone.utc), "target lease expired")
    for consumer in ("P10", "P20"):
        validation_ref = verify_ref(bundle.get("official_lease_validations", {}).get(consumer), f"{consumer} canonical lease validation")
        validation = load_json(Path(validation_ref["path"]), f"{consumer} canonical lease validation")
        require(validation.get("schema_version") == "cidr-batch-lease-admission-v2", "canonical lease validation schema drift")
        require(validation.get("state") == "PASS" and validation.get("consumer") == consumer, "canonical lease validation consumer drift")
        require(validation.get("lease") == lease_ref["path"] and validation.get("lease_sha256") == lease_ref["sha256"], "canonical lease validation ref drift")
        require(validation.get("repo_head") == bundle["static_inputs"]["repo_head"], "canonical lease validation repo drift")
        require(validation.get("binary_sha256") == bundle["static_inputs"]["bound_inputs"]["binary"]["sha256"], "canonical lease validation binary drift")
        require(validation.get("expires_at_utc") == expires, "canonical lease validation expiry drift")
    return ref


def prepare(plan: Mapping[str, Any], cell: Mapping[str, Any], plan_sha: str, cwd: Path) -> None:
    target_ref = revalidate_target(cell)
    runtime = cell["runtime"]
    policy = runtime["clone_policy"]
    require(
        Path(policy["mutable_clone"]).absolute() == (cwd / "mutable-store").absolute(),
        "mutable clone target/cell staging drift",
    )
    seal_ref = verify_ref(policy["source_seal"], "source store seal")
    seal = load_json(Path(seal_ref["path"]), "source store seal")
    require(seal.get("state") == "PASS", "source store seal is not PASS")
    require(seal.get("tree_sha256") == policy["tree_sha256"], "source tree SHA drift")
    require(Path(seal["immutable_root"]).resolve() == Path(policy["source_root"]).resolve(), "source root/seal drift")
    verification = verified_full_copy_and_thaw(
        Path(policy["source_root"]),
        Path(policy["mutable_clone"]),
        cwd,
        policy,
    )
    clone = verification["copy"]
    atomic_json(
        cwd / "receipts/store-clone.json",
        {
            **_base_receipt(cell, plan_sha, "store-clone"),
            "source_seal": seal_ref,
            "target_p02b": target_ref,
            "source_tree_sha256": policy["tree_sha256"],
            "source": clone["source"],
            "target": clone["target"],
            "target_dev": clone["target_dev"],
            "target_inode": clone["target_inode"],
            "full_content_hash_performed": True,
            "hash_outside_p31": True,
            "verification": verification,
        },
    )
    request = _expected_request(cell, Path(clone["target"]))
    request_path = cwd / "adapter-request.json"
    atomic_json(request_path, request)
    tokens = {
        "{STAGING}": str(cwd),
        "{MUTABLE_CLONE}": clone["target"],
        "{REQUEST}": str(request_path),
    }
    binary_argv = _replace_tokens(list(runtime["binary_argv"]), tokens)
    p31_argv = _replace_tokens(list(runtime["p31_argv"]), {**tokens, "{BINARY_ARGV_JSON}": json.dumps(binary_argv)})
    atomic_json(
        cwd / "receipts/prepared-command.json",
        {
            **_base_receipt(cell, plan_sha, "prepared-command"),
            "request": {
                "path": str(request_path),
                "sha256": sha256_file(request_path),
                "size_bytes": request_path.stat().st_size,
            },
            "binary_argv": binary_argv,
            "p31_argv": p31_argv,
            "timing_boundary": "p31-wraps-storage-bench-binary-only-v1",
            "asset_hash_inside_p31": False,
            "clone_inside_p31": False,
        },
    )


def run_p31(plan: Mapping[str, Any], cell: Mapping[str, Any], plan_sha: str, cwd: Path) -> None:
    target_ref = revalidate_target(cell)
    prepared = load_json(cwd / "receipts/prepared-command.json", "prepared command")
    require(prepared.get("backend_plan_sha256") == plan_sha, "prepared plan SHA drift")
    request_ref = verify_ref(prepared.get("request"), "prepared adapter request")
    request_value = load_json(Path(request_ref["path"]), "prepared adapter request")
    require(request_value == _expected_request(cell, cwd / "mutable-store"), "prepared adapter request drift")
    tokens = {
        "{STAGING}": str(cwd),
        "{MUTABLE_CLONE}": str(cwd / "mutable-store"),
        "{REQUEST}": request_ref["path"],
    }
    expected_binary = _replace_tokens(list(cell["runtime"]["binary_argv"]), tokens)
    expected_p31 = _replace_tokens(
        list(cell["runtime"]["p31_argv"]),
        {**tokens, "{BINARY_ARGV_JSON}": json.dumps(expected_binary)},
    )
    require(prepared.get("binary_argv") == expected_binary, "prepared binary argv drift")
    require(prepared.get("p31_argv") == expected_p31, "prepared P31 argv drift")
    clone_receipt = revalidate_prepared_clone(cell, cwd)
    argv = prepared.get("p31_argv")
    require(type(argv) is list and argv and Path(argv[0]).is_absolute(), "prepared P31 argv invalid")
    completed = subprocess.run(argv, cwd=cwd, check=False)
    require(completed.returncode == 0, f"P31 exited {completed.returncode}")
    done_path = Path(cell["runtime"]["p31_done"].replace("{STAGING}", str(cwd)))
    done = load_json(done_path, "P31 DONE")
    require(done.get("state") == "PASS", "P31 DONE is not PASS")
    atomic_json(
        cwd / "receipts/p31.json",
        {
            **_base_receipt(cell, plan_sha, "p31"),
            "timing_generated": True,
            "target_p02b": target_ref,
            "binary_only_boundary": True,
            "prepared_clone_receipt_sha256": sha256_file(cwd / "receipts/store-clone.json"),
            "prepared_clone_dev": clone_receipt["target_dev"],
            "prepared_clone_inode": clone_receipt["target_inode"],
            "p31_done": {
                "path": str(done_path.resolve()),
                "sha256": sha256_file(done_path),
                "size_bytes": done_path.stat().st_size,
            },
        },
    )


def finalize(plan: Mapping[str, Any], cell: Mapping[str, Any], plan_sha: str, cwd: Path) -> None:
    target_ref = revalidate_target(cell)
    p31 = load_json(cwd / "receipts/p31.json", "P31 receipt")
    require(p31.get("state") == "PASS" and p31.get("binary_only_boundary") is True, "P31 receipt invalid")
    runtime = cell["runtime"]
    adapter_ref = verify_ref(runtime["adapter_tool"], "adapter tool")
    adapter = _load_module(Path(adapter_ref["path"]))
    request_path = cwd / "adapter-request.json"
    request = load_json(request_path, "adapter request")
    prepared = load_json(cwd / "receipts/prepared-command.json", "prepared command")
    request_ref = verify_ref(prepared.get("request"), "prepared adapter request")
    require(Path(request_ref["path"]).resolve() == request_path.resolve(), "prepared request path drift")
    require(request == _expected_request(cell, cwd / "mutable-store"), "finalize adapter request drift")
    truth = Path(runtime["truth"]["path"]).resolve()
    truth_rows = adapter.read_truth(truth, request["truth"]["query_count"])
    output = cwd / "adapter-output"
    output.mkdir(exist_ok=True)
    raw_dir = output / "seml0-raw"
    args = argparse.Namespace(output_dir=output)
    adapter.convert_outputs(args, request, truth_rows, raw_dir)
    system = {
        "id": "seml0",
        "group": "embedded",
        "system_version": request["system_version"],
        "display_name": "SemL0",
        "fixture_only": False,
    }
    adapter.validate_adapter_outputs(
        output_dir=output,
        request=request,
        system=system,
        truth_rows=truth_rows,
        max_timeouts=0,
    )
    observations = output / "query-observations.tsv"
    mismatch = timeout = 0
    with observations.open("r", encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            status = row.get("status", "")
            mismatch += int(status not in {"ok", "PASS"})
            timeout += int(status == "timeout")
    require(mismatch == 0 and timeout == 0, "finalized observations contain mismatch/timeout")
    validated = {
        **_base_receipt(cell, plan_sha, "validated-result"),
        "target_p02b": target_ref,
        "adapter_result": {
            "path": str((output / "adapter-result.json").resolve()),
            "sha256": sha256_file(output / "adapter-result.json"),
            "size_bytes": (output / "adapter-result.json").stat().st_size,
        },
    }
    atomic_json(cwd / "validated-result.json", validated)
    atomic_json(
        cwd / "receipts/correctness.json",
        {
            **_base_receipt(cell, plan_sha, "correctness"),
            "mismatch_queries": mismatch,
            "timeout_queries": timeout,
            "query_count": request["truth"]["query_count"],
        },
    )
    atomic_json(
        cwd / "receipts/fairness.json",
        {
            **_base_receipt(cell, plan_sha, "fairness"),
            "p31_receipt_sha256": sha256_file(cwd / "receipts/p31.json"),
            "single_binary_process": True,
            "asset_hash_inside_boundary": False,
            "clone_inside_boundary": False,
        },
    )


def cleanup(plan: Mapping[str, Any], cell: Mapping[str, Any], plan_sha: str, cwd: Path) -> None:
    target_ref = revalidate_target(cell)
    for relative in ("validated-result.json", "receipts/correctness.json", "receipts/fairness.json"):
        require((cwd / relative).is_file(), f"cleanup blocked: {relative} missing")
    clone_receipt = load_json(cwd / "receipts/store-clone.json", "store clone receipt")
    target = Path(clone_receipt["target"])
    require(target.absolute() == (cwd / "mutable-store").absolute(), "cleanup receipt target drift")
    exact_cleanup(target, cwd, clone_receipt["target_dev"], clone_receipt["target_inode"])
    atomic_json(
        cwd / "receipts/cleanup.json",
        {
            **_base_receipt(cell, plan_sha, "cleanup"),
            "target_p02b": target_ref,
            "mutable_clone_removed": True,
            "mutable_clone_lexists_after": os.path.lexists(target),
            "mutable_clone": str(target),
            "released_dev": clone_receipt["target_dev"],
            "released_inode": clone_receipt["target_inode"],
        },
    )


def validate_clone_dry_run_plan(plan: Mapping[str, Any]) -> None:
    if plan.get("state") == "READY":
        require(plan.get("execution_state") == "READY", "READY execution state required")
        return
    require(plan.get("state") == "HOLD", "clone dry-run plan must be HOLD or READY")
    require(plan.get("execution_state") == "BLOCKED", "HOLD execution state required")
    require(
        plan.get("blockers") == CLONE_DRY_RUN_BLOCKERS,
        "HOLD plan has blockers beyond clone dry-run receipt",
    )
    for cell in plan.get("cells", []):
        commands = cell.get("phase_commands")
        require(
            type(commands) is dict
            and all(commands.get(phase) is None for phase in PHASES),
            "HOLD plan phase commands must remain unarmed",
        )


def clone_dry_run(
    plan: Mapping[str, Any],
    cell: Mapping[str, Any],
    plan_ref: Mapping[str, Any],
    output: Path,
) -> None:
    require(cell.get("cell_key") == "seml0:bridge-canary", "clone dry-run is bridge-only")
    output = output.absolute()
    require(not os.path.lexists(output), "dry-run output already exists, including dangling symlink")
    campaign_root = Path(plan["campaign_root"]).absolute()
    require(not os.path.lexists(campaign_root), "campaign root must remain absent during dry-run")
    require(
        output != campaign_root
        and output not in campaign_root.parents
        and campaign_root not in output.parents,
        "dry-run output/campaign root overlap",
    )
    target_ref_pre = revalidate_target(cell)
    policy = cell["runtime"]["clone_policy"]
    require(policy.get("copy_mode") == "explicit-full-copy-ext4-v1", "full-copy mode required")
    predecessor = plan.get("clone_fallback_predecessor")
    verify_ref(predecessor, "failed reflink predecessor")
    target = output / "mutable-store"
    full_copy_argv(policy, Path(policy["source_root"]), target)
    output.mkdir(parents=True)
    atomic_json(
        output / "STATE.json",
        {
            "schema_version": DRYRUN_SCHEMA,
            "state": "RUNNING",
            "cell_key": cell["cell_key"],
            "timing_generated": False,
            **FALSE_ELIGIBILITY,
        },
    )
    try:
        source = Path(policy["source_root"])
        verification = verified_full_copy_and_thaw(
            source,
            target,
            output,
            policy,
        )
        clone = verification["copy"]
        exact_cleanup(target, output, clone["target_dev"], clone["target_inode"])
        target_ref_post = revalidate_target(cell)
        require(target_ref_pre == target_ref_post, "target P02B ref drift across clone dry-run")
        pass_receipt = {
                "schema_version": DRYRUN_SCHEMA,
                "state": "PASS",
                "synthetic_test_only": False,
                "fixture_only": False,
                "backend_plan": dict(plan_ref),
                "backend_plan_sha256": plan_ref["sha256"],
                "cell_key": cell["cell_key"],
                "copy_mode": policy["copy_mode"],
                "filesystem_contract": policy["filesystem_contract"],
                "filesystem_evidence": verification["filesystem_evidence"],
                "capacity_evidence": verification["capacity_evidence"],
                "failed_reflink_predecessor": predecessor,
                "target_p02b": target_ref_pre,
                "source_seal": policy["source_seal"],
                "source_tree_sha256": policy["tree_sha256"],
                "source_tree_pre": verification["source_tree_pre"],
                "source_tree_post": verification["source_tree_post"],
                "source_identity_pre": verification["source_identity_pre"],
                "source_identity_post": verification["source_identity_post"],
                "source_files_pre": verification["source_files_pre"],
                "source_files_post": verification["source_files_post"],
                "clone": clone,
                "clone_files": verification["clone_files"],
                "clone_identity_before_thaw": verification["clone_identity_before_thaw"],
                "clone_identity_after_thaw": verification["clone_identity_after_thaw"],
                "clone_tree": verification["clone_tree"],
                "clone_space": verification["clone_space"],
                "thaw_manifest": verification["thaw_manifest"],
                "verification": verification,
                "mutable_clone_removed": True,
                "clone_root_absent_after_cleanup": not os.path.lexists(target),
                "timing_generated": False,
                **FALSE_ELIGIBILITY,
            }
        atomic_json(output / "CLONE-DRYRUN.json", pass_receipt)
        replace_state_json(
            output / "STATE.json",
            {
                "schema_version": DRYRUN_SCHEMA,
                "state": "PASS",
                "terminal_receipt": {
                    "path": str((output / "CLONE-DRYRUN.json").resolve()),
                    "sha256": sha256_file(output / "CLONE-DRYRUN.json"),
                    "size_bytes": (output / "CLONE-DRYRUN.json").stat().st_size,
                },
                "timing_generated": False,
                **FALSE_ELIGIBILITY,
            },
        )
    except BaseException as exc:
        if not (output / "FAILED.json").exists():
            failed_receipt = {
                    "schema_version": DRYRUN_SCHEMA,
                    "state": "FAILED_RETAINED",
                    "reason": str(exc),
                    "timing_generated": False,
                    **FALSE_ELIGIBILITY,
                }
            atomic_json(output / "FAILED.json", failed_receipt)
        if (output / "STATE.json").is_file():
            replace_state_json(
                output / "STATE.json",
                {
                    "schema_version": DRYRUN_SCHEMA,
                    "state": "FAILED_RETAINED",
                    "terminal_receipt": {
                        "path": str((output / "FAILED.json").resolve()),
                        "sha256": sha256_file(output / "FAILED.json"),
                        "size_bytes": (output / "FAILED.json").stat().st_size,
                    },
                    "timing_generated": False,
                    **FALSE_ELIGIBILITY,
                },
            )
        raise


def verify_executor_binding(plan: Mapping[str, Any]) -> dict[str, Any]:
    ref = verify_ref(plan.get("phase_executor"), "phase executor")
    actual = Path(__file__).resolve()
    require(Path(ref["path"]).resolve() == actual, "runtime executor path differs from plan")
    require(ref["sha256"] == sha256_file(actual), "runtime executor self SHA drift")
    return ref


def write_phase_failure(cwd: Path, phase_name: str, reason: str) -> None:
    if not cwd.is_dir():
        return
    path = cwd / "receipts" / f"{phase_name}-FAILED.json"
    if os.path.lexists(path):
        return
    atomic_json(
        path,
        {
            "schema_version": "cidr-e01-incremental-phase-failure-v1",
            "state": "FAILED_RETAINED",
            "phase": phase_name,
            "reason": reason,
            "timing_generated": phase_name in {"p31", "finalize", "cleanup"},
            **FALSE_ELIGIBILITY,
        },
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-plan", type=Path, required=True)
    parser.add_argument("--cell-key", required=True)
    parser.add_argument("--phase", choices=(*PHASES, "clone-dry-run"), required=True)
    parser.add_argument("--dry-run-output", type=Path)
    args = parser.parse_args(argv)
    failure_cwd: Optional[Path] = None
    try:
        plan_path = args.backend_plan.resolve()
        plan = load_json(plan_path, "backend plan")
        require(plan.get("schema_version") == PLAN_SCHEMA, "backend plan schema drift")
        require(plan.get("strict_serial") is True, "STRICT_SERIAL required")
        require(plan.get("synthetic_test_only") is False, "synthetic backend forbidden")
        require(
            [row.get("cell_key") for row in plan.get("cells", [])] == list(CELL_VARIANTS),
            "exact four-cell order required",
        )
        for row in plan["cells"]:
            require(row.get("runtime", {}).get("variant") == CELL_VARIANTS[row["cell_key"]], "cell/variant mapping drift")
        verify_executor_binding(plan)
        plan_sha = sha256_file(plan_path)
        plan_ref = {
            "path": str(plan_path),
            "sha256": plan_sha,
            "size_bytes": plan_path.stat().st_size,
        }
        cell = _cell(plan, args.cell_key)
        if args.phase == "clone-dry-run":
            require(args.dry_run_output is not None and args.dry_run_output.is_absolute(), "absolute dry-run output required")
            validate_clone_dry_run_plan(plan)
            clone_dry_run(plan, cell, plan_ref, args.dry_run_output)
        else:
            require(plan.get("state") == "READY" and plan.get("execution_state") == "READY", "backend plan is not READY")
            cwd = Path(cell["staging_cell_root"]).resolve()
            failure_cwd = cwd
            require(cwd == Path.cwd().resolve(), "phase cwd/staging drift")
            {"prepare": prepare, "p31": run_p31, "finalize": finalize, "cleanup": cleanup}[args.phase](
                plan, cell, plan_sha, cwd
            )
        return 0
    except (PhaseError, OSError, ValueError, subprocess.SubprocessError) as exc:
        if failure_cwd is not None:
            try:
                write_phase_failure(failure_cwd, args.phase, str(exc))
            except (PhaseError, OSError):
                pass
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
