#!/usr/bin/env python3
"""Create one offline, non-overlapping NebulaGraph store clone and receipt.

This utility never starts or stops containers.  It fails closed when any
running container mount overlaps the source, staging, or target paths.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import os
import secrets
import subprocess
import sys
from pathlib import Path

from store_contract import (
    CLONE_METHOD_COPY,
    CLONE_METHOD_REFLINK,
    CLONE_RECEIPT_SCHEMA,
    SYMLINK_POLICY,
    ContractError,
    assert_nonoverlapping,
    assert_offline,
    canonical_json_sha,
    current_git_state,
    file_ref,
    is_within,
    paths_overlap,
    require,
    stable_sha256_file,
    tree_metadata,
    tree_snapshot,
    write_json_exclusive,
)


AT_FDCWD = -100
RENAME_NOREPLACE = 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repeat-index", required=True, type=int)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--docker", type=Path, default=Path("/usr/bin/docker"))
    parser.add_argument("--copy-tool", type=Path, default=Path("/usr/bin/cp"))
    parser.add_argument("--copy-mode", choices=("reflink", "copy"), default="reflink")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _same_identity(left: dict[str, object], right: dict[str, object], context: str) -> None:
    for key in (
        "hash_method",
        "sha256",
        "file_count",
        "total_bytes",
        "directory_count",
        "symlink_count",
    ):
        require(left[key] == right[key], f"{context}: {key} mismatch")


def rename_noreplace(source: Path, target: Path) -> None:
    """Atomically publish a directory without replacing an existing name."""

    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    require(function is not None, "libc renameat2 is required for no-replace publication")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(target),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise ContractError(
            f"atomic no-replace publication failed for {target}: {os.strerror(error)}"
        )


def _metadata_equal_except_root(
    before: dict[str, tuple[str, tuple[int, ...], str | None]],
    after: dict[str, tuple[str, tuple[int, ...], str | None]],
    context: str,
) -> None:
    require(set(before) == set(after), f"{context}: entry set changed")
    for relative in before:
        if relative == "":
            continue
        require(before[relative] == after[relative], f"{context}: metadata changed at {relative}")


def _reject_shared_inodes(
    source: Path,
    target: Path,
    source_records: dict[str, dict[str, object]],
    target_records: dict[str, dict[str, object]],
) -> None:
    require(set(source_records) == set(target_records), "clone source/target entry set mismatch")
    for relative, source_record in source_records.items():
        target_record = target_records[relative]
        require(source_record["type"] == target_record["type"],
                f"clone source/target type mismatch at {relative}")
        if source_record["type"] != "file":
            continue
        source_stat = (source / relative).lstat()
        target_stat = (target / relative).lstat()
        require(
            (source_stat.st_dev, source_stat.st_ino) != (target_stat.st_dev, target_stat.st_ino),
            f"clone illegally shares a regular-file inode at {relative}",
        )


def run(args: argparse.Namespace) -> None:
    require(args.source.is_absolute(), "--source must be absolute")
    require(args.target.is_absolute(), "--target must be absolute")
    require(args.output.is_absolute(), "--output must be absolute")
    require(args.repeat_index >= 1, "--repeat-index must be >= 1")
    require(bool(args.run_id), "--run-id must be non-empty")

    require(args.source.is_dir() and not args.source.is_symlink(),
            "clone source must be a real directory")
    source = args.source.resolve()
    target = args.target.resolve(strict=False)
    output = args.output.resolve(strict=False)
    require(target.parent.is_dir() and not target.parent.is_symlink(), "clone target parent must be a real directory")
    require(not target.exists() and not target.is_symlink(), f"clone target already exists: {target}")
    require(output.parent.is_dir() and not output.parent.is_symlink(), "clone receipt parent must be a real directory")
    require(not output.exists() and not output.is_symlink(), f"clone receipt already exists: {output}")
    assert_nonoverlapping({"source": source, "target": target, "receipt": output})

    repo = current_git_state(args.repo_root)
    require(repo["clean"] is True, "clone requires a clean Git worktree")
    require(not is_within(output, Path(repo["root"])),
            "clone receipt output must be outside the Git worktree")
    require(args.docker.is_file() and not args.docker.is_symlink(),
            "pinned Docker client is missing or symlinked")
    require(args.copy_tool.is_file() and not args.copy_tool.is_symlink(),
            "pinned copy tool is missing or symlinked")
    docker = args.docker.resolve()
    copy_tool = args.copy_tool.resolve()
    require(os.access(docker, os.X_OK),
            "pinned Docker client is missing, symlinked, or non-executable")
    require(os.access(copy_tool, os.X_OK),
            "pinned copy tool is missing, symlinked, or non-executable")
    copy_tool_sha, _ = stable_sha256_file(copy_tool, "clone copy tool")

    staging = target.parent / f".{target.name}.clone-{secrets.token_hex(16)}"
    require(not staging.exists() and not staging.is_symlink(), "random clone staging path already exists")
    assert_nonoverlapping({"source": source, "staging": staging, "receipt": output})

    assert_offline((source, target, staging), docker=docker)
    source_identity, source_records = tree_snapshot(source)
    source_metadata = tree_metadata(source)
    assert_offline((source, target, staging), docker=docker)
    require(not target.exists(), "clone target appeared before copy")

    reflink_option = "--reflink=always" if args.copy_mode == "reflink" else "--reflink=never"
    method = CLONE_METHOD_REFLINK if args.copy_mode == "reflink" else CLONE_METHOD_COPY
    command = [
        str(copy_tool),
        "-a",
        reflink_option,
        "--no-target-directory",
        "--",
        str(source),
        str(staging),
    ]
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=None,
    )
    require(
        completed.returncode == 0,
        f"clone copy failed; partial evidence retained at {staging}: {completed.stderr.strip()}",
    )
    require(staging.is_dir() and not staging.is_symlink(), "copy command did not create a real staging tree")
    assert_offline((source, target, staging), docker=docker)
    target_identity, target_records = tree_snapshot(staging)
    _same_identity(source_identity, target_identity, "clone source/staging")
    require(tree_metadata(source) == source_metadata, "clone source metadata changed during copy")
    _reject_shared_inodes(source, staging, source_records, target_records)
    staging_metadata = tree_metadata(staging)

    require(not target.exists() and not target.is_symlink(), "clone target appeared before publication")
    rename_noreplace(staging, target)
    require(target.is_dir() and not target.is_symlink(), "published clone target is invalid")
    _metadata_equal_except_root(staging_metadata, tree_metadata(target), "published clone")
    _reject_shared_inodes(source, target, source_records, target_records)
    require(tree_metadata(source) == source_metadata, "clone source metadata changed before receipt")
    assert_offline((source, target), docker=docker)
    require(not paths_overlap(source, target), "clone source and target overlap after publication")

    created_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    source_ref = {"path": str(source), **source_identity}
    target_ref = {"path": str(target), **target_identity}
    receipt = {
        "schema_version": CLONE_RECEIPT_SCHEMA,
        "state": "PASS",
        "created_at_utc": created_at,
        "run_id": args.run_id,
        "repeat_index": args.repeat_index,
        "repo": repo,
        "method": method,
        "copy_tool": {
            "path": str(copy_tool),
            "sha256": copy_tool_sha,
            "argv": command,
            "argv_sha256": canonical_json_sha(command),
            "exit_code": completed.returncode,
        },
        "source_pre": source_ref,
        "source_post": source_ref,
        "target": target_ref,
        "safety": {
            "target_absent_before": True,
            "source_target_nonoverlap": True,
            "offline_check_count": 4,
            "no_shared_regular_inodes": True,
            "symlink_policy": SYMLINK_POLICY,
            "publish_noreplace": True,
        },
    }
    # Verify publication and copy-tool identity once more before reporting PASS.
    validate_copy_tool = file_ref(copy_tool, "clone copy tool post-check")
    require(validate_copy_tool["sha256"] == copy_tool_sha, "copy tool changed during clone")
    write_json_exclusive(output, receipt)
    print(output)


def main() -> int:
    try:
        run(parse_args())
        return 0
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        print(f"clone_store: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
