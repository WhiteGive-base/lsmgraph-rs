#!/usr/bin/env python3
"""Fail-closed validator for E01 CELL-DONE v2 evidence bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


ROLES = ("command", "adapter", "p31", "correctness", "fairness", "cgroup", "cleanup")
ROLE_SCHEMAS = {
    role: f"cidr-e01-{role}-receipt-v1" for role in ROLES
}
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RECEIPT_BYTES = 16 * 1024 * 1024


class EvidenceError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def false_eligibility(value: Mapping[str, Any], label: str) -> None:
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{label}: {key} must be exactly false")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    require(path.is_file(), f"{label}: missing file")
    require(not path.is_symlink(), f"{label}: symlink forbidden")
    require(path.stat().st_size <= MAX_RECEIPT_BYTES, f"{label}: file too large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"{label}: invalid JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value


def resolve_receipt(
    cell_root: Path, descriptor: Any, role: str
) -> tuple[Path, dict[str, Any]]:
    require(type(descriptor) is dict, f"{role}: reference object required")
    require(set(descriptor) == {"path", "sha256", "size_bytes"}, f"{role}: reference keys drift")
    raw = descriptor.get("path")
    require(type(raw) is str and raw, f"{role}: relative path required")
    require("\\" not in raw and not raw.startswith("/") and not re.match(r"^[A-Za-z]:", raw), f"{role}: absolute/backslash path forbidden")
    parts = PurePosixPath(raw).parts
    require(parts and all(part not in ("", ".", "..") for part in parts), f"{role}: path traversal forbidden")
    candidate = cell_root.joinpath(*parts)
    require(candidate.is_file(), f"{role}: receipt missing")
    require(not candidate.is_symlink(), f"{role}: receipt symlink forbidden")
    resolved_root = cell_root.resolve()
    resolved = candidate.resolve()
    try:
        inside = os.path.commonpath((str(resolved_root), str(resolved))) == str(resolved_root)
    except ValueError:
        inside = False
    require(inside, f"{role}: receipt resolves outside cell")
    require(type(descriptor.get("size_bytes")) is int and descriptor["size_bytes"] > 0, f"{role}: positive size required")
    require(candidate.stat().st_size == descriptor["size_bytes"], f"{role}: size drift")
    expected_sha = descriptor.get("sha256")
    require(type(expected_sha) is str and SHA256_RE.fullmatch(expected_sha) is not None, f"{role}: bad SHA")
    require(sha256_file(candidate) == expected_sha, f"{role}: SHA drift")
    return candidate, load_json(candidate, f"{role} receipt")


def validate_role(
    role: str,
    receipt: Mapping[str, Any],
    *,
    mode: str,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
) -> None:
    label = f"{role} receipt"
    require(receipt.get("schema_version") == ROLE_SCHEMAS[role], f"{label}: schema drift")
    require(receipt.get("state") == "PASS", f"{label}: state must PASS")
    require(receipt.get("mode") == mode, f"{label}: mode drift")
    require(receipt.get("synthetic_test_only") is (mode == "synthetic"), f"{label}: synthetic marker drift")
    require(receipt.get("run_key") == run_key, f"{label}: run drift")
    require(receipt.get("ordinal") == ordinal, f"{label}: ordinal drift")
    require(receipt.get("launch_manifest_sha256") == manifest_sha, f"{label}: manifest SHA drift")
    false_eligibility(receipt, label)
    if role == "command":
        require(receipt.get("returncode") == 0, "command receipt: returncode must be zero")
        require(receipt.get("adapter_invoked") is (mode == "production"), "command receipt: adapter invocation drift")
        require(receipt.get("timing_generated") is (mode == "production"), "command receipt: timing marker drift")
    elif role == "adapter":
        require(receipt.get("adapter_invoked") is (mode == "production"), "adapter receipt: invocation marker drift")
    elif role == "p31":
        require(receipt.get("resource_validation_pass") is True, "P31 receipt: resource validation must PASS")
        require(receipt.get("timing_generated") is (mode == "production"), "P31 receipt: timing marker drift")
    elif role == "correctness":
        require(receipt.get("mismatch_count") == 0, "correctness receipt: mismatch_count must be zero")
    elif role == "fairness":
        require(receipt.get("fairness_pass") is True, "fairness receipt must PASS")
        require(receipt.get("strict_serial") is True, "fairness receipt must attest STRICT_SERIAL")
    elif role == "cgroup":
        require(receipt.get("allocation_pass") is True, "cgroup receipt: allocation must PASS")
        require(type(receipt.get("cpuset")) is str and receipt["cpuset"], "cgroup receipt: cpuset required")
    elif role == "cleanup":
        require(receipt.get("cleanup_pass") is True, "cleanup receipt must PASS")
        require(receipt.get("residual_processes") == 0, "cleanup receipt: residual processes remain")


def validate_cell_evidence(
    cell_root: Path,
    *,
    expected_run: Mapping[str, Any],
    manifest_sha: str,
    expected_mode: str,
) -> dict[str, Any]:
    require(expected_mode in ("synthetic", "production"), "expected mode unsupported")
    require(type(manifest_sha) is str and SHA256_RE.fullmatch(manifest_sha) is not None, "bad manifest SHA")
    root = cell_root.resolve()
    done_path = root / "CELL-DONE.json"
    done = load_json(done_path, "CELL-DONE")
    run_key = expected_run.get("run_key")
    ordinal = expected_run.get("ordinal")
    require(done.get("schema_version") == "cidr-e01-cell-done-v2", "CELL-DONE schema v2 required")
    require(done.get("state") == "PASS", "CELL-DONE state must PASS")
    require(done.get("mode") == expected_mode, "CELL-DONE mode drift")
    synthetic = expected_mode == "synthetic"
    require(done.get("synthetic_test_only") is synthetic, "CELL-DONE synthetic marker drift")
    require(done.get("adapter_invoked") is (not synthetic), "CELL-DONE adapter invocation drift")
    require(done.get("timing_generated") is (not synthetic), "CELL-DONE timing marker drift")
    require(done.get("run_key") == run_key, "CELL-DONE run drift")
    require(done.get("ordinal") == ordinal, "CELL-DONE ordinal drift")
    require(done.get("launch_manifest_sha256") == manifest_sha, "CELL-DONE manifest SHA drift")
    false_eligibility(done, "CELL-DONE")
    result_path = root / ("synthetic-result.json" if synthetic else "validated-result.json")
    require(result_path.is_file(), "cell result missing")
    require(done.get("result_sha256") == sha256_file(result_path), "CELL-DONE result SHA drift")
    result = load_json(result_path, "cell result")
    expected_result_schema = (
        "cidr-e01-synthetic-cell-result-v1"
        if synthetic
        else "cidr-e01-production-cell-result-v1"
    )
    require(result.get("schema_version") == expected_result_schema, "cell result schema drift")
    require(result.get("state") == "PASS", "cell result must PASS")
    require(result.get("mode") == expected_mode, "cell result mode drift")
    require(result.get("synthetic_test_only") is synthetic, "cell result synthetic marker drift")
    require(result.get("adapter_invoked") is (not synthetic), "cell result adapter invocation drift")
    require(result.get("timing_generated") is (not synthetic), "cell result timing marker drift")
    require(result.get("run_key") == run_key, "cell result run drift")
    require(result.get("ordinal") == ordinal, "cell result ordinal drift")
    require(result.get("launch_manifest_sha256") == manifest_sha, "cell result manifest SHA drift")
    false_eligibility(result, "cell result")
    refs = done.get("receipts")
    require(type(refs) is dict and set(refs) == set(ROLES), "CELL-DONE must reference exactly seven receipts")
    receipt_shas: dict[str, str] = {}
    for role in ROLES:
        _, receipt = resolve_receipt(root, refs[role], role)
        validate_role(
            role,
            receipt,
            mode=expected_mode,
            run_key=run_key,
            ordinal=ordinal,
            manifest_sha=manifest_sha,
        )
        receipt_shas[role] = refs[role]["sha256"]
    return {
        "schema_version": "cidr-e01-cell-evidence-validation-v1",
        "state": "PASS",
        "mode": expected_mode,
        "synthetic_test_only": synthetic,
        "run_key": run_key,
        "ordinal": ordinal,
        "launch_manifest_sha256": manifest_sha,
        "cell_done_sha256": sha256_file(done_path),
        "receipt_sha256": receipt_shas,
        **FALSE_ELIGIBILITY,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cell-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--mode", choices=("synthetic", "production"), required=True)
    args = parser.parse_args()
    import run_e01_formal_matrix as scheduler

    manifest, manifest_sha = scheduler.validate_manifest(args.manifest)
    rows = [row for row in manifest["runs"] if row.get("run_key") == args.run_key]
    require(len(rows) == 1, "run key not unique/present in manifest")
    result = validate_cell_evidence(
        args.cell_root,
        expected_run=rows[0],
        manifest_sha=manifest_sha,
        expected_mode=args.mode,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (EvidenceError, OSError, ValueError) as exc:
        print(f"E01 CELL EVIDENCE FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
