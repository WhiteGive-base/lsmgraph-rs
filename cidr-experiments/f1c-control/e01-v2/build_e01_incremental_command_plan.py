#!/usr/bin/env python3
"""Build the four-cell SemL0 adapter command plan in explicit HOLD state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import build_e01_mixed_lineage as mixed
import inventory_e01_seml0_assets as inventory
import run_e01_incremental_matrix as matrix
import seal_e01_incremental_light_assets as seals


SCHEMA = "cidr-e01-incremental-adapter-command-plan-v1"
FALSE_ELIGIBILITY = matrix.FALSE_ELIGIBILITY


class CommandPlanError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CommandPlanError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CommandPlanError(f"{label}: cannot read JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value


def file_ref(path: Path, label: str) -> Dict[str, Any]:
    path = path.resolve()
    require(path.is_file() and not path.is_symlink(), f"{label}: file invalid")
    require(path.stat().st_size <= inventory.MAX_SMALL_FILE_BYTES, f"{label}: too large")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def verify_ref(value: Any, label: str) -> Dict[str, Any]:
    require(type(value) is dict, f"{label}: reference required")
    require(set(value) == {"path", "sha256", "size_bytes"}, f"{label}: keys drift")
    actual = file_ref(Path(value["path"]), label)
    require(actual == value, f"{label}: path/size/SHA drift")
    return actual


def _store(manifest_path: Path, expected_variant: str) -> Dict[str, Any]:
    ref = file_ref(manifest_path, f"{expected_variant} store manifest")
    value = load_json(manifest_path, f"{expected_variant} store manifest")
    require(value.get("schema_version") == inventory.STORE_SCHEMA, "store schema drift")
    root = Path(str(value.get("store_root", "")))
    require(root.is_dir() and not root.is_symlink(), f"{expected_variant}: store missing")
    return {
        "variant": expected_variant,
        "root": str(root.resolve()),
        "tree_sha256": value["store_sha256"],
        "manifest": ref,
        "fresh_store_seal": None,
    }


def build(
    *,
    mixed_plan_path: Path,
    asset_inventory_path: Path,
    adapter_identity_path: Path,
    binary_seal_path: Path,
    lineage_seal_path: Path,
    p31_wrapper_path: Path,
    p02b_validator_path: Path,
    current_store_manifest_path: Path,
    naive_store_manifest_path: Path,
    campaign_root: Path,
) -> Dict[str, Any]:
    plan_ref = file_ref(mixed_plan_path, "mixed-lineage plan")
    plan = load_json(mixed_plan_path, "mixed-lineage plan")
    require(plan.get("schema_version") == mixed.OUTPUT_SCHEMA, "mixed plan schema drift")
    inventory_ref = file_ref(asset_inventory_path, "asset inventory")
    asset_inventory = load_json(asset_inventory_path, "asset inventory")
    require(asset_inventory.get("schema_version") == inventory.SCHEMA, "inventory schema drift")
    adapter_ref = file_ref(adapter_identity_path, "adapter identity")
    adapter_identity = load_json(adapter_identity_path, "adapter identity")
    require(adapter_identity.get("schema_version") == seals.ADAPTER_SCHEMA, "adapter identity schema drift")
    require(adapter_identity.get("state") == "PASS", "adapter identity must PASS")
    binary_ref = file_ref(binary_seal_path, "binary seal")
    binary = load_json(binary_seal_path, "binary seal")
    require(binary.get("schema_version") == seals.BINARY_SCHEMA, "binary seal schema drift")
    require(binary.get("state") == "PASS" and binary.get("content_hashed_now") is True, "binary seal must freshly PASS")
    lineage_ref = file_ref(lineage_seal_path, "lineage seal")
    lineage = load_json(lineage_seal_path, "lineage seal")
    require(lineage.get("schema_version") == seals.LINEAGE_SCHEMA, "lineage seal schema drift")
    require(lineage.get("state") == "PASS", "lineage seal must PASS")
    require(lineage.get("large_content_rehashed_now") is False, "unexpected large-content hash")
    p31_ref = file_ref(p31_wrapper_path, "P31 wrapper")
    validator_ref = file_ref(p02b_validator_path, "P02B validator")
    current = _store(current_store_manifest_path, "budg-b64")
    naive = _store(naive_store_manifest_path, "naive")
    store_by_variant = {"budg-b64": current, "naive": naive}
    require(campaign_root.is_absolute(), "absolute campaign root required")
    require(not campaign_root.exists(), "campaign root must be absent")
    adapter_entry = adapter_identity["adapter"]
    require(
        adapter_entry["sha256"]
        == asset_inventory["adapter"]["entry"]["sha256"],
        "adapter identity/inventory drift",
    )
    binary_asset = binary["binary"]
    require(
        binary_asset["sha256"]
        == asset_inventory["engine_binary"]["candidate_sha256"],
        "binary seal/inventory drift",
    )
    shared_plan = lineage["query_trace"]["receipt"]
    truth = lineage["truth"]["receipt"]
    id_map = lineage["id_map"]["manifest"]
    cells = []
    for planned in matrix._planned_cells():
        variant = planned["variant"]
        store = store_by_variant[variant]
        cell_root = campaign_root / "cells" / (
            f"{planned['ordinal']:02d}-{planned['cell_key'].replace(':', '-')}"
        )
        request = cell_root / "adapter-request.json"
        output = cell_root / "adapter-output"
        known_argv = [
            adapter_entry["path"],
            "--mode",
            "formal",
            "--variant",
            variant,
            "--binary",
            binary_asset["path"],
            "--binary-sha256",
            binary_asset["sha256"],
            "--data-dir",
            store["root"],
            "--store-manifest",
            store["manifest"]["path"],
            "--store-manifest-sha256",
            store["manifest"]["sha256"],
            "--store-tree-sha256",
            store["tree_sha256"],
            "--sample-plan",
            shared_plan["path"],
            "--sample-plan-sha256",
            shared_plan["sha256"],
            "--truth",
            truth["path"],
            "--truth-sha256",
            truth["sha256"],
            "--id-map-dir",
            str(Path(id_map["path"]).parent),
            "--id-map-manifest-sha256",
            id_map["sha256"],
            "--p02b-validator",
            validator_ref["path"],
            "--p02b-validator-sha256",
            validator_ref["sha256"],
            "--p31-wrapper",
            p31_ref["path"],
            "--p31-wrapper-sha256",
            p31_ref["sha256"],
            "--repo-root",
            adapter_identity["repo"]["root"],
            "--request",
            str(request),
            "--output-dir",
            str(output),
        ]
        cells.append(
            {
                **planned,
                "cell_root": str(cell_root),
                "adapter_request_target": str(request),
                "adapter_output_target": str(output),
                "known_argv": known_argv,
                "command_argv": None,
                "unresolved_arguments": {
                    "--p02b-result": None,
                    "--p02b-result-sha256": None,
                    "fresh_store_seal": None,
                },
                "command_ready": False,
            }
        )
    blockers = [
        "budg-b64 fresh store seal absent",
        "naive fresh store seal absent",
        "fresh P02B result/validator admission receipt absent",
        "fresh resource gate and execution-enable receipt absent",
    ]
    return {
        "schema_version": SCHEMA,
        "state": "HOLD",
        "execution_state": "BLOCKED",
        "strict_serial": True,
        "cell_count": 4,
        "campaign_root": str(campaign_root.resolve()),
        "mixed_lineage_plan": plan_ref,
        "asset_inventory": inventory_ref,
        "adapter_identity": adapter_ref,
        "binary_file_seal": binary_ref,
        "dataset_trace_truth_lineage_seal": lineage_ref,
        "p31_wrapper": p31_ref,
        "p02b_validator": validator_ref,
        "stores": [current, naive],
        "cells": cells,
        "large_content_rehashed_now": False,
        "adapter_invoked": False,
        "timing_generated": False,
        "blockers": blockers,
        **FALSE_ELIGIBILITY,
    }


def validate(value_or_path: Any) -> Dict[str, Any]:
    value = (
        load_json(value_or_path.resolve(), "incremental command plan")
        if isinstance(value_or_path, Path)
        else value_or_path
    )
    require(type(value) is dict, "command plan object required")
    require(value.get("schema_version") == SCHEMA, "command plan schema drift")
    require(value.get("state") == "HOLD", "command plan must remain HOLD")
    require(value.get("execution_state") == "BLOCKED", "command plan must BLOCK")
    require(value.get("strict_serial") is True, "STRICT_SERIAL required")
    require(value.get("cell_count") == 4, "exactly four cells required")
    require(value.get("large_content_rehashed_now") is False, "large hash forbidden")
    require(value.get("adapter_invoked") is False, "adapter invocation forbidden")
    require(value.get("timing_generated") is False, "timing forbidden")
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{key} must remain false")
    for key in (
        "mixed_lineage_plan",
        "asset_inventory",
        "adapter_identity",
        "binary_file_seal",
        "dataset_trace_truth_lineage_seal",
        "p31_wrapper",
        "p02b_validator",
    ):
        verify_ref(value.get(key), key)
    cells = value.get("cells")
    require(type(cells) is list and len(cells) == 4, "four command cells required")
    require([cell.get("cell_key") for cell in cells] == list(matrix.RUN_KEYS), "cell order drift")
    for cell in cells:
        require(cell.get("command_ready") is False, "HOLD cell cannot be command ready")
        require(cell.get("command_argv") is None, "HOLD command argv must be absent")
        require(
            set(cell.get("unresolved_arguments", {}))
            == {"--p02b-result", "--p02b-result-sha256", "fresh_store_seal"},
            "unresolved argument set drift",
        )
        require(
            all(item is None for item in cell["unresolved_arguments"].values()),
            "HOLD unresolved arguments must stay null",
        )
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
    parser.add_argument("--mixed-plan", type=Path, required=True)
    parser.add_argument("--asset-inventory", type=Path, required=True)
    parser.add_argument("--adapter-identity", type=Path, required=True)
    parser.add_argument("--binary-seal", type=Path, required=True)
    parser.add_argument("--lineage-seal", type=Path, required=True)
    parser.add_argument("--p31-wrapper", type=Path, required=True)
    parser.add_argument("--p02b-validator", type=Path, required=True)
    parser.add_argument("--current-store-manifest", type=Path, required=True)
    parser.add_argument("--naive-store-manifest", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        value = build(
            mixed_plan_path=args.mixed_plan,
            asset_inventory_path=args.asset_inventory,
            adapter_identity_path=args.adapter_identity,
            binary_seal_path=args.binary_seal,
            lineage_seal_path=args.lineage_seal,
            p31_wrapper_path=args.p31_wrapper,
            p02b_validator_path=args.p02b_validator,
            current_store_manifest_path=args.current_store_manifest,
            naive_store_manifest_path=args.naive_store_manifest,
            campaign_root=args.campaign_root,
        )
        validate(value)
        atomic_write(args.output.resolve(), value)
        print(json.dumps({"state": value["state"], "cell_count": 4}, sort_keys=True))
        return 0
    except (CommandPlanError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
