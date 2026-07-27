#!/usr/bin/env python3
"""Four-cell E01 incremental spec and restart-safe STRICT_SERIAL scheduler.

Synthetic mode exercises only the state machine and writes unmistakably
synthetic receipts.  Production preflight is read-only and refuses before
creating a result root while any real identity/gate is absent.  This static
engineering stage deliberately contains no adapter/process launch primitive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import build_e01_mixed_lineage as mixed
import inventory_e01_seml0_assets as assets


SPEC_SCHEMA = "cidr-e01-incremental-production-spec-v1"
START_SCHEMA = "cidr-e01-incremental-matrix-start-v1"
CELL_SCHEMA = "cidr-e01-incremental-cell-done-v1"
DONE_SCHEMA = "cidr-e01-incremental-matrix-done-v1"
RUN_KEYS = tuple(item[0] for item in mixed.INCREMENTAL_CELLS)
GATE_KEYS = (
    "fresh_asset_seal",
    "adapter_identity",
    "fresh_p03",
    "fresh_p02b",
    "fresh_batch_lease",
    "fresh_resource_gate",
    "execution_enable",
)
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


class MatrixError(RuntimeError):
    pass


class ProductionBlocked(MatrixError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise MatrixError(message)


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
        raise MatrixError(f"{label}: cannot read JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value


def file_ref(path: Path, label: str) -> Dict[str, Any]:
    path = path.resolve()
    require(path.is_file(), f"{label}: file missing")
    require(not path.is_symlink(), f"{label}: symlink forbidden")
    require(path.stat().st_size <= assets.MAX_SMALL_FILE_BYTES, f"{label}: too large")
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


def false_eligibility(value: Mapping[str, Any], label: str) -> None:
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{label}: {key} must remain false")


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _planned_cells() -> list[Dict[str, Any]]:
    return [
        {
            "ordinal": ordinal,
            "cell_key": key,
            "system_id": system,
            "repeat_index": repeat,
            "variant": "budg-b64" if system == "seml0" else "naive",
            "role": role,
            "included_in_figure_rows": included,
        }
        for ordinal, (key, system, repeat, role, included) in enumerate(
            mixed.INCREMENTAL_CELLS, start=1
        )
    ]


def build_hold_spec(
    mixed_plan_path: Path,
    asset_inventory_path: Path,
    campaign_root: Path,
) -> Dict[str, Any]:
    plan_ref = file_ref(mixed_plan_path, "mixed-lineage plan")
    plan = load_json(mixed_plan_path, "mixed-lineage plan")
    require(plan.get("schema_version") == mixed.OUTPUT_SCHEMA, "mixed plan schema drift")
    inventory_ref = file_ref(asset_inventory_path, "asset inventory")
    inventory = load_json(asset_inventory_path, "asset inventory")
    require(inventory.get("schema_version") == assets.SCHEMA, "asset inventory schema drift")
    require(Path(campaign_root).is_absolute(), "absolute campaign root required")
    require(not campaign_root.exists(), "campaign root must not exist during spec build")
    blockers = list(inventory.get("blockers", [])) + [
        "four-cell adapter command plan absent",
        "production process invocation intentionally disabled",
    ]
    value = {
        "schema_version": SPEC_SCHEMA,
        "state": "HOLD",
        "execution_state": "BLOCKED",
        "synthetic_test_only": False,
        "strict_serial": True,
        "campaign_id": "E01-F1-MIXED-INCREMENTAL-V1",
        "campaign_root": str(campaign_root.resolve()),
        "mixed_lineage_plan": plan_ref,
        "asset_compatibility_inventory": inventory_ref,
        "cells": _planned_cells(),
        "campaign_gates": {key: None for key in GATE_KEYS},
        "adapter_command_plan": None,
        "blockers": blockers,
        **FALSE_ELIGIBILITY,
    }
    validate_spec(value, verify_assets=True)
    return value


def validate_spec(value_or_path: Any, *, verify_assets: bool = True) -> Dict[str, Any]:
    value = (
        load_json(value_or_path.resolve(), "incremental spec")
        if isinstance(value_or_path, Path)
        else value_or_path
    )
    require(type(value) is dict, "incremental spec object required")
    required = {
        "schema_version",
        "state",
        "execution_state",
        "synthetic_test_only",
        "strict_serial",
        "campaign_id",
        "campaign_root",
        "mixed_lineage_plan",
        "asset_compatibility_inventory",
        "cells",
        "campaign_gates",
        "adapter_command_plan",
        "blockers",
        *FALSE_ELIGIBILITY,
    }
    require(set(value) == required, "incremental spec keys drift")
    require(value.get("schema_version") == SPEC_SCHEMA, "incremental spec schema drift")
    require(value.get("strict_serial") is True, "STRICT_SERIAL required")
    false_eligibility(value, "incremental spec")
    root = Path(str(value.get("campaign_root", "")))
    require(root.is_absolute(), "absolute campaign root required")
    cells = value.get("cells")
    require(type(cells) is list and cells == _planned_cells(), "exact four-cell plan drift")
    gates = value.get("campaign_gates")
    require(type(gates) is dict and set(gates) == set(GATE_KEYS), "gate key set drift")
    blockers = value.get("blockers")
    require(type(blockers) is list and all(type(item) is str for item in blockers), "blockers list required")
    if verify_assets:
        verify_ref(value.get("mixed_lineage_plan"), "mixed-lineage plan")
        inventory_ref = verify_ref(
            value.get("asset_compatibility_inventory"), "asset inventory"
        )
        inventory = load_json(Path(inventory_ref["path"]), "asset inventory")
    else:
        inventory = None
    synthetic = value.get("synthetic_test_only")
    require(type(synthetic) is bool, "synthetic_test_only bool required")
    if value.get("state") == "HOLD":
        require(value.get("execution_state") == "BLOCKED", "HOLD execution must BLOCK")
        require(not synthetic, "committed HOLD spec must not be synthetic")
        require(all(item is None for item in gates.values()), "HOLD gates must be absent")
        require(value.get("adapter_command_plan") is None, "HOLD command plan must be absent")
        require(blockers, "HOLD blockers required")
        if inventory is not None:
            require(inventory.get("state") == "HOLD", "HOLD spec inventory drift")
            require(inventory.get("production_ready") is False, "HOLD inventory readiness drift")
        return value
    require(value.get("state") == "READY", "spec state must be HOLD or READY")
    require(value.get("execution_state") == "READY", "READY spec execution state drift")
    require(not blockers, "READY spec cannot have blockers")
    command_ref = verify_ref(value.get("adapter_command_plan"), "adapter command plan") if verify_assets else value.get("adapter_command_plan")
    require(type(command_ref) is dict, "READY adapter command plan required")
    if inventory is not None:
        if synthetic:
            require(inventory.get("synthetic_test_only") is True, "synthetic inventory required")
        else:
            require(inventory.get("state") == "PASS", "production inventory must PASS")
            require(inventory.get("production_ready") is True, "production inventory not ready")
    for key in GATE_KEYS:
        ref = verify_ref(gates[key], f"{key} gate") if verify_assets else gates[key]
        require(type(ref) is dict, f"{key}: gate receipt required")
        if verify_assets:
            gate = load_json(Path(ref["path"]), f"{key} gate")
            require(gate.get("state") == "PASS", f"{key}: gate must PASS")
            if synthetic:
                require(gate.get("synthetic_test_only") is True, f"{key}: synthetic marker required")
            else:
                require(gate.get("synthetic_test_only") is False, f"{key}: fixture/synthetic gate forbidden")
                require(gate.get("fixture_only") is False, f"{key}: fixture gate forbidden")
    return value


def build_hold_spec_file(
    mixed_plan_path: Path,
    asset_inventory_path: Path,
    campaign_root: Path,
    output: Path,
) -> Dict[str, Any]:
    value = build_hold_spec(mixed_plan_path, asset_inventory_path, campaign_root)
    require(not output.exists(), f"refusing to overwrite spec: {output}")
    atomic_json(output.resolve(), value)
    return value


def _cell_dir(root: Path, ordinal: int, key: str) -> Path:
    safe = key.replace(":", "-")
    return root / "cells" / f"{ordinal:02d}-{safe}"


def _validate_synthetic_cell(
    path: Path, *, ordinal: int, key: str, spec_sha: str
) -> Dict[str, Any]:
    require(path.is_dir() and not path.is_symlink(), f"{key}: cell directory invalid")
    done = load_json(path / "CELL-DONE.json", f"{key} CELL-DONE")
    require(done.get("schema_version") == CELL_SCHEMA, f"{key}: CELL-DONE schema drift")
    require(done.get("state") == "PASS", f"{key}: CELL-DONE state drift")
    require(done.get("mode") == "synthetic", f"{key}: synthetic mode required")
    require(done.get("synthetic_test_only") is True, f"{key}: synthetic marker required")
    require(done.get("adapter_invoked") is False, f"{key}: adapter invocation forbidden")
    require(done.get("timing_generated") is False, f"{key}: timing forbidden")
    require(done.get("cell_key") == key and done.get("ordinal") == ordinal, f"{key}: identity drift")
    require(done.get("production_spec_sha256") == spec_sha, f"{key}: spec SHA drift")
    false_eligibility(done, f"{key} CELL-DONE")
    return done


def inspect_synthetic_root(root: Path, spec: Mapping[str, Any], spec_sha: str) -> Dict[str, Any]:
    if not root.exists():
        return {"state": "NEW", "completed": 0}
    require(root.is_dir() and not root.is_symlink(), "synthetic root invalid")
    start = load_json(root / "MATRIX-START.json", "synthetic MATRIX-START")
    require(start.get("schema_version") == START_SCHEMA, "MATRIX-START schema drift")
    require(start.get("mode") == "synthetic", "MATRIX-START mode drift")
    require(start.get("synthetic_test_only") is True, "MATRIX-START synthetic marker missing")
    require(start.get("production_spec_sha256") == spec_sha, "MATRIX-START spec drift")
    false_eligibility(start, "MATRIX-START")
    cells_root = root / "cells"
    require(cells_root.is_dir() and not cells_root.is_symlink(), "cells directory invalid")
    expected = {
        _cell_dir(root, cell["ordinal"], cell["cell_key"]).name: cell
        for cell in spec["cells"]
    }
    unknown = sorted(item.name for item in cells_root.iterdir() if item.name not in expected)
    require(not unknown, f"unknown/incomplete synthetic cells: {unknown}")
    completed = 0
    gap_seen = False
    for name, cell in expected.items():
        path = cells_root / name
        if not path.exists():
            gap_seen = True
            continue
        require(not gap_seen, "synthetic cells are not a strict serial prefix")
        _validate_synthetic_cell(
            path,
            ordinal=cell["ordinal"],
            key=cell["cell_key"],
            spec_sha=spec_sha,
        )
        completed += 1
    matrix_done = root / "MATRIX-DONE.json"
    if matrix_done.exists():
        require(completed == 4, "premature MATRIX-DONE")
        done = load_json(matrix_done, "synthetic MATRIX-DONE")
        require(done.get("schema_version") == DONE_SCHEMA, "MATRIX-DONE schema drift")
        require(done.get("production_spec_sha256") == spec_sha, "MATRIX-DONE spec drift")
        require(done.get("completed_cells") == 4, "MATRIX-DONE count drift")
        require(done.get("synthetic_test_only") is True, "MATRIX-DONE synthetic marker missing")
        require(done.get("timing_generated") is False, "MATRIX-DONE timing drift")
        false_eligibility(done, "MATRIX-DONE")
    return {"state": "RESUME", "completed": completed}


def run_synthetic(
    spec_path: Path,
    result_root: Path,
    *,
    stop_after: Optional[int] = None,
) -> Dict[str, Any]:
    spec = validate_spec(spec_path.resolve(), verify_assets=True)
    require(spec.get("synthetic_test_only") is True, "synthetic spec required")
    require(Path(spec["campaign_root"]).resolve() == result_root.resolve(), "result root drift")
    require(stop_after is None or 0 <= stop_after <= 4, "stop_after must be 0..4")
    spec_sha = sha256_file(spec_path.resolve())
    resume = inspect_synthetic_root(result_root.resolve(), spec, spec_sha)
    if resume["state"] == "NEW":
        result_root.mkdir(parents=True, exist_ok=False)
        (result_root / "cells").mkdir()
        atomic_json(
            result_root / "MATRIX-START.json",
            {
                "schema_version": START_SCHEMA,
                "state": "PASS",
                "mode": "synthetic",
                "synthetic_test_only": True,
                "strict_serial": True,
                "adapter_invoked": False,
                "timing_generated": False,
                "production_spec_sha256": spec_sha,
                **FALSE_ELIGIBILITY,
            },
        )
        completed = 0
    else:
        completed = resume["completed"]
    target = 4 if stop_after is None else stop_after
    require(target >= completed, "stop_after cannot move backwards")
    for cell in spec["cells"][completed:target]:
        path = _cell_dir(result_root, cell["ordinal"], cell["cell_key"])
        path.mkdir(parents=False, exist_ok=False)
        try:
            atomic_json(
                path / "CELL-DONE.json",
                {
                    "schema_version": CELL_SCHEMA,
                    "state": "PASS",
                    "mode": "synthetic",
                    "synthetic_test_only": True,
                    "adapter_invoked": False,
                    "timing_generated": False,
                    "cell_key": cell["cell_key"],
                    "ordinal": cell["ordinal"],
                    "production_spec_sha256": spec_sha,
                    **FALSE_ELIGIBILITY,
                },
            )
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise
    completed = target
    if completed == 4 and not (result_root / "MATRIX-DONE.json").exists():
        atomic_json(
            result_root / "MATRIX-DONE.json",
            {
                "schema_version": DONE_SCHEMA,
                "state": "PASS",
                "mode": "synthetic",
                "synthetic_test_only": True,
                "strict_serial": True,
                "completed_cells": 4,
                "adapter_invoked": False,
                "timing_generated": False,
                "production_spec_sha256": spec_sha,
                **FALSE_ELIGIBILITY,
            },
        )
    return {
        "state": "PASS" if completed == 4 else "PARTIAL",
        "mode": "synthetic",
        "completed_cells": completed,
        "timing_generated": False,
    }


def production_preflight(spec_path: Path, result_root: Path) -> Dict[str, Any]:
    spec = validate_spec(spec_path.resolve(), verify_assets=True)
    require(not spec.get("synthetic_test_only"), "synthetic spec forbidden in production")
    require(Path(spec["campaign_root"]).resolve() == result_root.resolve(), "result root drift")
    blockers = list(spec.get("blockers", []))
    if spec.get("state") != "READY":
        blockers.append(f"spec.state={spec.get('state')}")
    if spec.get("execution_state") != "READY":
        blockers.append(f"spec.execution_state={spec.get('execution_state')}")
    blockers.append("executor.process_invocation=NOT_IMPLEMENTED")
    require(not result_root.exists(), "production root must remain absent during blocked preflight")
    return {
        "schema_version": "cidr-e01-incremental-production-preflight-v1",
        "state": "BLOCKED",
        "adapter_invoked": False,
        "timing_generated": False,
        "result_root_created": False,
        "blockers": blockers,
        **FALSE_ELIGIBILITY,
    }


def execute_production(spec_path: Path, result_root: Path) -> None:
    preflight = production_preflight(spec_path, result_root)
    raise ProductionBlocked(
        "production pre-root blockers: " + "; ".join(preflight["blockers"])
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-hold-spec")
    build.add_argument("--mixed-plan", type=Path, required=True)
    build.add_argument("--asset-inventory", type=Path, required=True)
    build.add_argument("--campaign-root", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--result-root", type=Path, required=True)
    run.add_argument("--synthetic-test-mode", action="store_true")
    run.add_argument("--stop-after", type=int)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "build-hold-spec":
            value = build_hold_spec_file(
                args.mixed_plan,
                args.asset_inventory,
                args.campaign_root,
                args.output,
            )
            print(json.dumps({"state": value["state"], "cell_count": 4}))
            return 0
        if args.synthetic_test_mode:
            result = run_synthetic(
                args.spec, args.result_root, stop_after=args.stop_after
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        execute_production(args.spec, args.result_root)
        return 0
    except (MatrixError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
