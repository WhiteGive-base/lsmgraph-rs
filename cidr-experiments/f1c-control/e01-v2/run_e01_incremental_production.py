#!/usr/bin/env python3
"""Fail-closed four-cell E01 production backend.

The module implements process orchestration, atomic cell publication,
strict-prefix resume, failed-root preservation, cleanup admission, and final
MATRIX-DONE publication.  It cannot run the committed HOLD plans: production
requires a separate READY backend plan with every gate and exact argv filled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence


BACKEND_SCHEMA = "cidr-e01-incremental-backend-plan-v1"
START_SCHEMA = "cidr-e01-incremental-production-matrix-start-v1"
CELL_SCHEMA = "cidr-e01-incremental-production-cell-done-v1"
DONE_SCHEMA = "cidr-e01-incremental-production-matrix-done-v1"
FAILED_SCHEMA = "cidr-e01-incremental-production-failed-v1"
CELL_ORDER = (
    "seml0:bridge-canary",
    "seml0-naive:r1",
    "seml0-naive:r2",
    "seml0-naive:r3",
)
PHASE_ORDER = ("prepare", "p31", "finalize", "cleanup")
RECEIPT_PATHS = {
    "prepared_command": "receipts/prepared-command.json",
    "p31": "receipts/p31.json",
    "validated_result": "validated-result.json",
    "correctness": "receipts/correctness.json",
    "fairness": "receipts/fairness.json",
    "store_clone": "receipts/store-clone.json",
    "cleanup": "receipts/cleanup.json",
}
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}
TARGET_P02B_SCHEMA = "cidr-e01-target-specific-p02b-v1"
CELL_VARIANTS = ("budg-b64", "naive", "naive", "naive")


class BackendError(RuntimeError):
    pass


class BackendBlocked(BackendError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BackendError(message)


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
        raise BackendError(f"{label}: cannot read JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    require(len(payload) <= MAX_RECEIPT_BYTES, f"{path.name}: receipt too large")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def file_ref(path: Path, root: Path, label: str) -> Dict[str, Any]:
    path = path.resolve()
    root = root.resolve()
    require(path.is_file(), f"{label}: file missing")
    require(not path.is_symlink(), f"{label}: symlink forbidden")
    require(os.path.commonpath((str(path), str(root))) == str(root), f"{label}: path escape")
    size = path.stat().st_size
    require(0 < size <= MAX_RECEIPT_BYTES, f"{label}: size invalid")
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_file(path),
        "size_bytes": size,
    }


def _absolute_argv(value: Any, label: str) -> list[str]:
    require(type(value) is list and value, f"{label}: argv array required")
    require(all(type(item) is str and item for item in value), f"{label}: argv strings required")
    require(Path(value[0]).is_absolute(), f"{label}: executable must be absolute")
    return list(value)


def _verify_external_ref(value: Any, label: str) -> Dict[str, Any]:
    require(type(value) is dict and set(value) == {"path", "sha256", "size_bytes"}, f"{label}: exact ref required")
    path = Path(value["path"]).resolve()
    require(path.is_file() and not path.is_symlink(), f"{label}: regular file required")
    actual = {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
    require(actual == value, f"{label}: path/size/SHA drift")
    return load_json(path, label)


def validate_backend_plan(value_or_path: Any) -> Dict[str, Any]:
    value = (
        load_json(value_or_path.resolve(), "backend plan")
        if isinstance(value_or_path, Path)
        else value_or_path
    )
    require(type(value) is dict, "backend plan object required")
    require(value.get("schema_version") == BACKEND_SCHEMA, "backend schema drift")
    require(value.get("state") in {"HOLD", "READY"}, "backend state invalid")
    require(value.get("strict_serial") is True, "STRICT_SERIAL required")
    require(value.get("synthetic_test_only") is False, "production backend cannot be synthetic")
    root = Path(str(value.get("campaign_root", "")))
    require(root.is_absolute(), "absolute campaign root required")
    cells = value.get("cells")
    require(type(cells) is list and len(cells) == 4, "exactly four backend cells required")
    require([row.get("cell_key") for row in cells] == list(CELL_ORDER), "cell order drift")
    for ordinal, row in enumerate(cells, start=1):
        require(row.get("ordinal") == ordinal, f"cell {ordinal}: ordinal drift")
        final = Path(str(row.get("final_cell_root", "")))
        staging = Path(str(row.get("staging_cell_root", "")))
        require(final.is_absolute() and staging.is_absolute(), "absolute cell roots required")
        require(final.parent == root / "cells", f"cell {ordinal}: final parent drift")
        require(staging.parent == root / "staging", f"cell {ordinal}: staging parent drift")
        runtime = row.get("runtime")
        if value["state"] == "HOLD" and runtime is None:
            runtime = {}
        require(type(runtime) is dict, f"cell {ordinal}: runtime required")
        variant = CELL_VARIANTS[ordinal - 1]
        if value["state"] == "READY":
            require(runtime.get("variant") == variant, f"cell {ordinal}: target variant drift")
        if value["state"] == "READY":
            bundle = _verify_external_ref(runtime.get("target_p02b"), f"cell {ordinal} target P02B")
            require(bundle.get("schema_version") == TARGET_P02B_SCHEMA, f"cell {ordinal}: target P02B schema drift")
            require(bundle.get("state") == "PASS" and bundle.get("variant") == variant, f"cell {ordinal}: target P02B state/variant drift")
            require(bundle.get("static_inputs", {}).get("query_plan") == runtime.get("target_query_plan"), f"cell {ordinal}: query-plan ref drift")
            require(bundle.get("lease") == runtime.get("target_lease"), f"cell {ordinal}: lease ref drift")
        phases = row.get("phase_commands")
        require(type(phases) is dict and tuple(phases) == PHASE_ORDER, f"cell {ordinal}: phase order drift")
        if value["state"] == "READY":
            for phase in PHASE_ORDER:
                _absolute_argv(phases[phase], f"cell {ordinal}.{phase}")
        else:
            require(all(phases[phase] is None for phase in PHASE_ORDER), "HOLD argv must be absent")
    gates = value.get("campaign_gates")
    require(type(gates) is dict and gates, "campaign gates required")
    if value["state"] == "READY":
        require(value.get("execution_state") == "READY", "READY execution state required")
        require(not value.get("blockers"), "READY backend cannot have blockers")
        for name, descriptor in gates.items():
            require(type(descriptor) is dict, f"{name}: gate reference required")
            gate = load_json(Path(descriptor["path"]), f"{name} gate")
            require(gate.get("state") == "PASS", f"{name}: gate must PASS")
            require(gate.get("synthetic_test_only") is False, f"{name}: synthetic gate forbidden")
            require(gate.get("fixture_only") is False, f"{name}: fixture gate forbidden")
    else:
        require(value.get("execution_state") == "BLOCKED", "HOLD backend must BLOCK")
        require(type(value.get("blockers")) is list and value["blockers"], "HOLD blockers required")
    return value


def _validate_receipt(
    staging: Path,
    role: str,
    *,
    cell_key: str,
    ordinal: int,
    plan_sha: str,
    expected_mode: str,
) -> Dict[str, Any]:
    path = staging / RECEIPT_PATHS[role]
    value = load_json(path, f"{cell_key} {role}")
    require(value.get("schema_version") == f"cidr-e01-incremental-{role.replace('_', '-')}-receipt-v1", f"{role}: schema drift")
    require(value.get("state") == "PASS", f"{role}: state is not PASS")
    require(value.get("cell_key") == cell_key, f"{role}: cell drift")
    require(value.get("ordinal") == ordinal, f"{role}: ordinal drift")
    require(value.get("backend_plan_sha256") == plan_sha, f"{role}: plan SHA drift")
    require(value.get("mode") == expected_mode, f"{role}: mode drift")
    if expected_mode == "production":
        require(value.get("synthetic_test_only") is False, f"{role}: synthetic receipt forbidden")
        require(value.get("fixture_only") is False, f"{role}: fixture receipt forbidden")
    else:
        require(value.get("synthetic_test_only") is True, f"{role}: synthetic marker required")
    if role == "p31":
        require(value.get("timing_generated") is (expected_mode == "production"), "P31 timing marker drift")
        require(value.get("binary_only_boundary") is True, "P31 binary-only boundary required")
    if role == "correctness":
        require(value.get("mismatch_queries") == 0, "correctness mismatch")
        require(value.get("timeout_queries") == 0, "correctness timeout")
    if role == "cleanup":
        require(value.get("mutable_clone_removed") is True, "mutable clone cleanup required")
        clone = value.get("mutable_clone")
        require(type(clone) is str and Path(clone).is_absolute(), "cleanup clone path required")
        require(not Path(clone).exists(), "cleanup claims removed clone that still exists")
    return value


def finalize_staging_cell(
    staging: Path,
    final: Path,
    *,
    cell_key: str,
    ordinal: int,
    plan_sha: str,
    expected_mode: str,
) -> Dict[str, Any]:
    require(staging.is_dir() and not staging.is_symlink(), "staging cell missing/invalid")
    require(not final.exists(), "final cell already exists")
    require(staging.parent.name == "staging", "staging parent drift")
    require(final.parent.name == "cells", "final parent drift")
    require(not (staging / "FAILED.json").exists(), "failed staging cell must be preserved")
    refs: Dict[str, Dict[str, Any]] = {}
    for role in RECEIPT_PATHS:
        _validate_receipt(
            staging,
            role,
            cell_key=cell_key,
            ordinal=ordinal,
            plan_sha=plan_sha,
            expected_mode=expected_mode,
        )
        refs[role] = file_ref(staging / RECEIPT_PATHS[role], staging, role)
    done = {
        "schema_version": CELL_SCHEMA,
        "state": "PASS",
        "mode": expected_mode,
        "synthetic_test_only": expected_mode != "production",
        "fixture_only": expected_mode != "production",
        "cell_key": cell_key,
        "ordinal": ordinal,
        "backend_plan_sha256": plan_sha,
        "adapter_invoked": expected_mode == "production",
        "timing_generated": expected_mode == "production",
        "receipts": refs,
        **FALSE_ELIGIBILITY,
    }
    atomic_json(staging / "CELL-DONE.json", done)
    final.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(final)
    return done


def validate_final_cell(
    final: Path,
    *,
    cell_key: str,
    ordinal: int,
    plan_sha: str,
    expected_mode: str,
) -> Dict[str, Any]:
    require(final.is_dir() and not final.is_symlink(), f"{cell_key}: final cell invalid")
    done = load_json(final / "CELL-DONE.json", f"{cell_key} CELL-DONE")
    require(done.get("schema_version") == CELL_SCHEMA, f"{cell_key}: CELL-DONE schema drift")
    require(done.get("state") == "PASS", f"{cell_key}: CELL-DONE state drift")
    require(done.get("mode") == expected_mode, f"{cell_key}: mode drift")
    require(done.get("cell_key") == cell_key and done.get("ordinal") == ordinal, f"{cell_key}: identity drift")
    require(done.get("backend_plan_sha256") == plan_sha, f"{cell_key}: plan SHA drift")
    refs = done.get("receipts")
    require(type(refs) is dict and set(refs) == set(RECEIPT_PATHS), f"{cell_key}: receipt set drift")
    for role, descriptor in refs.items():
        require(type(descriptor) is dict, f"{cell_key}.{role}: reference required")
        actual = file_ref(final / descriptor["path"], final, f"{cell_key}.{role}")
        require(actual == descriptor, f"{cell_key}.{role}: receipt drift")
        _validate_receipt(
            final,
            role,
            cell_key=cell_key,
            ordinal=ordinal,
            plan_sha=plan_sha,
            expected_mode=expected_mode,
        )
    return done


def inspect_resume_root(
    root: Path,
    plan: Mapping[str, Any],
    plan_sha: str,
    *,
    expected_mode: str = "production",
) -> Dict[str, Any]:
    if not root.exists():
        return {"state": "NEW", "completed_cells": 0}
    require(root.is_dir() and not root.is_symlink(), "campaign root invalid")
    require(not (root / "MATRIX-FAILED.json").exists(), "failed campaign root must be preserved")
    start = load_json(root / "MATRIX-START.json", "MATRIX-START")
    require(start.get("schema_version") == START_SCHEMA, "MATRIX-START schema drift")
    require(start.get("backend_plan_sha256") == plan_sha, "MATRIX-START plan drift")
    require(start.get("strict_serial") is True, "MATRIX-START serial drift")
    cells_root = root / "cells"
    staging_root = root / "staging"
    require(cells_root.is_dir() and staging_root.is_dir(), "campaign subroots missing")
    require(not any(staging_root.iterdir()), "incomplete/failed staging cell blocks resume")
    expected_names = [Path(row["final_cell_root"]).name for row in plan["cells"]]
    unknown = sorted(item.name for item in cells_root.iterdir() if item.name not in expected_names)
    require(not unknown, f"unknown final cells: {unknown}")
    completed = 0
    gap = False
    for row, name in zip(plan["cells"], expected_names):
        path = cells_root / name
        if not path.exists():
            gap = True
            continue
        require(not gap, "completed cells are not a strict prefix")
        validate_final_cell(
            path,
            cell_key=row["cell_key"],
            ordinal=row["ordinal"],
            plan_sha=plan_sha,
            expected_mode=expected_mode,
        )
        completed += 1
    done_path = root / "MATRIX-DONE.json"
    if done_path.exists():
        require(completed == 4, "premature MATRIX-DONE")
        done = load_json(done_path, "MATRIX-DONE")
        require(done.get("schema_version") == DONE_SCHEMA, "MATRIX-DONE schema drift")
        require(done.get("completed_cells") == 4, "MATRIX-DONE count drift")
        require(done.get("backend_plan_sha256") == plan_sha, "MATRIX-DONE plan drift")
    return {"state": "RESUME", "completed_cells": completed}


def _default_runner(argv: list[str], cwd: Path, stdout: Path, stderr: Path) -> int:
    with stdout.open("wb") as out, stderr.open("wb") as err:
        completed = subprocess.run(argv, cwd=cwd, stdout=out, stderr=err, check=False)
    return completed.returncode


def execute_production(
    plan_path: Path,
    *,
    runner: Callable[[list[str], Path, Path, Path], int] = _default_runner,
) -> Dict[str, Any]:
    plan = validate_backend_plan(plan_path.resolve())
    require(plan["state"] == "READY", "backend plan is not READY")
    root = Path(plan["campaign_root"]).resolve()
    plan_sha = sha256_file(plan_path.resolve())
    resume = inspect_resume_root(root, plan, plan_sha)
    if resume["state"] == "NEW":
        root.mkdir(parents=True, exist_ok=False)
        (root / "cells").mkdir()
        (root / "staging").mkdir()
        atomic_json(
            root / "MATRIX-START.json",
            {
                "schema_version": START_SCHEMA,
                "state": "PASS",
                "mode": "production",
                "synthetic_test_only": False,
                "strict_serial": True,
                "backend_plan_sha256": plan_sha,
                **FALSE_ELIGIBILITY,
            },
        )
        completed = 0
    else:
        completed = resume["completed_cells"]
        if completed == 4:
            return load_json(root / "MATRIX-DONE.json", "MATRIX-DONE")
    for row in plan["cells"][completed:]:
        staging = Path(row["staging_cell_root"])
        final = Path(row["final_cell_root"])
        require(not staging.exists() and not final.exists(), f"{row['cell_key']}: target exists")
        staging.mkdir(parents=False, exist_ok=False)
        try:
            for phase in PHASE_ORDER:
                argv = _absolute_argv(row["phase_commands"][phase], f"{row['cell_key']}.{phase}")
                code = runner(
                    argv,
                    staging,
                    staging / f"{phase}.stdout.log",
                    staging / f"{phase}.stderr.log",
                )
                require(code == 0, f"{row['cell_key']}: {phase} exited {code}")
            finalize_staging_cell(
                staging,
                final,
                cell_key=row["cell_key"],
                ordinal=row["ordinal"],
                plan_sha=plan_sha,
                expected_mode="production",
            )
        except BaseException as exc:
            if staging.exists() and not (staging / "FAILED.json").exists():
                atomic_json(
                    staging / "FAILED.json",
                    {
                        "schema_version": FAILED_SCHEMA,
                        "state": "FAILED",
                        "cell_key": row["cell_key"],
                        "ordinal": row["ordinal"],
                        "backend_plan_sha256": plan_sha,
                        "reason": str(exc),
                        **FALSE_ELIGIBILITY,
                    },
                )
            if not (root / "MATRIX-FAILED.json").exists():
                atomic_json(
                    root / "MATRIX-FAILED.json",
                    {
                        "schema_version": FAILED_SCHEMA,
                        "state": "FAILED",
                        "cell_key": row["cell_key"],
                        "ordinal": row["ordinal"],
                        "backend_plan_sha256": plan_sha,
                        "failed_staging_root": str(staging),
                        **FALSE_ELIGIBILITY,
                    },
                )
            raise
    cell_refs = []
    for row in plan["cells"]:
        done_path = Path(row["final_cell_root"]) / "CELL-DONE.json"
        cell_refs.append({"cell_key": row["cell_key"], "cell_done_sha256": sha256_file(done_path)})
    done = {
        "schema_version": DONE_SCHEMA,
        "state": "PASS",
        "mode": "production",
        "synthetic_test_only": False,
        "strict_serial": True,
        "completed_cells": 4,
        "backend_plan_sha256": plan_sha,
        "cells": cell_refs,
        **FALSE_ELIGIBILITY,
    }
    atomic_json(root / "MATRIX-DONE.json", done)
    return done


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-plan", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = execute_production(args.backend_plan)
        print(json.dumps({"state": result["state"], "completed_cells": 4}, sort_keys=True))
        return 0
    except (BackendError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
