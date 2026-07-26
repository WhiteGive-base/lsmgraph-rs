#!/usr/bin/env python3
"""Build and validate an E01 production command plan without executing it."""

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
import validate_e01_cell_evidence as evidence


SCHEMA = "cidr-e01-production-command-plan-v1"
SPEC_SCHEMA = "cidr-e01-production-command-spec-v1"
ALLOWED_ENV = {"LC_ALL", "LANG", "TZ", "RUST_BACKTRACE"}
PLACEHOLDERS = {
    "run_key",
    "ordinal",
    "manifest",
    "cell_root",
    "validated_result",
    "correctness_receipt",
    "cleanup_receipt",
    "binary_sha256",
    "store_sha256",
}
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
FALSE_ELIGIBILITY = manifest_builder.FALSE_ELIGIBILITY


class PlanError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanError(message)


def false_eligibility(value: Mapping[str, Any], label: str) -> None:
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{label}: {key} must be exactly false")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        return manifest_builder.load_json(path, label)
    except manifest_builder.BuildError as exc:
        raise PlanError(str(exc)) from exc


def atomic_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
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


def file_ref(path: Path, label: str) -> dict[str, Any]:
    require(path.is_absolute(), f"{label}: absolute path required")
    require(path.is_file(), f"{label}: file missing")
    require(not path.is_symlink(), f"{label}: symlink forbidden")
    require(path.stat().st_size <= manifest_builder.MAX_RECEIPT_BYTES, f"{label}: file too large")
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def verify_file_ref(value: Any, label: str) -> dict[str, Any]:
    require(type(value) is dict, f"{label}: file reference required")
    require(set(value) == {"path", "sha256", "size_bytes"}, f"{label}: reference keys drift")
    path = Path(value.get("path", ""))
    actual = file_ref(path, label)
    require(actual == value, f"{label}: path/size/SHA drift")
    return actual


def safe_relative(value: Any, label: str) -> PurePosixPath:
    require(type(value) is str and value, f"{label}: relative path required")
    require("\\" not in value and not value.startswith("/") and not re.match(r"^[A-Za-z]:", value), f"{label}: absolute/backslash path forbidden")
    path = PurePosixPath(value)
    require(path.parts and all(part not in ("", ".", "..") for part in path.parts), f"{label}: traversal forbidden")
    return path


def join_inside(root: Path, relative: PurePosixPath, label: str) -> Path:
    path = root.joinpath(*relative.parts).resolve()
    try:
        inside = os.path.commonpath((str(root.resolve()), str(path))) == str(root.resolve())
    except ValueError:
        inside = False
    require(inside, f"{label}: path escapes campaign root")
    return path


def expand_argv(template: Any, values: Mapping[str, str], label: str) -> list[str]:
    require(type(template) is list and template, f"{label}: non-empty argv array required")
    require(all(type(item) is str and item for item in template), f"{label}: argv strings required")
    result = []
    for item in template:
        names = PLACEHOLDER_RE.findall(item)
        require(set(names) <= PLACEHOLDERS, f"{label}: unsupported placeholder")
        try:
            result.append(item.format_map(values))
        except KeyError as exc:
            raise PlanError(f"{label}: missing placeholder value {exc}") from exc
    return result


def validate_environment(value: Any) -> dict[str, str]:
    require(type(value) is dict, "environment object required")
    require(set(value) <= ALLOWED_ENV, "environment contains non-allowlisted variables")
    require(all(type(key) is str and type(item) is str for key, item in value.items()), "environment strings required")
    require(value.get("LC_ALL") == "C", "environment must freeze LC_ALL=C")
    require(value.get("TZ") == "UTC", "environment must freeze TZ=UTC")
    return dict(sorted(value.items()))


def expected_targets(cell_root: Path) -> dict[str, str]:
    targets = {
        "validated_result": cell_root / "validated-result.json",
        "cell_done": cell_root / "CELL-DONE.json",
    }
    for role in evidence.ROLES:
        targets[f"{role}_receipt"] = cell_root / "receipts" / f"{role}.json"
    return {key: str(path) for key, path in targets.items()}


def build_command_plan(
    manifest_path: Path,
    spec_path: Path,
    evidence_schema_path: Path,
    output: Path,
) -> dict[str, Any]:
    try:
        manifest, manifest_sha = scheduler.validate_manifest(manifest_path)
    except scheduler.ContractError as exc:
        raise PlanError(str(exc)) from exc
    evidence_schema_ref = file_ref(evidence_schema_path.resolve(), "cell evidence schema")
    schema_value = load_json(evidence_schema_path.resolve(), "cell evidence schema")
    require(schema_value.get("title") == "CIDR E01 production/synthetic cell evidence interface", "cell evidence schema drift")
    required_roles = schema_value.get("properties", {}).get("receipts", {}).get("required")
    require(type(required_roles) is list and set(required_roles) == set(evidence.ROLES), "cell evidence role schema drift")

    spec = load_json(spec_path.resolve(), "production command spec")
    require(
        set(spec)
        == {
            "schema_version",
            "state",
            "campaign_id",
            "campaign_root",
            "environment",
            "p31_wrapper",
            "cgroup_wrapper",
            "systems",
            "classification",
        },
        "production command spec keys drift",
    )
    require(spec.get("schema_version") == SPEC_SCHEMA, "production command spec schema drift")
    require(spec.get("state") == "READY", "production command spec must be READY")
    require(spec.get("campaign_id") == manifest["campaign_id"], "campaign id drift")
    require(type(spec.get("classification")) is dict, "spec classification required")
    false_eligibility(spec["classification"], "production command spec")
    environment = validate_environment(spec.get("environment"))
    root_raw = spec.get("campaign_root")
    require(type(root_raw) is str and root_raw, "absolute campaign_root required")
    campaign_root = Path(root_raw)
    require(campaign_root.is_absolute(), "campaign_root must be absolute")
    campaign_root = campaign_root.resolve()
    require(not campaign_root.exists(), "campaign_root must not exist during plan build")
    p31_ref = file_ref(Path(spec.get("p31_wrapper", "")).resolve(), "P31 wrapper")
    cgroup_ref = file_ref(Path(spec.get("cgroup_wrapper", "")).resolve(), "cgroup wrapper")

    systems = spec.get("systems")
    require(type(systems) is list and len(systems) == 7, "exactly seven command systems required")
    require([row.get("system_key") for row in systems if type(row) is dict] == [key for key, _ in manifest_builder.SYSTEMS], "command system order drift")
    system_specs: dict[str, dict[str, Any]] = {}
    for row in systems:
        require(
            set(row) == {"system_key", "cwd_relative", "adapter_entry", "adapter_kind", "argv"},
            f"{row.get('system_key')}: command spec keys drift",
        )
        key = row["system_key"]
        require(row.get("adapter_kind") in ("binary", "python-script"), f"{key}: adapter_kind unsupported")
        cwd_relative = safe_relative(row.get("cwd_relative"), f"{key}.cwd_relative")
        adapter_ref = file_ref(Path(row.get("adapter_entry", "")).resolve(), f"{key} adapter entry")
        require(type(row.get("argv")) is list, f"{key}: argv array required; shell command strings forbidden")
        system_specs[key] = {
            "cwd_relative": cwd_relative,
            "adapter_kind": row["adapter_kind"],
            "adapter_entry": adapter_ref,
            "argv": row["argv"],
        }

    cells = []
    roots: set[str] = set()
    for row in manifest["runs"]:
        key = row["system_key"]
        command_spec = system_specs[key]
        cell_root = join_inside(
            campaign_root,
            PurePosixPath("cells") / f"{row['ordinal']:03d}-{key}-r{row['repeat_index']}",
            f"{row['run_key']} cell root",
        )
        require(str(cell_root) not in roots, "duplicate cell root")
        roots.add(str(cell_root))
        cwd = join_inside(cell_root, command_spec["cwd_relative"], f"{row['run_key']} cwd")
        targets = expected_targets(cell_root)
        values = {
            "run_key": row["run_key"],
            "ordinal": str(row["ordinal"]),
            "manifest": str(manifest_path.resolve()),
            "cell_root": str(cell_root),
            "validated_result": targets["validated_result"],
            "correctness_receipt": targets["correctness_receipt"],
            "cleanup_receipt": targets["cleanup_receipt"],
            "binary_sha256": row["binary_sha256"],
            "store_sha256": row["store_sha256"],
        }
        adapter_argv = expand_argv(
            command_spec["argv"], values, f"{row['run_key']} adapter argv"
        )
        adapter_entry = command_spec["adapter_entry"]
        require(
            adapter_argv[0] == adapter_entry["path"],
            f"{row['run_key']}: argv[0] must be the attested adapter entry",
        )
        command_argv = [
            p31_ref["path"],
            "--run-key",
            row["run_key"],
            "--run-dir",
            str(cell_root / "p31"),
            "--",
            cgroup_ref["path"],
            "--cell-root",
            str(cell_root),
            "--",
            *adapter_argv,
        ]
        cells.append(
            {
                "ordinal": row["ordinal"],
                "run_key": row["run_key"],
                "cell_root": str(cell_root),
                "cwd": str(cwd),
                "env": environment,
                "adapter": {
                    "kind": command_spec["adapter_kind"],
                    "entry": adapter_entry,
                    "engine_binary_sha256": row["binary_sha256"],
                    "store_sha256": row["store_sha256"],
                    "argv": adapter_argv,
                },
                "p31_wrapper": p31_ref,
                "cgroup_wrapper": cgroup_ref,
                "command_argv": command_argv,
                "targets": targets,
                **FALSE_ELIGIBILITY,
            }
        )
    require(len(cells) == 21 and len(roots) == 21, "exactly 21 unique cell roots required")
    plan = {
        "schema_version": SCHEMA,
        "state": "PASS",
        "execution_state": "NOT_IMPLEMENTED",
        "strict_serial": True,
        "campaign_id": manifest["campaign_id"],
        "campaign_root": str(campaign_root),
        "formal_manifest": {
            "path": str(manifest_path.resolve()),
            "sha256": manifest_sha,
            "size_bytes": manifest_path.resolve().stat().st_size,
        },
        "cell_evidence_schema": evidence_schema_ref,
        "source_spec": file_ref(spec_path.resolve(), "production command spec"),
        "cell_count": 21,
        "cells": cells,
        **FALSE_ELIGIBILITY,
    }
    validate_command_plan(plan, plan_path=None)
    atomic_json_exclusive(output.resolve(), plan)
    return plan


def validate_command_plan(plan_or_path: Any, plan_path: Path | None) -> dict[str, Any]:
    plan = (
        load_json(plan_or_path.resolve(), "production command plan")
        if isinstance(plan_or_path, Path)
        else plan_or_path
    )
    require(type(plan) is dict, "production command plan object required")
    require(plan.get("schema_version") == SCHEMA, "command plan schema drift")
    require(plan.get("state") == "PASS", "command plan must PASS")
    require(plan.get("execution_state") == "NOT_IMPLEMENTED", "production execution must remain NOT_IMPLEMENTED")
    require(plan.get("strict_serial") is True, "command plan must be STRICT_SERIAL")
    require(plan.get("cell_count") == 21, "command plan must contain 21 cells")
    false_eligibility(plan, "command plan")
    manifest_ref = verify_file_ref(plan.get("formal_manifest"), "formal manifest")
    try:
        manifest, manifest_sha = scheduler.validate_manifest(Path(manifest_ref["path"]))
    except scheduler.ContractError as exc:
        raise PlanError(str(exc)) from exc
    require(manifest_sha == manifest_ref["sha256"], "formal manifest SHA drift")
    verify_file_ref(plan.get("cell_evidence_schema"), "cell evidence schema")
    verify_file_ref(plan.get("source_spec"), "production command spec")
    root = Path(plan.get("campaign_root", ""))
    require(root.is_absolute(), "plan campaign_root must be absolute")
    cells = plan.get("cells")
    require(type(cells) is list and len(cells) == 21, "plan cells must be 21")
    require([cell.get("run_key") for cell in cells if type(cell) is dict] == list(scheduler.RUN_KEYS), "plan run order drift")
    roots: set[str] = set()
    for row, cell in zip(manifest["runs"], cells):
        false_eligibility(cell, f"{row['run_key']} command cell")
        require(cell.get("ordinal") == row["ordinal"], f"{row['run_key']}: ordinal drift")
        cell_root = Path(cell.get("cell_root", ""))
        require(cell_root.is_absolute(), f"{row['run_key']}: absolute cell root required")
        try:
            inside = os.path.commonpath((str(root), str(cell_root))) == str(root)
        except ValueError:
            inside = False
        require(inside, f"{row['run_key']}: cell root escapes campaign root")
        require(str(cell_root) not in roots, "duplicate cell root")
        roots.add(str(cell_root))
        cwd = Path(cell.get("cwd", ""))
        try:
            cwd_inside = os.path.commonpath((str(cell_root), str(cwd))) == str(cell_root)
        except ValueError:
            cwd_inside = False
        require(cwd_inside, f"{row['run_key']}: cwd escapes cell root")
        validate_environment(cell.get("env"))
        adapter = cell.get("adapter")
        require(type(adapter) is dict, f"{row['run_key']}: adapter object required")
        entry = verify_file_ref(adapter.get("entry"), f"{row['run_key']} adapter entry")
        require(adapter.get("engine_binary_sha256") == row["binary_sha256"], f"{row['run_key']}: binary SHA drift")
        require(adapter.get("store_sha256") == row["store_sha256"], f"{row['run_key']}: store SHA drift")
        argv = adapter.get("argv")
        require(type(argv) is list and argv and all(type(item) is str for item in argv), f"{row['run_key']}: adapter argv array required")
        require(argv[0] == entry["path"], f"{row['run_key']}: adapter argv entry drift")
        p31 = verify_file_ref(cell.get("p31_wrapper"), f"{row['run_key']} P31 wrapper")
        cgroup = verify_file_ref(cell.get("cgroup_wrapper"), f"{row['run_key']} cgroup wrapper")
        command = cell.get("command_argv")
        require(type(command) is list and command, f"{row['run_key']}: explicit command argv required")
        require(command[0] == p31["path"] and cgroup["path"] in command, f"{row['run_key']}: wrapper argv drift")
        targets = cell.get("targets")
        require(type(targets) is dict and targets == expected_targets(cell_root), f"{row['run_key']}: target paths drift")
    require(len(roots) == 21, "21 unique cell roots required")
    return plan


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--cell-evidence-schema", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = build_command_plan(
        args.manifest, args.spec, args.cell_evidence_schema, args.output
    )
    print(json.dumps({"state": "PASS", "execution_state": "NOT_IMPLEMENTED", "cell_count": plan["cell_count"], **FALSE_ELIGIBILITY}, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (PlanError, OSError, ValueError) as exc:
        print(f"E01 COMMAND PLAN BUILD FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
