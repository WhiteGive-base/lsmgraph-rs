#!/usr/bin/env python3
"""Fail-closed offline-store contracts for the NebulaGraph P10 adapter.

The helpers in this module do not start, stop, or mutate containers.  The
freezer and clone utilities use them to prove that a tree is offline and
stable while it is hashed, and the adapter uses the same parsers to reject
untyped historical provenance.
"""

from __future__ import annotations

import datetime as dt
import csv
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

P10_DIR = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(P10_DIR))

from p10_contract import ContractError, read_truth  # noqa: E402


STORE_SCHEMA = "cidr-p10-nebulagraph-store-v2"
IMPORT_RECEIPT_SCHEMA = "cidr-p10-nebulagraph-import-receipt-v2"
IMPORT_EXECUTION_SCHEMA = "cidr-p10-nebulagraph-import-execution-v2"
IMPORT_CORRECTNESS_SCHEMA = "cidr-p10-nebulagraph-import-correctness-v2"
IMPORTER_REPORT_SCHEMA = "cidr-p10-nebulagraph-importer-report-v1"
IMPORT_OBSERVATION_COLUMNS = (
    "query_index", "edge_type", "src", "expected_count", "actual_count",
    "expected_sum_hash", "actual_sum_hash", "expected_xor_hash", "actual_xor_hash", "status",
)
CLONE_RECEIPT_SCHEMA = "cidr-p10-nebulagraph-clone-receipt-v1"
STORE_TREE_HASH_METHOD = "sha256-tree-v2(type,path,size-or-target,file-sha256)"
SYMLINK_POLICY = "relative-in-tree-symlinks-only-v1"
CLONE_METHOD_REFLINK = "gnu-cp-a-reflink-always-atomic-noreplace-v1"
CLONE_METHOD_COPY = "gnu-cp-a-copy-atomic-noreplace-v1"
CLONE_METHODS = {CLONE_METHOD_REFLINK, CLONE_METHOD_COPY}
SYSTEM_VERSION = "NebulaGraph 3.8.0"
FORMAL_DATASET_SHA256 = "9727fed3d710a3f0897b1ee7379411a091e0568bef05902de8b1787efc39d258"
FORMAL_TRUTH_SHA256 = "876ec4be45bb7c220ce595b8d8efd5c79d13db285569cc0d6ae3e8bc19a1c788"
FORMAL_QUERY_COUNT = 1700
FORMAL_VERTEX_COUNT = 29_987_835
FORMAL_EDGE_COUNT = 355_185_382
LOGICAL_HOSTS = {
    "metad": "seml0-nebula-meta-sf10",
    "storaged": "seml0-nebula-storage-sf10",
    "graphd": "seml0-nebula-graph-sf10",
}
EXPECTED_IMAGES = {
    "graphd": {
        "tag": "vesoft/nebula-graphd:v3.8.0",
        "digest": "sha256:1040573cc684ea6cc5e673b667422e9abab607c9d553c2c17c7bac6ad8a74e05",
    },
    "metad": {
        "tag": "vesoft/nebula-metad:v3.8.0",
        "digest": "sha256:ab687bd32d3e441d436842b41427ba0961a5e169c46997ef03fa4dcfd5388b35",
    },
    "storaged": {
        "tag": "vesoft/nebula-storaged:v3.8.0",
        "digest": "sha256:7142642ee69001a5b5c50520196538d58bb2ec3930707c067cb4c4e2be3890f9",
    },
}

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def read_strict_json(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except ContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{context}: cannot read JSON: {exc}") from exc
    require(isinstance(value, dict), f"{context}: top level must be an object")
    return value


def exact_keys(value: object, keys: Iterable[str], context: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{context}: expected object")
    expected = set(keys)
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    require(not missing and not unknown, f"{context}: key drift missing={missing}, unknown={unknown}")
    return value


def exact_sha(value: object, context: str) -> str:
    require(isinstance(value, str) and HEX64_RE.fullmatch(value) is not None,
            f"{context}: expected lowercase SHA-256")
    return value


def integer(value: object, context: str, minimum: int = 0) -> int:
    require(not isinstance(value, bool) and isinstance(value, int) and value >= minimum,
            f"{context}: expected integer >= {minimum}")
    return value


def nonempty(value: object, context: str) -> str:
    require(isinstance(value, str) and bool(value), f"{context}: expected non-empty string")
    return value


def parse_timestamp(value: object, context: str) -> dt.datetime:
    text = nonempty(value, context)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{context}: invalid ISO-8601 timestamp") from exc
    require(parsed.tzinfo is not None, f"{context}: timezone is required")
    return parsed


def canonical_json_sha(value: object) -> str:
    # Object member order is not semantic JSON state.  Sorting is also required
    # because exclusive receipt publication pretty-prints with ``sort_keys=True``.
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def stable_sha256_file(path: Path, context: str) -> tuple[str, os.stat_result]:
    """Hash a regular file through a no-follow descriptor and reject mutation."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise ContractError(f"{context}: cannot lstat {path}: {exc}") from exc
    require(stat.S_ISREG(before.st_mode), f"{context}: expected a non-symlink regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    digest = hashlib.sha256()
    try:
        descriptor = os.open(str(path), flags)
        try:
            opened = os.fstat(descriptor)
            require(_stat_signature(opened) == _stat_signature(before),
                    f"{context}: file changed before hashing")
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
            after_fd = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = path.lstat()
    except OSError as exc:
        raise ContractError(f"{context}: cannot hash {path}: {exc}") from exc
    require(
        _stat_signature(before) == _stat_signature(after_fd) == _stat_signature(after_path),
        f"{context}: file mutated while hashing",
    )
    return digest.hexdigest(), after_path


def validate_file_ref(
    value: object,
    context: str,
    *,
    expected_path: Path | None = None,
    expected_sha256: str | None = None,
    recompute: bool = True,
) -> Path:
    item = exact_keys(value, ("path", "sha256"), context)
    raw_path = Path(nonempty(item["path"], f"{context}.path"))
    require(raw_path.is_file() and not raw_path.is_symlink(),
            f"{context}: referenced file is missing or a symlink")
    path = raw_path.resolve()
    claimed = exact_sha(item["sha256"], f"{context}.sha256")
    if expected_path is not None:
        require(path == expected_path.resolve(), f"{context}: path mismatch")
    if expected_sha256 is not None:
        require(claimed == exact_sha(expected_sha256, f"{context}.expected_sha256"),
                f"{context}: SHA-256 lineage mismatch")
    if recompute:
        actual, _ = stable_sha256_file(path, context)
        require(actual == claimed, f"{context}: SHA-256 mismatch")
    return path


def file_ref(path: Path, context: str = "file") -> dict[str, str]:
    require(path.is_file() and not path.is_symlink(), f"{context}: expected a non-symlink regular file")
    resolved = path.resolve()
    digest, _ = stable_sha256_file(resolved, context)
    return {"path": str(resolved), "sha256": digest}


def is_within(path: Path, root: Path) -> bool:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def paths_overlap(left: Path, right: Path) -> bool:
    left_resolved = left.resolve(strict=False)
    right_resolved = right.resolve(strict=False)
    return (
        left_resolved == right_resolved
        or left_resolved in right_resolved.parents
        or right_resolved in left_resolved.parents
    )


def assert_nonoverlapping(paths: dict[str, Path]) -> None:
    items = list(paths.items())
    for index, (left_name, left) in enumerate(items):
        for right_name, right in items[index + 1:]:
            require(
                not paths_overlap(left, right),
                f"paths overlap: {left_name}={left.resolve(strict=False)} and "
                f"{right_name}={right.resolve(strict=False)}",
            )


def _safe_symlink_record(path: Path, root: Path, context: str) -> tuple[str, os.stat_result]:
    before = path.lstat()
    require(stat.S_ISLNK(before.st_mode), f"{context}: expected symlink")
    try:
        target = os.readlink(path)
        after = path.lstat()
    except OSError as exc:
        raise ContractError(f"{context}: cannot inspect symlink: {exc}") from exc
    require(_stat_signature(before) == _stat_signature(after), f"{context}: symlink mutated")
    target_path = Path(target)
    require(bool(target) and not target_path.is_absolute(), f"{context}: symlink target must be relative")
    try:
        resolved_target = (path.parent / target_path).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ContractError(f"{context}: cannot resolve symlink target") from exc
    require(is_within(resolved_target, root), f"{context}: symlink escapes the store root")
    require(resolved_target.exists(), f"{context}: dangling symlink is not allowed")
    return target, after


def tree_metadata(root: Path) -> dict[str, tuple[str, tuple[int, ...], str | None]]:
    """Collect a no-follow metadata-only view used for post-hash checks."""

    root = root.resolve()
    result: dict[str, tuple[str, tuple[int, ...], str | None]] = {}
    for raw_directory, raw_dirnames, raw_filenames in os.walk(root, topdown=True, followlinks=False):
        directory = Path(raw_directory)
        directory_stat = directory.lstat()
        require(stat.S_ISDIR(directory_stat.st_mode), f"store tree found non-directory: {directory}")
        relative_directory = "" if directory == root else directory.relative_to(root).as_posix()
        result[relative_directory] = ("directory", _stat_signature(directory_stat), None)
        raw_dirnames.sort(key=os.fsencode)
        raw_filenames.sort(key=os.fsencode)
        descend: list[str] = []
        for name in raw_dirnames:
            path = directory / name
            item = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(item.st_mode):
                target, final_stat = _safe_symlink_record(path, root, f"store tree {relative}")
                result[relative] = ("symlink", _stat_signature(final_stat), target)
            elif stat.S_ISDIR(item.st_mode):
                descend.append(name)
            else:
                raise ContractError(f"store tree found unsafe directory entry: {path}")
        raw_dirnames[:] = descend
        for name in raw_filenames:
            path = directory / name
            item = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(item.st_mode):
                target, final_stat = _safe_symlink_record(path, root, f"store tree {relative}")
                result[relative] = ("symlink", _stat_signature(final_stat), target)
            elif stat.S_ISREG(item.st_mode):
                result[relative] = ("file", _stat_signature(item), None)
            else:
                raise ContractError(f"store tree found non-regular file: {path}")
    return result


def tree_snapshot(root: Path, *, allow_empty: bool = False) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return a stable v2 identity and private records for a safe tree."""

    require(root.exists() and root.is_dir() and not root.is_symlink(),
            f"store root is missing, non-directory, or symlink: {root}")
    root = root.resolve()
    directory_signatures: dict[Path, tuple[int, ...]] = {}
    entry_signatures: dict[Path, tuple[int, ...]] = {}
    records: dict[str, dict[str, Any]] = {}

    for raw_directory, raw_dirnames, raw_filenames in os.walk(root, topdown=True, followlinks=False):
        directory = Path(raw_directory)
        directory_stat = directory.lstat()
        require(stat.S_ISDIR(directory_stat.st_mode), f"store tree found non-directory: {directory}")
        directory_signatures[directory] = _stat_signature(directory_stat)
        if directory != root:
            relative_directory = directory.relative_to(root).as_posix()
            records[relative_directory] = {"type": "directory"}

        raw_dirnames.sort(key=os.fsencode)
        raw_filenames.sort(key=os.fsencode)
        descend: list[str] = []
        for name in raw_dirnames:
            path = directory / name
            item = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(item.st_mode):
                target, final_stat = _safe_symlink_record(path, root, f"store tree {relative}")
                records[relative] = {"type": "symlink", "target": target}
                entry_signatures[path] = _stat_signature(final_stat)
            elif stat.S_ISDIR(item.st_mode):
                descend.append(name)
            else:
                raise ContractError(f"store tree found unsafe directory entry: {path}")
        raw_dirnames[:] = descend

        for name in raw_filenames:
            path = directory / name
            item = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(item.st_mode):
                target, final_stat = _safe_symlink_record(path, root, f"store tree {relative}")
                records[relative] = {"type": "symlink", "target": target}
                entry_signatures[path] = _stat_signature(final_stat)
            elif stat.S_ISREG(item.st_mode):
                digest, final_stat = stable_sha256_file(path, f"store tree {relative}")
                records[relative] = {
                    "type": "file",
                    "size_bytes": final_stat.st_size,
                    "sha256": digest,
                }
                entry_signatures[path] = _stat_signature(final_stat)
            else:
                raise ContractError(f"store tree found non-regular file: {path}")

    require(allow_empty or bool(records), "store tree is empty")
    for path, signature in entry_signatures.items():
        try:
            current = _stat_signature(path.lstat())
        except OSError as exc:
            raise ContractError(f"store tree entry disappeared during hashing: {path}") from exc
        require(current == signature, f"store tree entry mutated during hashing: {path}")
    for path, signature in directory_signatures.items():
        try:
            current = _stat_signature(path.lstat())
        except OSError as exc:
            raise ContractError(f"store tree directory disappeared during hashing: {path}") from exc
        require(current == signature, f"store tree directory mutated during hashing: {path}")

    expected_metadata: dict[str, tuple[str, tuple[int, ...], str | None]] = {
        "": ("directory", directory_signatures[root], None)
    }
    for path, signature in directory_signatures.items():
        if path != root:
            expected_metadata[path.relative_to(root).as_posix()] = ("directory", signature, None)
    for relative, record in records.items():
        if record["type"] == "symlink":
            expected_metadata[relative] = (
                "symlink",
                entry_signatures[root / relative],
                record["target"],
            )
        elif record["type"] == "file":
            expected_metadata[relative] = ("file", entry_signatures[root / relative], None)
    require(tree_metadata(root) == expected_metadata,
            "store tree entry set or metadata changed after content hashing")

    digest = hashlib.sha256()
    total_bytes = 0
    file_count = 0
    symlink_count = 0
    directory_count = 0
    for relative in sorted(records, key=os.fsencode):
        record = records[relative]
        if record["type"] == "file":
            file_count += 1
            total_bytes += record["size_bytes"]
            digest.update(
                f"file\0{relative}\0{record['size_bytes']}\0{record['sha256']}\n".encode("utf-8")
            )
        elif record["type"] == "symlink":
            symlink_count += 1
            digest.update(f"symlink\0{relative}\0{record['target']}\n".encode("utf-8"))
        else:
            directory_count += 1
            digest.update(f"directory\0{relative}\n".encode("utf-8"))
    identity = {
        "hash_method": STORE_TREE_HASH_METHOD,
        "sha256": digest.hexdigest(),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "directory_count": directory_count,
        "symlink_count": symlink_count,
    }
    return identity, records


def tree_identity(root: Path, *, allow_empty: bool = False) -> dict[str, Any]:
    return tree_snapshot(root, allow_empty=allow_empty)[0]


def validate_tree_ref(
    value: object,
    context: str,
    *,
    expected_path: Path | None = None,
    recompute: bool = False,
    allow_empty: bool = False,
) -> tuple[Path, dict[str, Any]]:
    item = exact_keys(
        value,
        ("path", "hash_method", "sha256", "file_count", "total_bytes", "directory_count", "symlink_count"),
        context,
    )
    raw_path = Path(nonempty(item["path"], f"{context}.path"))
    require(raw_path.is_dir() and not raw_path.is_symlink(),
            f"{context}: tree root is missing or a symlink")
    path = raw_path.resolve()
    if expected_path is not None:
        require(path == expected_path.resolve(), f"{context}: path mismatch")
    require(item["hash_method"] == STORE_TREE_HASH_METHOD, f"{context}: hash method drift")
    exact_sha(item["sha256"], f"{context}.sha256")
    for key in ("file_count", "total_bytes", "directory_count", "symlink_count"):
        integer(item[key], f"{context}.{key}")
    if recompute:
        actual = tree_identity(path, allow_empty=allow_empty)
        for key, actual_value in actual.items():
            require(item[key] == actual_value, f"{context}.{key}: tree identity mismatch")
    return path, item


def assert_offline(
    protected_roots: Iterable[Path],
    *,
    docker: Path | str = "docker",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Reject every running container with a bind mount overlapping a root."""

    roots = [path.resolve(strict=False) for path in protected_roots]
    listed = runner(
        [str(docker), "container", "ls", "--quiet", "--no-trunc"],
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
    inspected = runner(
        [str(docker), "container", "inspect", *identifiers],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    require(inspected.returncode == 0, f"cannot inspect running containers: {inspected.stderr.strip()}")
    try:
        values = json.loads(inspected.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("running-container inspect returned malformed JSON") from exc
    require(isinstance(values, list), "running-container inspect returned a non-array")
    for container in values:
        require(isinstance(container, dict), "running-container inspect entry is malformed")
        name = str(container.get("Name", "")).lstrip("/") or str(container.get("Id", ""))
        mounts = container.get("Mounts", [])
        require(isinstance(mounts, list), f"running container {name!r} has malformed mounts")
        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            source = mount.get("Source")
            if not isinstance(source, str) or not Path(source).is_absolute():
                continue
            for root in roots:
                if paths_overlap(root, Path(source)):
                    raise ContractError(
                        f"running container {name!r} bind mount {source!r} overlaps protected root {root}"
                    )


def current_git_state(repo_root: Path) -> dict[str, Any]:
    repo = repo_root.resolve()
    require(repo.is_dir(), f"Git root is missing: {repo}")
    try:
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=normal"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(f"cannot audit Git repository: {exc}") from exc
    require(GIT_HEAD_RE.fullmatch(head) is not None, "Git HEAD must be a full SHA-1 commit")
    return {
        "root": str(repo),
        "head": head,
        "clean": status == "",
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def validate_repo(value: object, expected: dict[str, Any], context: str) -> dict[str, Any]:
    item = exact_keys(value, ("root", "head", "clean", "status_sha256"), context)
    require(Path(nonempty(item["root"], f"{context}.root")).resolve() == Path(expected["root"]).resolve(),
            f"{context}: root mismatch")
    require(item["head"] == expected["head"] and GIT_HEAD_RE.fullmatch(item["head"]) is not None,
            f"{context}: HEAD mismatch")
    require(item["clean"] is True and expected.get("clean") is True, f"{context}: clean Git state required")
    require(item["status_sha256"] == expected["status_sha256"] == hashlib.sha256(b"").hexdigest(),
            f"{context}: status digest mismatch")
    return item


def validate_images(value: object, context: str) -> list[dict[str, str]]:
    require(isinstance(value, list) and len(value) == 3, f"{context}: expected three ordered images")
    result: list[dict[str, str]] = []
    for index, role in enumerate(("graphd", "metad", "storaged")):
        item = exact_keys(value[index], ("role", "tag", "repo_digest", "image_id"), f"{context}[{index}]")
        expected = EXPECTED_IMAGES[role]
        require(item["role"] == role, f"{context}[{index}]: role/order drift")
        require(item["tag"] == expected["tag"], f"{context}[{index}]: tag drift")
        repository = expected["tag"].split(":", 1)[0]
        require(item["repo_digest"] == f"{repository}@{expected['digest']}",
                f"{context}[{index}]: RepoDigest drift")
        require(isinstance(item["image_id"], str) and IMAGE_ID_RE.fullmatch(item["image_id"]) is not None,
                f"{context}[{index}]: image ID is not pinned")
        result.append(dict(item))
    return result


def validate_identity_names(
    containers: object,
    logical_hosts: object,
    network: object,
    *,
    formal: bool,
) -> tuple[dict[str, str], dict[str, str], str]:
    container_map = exact_keys(containers, ("metad", "storaged", "graphd"), "store.containers")
    host_map = exact_keys(logical_hosts, ("metad", "storaged", "graphd"), "store.logical_hosts")
    network_name = nonempty(network, "store.network")
    container_values = list(container_map.values())
    host_values = list(host_map.values())
    require(all(isinstance(name, str) and NAME_RE.fullmatch(name) for name in container_values),
            "store container names are unsafe")
    require(all(isinstance(name, str) and NAME_RE.fullmatch(name) for name in host_values),
            "store logical hosts are unsafe")
    require(NAME_RE.fullmatch(network_name) is not None, "store network name is unsafe")
    require(len(set(container_values)) == 3 and len(set(host_values)) == 3,
            "store container/logical host identities must be unique")
    require(set(container_values).isdisjoint(host_values),
            "Docker container names must be distinct from frozen logical hosts")
    require(network_name not in set(container_values) | set(host_values),
            "store network must be distinct from container and logical host names")
    if formal:
        require(host_map == LOGICAL_HOSTS, "formal store logical hosts differ from historical RAFT identities")
    return dict(container_map), dict(host_map), network_name


def _same_tree_identity(left: dict[str, Any], right: dict[str, Any], context: str) -> None:
    for key in ("hash_method", "sha256", "file_count", "total_bytes", "directory_count", "symlink_count"):
        require(left[key] == right[key], f"{context}: {key} mismatch")


def validate_import_receipt(
    receipt_ref: object,
    *,
    repo: dict[str, Any],
    raw_dataset_manifest_path: Path,
    raw_dataset_manifest_sha256: str,
    dataset_path: Path,
    dataset_sha256: str,
    truth_path: Path,
    truth_sha256: str,
    runtime_manifest_path: Path,
    runtime_manifest_sha256: str,
    runtime_images: object,
    recompute_artifacts: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """Validate the only receipt class allowed to make a store formal."""

    receipt_path = validate_file_ref(receipt_ref, "store.import_receipt", recompute=True)
    value = exact_keys(
        read_strict_json(receipt_path, "NebulaGraph import receipt"),
        (
            "schema_version", "state", "formal_eligible", "historical_tag_only", "system_version",
            "completed_at_utc", "repo", "wrapper", "importer", "inputs", "images", "execution",
            "loaded", "correctness", "store",
        ),
        "NebulaGraph import receipt",
    )
    require(value["schema_version"] == IMPORT_RECEIPT_SCHEMA,
            "formal store requires the strict NebulaGraph import receipt schema")
    require(value["state"] == "PASS", "NebulaGraph import receipt is not PASS")
    require(value["formal_eligible"] is True, "NebulaGraph import receipt is not formal eligible")
    require(value["historical_tag_only"] is False,
            "historical tag-only import evidence can never become formal")
    require(value["system_version"] == SYSTEM_VERSION, "NebulaGraph import system version drift")
    receipt_completed = parse_timestamp(value["completed_at_utc"], "import.completed_at_utc")
    validate_repo(value["repo"], repo, "import.repo")

    wrapper = exact_keys(
        value["wrapper"], ("path", "sha256", "policy"), "import.wrapper"
    )
    wrapper_path = validate_file_ref(
        {"path": wrapper["path"], "sha256": wrapper["sha256"]},
        "import.wrapper.file",
        recompute=True,
    )
    require(
        wrapper_path == Path(__file__).resolve().with_name("build_import_receipt.py"),
        "import receipt was not produced by the repository strict wrapper",
    )
    require(wrapper["policy"] == "execute-and-derive-evidence-v1", "strict import wrapper policy drift")

    importer = exact_keys(
        value["importer"],
        ("path", "sha256", "cwd", "argv", "argv_sha256", "argv_source"),
        "import.importer",
    )
    importer_path = validate_file_ref(
        {"path": importer["path"], "sha256": importer["sha256"]},
        "import.importer.file",
        recompute=True,
    )
    require(is_within(importer_path, Path(repo["root"])), "importer must be inside the pinned repository")
    require(Path(nonempty(importer["cwd"], "import.importer.cwd")).resolve() == Path(repo["root"]).resolve(),
            "importer cwd differs from pinned repository")
    argv = importer["argv"]
    require(isinstance(argv, list) and bool(argv) and all(isinstance(item, str) for item in argv),
            "importer argv must be a non-empty string array")
    require(Path(argv[0]).resolve() == importer_path, "importer argv[0] differs from pinned importer")
    require(exact_sha(importer["argv_sha256"], "import.importer.argv_sha256") == canonical_json_sha(argv),
            "importer argv digest mismatch")
    argv_source_path = validate_file_ref(
        importer["argv_source"], "import.importer.argv_source", recompute=recompute_artifacts
    )
    try:
        source_argv = json.loads(argv_source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot re-read importer argv source: {exc}") from exc
    require(source_argv == argv, "importer argv differs from its immutable source JSON")

    inputs = exact_keys(
        value["inputs"],
        ("raw_dataset_manifest", "dense_dataset", "truth", "runtime_manifest"),
        "import.inputs",
    )
    raw_manifest = validate_file_ref(
        inputs["raw_dataset_manifest"],
        "import.inputs.raw_dataset_manifest",
        expected_path=raw_dataset_manifest_path,
        expected_sha256=raw_dataset_manifest_sha256,
        recompute=recompute_artifacts,
    )
    raw_value = read_strict_json(raw_manifest, "raw dataset manifest")
    require(raw_value.get("schema_version") == "p02b-dataset-manifest-v1",
            "raw dataset manifest schema drift")
    validate_file_ref(
        inputs["dense_dataset"],
        "import.inputs.dense_dataset",
        expected_path=dataset_path,
        expected_sha256=dataset_sha256,
        recompute=False,
    )
    validate_file_ref(
        inputs["truth"],
        "import.inputs.truth",
        expected_path=truth_path,
        expected_sha256=truth_sha256,
        recompute=False,
    )
    require(dataset_sha256 == FORMAL_DATASET_SHA256, "formal import dense dataset is not canonical SF10")
    require(truth_sha256 == FORMAL_TRUTH_SHA256, "formal import truth is not canonical SF10")
    runtime_manifest_path = validate_file_ref(
        inputs["runtime_manifest"],
        "import.inputs.runtime_manifest",
        expected_path=runtime_manifest_path,
        expected_sha256=runtime_manifest_sha256,
        recompute=recompute_artifacts,
    )

    receipt_images = validate_images(value["images"], "import.images")
    expected_images = validate_images(runtime_images, "runtime.images")
    require(receipt_images == expected_images, "import/runtime image identity mismatch")

    execution = exact_keys(
        value["execution"],
        (
            "started_at_utc", "completed_at_utc", "exit_code", "timed_out", "stdout", "stderr",
            "report", "result",
        ),
        "import.execution",
    )
    started = parse_timestamp(execution["started_at_utc"], "import.execution.started_at_utc")
    completed = parse_timestamp(execution["completed_at_utc"], "import.execution.completed_at_utc")
    require(completed >= started, "import execution timestamps are reversed")
    require(execution["exit_code"] == 0, "formal import command did not exit zero")
    require(execution["timed_out"] is False, "formal import command timed out")
    for key in ("stdout", "stderr", "report"):
        validate_file_ref(execution[key], f"import.execution.{key}", recompute=recompute_artifacts)
    execution_result_path = validate_file_ref(
        execution["result"], "import.execution.result", recompute=recompute_artifacts
    )
    execution_result = exact_keys(
        read_strict_json(execution_result_path, "import execution result"),
        (
            "schema_version", "state", "producer", "started_at_utc", "completed_at_utc", "exit_code",
            "timed_out", "cwd", "importer", "argv", "argv_sha256", "argv_source", "target_store",
            "inputs", "stdout", "stderr", "report", "loaded", "images",
        ),
        "import execution result",
    )
    require(
        execution_result["schema_version"] == IMPORT_EXECUTION_SCHEMA
        and execution_result["state"] == "PASS",
        "import execution result schema/state drift",
    )
    require(execution_result["started_at_utc"] == execution["started_at_utc"],
            "import execution start timestamp drift")
    require(execution_result["completed_at_utc"] == execution["completed_at_utc"],
            "import execution completion timestamp drift")
    require(receipt_completed == completed, "import receipt/execution completion timestamp drift")
    require(execution_result["exit_code"] == execution["exit_code"] == 0,
            "import execution result exit code drift")
    require(execution_result["timed_out"] is False, "import execution result claims timeout")
    require(execution_result["producer"] == {"path": str(wrapper_path), "sha256": wrapper["sha256"]},
            "import execution result producer drift")
    require(execution_result["cwd"] == repo["root"], "import execution cwd drift")
    require(execution_result["importer"] == {"path": str(importer_path), "sha256": importer["sha256"]},
            "import execution importer drift")
    require(execution_result["argv"] == argv and execution_result["argv_sha256"] == importer["argv_sha256"],
            "import execution argv drift")
    require(execution_result["argv_source"] == importer["argv_source"],
            "import execution argv source drift")
    require(isinstance(value["store"], dict), "import.store must be an object")
    imported_store_claim = Path(nonempty(value["store"].get("path"), "import.store.path")).resolve()
    require(Path(execution_result["target_store"]).resolve() == imported_store_claim,
            "import execution target store drift")
    require(execution_result["inputs"] == inputs, "import execution input closure drift")
    for key in ("stdout", "stderr", "report"):
        require(execution_result[key] == execution[key], f"import execution {key} reference drift")
    require(validate_images(execution_result["images"], "import execution images") == receipt_images,
            "import execution/receipt image identity mismatch")

    loaded = exact_keys(
        value["loaded"], ("vertex_count", "edge_count", "edge_type_count"), "import.loaded"
    )
    execution_loaded = exact_keys(
        execution_result["loaded"],
        ("vertex_count", "edge_count", "edge_type_count"),
        "import execution loaded",
    )
    require(loaded["vertex_count"] == FORMAL_VERTEX_COUNT, "formal import vertex count drift")
    require(loaded["edge_count"] == FORMAL_EDGE_COUNT, "formal import edge count drift")
    require(loaded["edge_type_count"] == 34, "formal import edge-type count drift")
    for key in ("vertex_count", "edge_count", "edge_type_count"):
        require(execution_loaded[key] == loaded[key], f"import execution/loaded {key} drift")
    report_path = validate_file_ref(execution["report"], "import execution report", recompute=recompute_artifacts)
    report = exact_keys(
        read_strict_json(report_path, "importer report"),
        (
            "schema_version", "store_root", "raw_dataset_manifest", "dense_dataset", "truth",
            "runtime_manifest", "loaded",
        ),
        "importer report",
    )
    require(report["schema_version"] == IMPORTER_REPORT_SCHEMA, "importer report schema drift")
    require(Path(report["store_root"]).resolve() == imported_store_claim, "importer report store drift")
    for key in ("raw_dataset_manifest", "dense_dataset", "truth", "runtime_manifest"):
        require(report[key] == inputs[key], f"importer report {key} drift")
    report_loaded = exact_keys(
        report["loaded"],
        ("vertex_count", "edge_count", "edge_type_count"),
        "importer report loaded",
    )
    require(report_loaded == loaded, "importer report loaded-count drift")

    def argv_bound_path(flag: str) -> Path:
        positions = [index for index, item in enumerate(argv) if item == flag]
        require(len(positions) == 1 and positions[0] + 1 < len(argv),
                f"importer argv requires exactly one {flag}")
        return Path(argv[positions[0] + 1]).resolve(strict=False)

    expected_argv_paths = {
        "--raw-dataset-manifest": raw_manifest,
        "--dense-dataset": dataset_path.resolve(),
        "--truth": truth_path.resolve(),
        "--runtime-manifest": runtime_manifest_path,
        "--store-root": imported_store_claim,
        "--importer-report": report_path,
    }
    for flag, expected_path in expected_argv_paths.items():
        require(argv_bound_path(flag) == expected_path.resolve(strict=False),
                f"importer argv {flag} closure drift")
    correctness = exact_keys(
        value["correctness"],
        ("query_count", "mismatch_count", "timeout_count", "observations", "result"),
        "import.correctness",
    )
    require(correctness["query_count"] == FORMAL_QUERY_COUNT, "formal import correctness query count drift")
    require(correctness["mismatch_count"] == 0, "formal import correctness has mismatches")
    require(correctness["timeout_count"] == 0, "formal import correctness has timeouts")
    observations_path = validate_file_ref(
        correctness["observations"], "import.correctness.observations", recompute=recompute_artifacts
    )
    require(argv_bound_path("--correctness-observations") == observations_path,
            "importer argv correctness observation closure drift")
    truth_rows = read_truth(truth_path, FORMAL_QUERY_COUNT)
    try:
        with observations_path.open("r", encoding="utf-8", newline="") as handle:
            observation_reader = csv.DictReader(handle, delimiter="\t")
            require(tuple(observation_reader.fieldnames or ()) == IMPORT_OBSERVATION_COLUMNS,
                    "import correctness observation header drift")
            observation_rows = list(observation_reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ContractError(f"cannot rederive import correctness observations: {exc}") from exc
    require(len(observation_rows) == len(truth_rows),
            "import correctness observation count drift")
    derived_mismatches = 0
    derived_timeouts = 0
    for index, (expected, observed) in enumerate(zip(truth_rows, observation_rows)):
        expected_values = {
            "query_index": str(expected["query_index"]),
            "edge_type": str(expected["edge_type"]),
            "src": str(expected["src"]),
            "expected_count": str(expected["count"]),
            "expected_sum_hash": str(expected["sum_hash"]),
            "expected_xor_hash": str(expected["xor_hash"]),
        }
        require(all(observed.get(key) == value for key, value in expected_values.items()),
                f"import correctness observation truth/order drift: {index}")
        status = observed.get("status")
        require(status in ("ok", "timeout"),
                f"import correctness observation status drift: {index}")
        if status == "timeout":
            derived_timeouts += 1
        else:
            derived_mismatches += (
                observed.get("actual_count"),
                observed.get("actual_sum_hash"),
                observed.get("actual_xor_hash"),
            ) != (
                expected_values["expected_count"],
                expected_values["expected_sum_hash"],
                expected_values["expected_xor_hash"],
            )
    require(
        correctness
        == {
            "query_count": len(observation_rows),
            "mismatch_count": derived_mismatches,
            "timeout_count": derived_timeouts,
            "observations": correctness["observations"],
            "result": correctness["result"],
        },
        "import correctness summary was not derived from raw observations",
    )
    correctness_result_path = validate_file_ref(
        correctness["result"], "import.correctness.result", recompute=recompute_artifacts
    )
    correctness_result = exact_keys(
        read_strict_json(correctness_result_path, "import correctness result"),
        (
            "schema_version", "state", "producer", "importer", "argv_sha256", "target_store",
            "query_count", "mismatch_count", "timeout_count", "truth", "dataset", "observations",
        ),
        "import correctness result",
    )
    require(
        correctness_result["schema_version"] == IMPORT_CORRECTNESS_SCHEMA
        and correctness_result["state"] == "PASS",
        "import correctness result schema/state drift",
    )
    for key in ("query_count", "mismatch_count", "timeout_count"):
        require(correctness_result[key] == correctness[key], f"import correctness {key} evidence drift")
    require(correctness_result["producer"] == {"path": str(wrapper_path), "sha256": wrapper["sha256"]},
            "import correctness producer drift")
    require(correctness_result["importer"] == {"path": str(importer_path), "sha256": importer["sha256"]},
            "import correctness importer drift")
    require(correctness_result["argv_sha256"] == importer["argv_sha256"],
            "import correctness argv drift")
    require(Path(correctness_result["target_store"]).resolve() == imported_store_claim,
            "import correctness target store drift")
    require(correctness_result["truth"] == inputs["truth"], "import correctness truth drift")
    require(correctness_result["dataset"] == inputs["dense_dataset"], "import correctness dataset drift")
    require(correctness_result["observations"] == correctness["observations"],
            "import correctness observation reference drift")
    imported_store_path, imported_tree = validate_tree_ref(value["store"], "import.store", recompute=False)
    require(not paths_overlap(receipt_path, imported_store_path),
            "import receipt must be outside the imported store")
    return receipt_path, {**value, "store": imported_tree}


def validate_clone_receipt(
    receipt_ref: object,
    *,
    repo: dict[str, Any],
    imported_tree: dict[str, Any],
    target_path: Path,
    target_tree: dict[str, Any],
    run_id: str,
    repeat_index: int,
    recompute_tool: bool = True,
) -> tuple[Path, dict[str, Any]]:
    receipt_path = validate_file_ref(receipt_ref, "store.clone_receipt", recompute=True)
    value = exact_keys(
        read_strict_json(receipt_path, "NebulaGraph clone receipt"),
        (
            "schema_version", "state", "created_at_utc", "run_id", "repeat_index", "repo", "method",
            "copy_tool", "source_pre", "source_post", "target", "safety",
        ),
        "NebulaGraph clone receipt",
    )
    require(value["schema_version"] == CLONE_RECEIPT_SCHEMA, "clone receipt schema drift")
    require(value["state"] == "PASS", "clone receipt is not PASS")
    parse_timestamp(value["created_at_utc"], "clone.created_at_utc")
    require(value["run_id"] == run_id, "clone receipt run_id mismatch")
    require(value["repeat_index"] == repeat_index, "clone receipt repeat_index mismatch")
    validate_repo(value["repo"], repo, "clone.repo")
    require(value["method"] in CLONE_METHODS, "clone method drift")

    tool = exact_keys(
        value["copy_tool"],
        ("path", "sha256", "argv", "argv_sha256", "exit_code"),
        "clone.copy_tool",
    )
    tool_path = validate_file_ref(
        {"path": tool["path"], "sha256": tool["sha256"]},
        "clone.copy_tool.file",
        recompute=recompute_tool,
    )
    argv = tool["argv"]
    require(isinstance(argv, list) and all(isinstance(item, str) for item in argv),
            "clone copy argv is malformed")
    require(bool(argv) and Path(argv[0]).resolve() == tool_path, "clone copy argv[0] mismatch")
    require(exact_sha(tool["argv_sha256"], "clone.copy_tool.argv_sha256") == canonical_json_sha(argv),
            "clone copy argv digest mismatch")
    require(tool["exit_code"] == 0, "clone copy command did not exit zero")

    source_path, source_pre = validate_tree_ref(value["source_pre"], "clone.source_pre", recompute=False)
    source_post_path, source_post = validate_tree_ref(value["source_post"], "clone.source_post", recompute=False)
    actual_target_path, receipt_target = validate_tree_ref(
        value["target"], "clone.target", expected_path=target_path, recompute=False
    )
    expected_reflink = "--reflink=always" if value["method"] == CLONE_METHOD_REFLINK else "--reflink=never"
    require(
        len(argv) == 7
        and argv[1:5] == ["-a", expected_reflink, "--no-target-directory", "--"],
        "clone copy argv flags differ from the declared method",
    )
    require(Path(argv[5]).resolve() == source_path, "clone copy argv source mismatch")
    staging = Path(argv[6]).resolve(strict=False)
    require(
        staging.parent == actual_target_path.parent
        and staging.name.startswith(f".{actual_target_path.name}.clone-")
        and staging != actual_target_path
        and not staging.exists(),
        "clone copy argv staging/publication path mismatch",
    )
    require(source_path == source_post_path, "clone source path changed")
    require(not paths_overlap(source_path, actual_target_path), "clone source and target overlap")
    require(not paths_overlap(receipt_path, source_path) and not paths_overlap(receipt_path, actual_target_path),
            "clone receipt must be outside source and target trees")
    _same_tree_identity(source_pre, source_post, "clone source pre/post")
    _same_tree_identity(source_pre, imported_tree, "clone source/import receipt")
    _same_tree_identity(source_pre, receipt_target, "clone source/target receipt")
    _same_tree_identity(receipt_target, target_tree, "clone receipt/current target")

    safety = exact_keys(
        value["safety"],
        (
            "target_absent_before", "source_target_nonoverlap", "offline_check_count",
            "no_shared_regular_inodes", "symlink_policy", "publish_noreplace",
        ),
        "clone.safety",
    )
    require(safety["target_absent_before"] is True, "clone did not prove target absence")
    require(safety["source_target_nonoverlap"] is True, "clone did not prove path separation")
    require(integer(safety["offline_check_count"], "clone.safety.offline_check_count", 3) >= 3,
            "clone requires at least three offline checks")
    require(safety["no_shared_regular_inodes"] is True, "clone contains shared hard-link inodes")
    require(safety["symlink_policy"] == SYMLINK_POLICY, "clone symlink policy drift")
    require(safety["publish_noreplace"] is True, "clone target publication was not no-replace")
    return receipt_path, value


def write_json_exclusive(path: Path, value: object) -> None:
    """Publish complete JSON through an exclusive hard link, never replacement."""

    output = path.resolve(strict=False)
    require(output.parent.is_dir() and not output.parent.is_symlink(),
            f"output parent must already be a real directory: {output.parent}")
    encoded = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    temporary = output.parent / f".{output.name}.tmp-{os.getpid()}-{os.urandom(8).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(str(temporary), flags, 0o600)
    except FileExistsError as exc:
        raise ContractError(f"exclusive temporary output unexpectedly exists: {temporary}") from exc
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, output, follow_symlinks=False)
    except FileExistsError as exc:
        raise ContractError(f"refusing to overwrite output: {output}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
