#!/usr/bin/env python3
"""Restart-safe STRICT_SERIAL synthetic scheduler contract for E01.

The only executable path implemented here is ``--synthetic-test-mode``.  It
never invokes a P10 adapter or benchmark binary.  Production execution remains
fail-closed until real fresh admission/assets and the adapter execution
contract are available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

import build_e01_formal_manifest as manifest_builder


SCHEMA = "cidr-e01-formal-launch-manifest-v1"
RUN_KEYS = tuple(
    f"{system}:r{repeat}"
    for system, _ in manifest_builder.SYSTEMS
    for repeat in manifest_builder.REPEATS
)
FALSE_ELIGIBILITY = manifest_builder.FALSE_ELIGIBILITY


class ContractError(RuntimeError):
    pass


class SyntheticStop(ContractError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def false_eligibility(value: Mapping[str, Any], label: str) -> None:
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{label}: {key} must be exactly false")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = manifest_builder.load_json(path, label)
    except manifest_builder.BuildError as exc:
        raise ContractError(str(exc)) from exc
    return value


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


def verified_ref(descriptor: Any, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    require(type(descriptor) is dict, f"{label}: reference object required")
    require(
        set(descriptor) == {"path", "sha256", "size_bytes"},
        f"{label}: reference keys drift",
    )
    raw_path = descriptor.get("path")
    require(type(raw_path) is str and raw_path, f"{label}: path required")
    path = Path(raw_path)
    require(path.is_absolute(), f"{label}: absolute path required")
    require(path.is_file(), f"{label}: referenced file missing")
    require(not path.is_symlink(), f"{label}: symlink forbidden")
    require(path.stat().st_size <= manifest_builder.MAX_RECEIPT_BYTES, f"{label}: receipt too large")
    require(path.stat().st_size == descriptor.get("size_bytes"), f"{label}: size drift")
    expected = manifest_builder.require_sha256(descriptor.get("sha256"), f"{label}.sha256")
    require(sha256_file(path) == expected, f"{label}: SHA drift")
    return dict(descriptor), load_json(path, label)


def validate_manifest(path: Path) -> tuple[dict[str, Any], str]:
    require(path.is_file(), f"formal manifest missing: {path}")
    require(not path.is_symlink(), "formal manifest symlink forbidden")
    manifest = load_json(path.resolve(), "formal manifest")
    require(manifest.get("schema_version") == SCHEMA, "formal manifest schema drift")
    require(manifest.get("state") == "PASS", "formal manifest must be PASS")
    require(manifest.get("run_count") == 21, "formal manifest must declare 21 runs")
    require(manifest.get("repeat_indices") == [1, 2, 3], "repeat order drift")
    require(manifest.get("serial_formal_timing") is True, "STRICT_SERIAL required")
    require(
        manifest.get("legacy_conditional_evidence_allowed") is False,
        "legacy conditional evidence forbidden",
    )
    false_eligibility(manifest, "formal manifest")
    protocol = manifest.get("protocol")
    require(type(protocol) is dict, "formal manifest protocol required")
    try:
        protocol_sha = manifest_builder.validate_protocol(protocol)
    except manifest_builder.BuildError as exc:
        raise ContractError(str(exc)) from exc
    require(manifest.get("protocol_sha256") == protocol_sha, "protocol SHA drift")
    campaign_id = manifest.get("campaign_id")
    require(type(campaign_id) is str and campaign_id, "campaign_id required")
    host_sha = protocol["host_fingerprint"]
    git_sha = protocol["harness_git_sha"]

    admission = manifest.get("admission")
    require(
        type(admission) is dict
        and set(admission) == {"p03_clean_ready", "p02b_gate", "batch_lease", "lease_marker"},
        "admission references drift",
    )
    p03_ref, p03 = verified_ref(admission["p03_clean_ready"], "P03 clean-ready")
    gate_ref, gate = verified_ref(admission["p02b_gate"], "P02B gate")
    lease_ref, lease = verified_ref(admission["batch_lease"], "batch lease")
    _, marker = verified_ref(admission["lease_marker"], "lease marker")
    for value, schema, label in (
        (p03, "cidr-e01-p03-clean-ready-v1", "P03 clean-ready"),
        (gate, "cidr-e01-p02b-gate-v1", "P02B gate"),
        (lease, "cidr-e01-formal-lease-v1", "batch lease"),
    ):
        try:
            manifest_builder.validate_fresh_receipt(
                value,
                schema=schema,
                campaign_id=campaign_id,
                protocol_sha=protocol_sha,
                host_sha=host_sha,
                git_sha=git_sha,
                label=label,
            )
        except manifest_builder.BuildError as exc:
            raise ContractError(str(exc)) from exc
    require(gate.get("p03_receipt_sha256") == p03_ref["sha256"], "P02B/P03 lineage drift")
    require(lease.get("p02b_gate_sha256") == gate_ref["sha256"], "lease/P02B lineage drift")
    require(lease.get("exclusive_single_host") is True, "exclusive lease required")
    require(lease.get("serial_formal_timing") is True, "serial lease required")
    require(marker.get("schema_version") == "cidr-e01-formal-lease-marker-v1", "lease marker schema drift")
    require(marker.get("state") == "PASS", "lease marker must PASS")
    require(marker.get("campaign_id") == campaign_id, "lease marker campaign drift")
    require(marker.get("lease_sha256") == lease_ref["sha256"], "lease marker lineage drift")
    false_eligibility(marker, "lease marker")

    seals = manifest.get("asset_seals")
    require(type(seals) is dict and set(seals) == {"dataset", "query_trace"}, "shared seal references drift")
    _, dataset = verified_ref(seals["dataset"], "dataset seal")
    _, trace = verified_ref(seals["query_trace"], "trace seal")
    for value, kind, asset_id, content_sha, label in (
        (dataset, "dataset", protocol["dataset_id"], protocol["input_sha256"], "dataset seal"),
        (trace, "query_trace", protocol["workload_id"], protocol["query_trace_sha256"], "trace seal"),
    ):
        try:
            manifest_builder.validate_asset_seal(
                value,
                campaign_id=campaign_id,
                kind=kind,
                asset_id=asset_id,
                content_sha=content_sha,
                protocol_sha=protocol_sha,
                label=label,
            )
        except manifest_builder.BuildError as exc:
            raise ContractError(str(exc)) from exc

    systems = manifest.get("systems")
    require(type(systems) is list and len(systems) == 7, "exactly seven systems required")
    require(
        [(row.get("system_key"), row.get("display_name")) for row in systems if type(row) is dict]
        == list(manifest_builder.SYSTEMS),
        "system order/name drift",
    )
    by_key: dict[str, dict[str, Any]] = {}
    for row in systems:
        key = row["system_key"]
        binary_sha = manifest_builder.require_sha256(row.get("binary_sha256"), f"{key}.binary_sha256")
        store_sha = manifest_builder.require_sha256(row.get("store_sha256"), f"{key}.store_sha256")
        _, binary = verified_ref(row.get("binary_seal"), f"{key} binary seal")
        _, store = verified_ref(row.get("store_seal"), f"{key} store seal")
        for value, kind, content_sha, label in (
            (binary, "binary", binary_sha, f"{key} binary seal"),
            (store, "store", store_sha, f"{key} store seal"),
        ):
            try:
                manifest_builder.validate_asset_seal(
                    value,
                    campaign_id=campaign_id,
                    kind=kind,
                    asset_id=key,
                    content_sha=content_sha,
                    protocol_sha=protocol_sha,
                    label=label,
                )
            except manifest_builder.BuildError as exc:
                raise ContractError(str(exc)) from exc
        by_key[key] = row

    runs = manifest.get("runs")
    require(type(runs) is list and len(runs) == 21, "exactly 21 run cells required")
    require([row.get("run_key") for row in runs if type(row) is dict] == list(RUN_KEYS), "run order drift")
    for ordinal, row in enumerate(runs, start=1):
        require(row.get("ordinal") == ordinal, f"run {ordinal}: ordinal drift")
        key, repeat_token = RUN_KEYS[ordinal - 1].split(":")
        system = by_key[key]
        expected = {
            "system_key": key,
            "display_name": system["display_name"],
            "variant": system["variant"],
            "repeat_index": int(repeat_token[1:]),
            "host_fingerprint": host_sha,
            "dataset_sha256": protocol["input_sha256"],
            "query_trace_sha256": protocol["query_trace_sha256"],
            "cache_state": protocol["cache_state"],
            "concurrency": protocol["concurrency"],
            "interface_scope": protocol["interface_scope"],
            "git_sha": git_sha,
            "binary_sha256": system["binary_sha256"],
            "store_sha256": system["store_sha256"],
        }
        for field, value in expected.items():
            require(row.get(field) == value, f"{row.get('run_key')}: {field} drift")
        false_eligibility(row, f"run {row.get('run_key')}")
    return manifest, sha256_file(path.resolve())


def cell_name(row: Mapping[str, Any]) -> str:
    return f"{row['ordinal']:03d}-{row['system_key']}-r{row['repeat_index']}"


def validate_cell(cell: Path, row: Mapping[str, Any], manifest_sha: str) -> str:
    require(cell.is_dir(), f"{row['run_key']}: incomplete/non-directory cell")
    done_path = cell / "CELL-DONE.json"
    result_path = cell / "synthetic-result.json"
    require(done_path.is_file(), f"{row['run_key']}: incomplete cell lacks CELL-DONE")
    require(result_path.is_file(), f"{row['run_key']}: synthetic result missing")
    result = load_json(result_path, f"{row['run_key']} synthetic result")
    done = load_json(done_path, f"{row['run_key']} CELL-DONE")
    require(result.get("schema_version") == "cidr-e01-synthetic-cell-result-v1", "synthetic result schema drift")
    require(result.get("state") == "PASS", "synthetic result must PASS")
    require(result.get("synthetic_test_only") is True, "real result forbidden in synthetic contract")
    require(result.get("run_key") == row["run_key"], "synthetic result run drift")
    require(result.get("ordinal") == row["ordinal"], "synthetic result ordinal drift")
    require(result.get("launch_manifest_sha256") == manifest_sha, "synthetic result manifest drift")
    false_eligibility(result, f"{row['run_key']} synthetic result")
    require(done.get("schema_version") == "cidr-e01-cell-done-v1", "CELL-DONE schema drift")
    require(done.get("state") == "PASS", "CELL-DONE must PASS")
    require(done.get("synthetic_test_only") is True, "CELL-DONE must be synthetic")
    require(done.get("run_key") == row["run_key"], "CELL-DONE run drift")
    require(done.get("ordinal") == row["ordinal"], "CELL-DONE ordinal drift")
    require(done.get("launch_manifest_sha256") == manifest_sha, "CELL-DONE manifest drift")
    require(done.get("result_sha256") == sha256_file(result_path), "CELL-DONE result SHA drift")
    false_eligibility(done, f"{row['run_key']} CELL-DONE")
    return sha256_file(done_path)


def create_synthetic_cell(root: Path, row: Mapping[str, Any], manifest_sha: str) -> None:
    final = root / cell_name(row)
    require(not final.exists(), f"{row['run_key']}: refusing to overwrite existing cell")
    temporary = root / f".{cell_name(row)}.tmp-{os.getpid()}"
    require(not temporary.exists(), f"{row['run_key']}: temporary cell already exists")
    temporary.mkdir()
    try:
        result = {
            "schema_version": "cidr-e01-synthetic-cell-result-v1",
            "state": "PASS",
            "synthetic_test_only": True,
            "adapter_invoked": False,
            "timing_generated": False,
            "run_key": row["run_key"],
            "ordinal": row["ordinal"],
            "launch_manifest_sha256": manifest_sha,
            **FALSE_ELIGIBILITY,
        }
        atomic_json_exclusive(temporary / "synthetic-result.json", result)
        done = {
            "schema_version": "cidr-e01-cell-done-v1",
            "state": "PASS",
            "synthetic_test_only": True,
            "run_key": row["run_key"],
            "ordinal": row["ordinal"],
            "launch_manifest_sha256": manifest_sha,
            "result_sha256": sha256_file(temporary / "synthetic-result.json"),
            **FALSE_ELIGIBILITY,
        }
        atomic_json_exclusive(temporary / "CELL-DONE.json", done)
        temporary.rename(final)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def run_matrix(
    manifest_path: Path,
    result_root: Path,
    *,
    synthetic_test_mode: bool,
    synthetic_fail_after: int | None = None,
) -> dict[str, Any]:
    manifest, manifest_sha = validate_manifest(manifest_path)
    require(
        synthetic_test_mode,
        "production launcher unavailable: real fresh gate/assets and P10 execution contract are not admitted",
    )
    require(
        synthetic_fail_after is None or synthetic_fail_after >= 0,
        "synthetic_fail_after must be nonnegative",
    )
    root = result_root.resolve()
    start_path = root / "MATRIX-START.json"
    if not root.exists():
        root.mkdir(parents=True, exist_ok=False)
        atomic_json_exclusive(
            start_path,
            {
                "schema_version": "cidr-e01-matrix-start-v1",
                "state": "PASS",
                "synthetic_test_only": True,
                "strict_serial": True,
                "launch_manifest": str(manifest_path.resolve()),
                "launch_manifest_sha256": manifest_sha,
                **FALSE_ELIGIBILITY,
            },
        )
    else:
        require(root.is_dir(), "result root is not a directory")
        start = load_json(start_path, "MATRIX-START")
        require(start.get("state") == "PASS", "MATRIX-START must PASS")
        require(start.get("synthetic_test_only") is True, "cannot resume non-synthetic root")
        require(start.get("strict_serial") is True, "resume root is not STRICT_SERIAL")
        require(start.get("launch_manifest_sha256") == manifest_sha, "resume manifest SHA drift")
        false_eligibility(start, "MATRIX-START")

    expected_cell_names = {cell_name(row) for row in manifest["runs"]}
    allowed_names = expected_cell_names | {"MATRIX-START.json", "MATRIX-DONE.json"}
    unknown_names = sorted(item.name for item in root.iterdir() if item.name not in allowed_names)
    require(not unknown_names, f"result root contains unknown/residual entries: {unknown_names}")
    done_path = root / "MATRIX-DONE.json"
    if done_path.exists():
        missing = sorted(name for name in expected_cell_names if not (root / name).is_dir())
        require(not missing, "premature MATRIX-DONE before 21/21 cell directories")

    completed: list[dict[str, Any]] = []
    for row in manifest["runs"]:
        cell = root / cell_name(row)
        if cell.exists():
            done_sha = validate_cell(cell, row, manifest_sha)
            completed.append({"run_key": row["run_key"], "cell_done_sha256": done_sha})
            continue
        if synthetic_fail_after is not None and len(completed) >= synthetic_fail_after:
            raise SyntheticStop(f"synthetic stop after {len(completed)} completed cells")
        create_synthetic_cell(root, row, manifest_sha)
        done_sha = validate_cell(cell, row, manifest_sha)
        completed.append({"run_key": row["run_key"], "cell_done_sha256": done_sha})

    require(len(completed) == 21, "MATRIX-DONE requires 21/21 CELL-DONE")
    expected = {
        "schema_version": "cidr-e01-matrix-done-v1",
        "state": "PASS",
        "synthetic_test_only": True,
        "strict_serial": True,
        "completed_cells": 21,
        "launch_manifest_sha256": manifest_sha,
        "cells": completed,
        **FALSE_ELIGIBILITY,
    }
    if done_path.exists():
        require(load_json(done_path, "MATRIX-DONE") == expected, "MATRIX-DONE drift")
    else:
        atomic_json_exclusive(done_path, expected)
    return expected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--synthetic-test-mode", action="store_true")
    parser.add_argument("--synthetic-fail-after", type=int)
    args = parser.parse_args()
    result = run_matrix(
        args.manifest,
        args.result_root,
        synthetic_test_mode=args.synthetic_test_mode,
        synthetic_fail_after=args.synthetic_fail_after,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ContractError, OSError, ValueError) as exc:
        print(f"E01 MATRIX FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
