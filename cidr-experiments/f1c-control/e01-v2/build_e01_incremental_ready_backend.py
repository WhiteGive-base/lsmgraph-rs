#!/usr/bin/env python3
"""Build the exact four-cell F1 backend plan, fail-closed until all gates bind.

This builder is receipt-only.  It never copies a store, hashes a large tree,
invokes P31, creates the campaign root, or generates timing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


SCHEMA = "cidr-e01-incremental-backend-plan-v1"
CELL_ORDER = (
    "seml0:bridge-canary",
    "seml0-naive:r1",
    "seml0-naive:r2",
    "seml0-naive:r3",
)
PHASES = ("prepare", "p31", "finalize", "cleanup")
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}
MAX_SMALL_FILE_BYTES = 16 * 1024 * 1024
TARGET_P02B_SCHEMA = "cidr-e01-target-specific-p02b-v1"
TARGETS = {
    "budg-b64": {"layout": "semantic-budgeted", "hint": True},
    "naive": {"layout": "naive", "hint": False},
}


class BuildError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load(path: Path, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path = path.resolve()
    require(path.is_file() and not path.is_symlink(), f"{label}: file required")
    require(0 < path.stat().st_size <= MAX_SMALL_FILE_BYTES, f"{label}: size invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"{label}: invalid JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value, {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def file_ref(path: Path, label: str) -> dict[str, Any]:
    path = path.resolve()
    require(path.is_file() and not path.is_symlink(), f"{label}: file required")
    require(0 < path.stat().st_size <= MAX_SMALL_FILE_BYTES, f"{label}: size invalid")
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def verify_ref(value: Any, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    require(type(value) is dict, f"{label}: reference required")
    loaded, actual = load(Path(value["path"]), label)
    require(actual == value, f"{label}: path/size/SHA drift")
    return loaded, actual


def atomic(path: Path, value: Mapping[str, Any]) -> None:
    require(not path.exists(), f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def _request(
    *,
    cell_key: str,
    repeat: int,
    variant: str,
    binary: Mapping[str, Any],
    dataset_manifest: Mapping[str, Any],
    truth: Mapping[str, Any],
    tree_sha: str,
) -> dict[str, Any]:
    return {
        "schema_version": "cidr-p10-adapter-request-v1",
        "contract_version": "cidr-typed-neighbor-adapter-v1",
        "suite_id": "E01-F1-MIXED-INCREMENTAL-V1",
        "run_id": cell_key.replace(":", "-"),
        "execution_mode": "formal",
        "system_id": "seml0",
        "group": "embedded",
        "system_version": "SemL0@62f7162-split-phase-v1",
        "interface_scope": "typed-neighbor-dense-id-v1",
        "repeat_index": repeat,
        "process_lifetime": "one-process-per-repeat",
        "binary": {"path": binary["path"], "sha256": binary["sha256"]},
        "dataset": {
            "path": dataset_manifest["dataset_root"],
            "sha256": dataset_manifest["dataset_sha256"],
        },
        "runtime_libraries": [],
        "store_roots": [{"label": variant, "path": "{MUTABLE_CLONE}", "sha256": tree_sha}],
        "truth": {
            "path": truth["path"],
            "sha256": truth["sha256"],
            "query_count": 1700,
            "digest_algorithm": "mix64-dense-dst-count-sum-xor-v1",
        },
        "timing": {
            "timing_boundary": "typed-neighbor-call-plus-result-materialization-and-digest-v1",
            "clock": "CLOCK_MONOTONIC",
            "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
            "process_reuse_between_phases": True,
            "warmup_passes": 1,
            "measured_passes": 10,
            "concurrency": 1,
            "per_query_timeout_ms": 1000,
            "sequence_digest_algorithm": "sha256-pass-query-count-sum-xor-v1",
        },
    }


def _target_p02b(path: Optional[Path], variant: str, store: Mapping[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], Optional[str]]:
    if path is None:
        return None, None, f"{variant}: fresh target-specific P02B bundle absent"
    value, ref = load(path, f"{variant} target P02B")
    expected = TARGETS[variant]
    require(value.get("schema_version") == TARGET_P02B_SCHEMA, f"{variant}: target P02B schema drift")
    require(value.get("state") == "PASS" and value.get("variant") == variant, f"{variant}: target P02B did not PASS")
    require(value.get("target") == expected, f"{variant}: target layout/hint drift")
    require(value.get("store_unchanged") is True, f"{variant}: store changed across P02B")
    require(value.get("store_pre") == value.get("store_post"), f"{variant}: pre/post tree receipt drift")
    require(value["store_pre"].get("full_tree_hash_performed") is True, f"{variant}: full-tree hash required")
    require(value["store_pre"].get("sha256") == store["tree_sha256"], f"{variant}: target store SHA drift")
    static = value.get("static_inputs")
    require(type(static) is dict, f"{variant}: static inputs required")
    for name in ("store_manifest", "store_seal", "query_plan"):
        verify_ref(static.get(name), f"{variant} {name}")
    if variant == "naive":
        equivalence, _ = verify_ref(static.get("naive_plan_equivalence"), "naive plan equivalence")
        require(equivalence.get("source_plan", {}).get("sha256") == "4520c88eb594903eb6e3f282838e6cd885e205ee5b0b1790565112d5940f8ea3", "naive source-plan SHA drift")
        require(equivalence.get("target_plan") == static["query_plan"], "naive target-plan equivalence drift")
    result, _ = verify_ref(value.get("sentinel_result"), f"{variant} sentinel result")
    require(result.get("state") == "PASS", f"{variant}: sentinel did not PASS")
    require(result.get("provenance", {}).get("store_sha256") == store["tree_sha256"], f"{variant}: sentinel store SHA drift")
    lease, _ = verify_ref(value.get("lease"), f"{variant} lease")
    require(lease.get("schema_version") == "cidr-batch-lease-v2", f"{variant}: lease schema drift")
    expires = lease.get("expires_at_utc") or lease.get("expires_at")
    require(type(expires) is str and _iso(expires) > dt.datetime.now(dt.timezone.utc), f"{variant}: lease expired")
    for consumer in ("P10", "P20"):
        validation, _ = verify_ref(value.get("official_validations", {}).get(consumer), f"{variant} {consumer} validation")
        require(validation.get("state") == "PASS" and validation.get("consumer") == consumer, f"{variant}: {consumer} validation drift")
        lease_validation, _ = verify_ref(value.get("official_lease_validations", {}).get(consumer), f"{variant} {consumer} lease validation")
        require(lease_validation.get("schema_version") == "cidr-batch-lease-admission-v2", f"{variant}: {consumer} lease validation schema drift")
        require(lease_validation.get("state") == "PASS" and lease_validation.get("consumer") == consumer, f"{variant}: {consumer} lease validation consumer drift")
        require(lease_validation.get("lease") == value["lease"]["path"] and lease_validation.get("lease_sha256") == value["lease"]["sha256"], f"{variant}: {consumer} lease validation ref drift")
        require(lease_validation.get("repo_head") == value["static_inputs"]["repo_head"], f"{variant}: {consumer} lease validation repo drift")
        require(lease_validation.get("binary_sha256") == value["static_inputs"]["bound_inputs"]["binary"]["sha256"], f"{variant}: {consumer} lease validation binary drift")
        require(lease_validation.get("expires_at_utc") == lease.get("expires_at_utc"), f"{variant}: {consumer} lease validation expiry drift")
    verify_ref(value.get("lease_marker"), f"{variant} lease marker")
    verify_ref(value.get("batch_lease_issuance"), f"{variant} lease issuance")
    require(value.get("formal_eligible") is False and value.get("performance_eligible") is False, f"{variant}: eligibility drift")
    return value, ref, None


def build(args: argparse.Namespace) -> dict[str, Any]:
    admission, admission_ref = load(args.admission_bundle, "admission bundle")
    require(admission.get("schema_version") == "cidr-e01-incremental-admission-bundle-v1", "admission schema drift")
    require(admission.get("state") == "PASS_ADMISSION_ONLY", "admission bundle did not PASS")
    require(admission.get("synthetic_test_only") is False and admission.get("fixture_only") is False, "real admission required")
    require(_iso(admission["lease"]["expires_at_utc"]) > dt.datetime.now(dt.timezone.utc), "lease expired")
    inventory, inventory_ref = verify_ref(admission["asset_inventory"], "asset inventory")
    asset_plan, asset_plan_ref = verify_ref(admission["asset_plan"], "asset plan")
    lineage, lineage_ref = verify_ref(inventory["dataset_trace_truth_lineage_seal"], "lineage seal")
    dataset_manifest, dataset_manifest_ref = verify_ref(lineage["logical_dataset"]["dataset_manifest"], "dataset manifest")
    sentinel, sentinel_ref = verify_ref(admission["p02b"]["sentinel"], "legacy P02B sentinel")
    executor_ref = file_ref(args.phase_executor, "phase executor")
    require(os.access(args.phase_executor, os.X_OK), "phase executor must be executable")
    require(args.campaign_root.is_absolute() and not args.campaign_root.exists(), "campaign root must be absolute and absent")
    require(args.output.is_absolute(), "absolute output path required")

    stores = {row["variant"]: row for row in inventory.get("stores", [])}
    require(set(stores) == {"budg-b64", "naive"}, "exact two stores required")
    blockers: list[str] = []
    target_values: dict[str, dict[str, Any]] = {}
    target_refs: dict[str, dict[str, Any]] = {}
    for variant, path in (("budg-b64", args.budg_target_p02b), ("naive", args.naive_target_p02b)):
        bundle, ref, blocker = _target_p02b(path, variant, stores[variant])
        if blocker:
            blockers.append(blocker)
        else:
            target_values[variant] = bundle
            target_refs[variant] = ref

    clone_ref = None
    if args.clone_dry_run is None:
        blockers.append("mutable clone lifecycle dry-run receipt absent")
    else:
        dryrun, clone_ref = load(args.clone_dry_run, "clone dry-run")
        require(dryrun.get("schema_version") == "cidr-e01-mutable-clone-dry-run-v1", "clone dry-run schema drift")
        require(dryrun.get("state") == "PASS" and dryrun.get("mutable_clone_removed") is True, "clone dry-run did not PASS")
        require(dryrun.get("timing_generated") is False, "clone dry-run generated timing")

    binary = inventory["binary"]
    truth = lineage["truth"]["receipt"]
    plan = lineage["query_trace"]["receipt"]
    id_map = lineage["id_map"]["manifest"]
    p31 = sentinel["provenance"]["p31_wrapper"] if type(sentinel.get("provenance")) is dict and "p31_wrapper" in sentinel["provenance"] else None
    if p31 is None:
        provenance = load(Path(sentinel["provenance"]["path"]), "P02B provenance")[0]
        p31 = provenance["files"]["p31_wrapper"]
    batch_gate = asset_plan["p03_contract"]["batch_gate_validator"]

    cells = []
    variants = ("budg-b64", "naive", "naive", "naive")
    repeats = (1, 1, 2, 3)
    layouts = ("semantic-budgeted", "naive", "naive", "naive")
    for ordinal, (key, variant, repeat, layout) in enumerate(zip(CELL_ORDER, variants, repeats, layouts), start=1):
        safe = key.replace(":", "-")
        staging = args.campaign_root / "staging" / f"{ordinal:02d}-{safe}"
        final = args.campaign_root / "cells" / f"{ordinal:02d}-{safe}"
        store = stores[variant]
        target_bundle = target_values.get(variant)
        target_ref = target_refs.get(variant)
        source_seal = store["fresh_store_seal"]
        target_plan = target_bundle["static_inputs"]["query_plan"] if target_bundle else plan
        target_lease = target_bundle["lease"] if target_bundle else admission["lease"]["receipt"]
        request = _request(
            cell_key=key,
            repeat=repeat,
            variant=variant,
            binary=binary,
            dataset_manifest=dataset_manifest,
            truth=truth,
            tree_sha=store["tree_sha256"],
        )
        binary_argv = [
            binary["path"], "--io-backend", "blocking",
            "--csr-metadata-cache-entries", "4096", "storage-bench",
            "--data-dir", "{MUTABLE_CLONE}", "--warmup-runs", "1", "--repeats", "10",
            "--sample-plan-in", target_plan["path"], "--p10-raw-output-dir", "{STAGING}/adapter-output/seml0-raw",
            "--p10-truth-tsv", truth["path"], "--p10-id-map-dir", str(Path(id_map["path"]).parent),
            "--p10-per-query-timeout-ms", "1000", "--l0-layout", layout,
            "--query-control-stage", "a6",
        ]
        if variant == "budg-b64":
            binary_argv.append("--semantic-degree-hint")
        p31_argv = [
            p31["path"], "--run-dir", "{STAGING}/p31", "--run-id", safe,
            "--task-id", f"E01-F1-{safe}", "--performance-eligible", "false",
            "--repo-root", admission["resource_gate"]["repo"]["root"],
            "--device", "nvme1n1", "--data-mount", "/data", "--interval", "1",
            "--disk-interval", "15", "--min-samples", "10",
            "--store", f"{variant}={{MUTABLE_CLONE}}",
            "--batch-lease", target_lease["path"], "--batch-gate-tool", batch_gate["path"],
            "--batch-consumer", "P10", "--batch-anchor-binary", binary["path"],
            "--binary", binary["path"], "--binary-sha256", binary["sha256"],
            "--dataset", dataset_manifest_ref["path"], "--dataset-sha256", dataset_manifest_ref["sha256"],
            "--truth", truth["path"], "--truth-sha256", truth["sha256"],
            "--query-or-trace", target_plan["path"], "--query-or-trace-sha256", target_plan["sha256"],
            "--config", "{REQUEST}", "--", *binary_argv,
        ]
        phase_commands = {
            phase: [
                "/usr/bin/python3", "-B", executor_ref["path"],
                "--backend-plan", str(args.output), "--cell-key", key, "--phase", phase,
            ]
            for phase in PHASES
        }
        cells.append({
            "ordinal": ordinal,
            "cell_key": key,
            "final_cell_root": str(final),
            "staging_cell_root": str(staging),
            "phase_commands": phase_commands if not blockers else {phase: None for phase in PHASES},
            "prospective_phase_commands": phase_commands,
            "runtime": {
                "variant": variant,
                "target_p02b": target_ref,
                "target_p02b_expected": TARGETS[variant],
                "target_query_plan": target_plan,
                "target_lease": target_lease,
                "request": request,
                "adapter_tool": inventory["adapter"],
                "truth": truth,
                "binary_argv": binary_argv,
                "p31_argv": p31_argv,
                "p31_done": "{STAGING}/p31/DONE",
                "clone_policy": {
                    "source_root": store["immutable_root"],
                    "source_seal": source_seal,
                    "tree_sha256": store["tree_sha256"],
                    "mutable_clone": str(staging / "mutable-store"),
                    "copy_argv": ["/bin/cp", "--archive", "--reflink=always", "--one-file-system", "--", "{SOURCE}", "{TARGET}"],
                    "full_content_hash_per_cell": False,
                },
            },
        })

    state = "HOLD" if blockers else "READY"
    gate_path = args.output.with_name(args.output.stem + ".ARMING-GATE.json")
    gate = {
        "schema_version": "cidr-e01-incremental-backend-arming-gate-v1",
        "state": "PASS" if not blockers else "HOLD",
        "synthetic_test_only": False,
        "fixture_only": False,
        "admission_bundle": admission_ref,
        "phase_executor": executor_ref,
        "clone_dry_run": clone_ref,
        "target_p02b": target_refs,
        "timing_generated": False,
        **FALSE_ELIGIBILITY,
    }
    return {
        "schema_version": SCHEMA,
        "state": state,
        "execution_state": "READY" if state == "READY" else "BLOCKED",
        "synthetic_test_only": False,
        "fixture_only": False,
        "strict_serial": True,
        "campaign_root": str(args.campaign_root),
        "campaign_gates": {"backend_arming": {"path": str(gate_path)}} if state == "READY" else {"backend_arming": None},
        "admission_bundle": admission_ref,
        "asset_inventory": inventory_ref,
        "asset_plan": asset_plan_ref,
        "lineage": lineage_ref,
        "dataset_manifest": dataset_manifest_ref,
        "p02b_sentinel": sentinel_ref,
        "phase_executor": executor_ref,
        "clone_dry_run": clone_ref,
        "target_p02b": target_refs,
        "cells": cells,
        "blockers": blockers,
        "large_content_rehashed_now": False,
        "adapter_invoked": False,
        "timing_generated": False,
        "_arming_gate": gate,
        **FALSE_ELIGIBILITY,
    }


def validate(value: Mapping[str, Any]) -> None:
    require(value.get("schema_version") == SCHEMA, "backend schema drift")
    require(value.get("strict_serial") is True, "STRICT_SERIAL required")
    require(value.get("synthetic_test_only") is False, "synthetic backend forbidden")
    require(value.get("large_content_rehashed_now") is False, "large hash forbidden")
    require(value.get("adapter_invoked") is False and value.get("timing_generated") is False, "execution occurred during build")
    require([row.get("cell_key") for row in value.get("cells", [])] == list(CELL_ORDER), "cell order drift")
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{key} must remain false")
    if value["state"] == "READY":
        require(not value["blockers"], "READY backend has blockers")
        require(all(all(row["phase_commands"][phase] for phase in PHASES) for row in value["cells"]), "READY argv missing")
    else:
        require(value["state"] == "HOLD" and value["blockers"], "HOLD blockers required")
        require(all(all(row["phase_commands"][phase] is None for phase in PHASES) for row in value["cells"]), "HOLD phase argv must be null")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission-bundle", type=Path, required=True)
    parser.add_argument("--phase-executor", type=Path, required=True)
    parser.add_argument("--clone-dry-run", type=Path)
    parser.add_argument("--budg-target-p02b", type=Path)
    parser.add_argument("--naive-target-p02b", type=Path)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = build(args)
        validate(value)
        gate = value.pop("_arming_gate")
        if value["state"] == "READY":
            atomic(Path(value["campaign_gates"]["backend_arming"]["path"]), gate)
        atomic(args.output, value)
        print(json.dumps({"state": value["state"], "blockers": value["blockers"]}, sort_keys=True))
        return 0
    except (BuildError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
