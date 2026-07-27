#!/usr/bin/env python3
"""Read-only SemL0 current/naive compatibility inventory for E01 incremental.

Only explicit small manifests and adapter source are hashed.  Store trees and
the engine binary are never read by this command; their old declared digests
remain candidates until fresh immutable seal receipts are supplied.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import build_e01_mixed_lineage as mixed


SCHEMA = "cidr-e01-seml0-asset-compatibility-inventory-v1"
STORE_SCHEMA = "p02b-store-manifest-v1"
MAX_SMALL_FILE_BYTES = 16 * 1024 * 1024
EXPECTED_VARIANTS = {
    "budg-b64": ("semantic-budgeted", True),
    "naive": ("naive", False),
}


class InventoryError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise InventoryError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def small_ref(path: Path, label: str) -> Dict[str, Any]:
    path = path.resolve()
    require(path.is_file(), f"{label}: file missing")
    require(not path.is_symlink(), f"{label}: symlink forbidden")
    require(path.stat().st_size <= MAX_SMALL_FILE_BYTES, f"{label}: file too large")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def load_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InventoryError(f"{label}: cannot read JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value


def verify_ref(value: Any, label: str) -> Dict[str, Any]:
    require(type(value) is dict, f"{label}: reference required")
    require(set(value) == {"path", "sha256", "size_bytes"}, f"{label}: keys drift")
    actual = small_ref(Path(value["path"]), label)
    require(actual == value, f"{label}: path/size/SHA drift")
    return actual


def parse_adapter_variants(path: Path) -> Dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "VARIANTS"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            require(type(value) is dict, "adapter VARIANTS must be a dict")
            return value
    raise InventoryError("adapter VARIANTS assignment not found")


def validate_store_manifest(path: Path, expected_variant: str) -> Dict[str, Any]:
    ref = small_ref(path, f"{expected_variant} store manifest")
    value = load_json(path, f"{expected_variant} store manifest")
    require(value.get("schema_version") == STORE_SCHEMA, "store manifest schema drift")
    for key in ("store_root", "store_sha256", "file_count", "total_bytes", "hash_method"):
        require(key in value, f"{expected_variant} store manifest missing {key}")
    root = Path(value["store_root"])
    require(root.is_absolute(), f"{expected_variant}: absolute store root required")
    require(root.is_dir(), f"{expected_variant}: store root missing")
    require(not root.is_symlink(), f"{expected_variant}: store root symlink forbidden")
    require(
        value["hash_method"] == "sha256-tree-v1(relative-path,size,file-sha256)",
        f"{expected_variant}: tree hash method drift",
    )
    require(
        type(value["store_sha256"]) is str
        and mixed.SHA256_RE.fullmatch(value["store_sha256"]) is not None,
        f"{expected_variant}: store SHA invalid",
    )
    require(type(value["file_count"]) is int and value["file_count"] > 0, "file count invalid")
    require(type(value["total_bytes"]) is int and value["total_bytes"] > 0, "bytes invalid")
    return {
        "variant": expected_variant,
        "candidate_root": str(root.resolve()),
        "candidate_tree_sha256": value["store_sha256"],
        "recorded_file_count": value["file_count"],
        "recorded_total_bytes": value["total_bytes"],
        "manifest": ref,
        "tree_rehashed_now": False,
        "root_listing_performed": False,
        "fresh_immutable_seal": None,
        "compatibility_state": "CANDIDATE_ONLY",
    }


def _legacy_binary(plan: Mapping[str, Any]) -> Dict[str, Any]:
    seml0 = [
        cell for cell in plan["legacy_cells"] if cell.get("system_id") == "seml0"
    ]
    require(len(seml0) == 3, "mixed plan must contain SemL0 legacy r1/r2/r3")
    digests = {cell["identity"]["binary_sha256"] for cell in seml0}
    request_refs = [cell["identity"]["adapter_request"] for cell in seml0]
    require(len(digests) == 1, "legacy SemL0 binary SHA drift")
    request = load_json(Path(request_refs[0]["path"]), "legacy SemL0 request")
    binary = request.get("binary")
    require(type(binary) is dict, "legacy SemL0 request binary missing")
    require(binary.get("sha256") == next(iter(digests)), "legacy binary backlink drift")
    binary_path = Path(str(binary.get("path", "")))
    require(binary_path.is_absolute(), "legacy binary path must be absolute")
    require(binary_path.is_file(), "legacy binary candidate missing")
    require(not binary_path.is_symlink(), "legacy binary candidate symlink forbidden")
    return {
        "candidate_path": str(binary_path.resolve()),
        "candidate_sha256": next(iter(digests)),
        "candidate_size_bytes": binary_path.stat().st_size,
        "source_request_receipts": request_refs,
        "binary_rehashed_now": False,
        "fresh_immutable_seal": None,
        "compatibility_state": "CANDIDATE_ONLY",
    }


def inventory(
    mixed_plan_path: Path,
    adapter_path: Path,
    current_store_manifest_path: Path,
    naive_store_manifest_path: Path,
) -> Dict[str, Any]:
    plan_ref = small_ref(mixed_plan_path, "mixed-lineage plan")
    plan = load_json(mixed_plan_path, "mixed-lineage plan")
    require(plan.get("schema_version") == mixed.OUTPUT_SCHEMA, "mixed plan schema drift")
    require(plan.get("state") == "HOLD", "asset inventory must precede incremental PASS")
    require(plan.get("classification", {}).get("formal_eligible") is False, "plan eligibility drift")
    adapter_ref = small_ref(adapter_path, "SemL0 adapter")
    variants = parse_adapter_variants(adapter_path)
    for name, expected in EXPECTED_VARIANTS.items():
        require(variants.get(name) == expected, f"adapter variant mapping drift: {name}")
    binary = _legacy_binary(plan)
    stores = [
        validate_store_manifest(current_store_manifest_path, "budg-b64"),
        validate_store_manifest(naive_store_manifest_path, "naive"),
    ]
    current_sha = next(
        cell["identity"]["physical_input_sha256"]
        for cell in plan["legacy_cells"]
        if cell["cell_key"] == "seml0:r1"
    )
    require(
        current_sha
        == plan["logical_dataset_identity"]["physical_representations"][0]["sha256"],
        "current SemL0 logical dataset binding drift",
    )
    blockers = [
        "binary fresh immutable seal absent",
        "budg-b64 store fresh immutable seal absent",
        "naive store fresh immutable seal absent",
        "SemL0 adapter fresh artifact identity receipt absent",
        "shared dataset/trace/truth fresh seal absent",
        "fresh P03/P02B/batch lease/resource gate absent",
    ]
    return {
        "schema_version": SCHEMA,
        "state": "HOLD",
        "experiment_id": "E01",
        "scope": "seml0-current-bridge-canary-plus-naive-r1-r3",
        "mixed_lineage_plan": plan_ref,
        "adapter": {
            "entry": adapter_ref,
            "kind": "python-script",
            "supported_variant_bindings": {
                key: {
                    "l0_layout": value[0],
                    "semantic_degree_hint": value[1],
                }
                for key, value in EXPECTED_VARIANTS.items()
            },
            "fresh_artifact_identity_receipt": None,
            "compatibility_state": "STATIC_VARIANT_MAPPING_PASS",
        },
        "engine_binary": binary,
        "stores": stores,
        "shared_protocol_binding": {
            "logical_dataset_id": plan["logical_dataset_identity"]["logical_dataset_id"],
            "physical_input_sha256": current_sha,
            "truth_sha256": plan["logical_dataset_identity"]["truth_sha256"],
            "expected_digest_sha256": plan["logical_dataset_identity"][
                "expected_digest_sha256"
            ],
            "interface_scope": plan["logical_dataset_identity"]["interface_scope"],
            "fresh_dataset_trace_truth_seal": None,
            "compatibility_state": "LEGACY_BINDING_ONLY",
        },
        "checks": {
            "adapter_supports_budg_b64_and_naive": "PASS",
            "same_binary_candidate_for_both_variants": "PASS",
            "current_store_manifest_small_receipt": "PASS",
            "naive_store_manifest_small_receipt": "PASS",
            "store_tree_rehash": "NOT_PERFORMED",
            "binary_rehash": "NOT_PERFORMED",
            "fresh_formal_asset_upgrade": "NOT_PERFORMED",
        },
        "production_ready": False,
        "blockers": blockers,
        "formal_eligible": False,
        "performance_eligible": False,
        "paper_claim_eligible": False,
    }


def atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path = path.resolve()
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
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--current-store-manifest", type=Path, required=True)
    parser.add_argument("--naive-store-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        value = inventory(
            args.mixed_plan.resolve(),
            args.adapter.resolve(),
            args.current_store_manifest.resolve(),
            args.naive_store_manifest.resolve(),
        )
        payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            print(payload, end="")
        else:
            atomic_write(args.output, value)
            print(json.dumps({"state": value["state"], "output": str(args.output.resolve())}))
        return 0
    except (InventoryError, OSError, ValueError, SyntaxError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
