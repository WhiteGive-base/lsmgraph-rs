#!/usr/bin/env python3
"""Validate E01 production command spec and frozen admission bindings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import build_e01_formal_manifest as manifest_builder
import run_e01_formal_matrix as scheduler
import validate_e01_adapter_artifact_identity as artifact_identity


SCHEMA_VERSION = "cidr-e01-production-command-spec-v1"
EXECUTOR_STATE = "NOT_IMPLEMENTED"
ALLOWED_ENV = {"LC_ALL", "LANG", "TZ", "RUST_BACKTRACE"}
FALSE_ELIGIBILITY = manifest_builder.FALSE_ELIGIBILITY
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SpecError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SpecError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def false_eligibility(value: Mapping[str, Any], label: str) -> None:
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{label}: {key} must be exactly false")


def safe_relative(value: Any, label: str) -> PurePosixPath:
    require(type(value) is str and value, f"{label}: relative path required")
    require("\\" not in value and not value.startswith("/") and not re.match(r"^[A-Za-z]:", value), f"{label}: absolute/backslash path forbidden")
    path = PurePosixPath(value)
    require(path.parts and all(part not in ("", ".", "..") for part in path.parts), f"{label}: traversal forbidden")
    return path


def small_attested_file(path_value: Any, label: str) -> Path:
    require(type(path_value) is str and path_value, f"{label}: path required")
    path = Path(path_value)
    require(path.is_absolute(), f"{label}: absolute path required")
    require(path.is_file(), f"{label}: file missing")
    require(not path.is_symlink(), f"{label}: symlink forbidden")
    require(path.stat().st_size <= manifest_builder.MAX_RECEIPT_BYTES, f"{label}: file too large")
    return path.resolve()


def expected_admission_binding(
    manifest: Mapping[str, Any],
    *,
    manifest_sha: str,
    evidence_schema_sha: str,
) -> dict[str, Any]:
    admission = manifest["admission"]
    return {
        "formal_manifest_sha256": manifest_sha,
        "p03_clean_ready_sha256": admission["p03_clean_ready"]["sha256"],
        "p02b_gate_sha256": admission["p02b_gate"]["sha256"],
        "batch_lease_sha256": admission["batch_lease"]["sha256"],
        "lease_marker_sha256": admission["lease_marker"]["sha256"],
        "cell_evidence_schema_sha256": evidence_schema_sha,
        "fresh_resource_gate_required": True,
        "production_executor_state": EXECUTOR_STATE,
        "synthetic_test_only": False,
        "fixture_only": False,
        **FALSE_ELIGIBILITY,
    }


def validate_production_spec(
    spec_path: Path,
    *,
    manifest_path: Path,
    evidence_schema_path: Path,
    contract_schema_path: Path,
) -> dict[str, Any]:
    contract_schema = manifest_builder.load_json(
        contract_schema_path.resolve(), "production command spec schema"
    )
    require(
        contract_schema.get("title")
        == "CIDR E01 production command specification and admission binding",
        "production command spec schema drift",
    )
    try:
        manifest, manifest_sha = scheduler.validate_manifest(manifest_path)
    except scheduler.ContractError as exc:
        raise SpecError(str(exc)) from exc
    evidence_schema = small_attested_file(
        str(evidence_schema_path.resolve()), "cell evidence schema"
    )
    evidence_schema_sha = sha256_file(evidence_schema)
    spec = manifest_builder.load_json(spec_path.resolve(), "production command spec")
    required = {
        "schema_version",
        "state",
        "campaign_id",
        "campaign_root",
        "environment",
        "p31_wrapper",
        "cgroup_wrapper",
        "systems",
        "admission_binding",
        "classification",
    }
    require(set(spec) == required, "production command spec keys drift")
    require(spec.get("schema_version") == SCHEMA_VERSION, "production command spec schema drift")
    require(spec.get("state") == "READY", "production command spec must be READY")
    require(spec.get("campaign_id") == manifest["campaign_id"], "campaign id drift")
    false_eligibility(spec.get("classification", {}), "production command spec")
    root_value = spec.get("campaign_root")
    require(type(root_value) is str and Path(root_value).is_absolute(), "absolute campaign root required")
    require(not Path(root_value).resolve().exists(), "campaign root must not exist")
    environment = spec.get("environment")
    require(type(environment) is dict and set(environment) <= ALLOWED_ENV, "environment allowlist drift")
    require(environment.get("LC_ALL") == "C" and environment.get("TZ") == "UTC", "LC_ALL=C and TZ=UTC required")
    require(all(type(value) is str for value in environment.values()), "environment values must be strings")
    small_attested_file(spec.get("p31_wrapper"), "P31 wrapper")
    small_attested_file(spec.get("cgroup_wrapper"), "cgroup wrapper")
    systems = spec.get("systems")
    require(type(systems) is list and len(systems) == 7, "exactly seven systems required")
    require(
        [row.get("system_key") for row in systems if type(row) is dict]
        == [key for key, _ in manifest_builder.SYSTEMS],
        "system order drift",
    )
    for row in systems:
        key = row["system_key"]
        require(
            set(row)
            == {
                "system_key",
                "cwd_relative",
                "adapter_entry",
                "artifact_identity_receipt",
                "adapter_kind",
                "argv",
            },
            f"{key}: system command keys drift",
        )
        safe_relative(row.get("cwd_relative"), f"{key}.cwd_relative")
        entry = small_attested_file(row.get("adapter_entry"), f"{key} adapter entry")
        require(row.get("adapter_kind") in ("binary", "python-script"), f"{key}: adapter kind drift")
        identity_path = small_attested_file(
            row.get("artifact_identity_receipt"), f"{key} artifact identity receipt"
        )
        try:
            artifact_identity.validate_identity_receipt(
                identity_path,
                manifest=manifest,
                system_key=key,
                expected_entry=entry,
                expected_kind=row["adapter_kind"],
            )
        except (artifact_identity.IdentityError, manifest_builder.BuildError) as exc:
            raise SpecError(str(exc)) from exc
        argv = row.get("argv")
        require(type(argv) is list and argv and all(type(item) is str and item for item in argv), f"{key}: explicit argv array required")
        require(argv[0] == str(Path(row["adapter_entry"]).resolve()), f"{key}: argv[0] must be attested adapter entry")
    binding = spec.get("admission_binding")
    require(type(binding) is dict, "admission binding object required")
    expected = expected_admission_binding(
        manifest,
        manifest_sha=manifest_sha,
        evidence_schema_sha=evidence_schema_sha,
    )
    require(binding == expected, "admission binding drift")
    false_eligibility(binding, "admission binding")
    return {
        "schema_version": "cidr-e01-production-spec-validation-v1",
        "state": "PASS",
        "execution_state": EXECUTOR_STATE,
        "campaign_id": manifest["campaign_id"],
        "formal_manifest_sha256": manifest_sha,
        "cell_evidence_schema_sha256": evidence_schema_sha,
        "admission_binding": binding,
        "synthetic_test_only": False,
        "fixture_only": False,
        **FALSE_ELIGIBILITY,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cell-evidence-schema", type=Path, required=True)
    parser.add_argument("--contract-schema", type=Path, required=True)
    args = parser.parse_args()
    result = validate_production_spec(
        args.spec,
        manifest_path=args.manifest,
        evidence_schema_path=args.cell_evidence_schema,
        contract_schema_path=args.contract_schema,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (SpecError, manifest_builder.BuildError, OSError, ValueError) as exc:
        print(f"E01 PRODUCTION SPEC FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
