#!/usr/bin/env python3
"""Seal real E01 admission evidence without arming bridge or formal timing."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import build_e01_fresh_asset_admission as assets
import run_e01_incremental_matrix as matrix


SCHEMA = "cidr-e01-incremental-admission-bundle-v1"
FALSE_ELIGIBILITY = matrix.FALSE_ELIGIBILITY
MIN_DATA_FREE_BYTES = 200 * 1024**3
MIN_MEM_AVAILABLE_KIB = 400 * 1024**2


class GateSealError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GateSealError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_ref(path: Path, label: str) -> dict[str, Any]:
    path = path.resolve()
    require(path.is_file() and not path.is_symlink(), f"{label}: regular file required")
    require(0 < path.stat().st_size <= assets.legacy.MAX_SMALL_FILE_BYTES, f"{label}: size invalid")
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def load(path: Path, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ref = file_ref(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateSealError(f"{label}: invalid JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value, ref


def false_eligibility(value: Mapping[str, Any], label: str) -> None:
    for key in FALSE_ELIGIBILITY:
        if key in value:
            require(value.get(key) is False, f"{label}: {key} must be false")


def iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def git_state(repo: Path) -> dict[str, Any]:
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain=v1"], text=True
    ).splitlines()
    require(len(head) == 40 and not status, "repository must be clean")
    return {"root": str(repo.resolve()), "head": head, "clean": True}


def resources(repo: Path) -> dict[str, Any]:
    data = os.statvfs("/data")
    data_free = data.f_bavail * data.f_frsize
    mem_available = 0
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            mem_available = int(line.split()[1])
            break
    active_builders = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if command in {"cargo", "rustc"}:
            active_builders.append({"pid": int(entry.name), "comm": command})
    require(data_free >= MIN_DATA_FREE_BYTES, "data retention gate failed")
    require(mem_available >= MIN_MEM_AVAILABLE_KIB, "MemAvailable gate failed")
    require(not active_builders, "cargo/rustc process active")
    return {
        "state": "PASS",
        "policy": "f1-data-retention-200gib-v1",
        "data_free_bytes": data_free,
        "data_free_min_bytes": MIN_DATA_FREE_BYTES,
        "mem_available_kib": mem_available,
        "mem_available_min_kib": MIN_MEM_AVAILABLE_KIB,
        "active_builders": active_builders,
        "repo": git_state(repo),
        "requires_revalidation_before_bridge": True,
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    plan, plan_ref = load(args.asset_plan, "asset admission plan")
    inventory, inventory_ref = load(args.asset_inventory, "fresh asset inventory")
    require(plan.get("schema_version") == assets.PLAN_SCHEMA, "asset plan schema drift")
    require(plan.get("state") == "READY_FOR_ADMISSION_GATES", "asset plan is not ready")
    require(inventory.get("schema_version") == assets.INVENTORY_SCHEMA, "asset inventory schema drift")
    require(inventory.get("state") == "PASS_ASSETS_ONLY", "fresh assets did not PASS")
    false_eligibility(plan, "asset plan")
    false_eligibility(inventory, "asset inventory")

    p03, p03_ref = load(args.p03_binding, "P03 binding")
    require(p03.get("schema_version") == "p02b-clean-ready-binding-v2", "P03 schema drift")
    require(p03.get("state") == "PASS", "P03 did not PASS")
    require(p03.get("required_consecutive_samples") == 5, "P03 five samples required")
    require(p03.get("observed_consecutive_samples") >= 5, "P03 observed sample drift")

    sentinel, sentinel_ref = load(args.sentinel_result, "P02B sentinel")
    require(sentinel.get("schema_version") == "p02b-sf10-sentinel-result-v2", "P02B schema drift")
    require(sentinel.get("state") == "PASS", "P02B did not PASS")
    stability = sentinel.get("stability")
    require(type(stability) is dict and stability.get("state") == "PASS", "stability did not PASS")
    runs = stability.get("runs")
    require(type(runs) is list and len(runs) == 3, "exactly three P02B runs required")
    require(all(row.get("overflow_count") == 0 for row in runs), "P02B overflow detected")

    validations = {}
    for consumer, path in (("P10", args.p10_validation), ("P20", args.p20_validation)):
        value, ref = load(path, f"{consumer} P02B validation")
        require(value.get("state") == "PASS", f"{consumer} P02B validation failed")
        require(value.get("consumer") == consumer, f"{consumer} validation drift")
        require(value.get("formal_required") is True and value.get("fixture_only") is False, f"{consumer} validation is not real")
        require(value.get("sentinel_result_sha256") == sentinel_ref["sha256"], f"{consumer} sentinel SHA drift")
        validations[consumer] = ref

    lease, lease_ref = load(args.lease, "batch lease")
    marker, marker_ref = load(args.lease_marker, "lease marker")
    require(lease.get("schema_version") == "cidr-batch-lease-v2", "lease schema drift")
    require(lease.get("classification", {}).get("performance_eligible") is False, "lease eligibility drift")
    require(lease.get("identity", {}).get("repo", {}).get("clean_at_issue") is True, "lease repo was dirty")
    require(iso(lease["expires_at_utc"]) > dt.datetime.now(dt.timezone.utc), "lease expired")
    require(marker.get("schema_version") == "cidr-batch-lease-marker-v2", "lease marker schema drift")
    require(marker.get("state") == "PASS", "lease marker did not PASS")
    require(marker.get("lease_sha256") == lease_ref["sha256"], "lease marker SHA drift")
    lease_validations = {}
    for consumer, path in (("P10", args.p10_lease_validation), ("P20", args.p20_lease_validation)):
        value, ref = load(path, f"{consumer} lease validation")
        require(value.get("schema_version") == "cidr-batch-lease-admission-v2", "lease validation schema drift")
        require(value.get("state") == "PASS" and value.get("consumer") == consumer, "lease validation failed")
        require(value.get("lease_sha256") == lease_ref["sha256"], "lease validation SHA drift")
        lease_validations[consumer] = ref

    repo = Path(args.repo_root).resolve()
    resource = resources(repo)
    require(resource["repo"]["head"] == lease["identity"]["repo"]["head"], "resource/lease HEAD drift")
    require(lease["identity"]["binary"]["sha256"] == inventory["binary"]["sha256"], "lease/binary SHA drift")
    return {
        "schema_version": SCHEMA,
        "state": "PASS_ADMISSION_ONLY",
        "synthetic_test_only": False,
        "fixture_only": False,
        "strict_serial": True,
        "asset_plan": plan_ref,
        "asset_inventory": inventory_ref,
        "p03": p03_ref,
        "p02b": {
            "sentinel": sentinel_ref,
            "validations": validations,
            "run_count": 3,
            "gate_contract_sha256": sentinel["gate_contract"]["contract_sha256"],
        },
        "lease": {
            "receipt": lease_ref,
            "marker": marker_ref,
            "validations": lease_validations,
            "expires_at_utc": lease["expires_at_utc"],
        },
        "resource_gate": resource,
        "backend_ready": False,
        "bridge_canary_armed": False,
        "blockers": [
            "receipt-bound production phase executor absent",
            "mutable clone lifecycle dry-run absent",
            "per-cell resource/P31/finalize/cleanup command plan absent",
        ],
        "timing_generated": False,
        **FALSE_ELIGIBILITY,
    }


def atomic(path: Path, value: Mapping[str, Any]) -> None:
    require(not path.exists(), f"refusing to overwrite: {path}")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-plan", type=Path, required=True)
    parser.add_argument("--asset-inventory", type=Path, required=True)
    parser.add_argument("--p03-binding", type=Path, required=True)
    parser.add_argument("--sentinel-result", type=Path, required=True)
    parser.add_argument("--p10-validation", type=Path, required=True)
    parser.add_argument("--p20-validation", type=Path, required=True)
    parser.add_argument("--lease", type=Path, required=True)
    parser.add_argument("--lease-marker", type=Path, required=True)
    parser.add_argument("--p10-lease-validation", type=Path, required=True)
    parser.add_argument("--p20-lease-validation", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        value = build(args)
        atomic(args.output.resolve(), value)
        print(json.dumps({"state": value["state"], "output": str(args.output.resolve())}, sort_keys=True))
        return 0
    except (GateSealError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
