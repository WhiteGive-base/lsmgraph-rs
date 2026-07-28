#!/usr/bin/env python3
"""Build the fail-closed E01 immutable-store and per-cell clone contract.

This builder reads only small JSON inputs.  It never walks, hashes, copies,
chmods, or deletes a store tree.  The output deliberately remains HOLD until
separate one-time copy/hash seals and execution gates exist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


SCHEMA = "cidr-e01-store-lifecycle-plan-v1"
INVENTORY_SCHEMA = "cidr-e01-seml0-asset-compatibility-inventory-v1"
COMMAND_SCHEMA = "cidr-e01-incremental-adapter-command-plan-v1"
TREE_HASH_METHOD = "sha256-tree-v1(relative-path,size,file-sha256)"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_SMALL_FILE_BYTES = 16 * 1024 * 1024
CELL_ORDER = (
    ("seml0:bridge-canary", "budg-b64"),
    ("seml0-naive:r1", "naive"),
    ("seml0-naive:r2", "naive"),
    ("seml0-naive:r3", "naive"),
)
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


class LifecycleError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise LifecycleError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LifecycleError(f"{label}: cannot read JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value


def file_ref(path: Path, label: str) -> Dict[str, Any]:
    path = path.resolve()
    require(path.is_file(), f"{label}: file missing")
    require(not path.is_symlink(), f"{label}: symlink forbidden")
    size = path.stat().st_size
    require(0 < size <= MAX_SMALL_FILE_BYTES, f"{label}: small file size invalid")
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": size}


def verify_ref(value: Any, label: str) -> Dict[str, Any]:
    require(type(value) is dict, f"{label}: reference required")
    require(set(value) == {"path", "sha256", "size_bytes"}, f"{label}: keys drift")
    actual = file_ref(Path(value["path"]), label)
    require(actual == value, f"{label}: path/size/SHA drift")
    return actual


def _absolute_new_root(path: Path, label: str) -> Path:
    require(path.is_absolute(), f"{label}: absolute path required")
    resolved = path.resolve()
    require(not resolved.exists(), f"{label}: target must remain absent at plan time")
    return resolved


def _safe_component(cell_key: str) -> str:
    return cell_key.replace(":", "-")


def _store_rows(inventory: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    stores = inventory.get("stores")
    require(type(stores) is list and len(stores) == 2, "exactly two source stores required")
    by_variant: Dict[str, Dict[str, Any]] = {}
    for row in stores:
        require(type(row) is dict, "store row object required")
        variant = row.get("variant")
        require(variant in {"budg-b64", "naive"}, "unsupported store variant")
        require(variant not in by_variant, "duplicate store variant")
        root = Path(str(row.get("candidate_root", "")))
        require(root.is_absolute(), f"{variant}: absolute source root required")
        digest = row.get("candidate_tree_sha256")
        require(type(digest) is str and SHA256_RE.fullmatch(digest), f"{variant}: SHA drift")
        require(
            type(row.get("recorded_file_count")) is int
            and row["recorded_file_count"] > 0,
            f"{variant}: file count invalid",
        )
        require(
            type(row.get("recorded_total_bytes")) is int
            and row["recorded_total_bytes"] > 0,
            f"{variant}: total bytes invalid",
        )
        require(row.get("tree_rehashed_now") is False, f"{variant}: inventory scope drift")
        by_variant[variant] = dict(row)
    require(set(by_variant) == {"budg-b64", "naive"}, "store variant set drift")
    return by_variant


def build(
    *,
    asset_inventory_path: Path,
    command_plan_path: Path,
    immutable_root: Path,
    campaign_root: Path,
) -> Dict[str, Any]:
    inventory_ref = file_ref(asset_inventory_path, "asset inventory")
    inventory = load_json(asset_inventory_path, "asset inventory")
    require(inventory.get("schema_version") == INVENTORY_SCHEMA, "inventory schema drift")
    command_ref = file_ref(command_plan_path, "adapter command plan")
    command = load_json(command_plan_path, "adapter command plan")
    require(command.get("schema_version") == COMMAND_SCHEMA, "command plan schema drift")
    require(command.get("state") == "HOLD", "input command plan must HOLD")
    require(command.get("execution_state") == "BLOCKED", "input command plan must BLOCK")
    require(command.get("timing_generated") is False, "input command plan timing drift")
    immutable_root = _absolute_new_root(immutable_root, "immutable root")
    campaign_root = _absolute_new_root(campaign_root, "campaign root")
    require(
        Path(command.get("campaign_root", "")).resolve() == campaign_root,
        "campaign root/command plan drift",
    )
    stores = _store_rows(inventory)
    command_stores = {
        row["variant"]: row for row in command.get("stores", []) if type(row) is dict
    }
    require(set(command_stores) == set(stores), "command plan store set drift")

    immutable: list[Dict[str, Any]] = []
    for variant in ("budg-b64", "naive"):
        source = stores[variant]
        command_store = command_stores[variant]
        require(
            Path(command_store.get("root", "")).resolve()
            == Path(source["candidate_root"]).resolve(),
            f"{variant}: source root drift",
        )
        require(
            command_store.get("tree_sha256") == source["candidate_tree_sha256"],
            f"{variant}: source SHA drift",
        )
        final_root = immutable_root / "stores" / variant
        staging_root = immutable_root / "staging" / f"{variant}.tmp"
        receipt_root = immutable_root / "receipts"
        immutable.append(
            {
                "variant": variant,
                "source_root": str(Path(source["candidate_root"]).resolve()),
                "source_manifest": source["manifest"],
                "expected_tree_sha256": source["candidate_tree_sha256"],
                "expected_file_count": source["recorded_file_count"],
                "expected_total_bytes": source["recorded_total_bytes"],
                "staging_root": str(staging_root),
                "immutable_root": str(final_root),
                "fresh_store_manifest_target": str(
                    receipt_root / f"{variant}-store-manifest.json"
                ),
                "immutable_seal_target": str(
                    receipt_root / f"{variant}-immutable-seal.json"
                ),
                "copy_policy": {
                    "source_mutation_forbidden": True,
                    "symlinks_forbidden": True,
                    "hardlinks_forbidden": True,
                    "preferred_method": "cp-reflink-always",
                    "full_copy_fallback_requires_explicit_enable": True,
                    "staging_then_atomic_rename": True,
                },
                "one_time_validation": {
                    "hash_method": TREE_HASH_METHOD,
                    "full_content_hash_count": 1,
                    "require_expected_sha_match": True,
                    "require_file_count_match": True,
                    "require_total_bytes_match": True,
                    "fresh_manifest_root_key": "store_path",
                    "post_hash_directory_mode": "0555",
                    "post_hash_file_mode": "0444",
                    "group_or_world_writable_forbidden": True,
                    "seal_outside_hashed_tree": True,
                },
                "state": "HOLD",
                "fresh_manifest": None,
                "immutable_seal": None,
            }
        )

    cells: list[Dict[str, Any]] = []
    for ordinal, (cell_key, variant) in enumerate(CELL_ORDER, start=1):
        safe = _safe_component(cell_key)
        cell_root = campaign_root / "cells" / f"{ordinal:02d}-{safe}"
        immutable_store = immutable_root / "stores" / variant
        cells.append(
            {
                "ordinal": ordinal,
                "cell_key": cell_key,
                "variant": variant,
                "immutable_source": str(immutable_store),
                "staging_clone": str(cell_root / ".mutable-store.tmp"),
                "mutable_clone": str(cell_root / "mutable-store"),
                "clone_receipt_target": str(cell_root / "receipts" / "store-clone.json"),
                "cleanup_receipt_target": str(
                    cell_root / "receipts" / "store-cleanup.json"
                ),
                "clone_policy": {
                    "method": "cp-reflink-always",
                    "full_copy_fallback": False,
                    "hardlinks_forbidden": True,
                    "symlinks_forbidden": True,
                    "one_live_clone_globally": True,
                    "source_seal_revalidated_before_clone": True,
                    "content_rehash_per_cell": False,
                    "identity_basis": "immutable-seal+reflink-copy-receipt+pre/post-stat",
                    "publish_by_atomic_rename": True,
                },
                "cleanup_policy": {
                    "only_after_adapter_p31_correctness_fairness_pass": True,
                    "remove_mutable_clone_only": True,
                    "immutable_source_deletion_forbidden": True,
                    "failed_staging_root_preserved": True,
                    "post_cleanup_absence_required": True,
                },
                "state": "HOLD",
                "clone_receipt": None,
                "cleanup_receipt": None,
            }
        )

    return {
        "schema_version": SCHEMA,
        "state": "HOLD",
        "execution_state": "NOT_STARTED",
        "strict_serial": True,
        "max_live_mutable_clones": 1,
        "campaign_id": "E01-F1-MIXED-INCREMENTAL-V1",
        "campaign_root": str(campaign_root),
        "immutable_asset_root": str(immutable_root),
        "asset_inventory": inventory_ref,
        "adapter_command_plan": command_ref,
        "stores": immutable,
        "cells": cells,
        "large_content_read_now": False,
        "large_copy_performed_now": False,
        "source_store_modified_now": False,
        "blockers": [
            "one-time immutable copies and fresh tree hashes not executed",
            "immutable store seals absent",
            "per-cell clone executor not admitted",
            "fresh P03/P02B/lease/resource/execution gates absent",
        ],
        **FALSE_ELIGIBILITY,
    }


def validate(
    value_or_path: Any,
    *,
    verify_inputs: bool = True,
    verify_live_absence: bool = True,
) -> Dict[str, Any]:
    value = (
        load_json(value_or_path.resolve(), "store lifecycle plan")
        if isinstance(value_or_path, Path)
        else value_or_path
    )
    require(type(value) is dict, "store lifecycle plan object required")
    require(value.get("schema_version") == SCHEMA, "lifecycle schema drift")
    require(value.get("state") == "HOLD", "lifecycle plan must HOLD")
    require(value.get("execution_state") == "NOT_STARTED", "execution state drift")
    require(value.get("strict_serial") is True, "STRICT_SERIAL required")
    require(value.get("max_live_mutable_clones") == 1, "one live clone required")
    require(value.get("large_content_read_now") is False, "large read forbidden")
    require(value.get("large_copy_performed_now") is False, "large copy forbidden")
    require(value.get("source_store_modified_now") is False, "source mutation forbidden")
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{key} must remain false")
    immutable_root = Path(str(value.get("immutable_asset_root", "")))
    campaign_root = Path(str(value.get("campaign_root", "")))
    require(immutable_root.is_absolute(), "absolute immutable root required")
    require(campaign_root.is_absolute(), "absolute campaign root required")
    if verify_live_absence:
        require(not immutable_root.exists(), "immutable root must remain absent in HOLD")
    require(not campaign_root.exists(), "campaign root must remain absent in HOLD")
    if verify_inputs:
        verify_ref(value.get("asset_inventory"), "asset inventory")
        verify_ref(value.get("adapter_command_plan"), "adapter command plan")
    stores = value.get("stores")
    require(
        type(stores) is list
        and [row.get("variant") for row in stores if type(row) is dict]
        == ["budg-b64", "naive"],
        "immutable store order drift",
    )
    for row in stores:
        require(row.get("state") == "HOLD", f"{row.get('variant')}: must HOLD")
        require(row.get("fresh_manifest") is None, "fresh manifest must be absent")
        require(row.get("immutable_seal") is None, "immutable seal must be absent")
        require(
            row.get("one_time_validation", {}).get("full_content_hash_count") == 1,
            "exactly one content hash required",
        )
        require(
            row.get("one_time_validation", {}).get("fresh_manifest_root_key")
            == "store_path",
            "fresh manifest must use store_path",
        )
    cells = value.get("cells")
    require(
        type(cells) is list
        and [(row.get("cell_key"), row.get("variant")) for row in cells if type(row) is dict]
        == list(CELL_ORDER),
        "cell clone order drift",
    )
    for row in cells:
        policy = row.get("clone_policy", {})
        require(policy.get("content_rehash_per_cell") is False, "per-cell rehash forbidden")
        require(policy.get("method") == "cp-reflink-always", "reflink-only clone required")
        require(policy.get("full_copy_fallback") is False, "cell full-copy fallback forbidden")
        require(row.get("clone_receipt") is None, "clone receipt must be absent")
        require(row.get("cleanup_receipt") is None, "cleanup receipt must be absent")
    blockers = value.get("blockers")
    require(type(blockers) is list and blockers, "HOLD blockers required")
    return value


def atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    require(not path.exists(), f"refusing to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-inventory", type=Path, required=True)
    parser.add_argument("--command-plan", type=Path, required=True)
    parser.add_argument("--immutable-root", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        value = build(
            asset_inventory_path=args.asset_inventory.resolve(),
            command_plan_path=args.command_plan.resolve(),
            immutable_root=args.immutable_root,
            campaign_root=args.campaign_root,
        )
        validate(value)
        atomic_write(args.output.resolve(), value)
        print(
            json.dumps(
                {
                    "state": value["state"],
                    "store_count": len(value["stores"]),
                    "cell_count": len(value["cells"]),
                    "large_content_read_now": False,
                    "large_copy_performed_now": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except (LifecycleError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
