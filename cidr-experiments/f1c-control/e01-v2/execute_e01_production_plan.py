#!/usr/bin/env python3
"""Fail-closed production orchestration interfaces for E01.

This module deliberately has no process-launching primitive.  It validates a
frozen production plan, reports every execution blocker before a campaign root
is created, provides seven receipt producer interfaces for already-generated
source evidence, and atomically publishes a completed production cell.

The current command-plan and adapter-identity contracts remain
``NOT_IMPLEMENTED``.  Consequently :func:`execute_production_plan` always
fails before creating a result root or invoking an adapter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import build_e01_production_command_plan as command_plan
import run_e01_formal_matrix as scheduler
import validate_e01_cell_evidence as evidence


FALSE_ELIGIBILITY = scheduler.FALSE_ELIGIBILITY
READY_EXECUTION_STATE = "READY"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FAILED_MARKERS = ("FAILED", "FAILED.json", "MATRIX-FAILED.json")
MATRIX_START_SCHEMA = "cidr-e01-production-matrix-start-v1"


class ProductionError(RuntimeError):
    pass


class ProductionBlocked(ProductionError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProductionError(message)


def false_eligibility(value: Mapping[str, Any], label: str) -> None:
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{label}: {key} must be exactly false")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    require(len(payload) <= evidence.MAX_RECEIPT_BYTES, f"{path.name}: file too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def relative_file_ref(path: Path, root: Path, label: str) -> dict[str, Any]:
    require(path.is_file(), f"{label}: source file missing")
    require(not path.is_symlink(), f"{label}: source symlink forbidden")
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        inside = os.path.commonpath((str(resolved_root), str(resolved))) == str(
            resolved_root
        )
    except ValueError:
        inside = False
    require(inside, f"{label}: source must be inside staging cell")
    size = path.stat().st_size
    require(0 < size <= evidence.MAX_RECEIPT_BYTES, f"{label}: source size invalid")
    return {
        "path": path.relative_to(resolved_root).as_posix(),
        "sha256": sha256_file(path),
        "size_bytes": size,
    }


def resolve_source_ref(
    cell_root: Path, descriptor: Any, label: str
) -> tuple[Path, dict[str, Any]]:
    require(type(descriptor) is dict, f"{label}: source reference required")
    require(
        set(descriptor) == {"path", "sha256", "size_bytes"},
        f"{label}: source reference keys drift",
    )
    raw = descriptor.get("path")
    require(type(raw) is str and raw, f"{label}: source relative path required")
    require(
        "\\" not in raw
        and not raw.startswith("/")
        and not re.match(r"^[A-Za-z]:", raw),
        f"{label}: source path must be relative",
    )
    relative = PurePosixPath(raw)
    require(
        relative.parts
        and all(part not in ("", ".", "..") for part in relative.parts),
        f"{label}: source traversal forbidden",
    )
    path = cell_root.joinpath(*relative.parts)
    actual = relative_file_ref(path, cell_root, label)
    require(actual == descriptor, f"{label}: source path/size/SHA drift")
    return path, actual


def _receipt_common(
    *,
    role: str,
    cell_root: Path,
    source_evidence: Path,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
) -> dict[str, Any]:
    require(role in evidence.ROLES, f"unsupported receipt role: {role}")
    require(run_key in scheduler.RUN_KEYS, "run_key is not one of the fixed 21 cells")
    require(
        ordinal == scheduler.RUN_KEYS.index(run_key) + 1,
        "ordinal/run_key drift",
    )
    require(
        type(manifest_sha) is str and SHA256_RE.fullmatch(manifest_sha) is not None,
        "manifest SHA-256 required",
    )
    require(cell_root.is_dir(), "staging cell must already exist")
    require(not cell_root.is_symlink(), "staging cell symlink forbidden")
    return {
        "schema_version": evidence.ROLE_SCHEMAS[role],
        "state": "PASS",
        "mode": "production",
        "synthetic_test_only": False,
        "fixture_only": False,
        "run_key": run_key,
        "ordinal": ordinal,
        "launch_manifest_sha256": manifest_sha,
        "source_evidence": relative_file_ref(
            source_evidence, cell_root, f"{role} source evidence"
        ),
        **FALSE_ELIGIBILITY,
    }


def _produce_receipt(
    *,
    role: str,
    cell_root: Path,
    source_evidence: Path,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
    fields: Mapping[str, Any],
) -> dict[str, Any]:
    reserved = {
        "schema_version",
        "state",
        "mode",
        "synthetic_test_only",
        "fixture_only",
        "run_key",
        "ordinal",
        "launch_manifest_sha256",
        "source_evidence",
        *FALSE_ELIGIBILITY,
    }
    require(not (set(fields) & reserved), f"{role}: reserved receipt field supplied")
    receipt = {
        **_receipt_common(
            role=role,
            cell_root=cell_root,
            source_evidence=source_evidence,
            run_key=run_key,
            ordinal=ordinal,
            manifest_sha=manifest_sha,
        ),
        **dict(fields),
    }
    try:
        evidence.validate_role(
            role,
            receipt,
            mode="production",
            run_key=run_key,
            ordinal=ordinal,
            manifest_sha=manifest_sha,
        )
    except evidence.EvidenceError as exc:
        raise ProductionError(str(exc)) from exc
    target = cell_root.resolve() / "receipts" / f"{role}.json"
    require(not target.exists(), f"{role}: refusing to overwrite receipt")
    atomic_json_exclusive(target, receipt)
    return relative_file_ref(target, cell_root.resolve(), f"{role} receipt")


def produce_command_receipt(
    cell_root: Path,
    *,
    source_evidence: Path,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
    returncode: int,
    adapter_invoked: bool,
    timing_generated: bool,
) -> dict[str, Any]:
    return _produce_receipt(
        role="command",
        cell_root=cell_root,
        source_evidence=source_evidence,
        run_key=run_key,
        ordinal=ordinal,
        manifest_sha=manifest_sha,
        fields={
            "returncode": returncode,
            "adapter_invoked": adapter_invoked,
            "timing_generated": timing_generated,
        },
    )


def produce_adapter_receipt(
    cell_root: Path,
    *,
    source_evidence: Path,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
    adapter_invoked: bool,
) -> dict[str, Any]:
    return _produce_receipt(
        role="adapter",
        cell_root=cell_root,
        source_evidence=source_evidence,
        run_key=run_key,
        ordinal=ordinal,
        manifest_sha=manifest_sha,
        fields={"adapter_invoked": adapter_invoked},
    )


def produce_p31_receipt(
    cell_root: Path,
    *,
    source_evidence: Path,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
    resource_validation_pass: bool,
    timing_generated: bool,
) -> dict[str, Any]:
    return _produce_receipt(
        role="p31",
        cell_root=cell_root,
        source_evidence=source_evidence,
        run_key=run_key,
        ordinal=ordinal,
        manifest_sha=manifest_sha,
        fields={
            "resource_validation_pass": resource_validation_pass,
            "timing_generated": timing_generated,
        },
    )


def produce_correctness_receipt(
    cell_root: Path,
    *,
    source_evidence: Path,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
    mismatch_count: int,
) -> dict[str, Any]:
    return _produce_receipt(
        role="correctness",
        cell_root=cell_root,
        source_evidence=source_evidence,
        run_key=run_key,
        ordinal=ordinal,
        manifest_sha=manifest_sha,
        fields={"mismatch_count": mismatch_count},
    )


def produce_fairness_receipt(
    cell_root: Path,
    *,
    source_evidence: Path,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
    fairness_pass: bool,
    strict_serial: bool,
) -> dict[str, Any]:
    return _produce_receipt(
        role="fairness",
        cell_root=cell_root,
        source_evidence=source_evidence,
        run_key=run_key,
        ordinal=ordinal,
        manifest_sha=manifest_sha,
        fields={"fairness_pass": fairness_pass, "strict_serial": strict_serial},
    )


def produce_cgroup_receipt(
    cell_root: Path,
    *,
    source_evidence: Path,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
    allocation_pass: bool,
    cpuset: str,
) -> dict[str, Any]:
    return _produce_receipt(
        role="cgroup",
        cell_root=cell_root,
        source_evidence=source_evidence,
        run_key=run_key,
        ordinal=ordinal,
        manifest_sha=manifest_sha,
        fields={"allocation_pass": allocation_pass, "cpuset": cpuset},
    )


def produce_cleanup_receipt(
    cell_root: Path,
    *,
    source_evidence: Path,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
    cleanup_pass: bool,
    residual_processes: int,
) -> dict[str, Any]:
    return _produce_receipt(
        role="cleanup",
        cell_root=cell_root,
        source_evidence=source_evidence,
        run_key=run_key,
        ordinal=ordinal,
        manifest_sha=manifest_sha,
        fields={
            "cleanup_pass": cleanup_pass,
            "residual_processes": residual_processes,
        },
    )


def validate_production_receipt(
    cell_root: Path,
    role: str,
    *,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
) -> dict[str, Any]:
    receipt_path = cell_root / "receipts" / f"{role}.json"
    receipt = evidence.load_json(receipt_path, f"{role} receipt")
    require(receipt.get("fixture_only") is False, f"{role}: fixture receipt forbidden")
    resolve_source_ref(
        cell_root, receipt.get("source_evidence"), f"{role} source evidence"
    )
    try:
        evidence.validate_role(
            role,
            receipt,
            mode="production",
            run_key=run_key,
            ordinal=ordinal,
            manifest_sha=manifest_sha,
        )
    except evidence.EvidenceError as exc:
        raise ProductionError(str(exc)) from exc
    return receipt


def validate_production_result(
    cell_root: Path,
    *,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
) -> Path:
    result_path = cell_root / "validated-result.json"
    result = evidence.load_json(result_path, "production cell result")
    require(
        result.get("schema_version") == "cidr-e01-production-cell-result-v1",
        "production result schema drift",
    )
    require(result.get("state") == "PASS", "production result must PASS")
    require(result.get("mode") == "production", "production result mode drift")
    require(result.get("synthetic_test_only") is False, "synthetic result forbidden")
    require(result.get("adapter_invoked") is True, "adapter invocation required")
    require(result.get("timing_generated") is True, "timing marker required")
    require(result.get("run_key") == run_key, "production result run drift")
    require(result.get("ordinal") == ordinal, "production result ordinal drift")
    require(
        result.get("launch_manifest_sha256") == manifest_sha,
        "production result manifest drift",
    )
    false_eligibility(result, "production result")
    return result_path


def finalize_production_cell(
    staging_cell: Path,
    final_cell: Path,
    *,
    expected_run: Mapping[str, Any],
    manifest_sha: str,
) -> dict[str, Any]:
    """Validate seven receipts and atomically rename a complete cell.

    Failed staging roots are preserved.  A failed validation removes only a
    just-created ``CELL-DONE.json`` and never deletes source evidence.
    """

    require(staging_cell.is_dir(), "staging cell directory missing")
    require(not staging_cell.is_symlink(), "staging cell symlink forbidden")
    require(not final_cell.exists(), "final cell already exists")
    require(
        staging_cell.resolve().parent == final_cell.resolve().parent,
        "staging/final cells must share a parent for atomic rename",
    )
    require(
        staging_cell.name.startswith(f".{final_cell.name}.tmp-"),
        "staging cell name does not match final cell",
    )
    failed = [name for name in FAILED_MARKERS if (staging_cell / name).exists()]
    require(not failed, f"failed staging root must be preserved: {failed}")
    done_path = staging_cell / "CELL-DONE.json"
    require(not done_path.exists(), "CELL-DONE already exists")
    run_key = expected_run.get("run_key")
    ordinal = expected_run.get("ordinal")
    require(run_key in scheduler.RUN_KEYS, "expected run key unsupported")
    require(
        ordinal == scheduler.RUN_KEYS.index(run_key) + 1,
        "expected ordinal/run key drift",
    )
    result_path = validate_production_result(
        staging_cell,
        run_key=run_key,
        ordinal=ordinal,
        manifest_sha=manifest_sha,
    )
    refs: dict[str, dict[str, Any]] = {}
    for role in evidence.ROLES:
        validate_production_receipt(
            staging_cell,
            role,
            run_key=run_key,
            ordinal=ordinal,
            manifest_sha=manifest_sha,
        )
        refs[role] = relative_file_ref(
            staging_cell / "receipts" / f"{role}.json",
            staging_cell,
            f"{role} receipt",
        )
    done = {
        "schema_version": "cidr-e01-cell-done-v2",
        "state": "PASS",
        "mode": "production",
        "synthetic_test_only": False,
        "adapter_invoked": True,
        "timing_generated": True,
        "run_key": run_key,
        "ordinal": ordinal,
        "launch_manifest_sha256": manifest_sha,
        "result_sha256": sha256_file(result_path),
        "receipts": refs,
        **FALSE_ELIGIBILITY,
    }
    wrote_done = False
    try:
        atomic_json_exclusive(done_path, done)
        wrote_done = True
        validation = evidence.validate_cell_evidence(
            staging_cell,
            expected_run=expected_run,
            manifest_sha=manifest_sha,
            expected_mode="production",
        )
        fsync_directory(staging_cell)
        staging_cell.rename(final_cell)
        fsync_directory(final_cell.parent)
        return validation
    except BaseException:
        if wrote_done and done_path.exists() and staging_cell.exists():
            done_path.unlink()
        raise


def _load_command_plan(path: Path) -> tuple[dict[str, Any], str]:
    require(path.is_file(), f"production command plan missing: {path}")
    require(not path.is_symlink(), "production command plan symlink forbidden")
    try:
        value = command_plan.validate_command_plan(path.resolve(), path.resolve())
    except command_plan.PlanError as exc:
        raise ProductionError(str(exc)) from exc
    return value, sha256_file(path.resolve())


def inspect_resume_root(
    result_root: Path,
    *,
    manifest: Mapping[str, Any],
    manifest_sha: str,
    plan: Mapping[str, Any],
    plan_sha: str,
) -> dict[str, Any]:
    root = result_root.resolve()
    if not root.exists():
        return {"state": "NEW", "completed_cells": 0}
    require(root.is_dir(), "existing production root is not a directory")
    require(not root.is_symlink(), "production root symlink forbidden")
    failed = [name for name in FAILED_MARKERS if (root / name).exists()]
    require(not failed, f"failed production root is immutable/preserved: {failed}")
    start_path = root / "MATRIX-START.json"
    try:
        start = evidence.load_json(start_path, "production MATRIX-START")
    except evidence.EvidenceError as exc:
        raise ProductionError(str(exc)) from exc
    require(
        start.get("schema_version") == MATRIX_START_SCHEMA,
        "production MATRIX-START schema drift",
    )
    require(start.get("state") == "PASS", "production MATRIX-START must PASS")
    require(start.get("mode") == "production", "production MATRIX-START mode drift")
    require(start.get("synthetic_test_only") is False, "synthetic resume root forbidden")
    require(start.get("strict_serial") is True, "resume root is not STRICT_SERIAL")
    require(
        start.get("launch_manifest_sha256") == manifest_sha,
        "resume manifest SHA drift",
    )
    require(
        start.get("production_command_plan_sha256") == plan_sha,
        "resume command-plan SHA drift",
    )
    false_eligibility(start, "production MATRIX-START")
    cells_root = root / "cells"
    require(cells_root.is_dir(), "resume root cells directory missing")
    require(not cells_root.is_symlink(), "resume cells symlink forbidden")
    expected_by_path = {
        Path(cell["cell_root"]).resolve(): row
        for cell, row in zip(plan["cells"], manifest["runs"])
    }
    require(
        all(path.parent == cells_root for path in expected_by_path),
        "plan cell roots do not match resume root",
    )
    expected_names = {path.name for path in expected_by_path}
    unknown = sorted(item.name for item in cells_root.iterdir() if item.name not in expected_names)
    require(not unknown, f"resume cells contain unknown/incomplete entries: {unknown}")
    completed = 0
    for path, row in expected_by_path.items():
        if not path.exists():
            continue
        require(path.is_dir(), f"{row['run_key']}: completed cell is not a directory")
        try:
            evidence.validate_cell_evidence(
                path,
                expected_run=row,
                manifest_sha=manifest_sha,
                expected_mode="production",
            )
        except evidence.EvidenceError as exc:
            raise ProductionError(str(exc)) from exc
        validate_production_receipt(
            path,
            "cleanup",
            run_key=row["run_key"],
            ordinal=row["ordinal"],
            manifest_sha=manifest_sha,
        )
        completed += 1
    matrix_done = root / "MATRIX-DONE.json"
    if matrix_done.exists():
        require(completed == 21, "premature production MATRIX-DONE")
    return {"state": "RESUME", "completed_cells": completed}


def production_preflight(
    manifest_path: Path,
    command_plan_path: Path,
    result_root: Path,
) -> dict[str, Any]:
    try:
        manifest, manifest_sha = scheduler.validate_manifest(manifest_path)
    except scheduler.ContractError as exc:
        raise ProductionError(str(exc)) from exc
    plan, plan_sha = _load_command_plan(command_plan_path)
    root = result_root.resolve()
    require(
        Path(plan.get("campaign_root", "")).resolve() == root,
        "result root/command plan campaign root drift",
    )
    require(
        plan.get("formal_manifest", {}).get("sha256") == manifest_sha,
        "command plan/formal manifest SHA drift",
    )
    resume = inspect_resume_root(
        root,
        manifest=manifest,
        manifest_sha=manifest_sha,
        plan=plan,
        plan_sha=plan_sha,
    )
    blockers: list[str] = []
    if plan.get("execution_state") != READY_EXECUTION_STATE:
        blockers.append(
            f"command_plan.execution_state={plan.get('execution_state')}"
        )
    binding_state = plan.get("admission_binding", {}).get(
        "production_executor_state"
    )
    if binding_state != READY_EXECUTION_STATE:
        blockers.append(f"admission.production_executor_state={binding_state}")
    identity_states: set[str] = set()
    for cell in plan["cells"]:
        receipt_path = Path(
            cell["adapter"]["artifact_identity_receipt"]["path"]
        )
        identity = scheduler.load_json(
            receipt_path, f"{cell['run_key']} artifact identity"
        )
        identity_states.add(str(identity.get("execution_state")))
    if identity_states != {READY_EXECUTION_STATE}:
        blockers.append(
            "adapter_identity.execution_state="
            + ",".join(sorted(identity_states))
        )
    return {
        "schema_version": "cidr-e01-production-preflight-v1",
        "state": "BLOCKED" if blockers else "PASS",
        "orchestration_only": True,
        "adapter_invoked": False,
        "timing_generated": False,
        "launch_manifest_sha256": manifest_sha,
        "production_command_plan_sha256": plan_sha,
        "result_root": str(root),
        "resume": resume,
        "blockers": blockers,
        **FALSE_ELIGIBILITY,
    }


def execute_production_plan(
    manifest_path: Path,
    command_plan_path: Path,
    result_root: Path,
) -> dict[str, Any]:
    preflight = production_preflight(
        manifest_path, command_plan_path, result_root
    )
    if preflight["blockers"]:
        raise ProductionBlocked(
            "production pre-root blockers: " + "; ".join(preflight["blockers"])
        )
    raise ProductionBlocked(
        "production orchestration interface only: adapter/process invocation "
        "is intentionally disabled before campaign-root creation"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--command-plan", type=Path, required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    args = parser.parse_args()
    execute_production_plan(args.manifest, args.command_plan, args.result_root)


if __name__ == "__main__":
    try:
        main()
    except (ProductionError, OSError, ValueError) as exc:
        print(f"E01 PRODUCTION EXECUTOR BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(1)
