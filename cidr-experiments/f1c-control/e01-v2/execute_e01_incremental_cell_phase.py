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
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Optional, Sequence


PLAN_SCHEMA = "cidr-e01-incremental-backend-plan-v1"
DRYRUN_SCHEMA = "cidr-e01-mutable-clone-dry-run-v1"
PHASES = ("prepare", "p31", "finalize", "cleanup")
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}
MAX_SMALL_FILE_BYTES = 16 * 1024 * 1024


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


def _safe_clone_roots(source: Path, target: Path, allowed_parent: Path) -> tuple[Path, Path]:
    source = source.resolve()
    target = target.absolute()
    allowed_parent = allowed_parent.resolve()
    require(source.is_dir() and not source.is_symlink(), "clone source invalid")
    require(not target.exists(), "clone target already exists")
    require(target.parent.resolve() == allowed_parent, "clone target parent drift")
    require(not os.path.ismount(source), "clone source must not be a mount point")
    require(not os.path.ismount(target.parent), "clone target parent must not be a mount point")
    require(source != target and source not in target.parents, "clone overlap")
    return source, target


def copy_clone(source: Path, target: Path, allowed_parent: Path, copy_argv: list[str]) -> dict[str, Any]:
    source, target = _safe_clone_roots(source, target, allowed_parent)
    require(copy_argv and Path(copy_argv[0]).is_absolute(), "absolute clone command required")
    require("{SOURCE}" in copy_argv and "{TARGET}" in copy_argv, "clone tokens required")
    source_meta = metadata_manifest(source)
    command = [
        str(source) + "/." if item == "{SOURCE}" else str(target) if item == "{TARGET}" else item
        for item in copy_argv
    ]
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


def exact_cleanup(target: Path, allowed_parent: Path, expected_dev: int, expected_inode: int) -> None:
    target = target.resolve()
    allowed_parent = allowed_parent.resolve()
    require(target.parent == allowed_parent, "cleanup parent drift")
    require(target.is_dir() and not target.is_symlink(), "cleanup target invalid")
    require(not os.path.ismount(target), "cleanup target is a mount point")
    stat = os.stat(target, follow_symlinks=False)
    require(stat.st_dev == expected_dev and stat.st_ino == expected_inode, "cleanup dev/inode drift")
    shutil.rmtree(target)
    require(not target.exists(), "cleanup target still exists")


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


def prepare(plan: Mapping[str, Any], cell: Mapping[str, Any], plan_sha: str, cwd: Path) -> None:
    runtime = cell["runtime"]
    policy = runtime["clone_policy"]
    seal_ref = verify_ref(policy["source_seal"], "source store seal")
    seal = load_json(Path(seal_ref["path"]), "source store seal")
    require(seal.get("state") == "PASS", "source store seal is not PASS")
    require(seal.get("tree_sha256") == policy["tree_sha256"], "source tree SHA drift")
    require(Path(seal["immutable_root"]).resolve() == Path(policy["source_root"]).resolve(), "source root/seal drift")
    clone = copy_clone(
        Path(policy["source_root"]),
        Path(policy["mutable_clone"]),
        cwd,
        list(policy["copy_argv"]),
    )
    atomic_json(
        cwd / "receipts/store-clone.json",
        {
            **_base_receipt(cell, plan_sha, "store-clone"),
            "source_seal": seal_ref,
            "source_tree_sha256": policy["tree_sha256"],
            "full_content_hash_performed": False,
            **clone,
        },
    )
    request = json.loads(
        json.dumps(runtime["request"]).replace("{MUTABLE_CLONE}", clone["target"])
    )
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
    prepared = load_json(cwd / "receipts/prepared-command.json", "prepared command")
    require(prepared.get("backend_plan_sha256") == plan_sha, "prepared plan SHA drift")
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
            "binary_only_boundary": True,
            "p31_done": {
                "path": str(done_path.resolve()),
                "sha256": sha256_file(done_path),
                "size_bytes": done_path.stat().st_size,
            },
        },
    )


def finalize(plan: Mapping[str, Any], cell: Mapping[str, Any], plan_sha: str, cwd: Path) -> None:
    p31 = load_json(cwd / "receipts/p31.json", "P31 receipt")
    require(p31.get("state") == "PASS" and p31.get("binary_only_boundary") is True, "P31 receipt invalid")
    runtime = cell["runtime"]
    adapter_ref = verify_ref(runtime["adapter_tool"], "adapter tool")
    adapter = _load_module(Path(adapter_ref["path"]))
    request_path = cwd / "adapter-request.json"
    request = load_json(request_path, "adapter request")
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
    for relative in ("validated-result.json", "receipts/correctness.json", "receipts/fairness.json"):
        require((cwd / relative).is_file(), f"cleanup blocked: {relative} missing")
    clone_receipt = load_json(cwd / "receipts/store-clone.json", "store clone receipt")
    target = Path(clone_receipt["target"])
    exact_cleanup(target, cwd, clone_receipt["target_dev"], clone_receipt["target_inode"])
    atomic_json(
        cwd / "receipts/cleanup.json",
        {
            **_base_receipt(cell, plan_sha, "cleanup"),
            "mutable_clone_removed": True,
            "mutable_clone": str(target),
            "released_dev": clone_receipt["target_dev"],
            "released_inode": clone_receipt["target_inode"],
        },
    )


def clone_dry_run(plan: Mapping[str, Any], cell: Mapping[str, Any], plan_sha: str, output: Path) -> None:
    require(not output.exists(), "dry-run output already exists")
    output.mkdir(parents=True)
    policy = cell["runtime"]["clone_policy"]
    target = output / "mutable-store"
    try:
        clone = copy_clone(
            Path(policy["source_root"]),
            target,
            output,
            list(policy["copy_argv"]),
        )
        exact_cleanup(target, output, clone["target_dev"], clone["target_inode"])
        atomic_json(
            output / "CLONE-DRYRUN.json",
            {
                "schema_version": DRYRUN_SCHEMA,
                "state": "PASS",
                "synthetic_test_only": False,
                "fixture_only": False,
                "backend_plan_sha256": plan_sha,
                "cell_key": cell["cell_key"],
                "source_tree_sha256": policy["tree_sha256"],
                "clone": clone,
                "mutable_clone_removed": True,
                "timing_generated": False,
                **FALSE_ELIGIBILITY,
            },
        )
    except BaseException as exc:
        if not (output / "FAILED.json").exists():
            atomic_json(
                output / "FAILED.json",
                {
                    "schema_version": DRYRUN_SCHEMA,
                    "state": "FAILED_RETAINED",
                    "reason": str(exc),
                    "timing_generated": False,
                    **FALSE_ELIGIBILITY,
                },
            )
        raise


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-plan", type=Path, required=True)
    parser.add_argument("--cell-key", required=True)
    parser.add_argument("--phase", choices=(*PHASES, "clone-dry-run"), required=True)
    parser.add_argument("--dry-run-output", type=Path)
    args = parser.parse_args(argv)
    try:
        plan_path = args.backend_plan.resolve()
        plan = load_json(plan_path, "backend plan")
        require(plan.get("schema_version") == PLAN_SCHEMA, "backend plan schema drift")
        require(plan.get("synthetic_test_only") is False, "synthetic backend forbidden")
        plan_sha = sha256_file(plan_path)
        cell = _cell(plan, args.cell_key)
        if args.phase == "clone-dry-run":
            require(args.dry_run_output is not None and args.dry_run_output.is_absolute(), "absolute dry-run output required")
            clone_dry_run(plan, cell, plan_sha, args.dry_run_output)
        else:
            cwd = Path(cell["staging_cell_root"]).resolve()
            require(cwd == Path.cwd().resolve(), "phase cwd/staging drift")
            {"prepare": prepare, "p31": run_p31, "finalize": finalize, "cleanup": cleanup}[args.phase](
                plan, cell, plan_sha, cwd
            )
        return 0
    except (PhaseError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
