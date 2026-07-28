#!/usr/bin/env python3
"""Bind fresh E01 light/store seals without arming or running formal timing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import inventory_e01_seml0_assets as legacy
import run_e01_incremental_matrix as matrix
import seal_e01_incremental_light_assets as light


INVENTORY_SCHEMA = "cidr-e01-fresh-asset-inventory-v2"
PLAN_SCHEMA = "cidr-e01-incremental-asset-admission-plan-v1"
STORE_SCHEMA = "cidr-e01-immutable-store-seal-v1"
FALSE_ELIGIBILITY = matrix.FALSE_ELIGIBILITY


class AdmissionError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AdmissionError(message)


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
        raise AdmissionError(f"{label}: cannot read JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value


def file_ref(path: Path, label: str) -> dict[str, Any]:
    path = path.resolve()
    require(path.is_file() and not path.is_symlink(), f"{label}: regular file required")
    require(path.stat().st_size <= legacy.MAX_SMALL_FILE_BYTES, f"{label}: file too large")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def false_eligibility(value: Mapping[str, Any], label: str) -> None:
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{label}: {key} must be false")


def validate_light(path: Path, expected_schema: str, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ref = file_ref(path, label)
    value = load_json(path, label)
    require(value.get("schema_version") == expected_schema, f"{label}: schema drift")
    require(value.get("state") == "PASS", f"{label}: PASS required")
    false_eligibility(value, label)
    return value, ref


def validate_store(
    path: Path,
    expected_variant: str,
    legacy_store: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    ref = file_ref(path, f"{expected_variant} store seal")
    value = load_json(path, f"{expected_variant} store seal")
    require(value.get("schema_version") == STORE_SCHEMA, f"{expected_variant}: schema drift")
    require(value.get("state") == "PASS", f"{expected_variant}: PASS required")
    require(value.get("variant") == expected_variant, f"{expected_variant}: variant drift")
    require(value.get("hash_method") == "sha256-tree-v1(relative-path,size,file-sha256)", f"{expected_variant}: hash method drift")
    require(value.get("source_modified") is False, f"{expected_variant}: source changed")
    require(value.get("formal_data_collected") is False, f"{expected_variant}: unexpected formal data")
    false_eligibility(value, f"{expected_variant} store seal")
    digests = {
        value.get("tree_sha256"),
        value.get("fresh_source_tree_sha256"),
        value.get("fresh_target_tree_sha256"),
    }
    require(len(digests) == 1 and None not in digests, f"{expected_variant}: tree SHA disagreement")
    tree_sha = next(iter(digests))
    require(tree_sha == legacy_store.get("candidate_tree_sha256"), f"{expected_variant}: legacy/fresh tree SHA drift")
    root = Path(str(value.get("immutable_root", "")))
    require(root.is_absolute() and root.is_dir(), f"{expected_variant}: immutable root missing")
    require(not root.is_symlink(), f"{expected_variant}: immutable root symlink forbidden")
    mode = stat.S_IMODE(root.stat().st_mode)
    require(not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH), f"{expected_variant}: immutable root writable")
    require(format(mode, "04o") == value.get("immutable_directory_mode"), f"{expected_variant}: root mode drift")
    return value, ref


def build(
    *,
    legacy_inventory_path: Path,
    adapter_identity_path: Path,
    binary_seal_path: Path,
    lineage_seal_path: Path,
    budg_store_seal_path: Path,
    naive_store_seal_path: Path,
    campaign_root: Path,
    p03_output_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    legacy_ref = file_ref(legacy_inventory_path, "legacy inventory")
    old = load_json(legacy_inventory_path, "legacy inventory")
    require(old.get("schema_version") == legacy.SCHEMA, "legacy inventory schema drift")
    adapter, adapter_ref = validate_light(adapter_identity_path, light.ADAPTER_SCHEMA, "adapter identity")
    binary, binary_ref = validate_light(binary_seal_path, light.BINARY_SCHEMA, "binary seal")
    lineage, lineage_ref = validate_light(lineage_seal_path, light.LINEAGE_SCHEMA, "lineage seal")
    require(adapter.get("fresh_identity_captured") is True, "fresh adapter identity required")
    require(adapter.get("path_group_or_world_writable") is False, "adapter path remains group/world writable")
    require(adapter.get("repo", {}).get("clean_at_capture") is True, "adapter repo was not clean")
    adapter_path = Path(adapter["adapter"]["path"])
    adapter_mode = stat.S_IMODE(adapter_path.stat().st_mode)
    require(adapter_mode == 0o755, "adapter mode must be exactly 0755")
    require(binary.get("content_hashed_now") is True, "fresh binary hash required")
    require(lineage.get("large_content_rehashed_now") is False, "large lineage rehash forbidden")
    require(lineage.get("lineage_only") is True, "lineage-only receipt required")
    require(binary["binary"]["sha256"] == old["engine_binary"]["candidate_sha256"], "binary SHA drift")
    require(adapter["adapter"]["sha256"] == old["adapter"]["entry"]["sha256"], "adapter SHA drift")
    old_stores = {item["variant"]: item for item in old["stores"]}
    budg, budg_ref = validate_store(budg_store_seal_path, "budg-b64", old_stores["budg-b64"])
    naive, naive_ref = validate_store(naive_store_seal_path, "naive", old_stores["naive"])
    require(campaign_root.is_absolute(), "campaign root must be absolute")
    require(not campaign_root.exists(), "campaign root must remain absent")
    require(p03_output_dir.is_absolute(), "P03 output directory must be absolute")
    require(not p03_output_dir.exists(), "P03 output directory must remain absent at plan build")
    repo_root = Path(adapter["repo"]["root"])
    monitor_path = repo_root / "cidr-experiments/runs/P03-CLEAN-WINDOW-MONITOR/monitor_clean_window.sh"
    batch_gate_path = repo_root / "cidr-experiments/runners/batch_gate_v2.py"
    p02b_runner_path = repo_root / "cidr-experiments/runners/p02b/run_sf10_sentinel.py"
    p02b_validator_path = repo_root / "cidr-experiments/runners/p02b/validate_sentinel_result.py"
    p02b_config_path = repo_root / "cidr-experiments/runners/p02b/configs/sf10-seml0-short-gate-v4-quantization-aware.json"
    monitor_ref = file_ref(monitor_path, "P03 monitor")
    batch_gate_ref = file_ref(batch_gate_path, "batch gate")
    p02b_runner_ref = file_ref(p02b_runner_path, "P02B runner")
    p02b_validator_ref = file_ref(p02b_validator_path, "P02B validator")
    p02b_config_ref = file_ref(p02b_config_path, "P02B config")

    stores = []
    for value, ref in ((budg, budg_ref), (naive, naive_ref)):
        stores.append(
            {
                "variant": value["variant"],
                "immutable_root": value["immutable_root"],
                "tree_sha256": value["tree_sha256"],
                "file_count": value["file_count"],
                "total_bytes": value["total_bytes"],
                "fresh_store_seal": ref,
                "fresh_immutable_seal": True,
                "tree_rehashed_now": False,
                "state": "PASS",
            }
        )
    inventory = {
        "schema_version": INVENTORY_SCHEMA,
        "state": "PASS_ASSETS_ONLY",
        "experiment_id": "E01",
        "scope": "bridge-canary-plus-seml0-naive-r1-r3",
        "legacy_inventory": legacy_ref,
        "adapter_identity": adapter_ref,
        "binary_file_seal": binary_ref,
        "dataset_trace_truth_lineage_seal": lineage_ref,
        "adapter": adapter["adapter"],
        "adapter_mode_octal": format(adapter_mode, "04o"),
        "adapter_mode_revalidate_before_each_cell": True,
        "binary": binary["binary"],
        "logical_dataset_id": lineage["logical_dataset_id"],
        "stores": stores,
        "checks": {
            "adapter_fresh_identity": "PASS",
            "adapter_repo_clean": "PASS",
            "adapter_path_not_group_or_world_writable": "PASS",
            "binary_fresh_file_hash": "PASS",
            "shared_lineage_receipt": "PASS",
            "budg_b64_immutable_store": "PASS",
            "naive_immutable_store": "PASS",
            "large_tree_rehashed_now": "NOT_PERFORMED",
        },
        "production_ready": False,
        "production_blocker": "fresh P03/P02B, exclusive lease, resource gate, and runtime backend receipts absent",
        **FALSE_ELIGIBILITY,
    }
    remaining = {
        "fresh_p03_receipt": None,
        "fresh_p02b_receipts": None,
        "exclusive_lease_receipt": None,
        "resource_gate_receipt": None,
        "runtime_backend_receipt": None,
    }
    p03_environment = {
        "ROOT": str(repo_root),
        "RUN_ID": p03_output_dir.name,
        "OUT_DIR": str(p03_output_dir),
        "GATE_MODE": "seml0",
        "SAMPLE_INTERVAL_SECONDS": "60",
        "MEASURE_SECONDS": "1",
        "READY_SAMPLES": "5",
        "DEVICE": "nvme1n1",
        "LOAD_MAX": "5",
        "CPU_IDLE_MIN_PCT": "95",
        "MEM_AVAILABLE_MIN_KIB": "419430400",
        "DATA_FREE_MIN_KIB": "209715200",
        "DISK_UTIL_MAX_PCT": "5",
        "DISK_AWAIT_MAX_MS": "5",
    }
    plan = {
        "schema_version": PLAN_SCHEMA,
        "state": "READY_FOR_ADMISSION_GATES",
        "execution_state": "BLOCKED",
        "strict_serial": True,
        "cell_count": 4,
        "cell_keys": list(matrix.RUN_KEYS),
        "campaign_root": str(campaign_root.resolve()),
        "campaign_root_absent": True,
        "fresh_asset_inventory": None,
        "bound_receipts": {
            "adapter_identity": adapter_ref,
            "binary_file_seal": binary_ref,
            "dataset_trace_truth_lineage_seal": lineage_ref,
            "budg_b64_store_seal": budg_ref,
            "naive_store_seal": naive_ref,
        },
        "p03_contract": {
            "policy": "f1-data-retention-200gib-v1",
            "data_free_min_bytes": 214748364800,
            "data_free_min_kib": 209715200,
            "default_monitor_threshold_overridden_explicitly": True,
            "monitor": monitor_ref,
            "batch_gate_validator": batch_gate_ref,
            "environment": p03_environment,
            "argv": [monitor_ref["path"]],
            "receipt_must_bind_environment_and_monitor_sha": True,
            "timing_generated": False,
        },
        "p02b_next_unique_task": {
            "state": "BLOCKED_UNTIL_P03_PASS",
            "estimated_minutes": [13, 25],
            "runner": p02b_runner_ref,
            "validator": p02b_validator_ref,
            "config": p02b_config_ref,
            "requires_real_p31": True,
            "auto_start": False,
        },
        "remaining_admission_gates": remaining,
        "large_content_rehashed_now": False,
        "adapter_invoked": False,
        "timing_generated": False,
        "production_ready": False,
        "blockers": list(remaining),
        **FALSE_ELIGIBILITY,
    }
    return inventory, plan


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    require(not path.exists(), f"refusing to overwrite: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-inventory", type=Path, required=True)
    parser.add_argument("--adapter-identity", type=Path, required=True)
    parser.add_argument("--binary-seal", type=Path, required=True)
    parser.add_argument("--lineage-seal", type=Path, required=True)
    parser.add_argument("--budg-store-seal", type=Path, required=True)
    parser.add_argument("--naive-store-seal", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--p03-output-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        inventory, plan = build(
            legacy_inventory_path=args.legacy_inventory,
            adapter_identity_path=args.adapter_identity,
            binary_seal_path=args.binary_seal,
            lineage_seal_path=args.lineage_seal,
            budg_store_seal_path=args.budg_store_seal,
            naive_store_seal_path=args.naive_store_seal,
            campaign_root=args.campaign_root,
            p03_output_dir=args.p03_output_dir,
        )
        output = args.output_dir.resolve()
        require(not output.exists(), f"refusing to overwrite output directory: {output}")
        output.mkdir(parents=True)
        inventory_path = output / "FRESH-ASSET-INVENTORY.json"
        plan_path = output / "ASSET-ADMISSION-PLAN.json"
        atomic_json(inventory_path, inventory)
        plan["fresh_asset_inventory"] = file_ref(inventory_path, "fresh asset inventory")
        atomic_json(plan_path, plan)
        sums = (
            f"{sha256_file(inventory_path)}  {inventory_path.name}\n"
            f"{sha256_file(plan_path)}  {plan_path.name}\n"
        )
        (output / "SHA256SUMS").write_text(sums, encoding="utf-8")
        print(json.dumps({"state": plan["state"], "output_dir": str(output)}, sort_keys=True))
        return 0
    except (AdmissionError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
