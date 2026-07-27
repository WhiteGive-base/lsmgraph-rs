#!/usr/bin/env python3
"""Freeze the E01 exact P31 boundary and no-repeat-hash contract.

The builder consumes only small HOLD plans.  It does not create a campaign
root, invoke an adapter or binary, hash a large asset, or generate timing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


SCHEMA = "cidr-e01-exact-timing-boundary-plan-v1"
COMMAND_SCHEMA = "cidr-e01-incremental-adapter-command-plan-v1"
LIFECYCLE_SCHEMA = "cidr-e01-store-lifecycle-plan-v1"
MAX_SMALL_FILE_BYTES = 16 * 1024 * 1024
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


class BoundaryError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BoundaryError(message)


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
        raise BoundaryError(f"{label}: cannot read JSON: {exc}") from exc
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


def build(
    *,
    command_plan_path: Path,
    store_lifecycle_path: Path,
    p31_bridge_path: Path,
    p31_wrapper_path: Path,
) -> Dict[str, Any]:
    command_ref = file_ref(command_plan_path, "adapter command plan")
    command = load_json(command_plan_path, "adapter command plan")
    require(command.get("schema_version") == COMMAND_SCHEMA, "command plan schema drift")
    require(command.get("state") == "HOLD", "command plan must HOLD")
    require(command.get("execution_state") == "BLOCKED", "command plan must BLOCK")
    require(command.get("adapter_invoked") is False, "adapter invocation drift")
    require(command.get("timing_generated") is False, "timing drift")
    lifecycle_ref = file_ref(store_lifecycle_path, "store lifecycle plan")
    lifecycle = load_json(store_lifecycle_path, "store lifecycle plan")
    require(
        lifecycle.get("schema_version") == LIFECYCLE_SCHEMA,
        "store lifecycle schema drift",
    )
    require(lifecycle.get("state") == "HOLD", "store lifecycle must HOLD")
    require(lifecycle.get("strict_serial") is True, "store lifecycle must be serial")
    require(
        lifecycle.get("campaign_root") == command.get("campaign_root"),
        "campaign root drift",
    )
    bridge_ref = file_ref(p31_bridge_path, "P31 bridge")
    wrapper_ref = file_ref(p31_wrapper_path, "P31 wrapper")
    require(os.access(p31_bridge_path, os.X_OK), "P31 bridge must be executable")
    require(os.access(p31_wrapper_path, os.X_OK), "P31 wrapper must be executable")

    lifecycle_cells = {
        row["cell_key"]: row
        for row in lifecycle.get("cells", [])
        if type(row) is dict and type(row.get("cell_key")) is str
    }
    command_cells = command.get("cells")
    require(type(command_cells) is list and len(command_cells) == 4, "four command cells required")
    require(
        set(lifecycle_cells)
        == {row.get("cell_key") for row in command_cells if type(row) is dict},
        "command/lifecycle cell set drift",
    )

    cells: list[Dict[str, Any]] = []
    for command_cell in command_cells:
        key = command_cell["cell_key"]
        lifecycle_cell = lifecycle_cells[key]
        cell_root = Path(command_cell["cell_root"]).resolve()
        require(
            Path(lifecycle_cell["mutable_clone"]).parent == cell_root,
            f"{key}: mutable clone/cell root drift",
        )
        cells.append(
            {
                "ordinal": command_cell["ordinal"],
                "cell_key": key,
                "variant": command_cell["variant"],
                "cell_root": str(cell_root),
                "mutable_store": lifecycle_cell["mutable_clone"],
                "phase_order": [
                    "admission-and-small-identity",
                    "materialize-mutable-clone",
                    "prepare-binary-command",
                    "p31-binary-only",
                    "convert-and-correctness",
                    "fairness-and-cleanup",
                    "publish-cell-done",
                ],
                "pre_timing": {
                    "inside_p31": False,
                    "adapter_identity_revalidate": True,
                    "binary_file_revalidate_max_bytes": 64 * 1024 * 1024,
                    "immutable_store_seal_revalidate": True,
                    "mutable_clone_receipt_revalidate": True,
                    "dataset_content_hash": False,
                    "store_content_hash": False,
                    "id_map_content_hash": False,
                    "p02b_lease_resource_gate_revalidate": True,
                    "prepared_binary_argv_receipt_required": True,
                },
                "timing": {
                    "inside_p31": True,
                    "process_scope": "lsmgraph-storage-bench-only",
                    "python_adapter_process_inside_boundary": False,
                    "asset_hash_inside_boundary": False,
                    "clone_or_cleanup_inside_boundary": False,
                    "result_conversion_inside_boundary": False,
                    "correctness_validation_inside_boundary": False,
                    "dataset_sha256_mode": "declared-no-read-v1",
                    "p31_run_dir": str(cell_root / "p31"),
                    "p31_bridge": bridge_ref,
                    "p31_wrapper": wrapper_ref,
                    "p31_bridge_argv": None,
                    "binary_argv": None,
                    "unresolved": {
                        "prepared_binary_argv_receipt": None,
                        "fresh_p31_config": None,
                        "fresh_batch_lease": None,
                        "fresh_resource_gate": None,
                    },
                },
                "post_timing": {
                    "inside_p31": False,
                    "binary_exit_zero_required": True,
                    "p31_done_and_validation_required": True,
                    "adapter_output_conversion_required": True,
                    "correctness_zero_mismatch_timeout_required": True,
                    "fairness_receipt_required": True,
                    "mutable_clone_cleanup_required": True,
                    "cleanup_receipt_required_before_cell_done": True,
                },
                "large_content_hashes_per_cell": {
                    "dataset": 0,
                    "immutable_store": 0,
                    "mutable_store": 0,
                    "id_map": 0,
                },
                "state": "HOLD",
            }
        )

    return {
        "schema_version": SCHEMA,
        "state": "HOLD",
        "execution_state": "NOT_IMPLEMENTED",
        "strict_serial": True,
        "campaign_root": command["campaign_root"],
        "adapter_command_plan": command_ref,
        "store_lifecycle_plan": lifecycle_ref,
        "p31_bridge": bridge_ref,
        "p31_wrapper": wrapper_ref,
        "global_boundary": {
            "clock_scope": "P31 wraps only prepared lsmgraph storage-bench argv",
            "begin": "immediately before P31 execs the prepared binary argv",
            "end": "immediately after the prepared binary process exits",
            "preflight_and_postprocessing_excluded": True,
            "full_content_hash_allowed_only_in_one_time_asset_stage": True,
            "adapter_monolithic_formal_path_forbidden": True,
            "old_known_argv_is_not_executable": True,
        },
        "required_backend_interfaces": {
            "prepare_binary_argv_without_timing": "NOT_IMPLEMENTED",
            "invoke_p31_binary_only": "NOT_IMPLEMENTED",
            "finalize_adapter_outputs_without_timing": "NOT_IMPLEMENTED",
            "validate_no_repeat_hash_receipts": "NOT_IMPLEMENTED",
        },
        "cells": cells,
        "large_content_read_now": False,
        "adapter_invoked": False,
        "timing_generated": False,
        "blockers": [
            "split prepare/run/finalize backend not implemented",
            "fresh immutable store and ID-map content seals absent",
            "fresh P03/P02B/lease/resource/execution gates absent",
            "prepared binary argv receipts absent",
        ],
        **FALSE_ELIGIBILITY,
    }


def validate(value_or_path: Any, *, verify_inputs: bool = True) -> Dict[str, Any]:
    value = (
        load_json(value_or_path.resolve(), "timing boundary plan")
        if isinstance(value_or_path, Path)
        else value_or_path
    )
    require(type(value) is dict, "timing boundary plan object required")
    require(value.get("schema_version") == SCHEMA, "boundary schema drift")
    require(value.get("state") == "HOLD", "boundary plan must HOLD")
    require(value.get("execution_state") == "NOT_IMPLEMENTED", "execution state drift")
    require(value.get("strict_serial") is True, "STRICT_SERIAL required")
    require(value.get("large_content_read_now") is False, "large read forbidden")
    require(value.get("adapter_invoked") is False, "adapter invocation forbidden")
    require(value.get("timing_generated") is False, "timing forbidden")
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{key} must remain false")
    if verify_inputs:
        verify_ref(value.get("adapter_command_plan"), "adapter command plan")
        verify_ref(value.get("store_lifecycle_plan"), "store lifecycle plan")
        verify_ref(value.get("p31_bridge"), "P31 bridge")
        verify_ref(value.get("p31_wrapper"), "P31 wrapper")
    boundary = value.get("global_boundary")
    require(type(boundary) is dict, "global boundary required")
    require(boundary.get("adapter_monolithic_formal_path_forbidden") is True, "monolithic path must be forbidden")
    require(boundary.get("full_content_hash_allowed_only_in_one_time_asset_stage") is True, "hash scope drift")
    interfaces = value.get("required_backend_interfaces")
    require(
        type(interfaces) is dict
        and interfaces
        and set(interfaces.values()) == {"NOT_IMPLEMENTED"},
        "backend interfaces must remain NOT_IMPLEMENTED",
    )
    cells = value.get("cells")
    require(type(cells) is list and len(cells) == 4, "exactly four boundary cells required")
    require([row.get("ordinal") for row in cells] == [1, 2, 3, 4], "ordinal drift")
    for row in cells:
        require(row.get("state") == "HOLD", "cell must HOLD")
        timing = row.get("timing")
        require(type(timing) is dict and timing.get("inside_p31") is True, "P31 timing required")
        require(timing.get("process_scope") == "lsmgraph-storage-bench-only", "timed process drift")
        require(timing.get("python_adapter_process_inside_boundary") is False, "adapter must remain outside timing")
        require(timing.get("asset_hash_inside_boundary") is False, "asset hash inside timing")
        require(timing.get("dataset_sha256_mode") == "declared-no-read-v1", "dataset no-read mode required")
        require(timing.get("p31_bridge_argv") is None, "P31 argv must remain unresolved")
        require(timing.get("binary_argv") is None, "binary argv must remain unresolved")
        hashes = row.get("large_content_hashes_per_cell")
        require(type(hashes) is dict and set(hashes.values()) == {0}, "per-cell large hash forbidden")
        require(row.get("pre_timing", {}).get("inside_p31") is False, "preflight boundary drift")
        require(row.get("post_timing", {}).get("inside_p31") is False, "postprocess boundary drift")
    require(type(value.get("blockers")) is list and value["blockers"], "HOLD blockers required")
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
    parser.add_argument("--command-plan", type=Path, required=True)
    parser.add_argument("--store-lifecycle", type=Path, required=True)
    parser.add_argument("--p31-bridge", type=Path, required=True)
    parser.add_argument("--p31-wrapper", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        value = build(
            command_plan_path=args.command_plan.resolve(),
            store_lifecycle_path=args.store_lifecycle.resolve(),
            p31_bridge_path=args.p31_bridge.resolve(),
            p31_wrapper_path=args.p31_wrapper.resolve(),
        )
        validate(value)
        atomic_write(args.output.resolve(), value)
        print(
            json.dumps(
                {
                    "state": value["state"],
                    "cell_count": len(value["cells"]),
                    "large_content_read_now": False,
                    "timing_generated": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except (BoundaryError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
