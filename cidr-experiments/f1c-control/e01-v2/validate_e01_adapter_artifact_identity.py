#!/usr/bin/env python3
"""Validate one E01 adapter artifact identity receipt without execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

import build_e01_formal_manifest as manifest_builder
import run_e01_formal_matrix as scheduler


SCHEMA_VERSION = "cidr-e01-adapter-artifact-identity-v1"
PATH_POLICY = "canonical-absolute-existing-nonsymlink-small-v1"
FALSE_ELIGIBILITY = manifest_builder.FALSE_ELIGIBILITY


class IdentityError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise IdentityError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def false_eligibility(value: Mapping[str, Any], label: str) -> None:
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{label}: {key} must be exactly false")


def canonical_small_file(path_value: Any, label: str) -> Path:
    require(type(path_value) is str and path_value, f"{label}: path required")
    path = Path(path_value)
    require(path.is_absolute(), f"{label}: absolute path required")
    require(path.is_file(), f"{label}: file missing")
    require(not path.is_symlink(), f"{label}: symlink forbidden")
    resolved = path.resolve()
    require(str(path) == str(resolved), f"{label}: canonical path required")
    require(path.stat().st_size <= manifest_builder.MAX_RECEIPT_BYTES, f"{label}: file too large")
    return resolved


def validate_identity_receipt(
    receipt_path: Path,
    *,
    manifest: Mapping[str, Any],
    system_key: str,
    expected_entry: Path,
    expected_kind: str,
) -> dict[str, Any]:
    receipt_file = canonical_small_file(str(receipt_path), "artifact identity receipt")
    receipt = manifest_builder.load_json(receipt_file, "artifact identity receipt")
    required = {
        "schema_version",
        "state",
        "execution_state",
        "system_key",
        "adapter_kind",
        "adapter_entry",
        "path_resolution_policy",
        "engine_binary_sha256",
        "store_sha256",
        "harness_git_sha",
        "protocol_sha256",
        "synthetic_test_only",
        "fixture_only",
        *FALSE_ELIGIBILITY,
    }
    require(set(receipt) == required, "artifact identity receipt keys drift")
    require(receipt.get("schema_version") == SCHEMA_VERSION, "identity schema drift")
    require(receipt.get("state") == "PASS", "identity receipt must PASS")
    require(receipt.get("execution_state") == "NOT_IMPLEMENTED", "identity receipt cannot enable execution")
    require(receipt.get("system_key") == system_key, "identity system drift")
    require(receipt.get("adapter_kind") == expected_kind, "identity adapter kind drift")
    require(receipt.get("path_resolution_policy") == PATH_POLICY, "path resolution policy drift")
    require(receipt.get("synthetic_test_only") is False, "synthetic identity forbidden")
    require(receipt.get("fixture_only") is False, "fixture identity forbidden")
    false_eligibility(receipt, "artifact identity receipt")
    entry = receipt.get("adapter_entry")
    require(type(entry) is dict and set(entry) == {"path", "sha256", "size_bytes"}, "adapter entry reference drift")
    resolved_entry = canonical_small_file(entry.get("path"), "adapter entry")
    require(resolved_entry == expected_entry.resolve(), "adapter entry path drift")
    require(entry.get("size_bytes") == resolved_entry.stat().st_size, "adapter entry size drift")
    require(entry.get("sha256") == sha256_file(resolved_entry), "adapter entry SHA drift")
    systems = manifest.get("systems")
    rows = [row for row in systems if row.get("system_key") == system_key]
    require(len(rows) == 1, "formal manifest system identity not unique")
    system = rows[0]
    require(receipt.get("engine_binary_sha256") == system["binary_sha256"], "engine binary SHA drift")
    require(receipt.get("store_sha256") == system["store_sha256"], "store SHA drift")
    require(receipt.get("harness_git_sha") == manifest["protocol"]["harness_git_sha"], "harness git drift")
    require(receipt.get("protocol_sha256") == manifest["protocol_sha256"], "protocol SHA drift")
    return {
        "schema_version": "cidr-e01-adapter-artifact-identity-validation-v1",
        "state": "PASS",
        "execution_state": "NOT_IMPLEMENTED",
        "system_key": system_key,
        "identity_receipt": {
            "path": str(receipt_file),
            "sha256": sha256_file(receipt_file),
            "size_bytes": receipt_file.stat().st_size,
        },
        "adapter_entry": dict(entry),
        "synthetic_test_only": False,
        "fixture_only": False,
        **FALSE_ELIGIBILITY,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--system-key", required=True)
    parser.add_argument("--adapter-entry", type=Path, required=True)
    parser.add_argument("--adapter-kind", choices=("binary", "python-script"), required=True)
    args = parser.parse_args()
    try:
        manifest, _ = scheduler.validate_manifest(args.manifest)
    except scheduler.ContractError as exc:
        raise IdentityError(str(exc)) from exc
    result = validate_identity_receipt(
        args.receipt,
        manifest=manifest,
        system_key=args.system_key,
        expected_entry=args.adapter_entry,
        expected_kind=args.adapter_kind,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (IdentityError, manifest_builder.BuildError, OSError, ValueError) as exc:
        print(f"E01 ADAPTER IDENTITY FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
