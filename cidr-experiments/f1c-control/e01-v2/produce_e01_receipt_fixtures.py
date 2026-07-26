#!/usr/bin/env python3
"""Atomically produce seven synthetic-only E01 receipt fixtures.

This module never invokes an adapter and never generates timing.  Production
mode is intentionally unavailable and fails before touching the output root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import build_e01_formal_manifest as manifest_builder
import validate_e01_cell_evidence as evidence


RUN_KEYS = tuple(
    f"{system}:r{repeat}"
    for system, _ in manifest_builder.SYSTEMS
    for repeat in manifest_builder.REPEATS
)
FALSE_ELIGIBILITY = manifest_builder.FALSE_ELIGIBILITY
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProducerError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProducerError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    require(len(payload) <= evidence.MAX_RECEIPT_BYTES, f"{path.name}: receipt too large")
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


def safe_relative(value: str, label: str) -> PurePosixPath:
    require(type(value) is str and value, f"{label}: relative path required")
    require("\\" not in value and not value.startswith("/") and not re.match(r"^[A-Za-z]:", value), f"{label}: absolute/backslash path forbidden")
    relative = PurePosixPath(value)
    require(relative.parts and all(part not in ("", ".", "..") for part in relative.parts), f"{label}: traversal forbidden")
    return relative


def relative_ref(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def build_receipts(
    *, run_key: str, ordinal: int, manifest_sha: str
) -> dict[str, dict[str, Any]]:
    require(run_key in RUN_KEYS, "run_key is not one of the fixed 21 cells")
    require(ordinal == RUN_KEYS.index(run_key) + 1, "ordinal/run_key drift")
    require(SHA256_RE.fullmatch(manifest_sha) is not None, "manifest SHA-256 required")
    common = {
        "state": "PASS",
        "mode": "synthetic",
        "synthetic_test_only": True,
        "fixture_only": True,
        "run_key": run_key,
        "ordinal": ordinal,
        "launch_manifest_sha256": manifest_sha,
        **FALSE_ELIGIBILITY,
    }
    return {
        "command": {
            "schema_version": "cidr-e01-command-receipt-v1",
            "returncode": 0,
            "adapter_invoked": False,
            "timing_generated": False,
            **common,
        },
        "adapter": {
            "schema_version": "cidr-e01-adapter-receipt-v1",
            "adapter_invoked": False,
            **common,
        },
        "p31": {
            "schema_version": "cidr-e01-p31-receipt-v1",
            "resource_validation_pass": True,
            "timing_generated": False,
            **common,
        },
        "correctness": {
            "schema_version": "cidr-e01-correctness-receipt-v1",
            "mismatch_count": 0,
            **common,
        },
        "fairness": {
            "schema_version": "cidr-e01-fairness-receipt-v1",
            "fairness_pass": True,
            "strict_serial": True,
            **common,
        },
        "cgroup": {
            "schema_version": "cidr-e01-cgroup-receipt-v1",
            "allocation_pass": True,
            "cpuset": "synthetic-none",
            **common,
        },
        "cleanup": {
            "schema_version": "cidr-e01-cleanup-receipt-v1",
            "cleanup_pass": True,
            "residual_processes": 0,
            **common,
        },
    }


def produce_fixture_receipts(
    cell_root: Path,
    *,
    run_key: str,
    ordinal: int,
    manifest_sha: str,
    fixture_only: bool,
    receipt_dir_relative: str = "receipts",
) -> dict[str, dict[str, Any]]:
    require(
        fixture_only,
        "production receipt producer NOT_IMPLEMENTED; refusing before output",
    )
    require(cell_root.is_dir(), "cell root must already exist")
    require(not cell_root.is_symlink(), "cell root symlink forbidden")
    resolved_root = cell_root.resolve()
    relative = safe_relative(receipt_dir_relative, "receipt directory")
    final = resolved_root.joinpath(*relative.parts)
    try:
        inside = os.path.commonpath((str(resolved_root), str(final.resolve()))) == str(resolved_root)
    except ValueError:
        inside = False
    require(inside, "receipt directory escapes cell root")
    require(not final.exists(), "receipt directory already exists; refusing overwrite")
    temporary = final.parent / f".{final.name}.tmp-{os.getpid()}"
    require(not temporary.exists(), "temporary receipt directory already exists")
    receipts = build_receipts(
        run_key=run_key, ordinal=ordinal, manifest_sha=manifest_sha
    )
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        for role in evidence.ROLES:
            atomic_json_exclusive(temporary / f"{role}.json", receipts[role])
        temporary.rename(final)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    refs = {
        role: relative_ref(final / f"{role}.json", resolved_root)
        for role in evidence.ROLES
    }
    require(
        all(ref["size_bytes"] <= evidence.MAX_RECEIPT_BYTES for ref in refs.values()),
        "receipt size limit exceeded",
    )
    return refs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cell-root", type=Path, required=True)
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--ordinal", type=int, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--receipt-dir", default="receipts")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture-only", action="store_true")
    mode.add_argument("--production", action="store_true")
    args = parser.parse_args()
    refs = produce_fixture_receipts(
        args.cell_root,
        run_key=args.run_key,
        ordinal=args.ordinal,
        manifest_sha=args.manifest_sha256,
        fixture_only=args.fixture_only and not args.production,
        receipt_dir_relative=args.receipt_dir,
    )
    print(
        json.dumps(
            {
                "state": "PASS",
                "mode": "synthetic",
                "synthetic_test_only": True,
                "fixture_only": True,
                "adapter_invoked": False,
                "timing_generated": False,
                "receipts": refs,
                **FALSE_ELIGIBILITY,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (ProducerError, OSError, ValueError) as exc:
        print(f"E01 RECEIPT FIXTURE PRODUCER FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
