#!/usr/bin/env python3
"""Run one target-specific E01 P02B with full-tree pre/post integrity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import stat
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


SCHEMA = "cidr-e01-target-specific-p02b-v1"
TREE_METHOD = "sha256-tree-v1(relative-path,size,file-sha256)"
SOURCE_PLAN_SHA256 = "4520c88eb594903eb6e3f282838e6cd885e205ee5b0b1790565112d5940f8ea3"
VARIANTS = {
    "budg-b64": {"layout": "semantic-budgeted", "hint": True, "tree_sha256": "ae77255c03c40d9d7e55071374ab3adc1dc67942f9443ad7d97dacacbe78c8b5"},
    "naive": {"layout": "naive", "hint": False, "tree_sha256": "133e2ab535dd2c915d93e6e5ded65295151e199ec7268609f0a3e2f65387ddd2"},
}
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


class TargetError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise TargetError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    require(root.is_dir() and not root.is_symlink(), "store root invalid")
    files: list[Path] = []
    writable: list[str] = []
    directory_count = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        directory_count += 1
        if os.stat(base, follow_symlinks=False).st_mode & 0o222:
            writable.append(base.relative_to(root).as_posix() or ".")
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            require(not (base / name).is_symlink(), "symlink directory forbidden")
        for name in filenames:
            path = base / name
            require(path.is_file() and not path.is_symlink(), f"non-regular store file: {path}")
            if os.stat(path, follow_symlinks=False).st_mode & 0o222:
                writable.append(path.relative_to(root).as_posix())
            files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix().encode())
    require(files, "store is empty")
    digest = hashlib.sha256()
    total = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total += size
        digest.update(f"file\0{relative}\0{size}\0{sha256_file(path)}\n".encode())
    stat = root.stat()
    return {
        "canonical_path": str(root),
        "dev": stat.st_dev,
        "inode": stat.st_ino,
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "directory_count": directory_count,
        "total_bytes": total,
        "hash_method": TREE_METHOD,
        "full_tree_hash_performed": True,
        "writable_entries": writable,
        "immutable_permissions_pass": not writable,
    }


def load(path: Path, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path = path.resolve()
    require(path.is_file() and not path.is_symlink(), f"{label}: regular file required")
    value = json.loads(path.read_text(encoding="utf-8"))
    require(type(value) is dict, f"{label}: object required")
    return value, {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def file_ref(path: Path, label: str) -> dict[str, Any]:
    path = path.resolve()
    require(path.is_file() and not path.is_symlink(), f"{label}: regular file required")
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def permission_evidence(root: Path) -> dict[str, Any]:
    root = root.resolve()
    writable = []
    files = directories = 0
    for directory, _, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        directories += 1
        if os.stat(base, follow_symlinks=False).st_mode & 0o222:
            writable.append(base.relative_to(root).as_posix() or ".")
        for name in filenames:
            path = base / name
            require(path.is_file() and not path.is_symlink(), f"non-regular store file: {path}")
            files += 1
            if os.stat(path, follow_symlinks=False).st_mode & 0o222:
                writable.append(path.relative_to(root).as_posix())
    require(files > 0 and not writable, "immutable store permission drift")
    return {"state": "PASS", "directory_count": directories, "file_count": files, "writable_entries": writable}


def validate_tree_contract(tree: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    require(tree.get("sha256") == manifest.get("store_sha256"), "store tree SHA drift")
    require(tree.get("file_count") == manifest.get("file_count"), "store manifest file_count drift")
    require(tree.get("total_bytes") == manifest.get("total_bytes"), "store manifest total_bytes drift")
    require(tree.get("immutable_permissions_pass") is True, "target store contains writable entries")


def validate_static(args: argparse.Namespace) -> dict[str, Any]:
    expected = VARIANTS[args.variant]
    config, config_ref = load(args.config, "config")
    require(config.get("schema_version") == "p02b-sf10-sentinel-config-v2", "config schema drift")
    require(config.get("fixture_mode") is False, "formal config required")
    require(config.get("expected_queries") == 1700, "1700 queries required")
    require(config.get("l0_layout") == expected["layout"], "target layout drift")
    require(config.get("semantic_degree_hint") is expected["hint"], "target hint drift")
    store_manifest, manifest_ref = load(args.store_manifest, "store manifest")
    require(store_manifest.get("schema_version") == "p02b-store-manifest-v1", "store manifest schema drift")
    require(Path(store_manifest["store_path"]).resolve() == args.store.resolve(), "store path drift")
    require(store_manifest.get("hash_method") == TREE_METHOD, "store hash method drift")
    require(store_manifest.get("store_sha256") == expected["tree_sha256"], "unexpected target tree SHA")
    seal, seal_ref = load(args.store_seal, "store seal")
    require(seal.get("state") == "PASS" and seal.get("variant") == args.variant, "store seal drift")
    require(Path(seal["immutable_root"]).resolve() == args.store.resolve(), "seal root drift")
    require(seal.get("tree_sha256") == store_manifest["store_sha256"], "seal/manifest SHA drift")
    require(seal.get("schema_version") == "cidr-e01-immutable-store-seal-v1", "store seal schema drift")
    require(seal.get("formal_eligible") is False and seal.get("performance_eligible") is False and seal.get("paper_claim_eligible") is False, "store seal eligibility drift")
    store_stat = os.stat(args.store.resolve(), follow_symlinks=False)
    require(store_stat.st_dev == seal.get("immutable_dev") and store_stat.st_ino == seal.get("immutable_inode"), "store dev/inode drift")
    require(store_stat.st_mode & 0o222 == 0, "store root is writable")
    permissions = permission_evidence(args.store)
    require(permissions["file_count"] == store_manifest.get("file_count"), "permission inventory/file_count drift")
    plan, plan_ref = load(args.query_plan, "query plan")
    require(plan.get("semantic_degree_hint") is expected["hint"], "query plan hint drift")
    entries = plan.get("entries")
    require(type(entries) is list and sum(len(row.get("samples", [])) for row in entries) == 1700, "query plan count drift")
    naive_equivalence = None
    if args.variant == "naive":
        require(args.plan_equivalence is not None, "naive plan equivalence receipt required")
        equivalence, naive_equivalence = load(args.plan_equivalence, "naive plan equivalence")
        require(equivalence.get("schema_version") == "cidr-e01-naive-p02b-plan-equivalence-v1", "naive equivalence schema drift")
        require(equivalence.get("state") == "PASS" and equivalence.get("exact_sequence_equal") is True, "naive equivalence did not PASS")
        require(equivalence.get("target_plan") == plan_ref, "naive equivalence target-plan drift")
        require(equivalence.get("source_plan", {}).get("sha256") == SOURCE_PLAN_SHA256, "naive source-plan SHA drift")
        require(equivalence.get("only_changed_field") == "semantic_degree_hint", "naive equivalence changed extra fields")
        require(equivalence.get("formal_eligible") is False and equivalence.get("performance_eligible") is False, "naive equivalence eligibility drift")
    bound = {}
    for name in ("runner", "clean_ready", "repo_root", "binary", "dataset_manifest", "truth", "id_map_dir", "p31_wrapper", "batch_gate_tool", "p02b_validator"):
        path = getattr(args, name)
        if name in ("repo_root", "id_map_dir"):
            require(path.resolve().is_dir() and not path.resolve().is_symlink(), f"{name}: directory required")
            bound[name] = {"path": str(path.resolve())}
        else:
            bound[name] = load(path, name)[1] if path.suffix == ".json" else {
                "path": str(path.resolve()), "sha256": sha256_file(path.resolve()), "size_bytes": path.resolve().stat().st_size
            }
    git_head = subprocess.run(
        ["git", "-C", str(args.repo_root.resolve()), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    require(len(git_head) == 40, "repo HEAD drift")
    return {
        "config": config_ref,
        "store_manifest": manifest_ref,
        "store_seal": seal_ref,
        "query_plan": plan_ref,
        "naive_plan_equivalence": naive_equivalence,
        "store_tree_sha256": store_manifest["store_sha256"],
        "bound_inputs": bound,
        "repo_head": git_head,
        "immutable_permissions": permissions,
    }


def atomic(path: Path, value: Mapping[str, Any]) -> None:
    require(not path.exists(), f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def run(args: argparse.Namespace) -> dict[str, Any]:
    static = validate_static(args)
    require(not args.output.exists(), "target P02B output already exists")
    require(not args.run_dir.exists(), "P02B run root already exists")
    require(not args.lease_output.exists(), "lease output already exists")
    before = tree_manifest(args.store)
    validate_tree_contract(before, load(args.store_manifest, "store manifest")[0])
    command = [
        str(args.runner.resolve()),
        "--run-dir", str(args.run_dir.resolve()),
        "--clean-ready", str(args.clean_ready.resolve()),
        "--repo-root", str(args.repo_root.resolve()),
        "--binary", str(args.binary.resolve()),
        "--dataset-manifest", str(args.dataset_manifest.resolve()),
        "--store", str(args.store.resolve()),
        "--store-manifest", str(args.store_manifest.resolve()),
        "--truth", str(args.truth.resolve()),
        "--query-plan", str(args.query_plan.resolve()),
        "--id-map-dir", str(args.id_map_dir.resolve()),
        "--config", str(args.config.resolve()),
        "--p31-wrapper", str(args.p31_wrapper.resolve()),
        "--batch-gate-tool", str(args.batch_gate_tool.resolve()),
        "--batch-lease-output", str(args.lease_output.resolve()),
    ]
    completed = subprocess.run(command, cwd=args.repo_root, check=False, timeout=args.timeout_seconds)
    require(completed.returncode == 0, f"target P02B runner exited {completed.returncode}")
    result, result_ref = load(args.run_dir / "sentinel-result.json", "sentinel result")
    require(result.get("state") == "PASS", "target P02B did not PASS")
    require(result.get("schema_version") == "p02b-sf10-sentinel-result-v2", "sentinel schema drift")
    require(result.get("stability", {}).get("state") == "PASS", "stability did not PASS")
    require(result.get("downstream_release_eligible") is True, "sentinel not downstream-release eligible")
    require(result.get("provenance", {}).get("store_sha256") == before["sha256"], "P02B result store SHA drift")
    provenance = result["provenance"]
    dataset_manifest = load(args.dataset_manifest, "dataset manifest")[0]
    for name, actual in (
        ("binary_sha256", sha256_file(args.binary)),
        ("config_sha256", sha256_file(args.config)),
        ("dataset_sha256", dataset_manifest.get("dataset_sha256")),
        ("query_plan_sha256", sha256_file(args.query_plan)),
        ("truth_sha256", sha256_file(args.truth)),
    ):
        require(provenance.get(name) == actual, f"sentinel {name} drift")
    pass_ref = file_ref(args.run_dir / "PASS", "sentinel PASS marker")
    issuance, issuance_ref = load(args.run_dir / "BATCH-LEASE-ISSUED.json", "batch lease issuance")
    require(issuance.get("state") == "PASS" and issuance.get("schema_version") == "p02b-batch-lease-issuance-v2", "lease issuance drift")
    lease, lease_ref = load(args.lease_output, "lease")
    require(lease.get("schema_version") == "cidr-batch-lease-v2", "lease schema drift")
    require(lease.get("state") == "PASS", "lease did not PASS")
    require(issuance.get("lease") == lease_ref, "issuance/lease ref drift")
    marker_path = args.lease_output.with_name(args.lease_output.name + ".PASS.json")
    marker, marker_ref = load(marker_path, "lease marker")
    require(marker.get("schema_version") == "cidr-batch-lease-marker-v2" and marker.get("state") == "PASS", "lease marker drift")
    require(marker.get("lease") == str(args.lease_output.resolve()) and marker.get("lease_sha256") == lease_ref["sha256"], "lease marker cross-binding drift")
    require(issuance.get("lease_marker") == marker_ref, "issuance/marker ref drift")
    clean = result.get("clean_ready")
    require(type(clean) is dict, "sentinel/P03 clean-ready binding absent")
    clean_ref = file_ref(args.clean_ready, "clean ready marker")
    require(clean.get("artifacts", {}).get("READY") == clean_ref, "sentinel/P03 READY drift")
    require(clean.get("state") == "PASS" and clean.get("protocol_version") == "short-clean-window-v2", "P03 clean-ready did not PASS")
    for item in clean.get("artifacts", {}).values():
        require(file_ref(Path(item["path"]), "P03 artifact") == item, "P03 artifact ref drift")
    validations = {}
    for consumer in ("P10", "P20"):
        validation_path = args.run_dir / f"VALIDATION-{consumer}.json"
        validator_argv = [
            str(args.p02b_validator.resolve()), "--result", str((args.run_dir / "sentinel-result.json").resolve()),
            "--consumer", consumer, "--require-formal", "--expected-repo-root", str(args.repo_root.resolve()),
            "--expected-repo-head", static["repo_head"], "--expected-binary-sha256", sha256_file(args.binary),
        ]
        checked = subprocess.run(validator_argv, cwd=args.repo_root, check=False, capture_output=True, text=True, timeout=args.timeout_seconds)
        require(checked.returncode == 0, f"{consumer} validator failed: {checked.stderr.strip()}")
        validation = json.loads(checked.stdout)
        require(validation.get("state") == "PASS" and validation.get("consumer") == consumer, f"{consumer} validation drift")
        atomic(validation_path, validation)
        validations[consumer] = load(validation_path, f"{consumer} validation")[1]
        admission = lease.get("p02b", {}).get("admissions", {}).get(consumer, {})
        require(admission.get("consumer") == consumer and admission.get("formal_required") is True, f"{consumer} lease admission drift")
        require(admission.get("binary_sha256") == sha256_file(args.binary), f"{consumer} lease/binary drift")
        require(admission.get("repo_root") == str(args.repo_root.resolve()) and admission.get("repo_head") == static["repo_head"], f"{consumer} lease/repo drift")
        require(admission.get("sentinel_result") == str((args.run_dir / "sentinel-result.json").resolve()), f"{consumer} lease/sentinel drift")
        require(admission.get("pass_marker") == pass_ref["path"] and admission.get("pass_marker_sha256") == pass_ref["sha256"], f"{consumer} lease/PASS drift")
    after = tree_manifest(args.store)
    require(after == before, "target store changed across P02B")
    done = {
        "schema_version": SCHEMA,
        "state": "PASS",
        "variant": args.variant,
        "target": {name: VARIANTS[args.variant][name] for name in ("layout", "hint")},
        "static_inputs": static,
        "clean_ready": {"path": str(args.clean_ready.resolve()), "sha256": sha256_file(args.clean_ready)},
        "store_pre": before,
        "store_post": after,
        "store_unchanged": True,
        "sentinel_result": result_ref,
        "lease": lease_ref,
        "lease_marker": marker_ref,
        "batch_lease_issuance": issuance_ref,
        "sentinel_pass_marker": pass_ref,
        "official_validations": validations,
        "clean_ready": clean_ref,
        "runner_argv": command,
        "query_count": 1700,
        "formal_figure_timing_generated": False,
        "admission_timing_generated": True,
        **FALSE_ELIGIBILITY,
    }
    atomic(args.output, done)
    return done


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--variant", choices=tuple(VARIANTS), required=True)
    for name in (
        "runner", "clean_ready", "repo_root", "binary", "dataset_manifest",
        "store", "store_manifest", "store_seal", "truth", "query_plan",
        "id_map_dir", "config", "p31_wrapper", "batch_gate_tool",
        "lease_output", "run_dir", "output", "p02b_validator",
    ):
        value.add_argument("--" + name.replace("_", "-"), type=Path, required=True)
    value.add_argument("--preflight-only", action="store_true")
    value.add_argument("--plan-equivalence", type=Path)
    value.add_argument("--timeout-seconds", type=int, default=1800)
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        static = validate_static(args)
        if args.preflight_only:
            print(json.dumps({"state": "READY", "variant": args.variant, "static": static}, sort_keys=True))
        else:
            result = run(args)
            print(json.dumps({"state": result["state"], "variant": args.variant}, sort_keys=True))
        return 0
    except (TargetError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        if "args" in locals() and not args.preflight_only and not args.output.exists():
            try:
                atomic(args.output.with_name(args.output.name + ".FAILED_RETAINED.json"), {
                    "schema_version": SCHEMA,
                    "state": "FAILED_RETAINED",
                    "reason": str(exc),
                    "formal_figure_timing_generated": False,
                    "admission_timing_generated": args.run_dir.exists(),
                    **FALSE_ELIGIBILITY,
                })
            except BaseException:
                pass
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
