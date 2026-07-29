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


BACKEND_SCHEMA = "cidr-e01-incremental-backend-plan-v3"
START_SCHEMA = "cidr-e01-incremental-production-matrix-start-v1"
CELL_SCHEMA = "cidr-e01-incremental-production-cell-done-v1"
DONE_SCHEMA = "cidr-e01-incremental-production-matrix-done-v1"
FAILED_SCHEMA = "cidr-e01-incremental-production-failed-v1"
CANARY_PENDING_SCHEMA = "cidr-e01-incremental-canary-pending-v1"
CANARY_ACCEPTED_SCHEMA = "cidr-e01-incremental-canary-accepted-v1"
CANARY_EVALUATION_SCHEMA = "cidr-e01-bridge-canary-checkpoint-receipt-v2"
SPLIT_PROVENANCE_SCHEMA = "cidr-e01-split-phase-provenance-binding-v1"
SEML0_PROVENANCE_SCHEMA = "p10-seml0-adapter-provenance-v1"
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
    "command_topology": "receipts/command-topology.json",
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


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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


def verify_external_file_ref(value: Any, label: str) -> Dict[str, Any]:
    require(type(value) is dict and set(value) == {"path", "sha256", "size_bytes"}, f"{label}: exact ref required")
    path = Path(value["path"]).resolve()
    require(path.is_file() and not path.is_symlink(), f"{label}: regular file required")
    actual = {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
    require(actual == value, f"{label}: path/size/SHA drift")
    return actual


def _verify_external_ref(value: Any, label: str) -> Dict[str, Any]:
    actual = verify_external_file_ref(value, label)
    path = Path(actual["path"])
    return load_json(path, label)


def external_file_ref(path: Path) -> Dict[str, Any]:
    path = path.resolve()
    require(path.is_file() and not path.is_symlink(), "external reference file required")
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def verify_scheduler_binding(value: Mapping[str, Any]) -> Dict[str, Any]:
    actual_path = Path(__file__).resolve()
    ref = verify_external_file_ref(value.get("production_scheduler"), "production scheduler")
    require(Path(ref["path"]).resolve() == actual_path, "runtime scheduler path differs from plan")
    require(ref["sha256"] == sha256_file(actual_path), "runtime scheduler self SHA drift")
    return ref


def _frozen_legacy_protocol(
    mixed: Mapping[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    keys = ("seml0:r1", "seml0:r2", "seml0:r3")
    rows = {
        row.get("cell_key"): row
        for row in mixed.get("legacy_cells", [])
        if type(row) is dict and row.get("cell_key") in keys
    }
    require(set(rows) == set(keys), "legacy SemL0 rows missing")
    protocols = [rows[key].get("protocol") for key in keys]
    require(all(type(value) is dict for value in protocols), "legacy protocols missing")
    require(protocols[1:] == protocols[:-1], "legacy protocols disagree")
    required = {
        "interface_scope", "warmup_passes", "measured_passes", "process_lifetime",
        "clock", "concurrency", "timing_boundary", "per_query_timeout_ms",
    }
    require(required <= set(protocols[0]), "legacy protocol fields missing")
    protocol = {key: protocols[0][key] for key in sorted(required)}
    require(
        type(protocol["per_query_timeout_ms"]) is int
        and protocol["per_query_timeout_ms"] == 30000,
        "legacy per-query timeout drift",
    )
    request_refs: Dict[str, Dict[str, Any]] = {}
    for key in keys:
        identity = rows[key].get("identity")
        require(type(identity) is dict, f"{key}: legacy identity missing")
        request_ref = verify_external_file_ref(
            identity.get("adapter_request"), f"{key}: legacy adapter request"
        )
        request = load_json(Path(request_ref["path"]), f"{key}: legacy adapter request")
        timing = request.get("timing")
        require(type(timing) is dict, f"{key}: legacy request timing missing")
        require(
            request.get("process_lifetime") == protocol["process_lifetime"]
            and request.get("interface_scope") == protocol["interface_scope"]
            and timing.get("warmup_passes") == protocol["warmup_passes"]
            and timing.get("measured_passes") == protocol["measured_passes"]
            and timing.get("per_query_timeout_ms") == protocol["per_query_timeout_ms"]
            and timing.get("clock") == protocol["clock"]
            and timing.get("concurrency") == protocol["concurrency"]
            and timing.get("timing_boundary") == protocol["timing_boundary"],
            f"{key}: legacy request/protocol mismatch",
        )
        request_refs[key] = request_ref
    return protocol, request_refs


def _canary_contract(value: Mapping[str, Any], root: Path) -> Dict[str, Any]:
    contract = value.get("canary_checkpoint")
    require(type(contract) is dict, "canary checkpoint contract required")
    require(
        contract.get("schema_version") == "cidr-e01-incremental-canary-checkpoint-contract-v2",
        "canary checkpoint contract schema drift",
    )
    verify_external_file_ref(contract.get("evaluator"), "canary evaluator")
    mixed_ref = verify_external_file_ref(
        contract.get("mixed_lineage_plan"), "formal mixed-lineage plan"
    )
    mixed = load_json(Path(mixed_ref["path"]), "formal mixed-lineage plan")
    require(
        mixed.get("schema_version") == "cidr-e01-mixed-lineage-composition-v1",
        "formal mixed-lineage schema drift",
    )
    legacy_protocol, legacy_request_refs = _frozen_legacy_protocol(mixed)
    require(contract.get("legacy_protocol") == legacy_protocol, "checkpoint legacy protocol drift")
    require(
        contract.get("legacy_adapter_requests") == legacy_request_refs,
        "checkpoint legacy adapter-request refs drift",
    )
    require(
        contract.get("bridge_request_argv_binding")
        == {
            "request_protocol_exact": True,
            "binary_argv_exact": True,
            "warmup_runs": legacy_protocol["warmup_passes"],
            "measured_repeats": legacy_protocol["measured_passes"],
            "process_lifetime": legacy_protocol["process_lifetime"],
            "per_query_timeout_ms": legacy_protocol["per_query_timeout_ms"],
        },
        "checkpoint request/argv binding drift",
    )
    comparability = mixed.get("incremental_plan", {}).get(
        "bridge_canary_comparability_contract"
    )
    require(type(comparability) is dict, "comparability contract missing")
    contract_body = dict(comparability)
    declared_contract_sha = contract_body.pop("contract_sha256", None)
    actual_contract_sha = hashlib.sha256(
        json.dumps(contract_body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    require(
        type(contract.get("comparability_contract_sha256")) is str
        and contract["comparability_contract_sha256"]
        == declared_contract_sha
        == actual_contract_sha,
        "comparability contract SHA drift",
    )
    require(
        contract.get("evidence_schema") == "cidr-e01-bridge-canary-evidence-v2",
        "canary evidence schema drift",
    )
    require(
        contract.get("evidence_binding", {}).get("cell_key") == "seml0:bridge-canary"
        and contract["evidence_binding"].get("backend_plan") is True
        and contract["evidence_binding"].get("bridge_cell_done") is True
        and set(contract["evidence_binding"].get("bridge_receipts", []))
        == set(RECEIPT_PATHS),
        "canary cell1 evidence binding drift",
    )
    require(
        contract.get("validated_output_binding")
        == {
            "receipt_schema": "cidr-e01-incremental-validated-result-receipt-v1",
            "adapter_schema": "cidr-p10-validated-repeat-v1",
            "prepared_request_ref": True,
            "p31_receipt_ref": True,
            "p31_run_manifest_ref": True,
            "command_topology_ref": True,
            "deadline_exact": True,
            "backend_plan_ref": True,
            "target_p02b_ref": True,
            "final_cell_root": True,
        },
        "canary validated-output binding drift",
    )
    expected = {
        "evidence_path": root / "CANARY-EVIDENCE.json",
        "pending_path": root / "CANARY-PENDING.json",
        "comparability_path": root / "CANARY-COMPARABILITY.json",
        "evaluation_path": root / "CANARY-EVALUATION.json",
        "accepted_path": root / "CANARY-ACCEPTED.json",
    }
    for key, path in expected.items():
        require(
            Path(str(contract.get(key, ""))).absolute() == path.absolute(),
            f"canary checkpoint {key} drift",
        )
    return dict(contract)


def validate_backend_plan(value_or_path: Any) -> Dict[str, Any]:
    plan_path = value_or_path.resolve() if isinstance(value_or_path, Path) else None
    value = (
        load_json(plan_path, "backend plan")
        if plan_path is not None
        else value_or_path
    )
    require(type(value) is dict, "backend plan object required")
    require(value.get("schema_version") == BACKEND_SCHEMA, "backend schema drift")
    require(value.get("state") in {"HOLD", "READY"}, "backend state invalid")
    require(value.get("strict_serial") is True, "STRICT_SERIAL required")
    require(value.get("synthetic_test_only") is False, "production backend cannot be synthetic")
    root = Path(str(value.get("campaign_root", "")))
    require(root.is_absolute(), "absolute campaign root required")
    scheduler_ref = verify_scheduler_binding(value)
    executor_ref = value.get("phase_executor")
    verify_external_file_ref(executor_ref, "phase executor")
    checkpoint = _canary_contract(value, root)
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
            request = runtime.get("request")
            require(type(request) is dict and type(request.get("timing")) is dict, f"cell {ordinal}: request missing")
            timing = request["timing"]
            require(
                request.get("interface_scope") == checkpoint["legacy_protocol"]["interface_scope"]
                and request.get("process_lifetime") == checkpoint["legacy_protocol"]["process_lifetime"]
                and timing.get("warmup_passes") == checkpoint["legacy_protocol"]["warmup_passes"]
                and timing.get("measured_passes") == checkpoint["legacy_protocol"]["measured_passes"]
                and timing.get("per_query_timeout_ms")
                == checkpoint["legacy_protocol"]["per_query_timeout_ms"]
                and timing.get("clock") == checkpoint["legacy_protocol"]["clock"]
                and timing.get("concurrency") == checkpoint["legacy_protocol"]["concurrency"]
                and timing.get("timing_boundary") == checkpoint["legacy_protocol"]["timing_boundary"],
                f"cell {ordinal}: request/legacy protocol mismatch",
            )
            binary_argv = _absolute_argv(runtime.get("binary_argv"), f"cell {ordinal} binary")
            require(
                binary_argv[binary_argv.index("--warmup-runs") + 1]
                == str(checkpoint["legacy_protocol"]["warmup_passes"])
                and binary_argv[binary_argv.index("--repeats") + 1]
                == str(checkpoint["legacy_protocol"]["measured_passes"]),
                f"cell {ordinal}: binary argv/legacy protocol mismatch",
            )
            require(
                binary_argv[binary_argv.index("--p10-per-query-timeout-ms") + 1]
                == str(checkpoint["legacy_protocol"]["per_query_timeout_ms"]),
                f"cell {ordinal}: binary argv deadline mismatch",
            )
        phases = row.get("phase_commands")
        require(
            type(phases) is dict and set(phases) == set(PHASE_ORDER),
            f"cell {ordinal}: phase key set drift",
        )
        if value["state"] == "READY":
            for phase in PHASE_ORDER:
                argv = _absolute_argv(phases[phase], f"cell {ordinal}.{phase}")
                require(
                    len(argv) == 9
                    and argv[:3] == ["/usr/bin/python3", "-B", executor_ref["path"]]
                    and argv[3] == "--backend-plan"
                    and Path(argv[4]).is_absolute()
                    and argv[5:] == ["--cell-key", row["cell_key"], "--phase", phase],
                    f"cell {ordinal}.{phase}: phase executor dispatch drift",
                )
                if plan_path is not None:
                    require(
                        Path(argv[4]).resolve() == plan_path,
                        f"cell {ordinal}.{phase}: backend plan dispatch drift",
                    )
        else:
            require(all(phases[phase] is None for phase in PHASE_ORDER), "HOLD argv must be absent")
    gates = value.get("campaign_gates")
    require(type(gates) is dict and gates, "campaign gates required")
    if value["state"] == "READY":
        require(value.get("execution_state") == "READY", "READY execution state required")
        require(not value.get("blockers"), "READY backend cannot have blockers")
        for name, descriptor in gates.items():
            require(type(descriptor) is dict and set(descriptor) == {"path"}, f"{name}: gate path required")
            gate = load_json(Path(descriptor["path"]), f"{name} gate")
            require(gate.get("state") == "PASS", f"{name}: gate must PASS")
            require(gate.get("synthetic_test_only") is False, f"{name}: synthetic gate forbidden")
            require(gate.get("fixture_only") is False, f"{name}: fixture gate forbidden")
            require(gate.get("phase_executor") == executor_ref, f"{name}: phase executor backlink drift")
            require(gate.get("production_scheduler") == scheduler_ref, f"{name}: scheduler backlink drift")
            require(gate.get("canary_evaluator") == checkpoint["evaluator"], f"{name}: evaluator backlink drift")
            require(gate.get("canary_checkpoint") == checkpoint, f"{name}: checkpoint backlink drift")
            require(type(gate.get("backend_plan")) is dict, f"{name}: backend plan backlink required")
            if plan_path is not None:
                require(
                    gate["backend_plan"] == external_file_ref(plan_path),
                    f"{name}: backend plan backlink drift",
                )
    else:
        require(value.get("execution_state") == "BLOCKED", "HOLD backend must BLOCK")
        require(type(value.get("blockers")) is list and value["blockers"], "HOLD blockers required")
    return value


def _bridge_receipts(final: Path, done: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    refs = done.get("receipts")
    require(type(refs) is dict and set(refs) == set(RECEIPT_PATHS), "bridge receipt set drift")
    result: Dict[str, Dict[str, Any]] = {}
    for role, descriptor in refs.items():
        require(type(descriptor) is dict and type(descriptor.get("path")) is str, f"{role}: bridge ref drift")
        result[role] = external_file_ref(final / descriptor["path"])
    return result


def _arming_gate_ref(plan: Mapping[str, Any]) -> Dict[str, Any]:
    descriptor = plan.get("campaign_gates", {}).get("backend_arming")
    require(type(descriptor) is dict and set(descriptor) == {"path"}, "backend arming gate path drift")
    return external_file_ref(Path(descriptor["path"]))


def expected_canary_pending(
    root: Path,
    plan: Mapping[str, Any],
    plan_ref: Mapping[str, Any],
) -> Dict[str, Any]:
    bridge = plan["cells"][0]
    final = Path(bridge["final_cell_root"])
    done = validate_final_cell(
        final,
        cell_key=bridge["cell_key"],
        ordinal=bridge["ordinal"],
        plan_sha=plan_ref["sha256"],
        expected_mode="production",
        target_p02b=bridge["runtime"]["target_p02b"],
        target_query_plan=bridge["runtime"]["target_query_plan"],
        target_lease=bridge["runtime"]["target_lease"],
        adapter_tool=bridge["runtime"]["adapter_tool"],
    )
    return {
        "schema_version": CANARY_PENDING_SCHEMA,
        "state": "CANARY_PENDING",
        "completed_cells": 1,
        "campaign_root": str(root),
        "backend_plan": dict(plan_ref),
        "arming_gate": _arming_gate_ref(plan),
        "bridge_cell_done": external_file_ref(final / "CELL-DONE.json"),
        "bridge_receipts": _bridge_receipts(final, done),
        "target_p02b": bridge["runtime"]["target_p02b"],
        "target_query_plan": bridge["runtime"]["target_query_plan"],
        "target_lease": bridge["runtime"]["target_lease"],
        "canary_evaluator": plan["canary_checkpoint"]["evaluator"],
        "mixed_lineage_plan": plan["canary_checkpoint"]["mixed_lineage_plan"],
        "comparability_contract_sha256": plan["canary_checkpoint"][
            "comparability_contract_sha256"
        ],
        "evidence_schema": plan["canary_checkpoint"]["evidence_schema"],
        "evidence_path": plan["canary_checkpoint"]["evidence_path"],
        "matrix_terminal": False,
        **FALSE_ELIGIBILITY,
    }


def ensure_canary_pending(
    root: Path,
    plan: Mapping[str, Any],
    plan_ref: Mapping[str, Any],
) -> Dict[str, Any]:
    require(not os.path.lexists(root / "MATRIX-DONE.json"), "bridge checkpoint cannot be MATRIX-DONE")
    require(not os.path.lexists(root / "MATRIX-FAILED.json"), "failed campaign cannot enter canary checkpoint")
    expected = expected_canary_pending(root, plan, plan_ref)
    path = Path(plan["canary_checkpoint"]["pending_path"])
    if os.path.lexists(path):
        require(path.is_file() and not path.is_symlink(), "CANARY_PENDING path invalid")
        require(load_json(path, "CANARY_PENDING") == expected, "CANARY_PENDING drift/replay")
    else:
        atomic_json(path, expected)
    return expected


def consume_canary_evaluation(
    root: Path,
    plan: Mapping[str, Any],
    plan_ref: Mapping[str, Any],
    pending: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    contract = plan["canary_checkpoint"]
    evaluation_path = Path(contract["evaluation_path"])
    if not os.path.lexists(evaluation_path):
        return None
    require(
        evaluation_path.is_file() and not evaluation_path.is_symlink(),
        "canary evaluation path invalid",
    )
    evaluation_ref = external_file_ref(evaluation_path)
    evaluation = load_json(evaluation_path, "canary evaluation")
    require(evaluation.get("schema_version") == CANARY_EVALUATION_SCHEMA, "canary evaluation schema drift")
    require(evaluation.get("state") == "PASS", "canary evaluation did not PASS")
    require(evaluation.get("campaign_root") == str(root), "canary evaluation campaign drift")
    require(evaluation.get("backend_plan") == plan_ref, "canary evaluation plan drift/replay")
    require(evaluation.get("arming_gate") == pending["arming_gate"], "canary evaluation gate drift")
    require(
        evaluation.get("canary_pending") == external_file_ref(Path(contract["pending_path"])),
        "canary evaluation pending drift/replay",
    )
    require(evaluation.get("bridge_cell_done") == pending["bridge_cell_done"], "canary bridge result drift")
    require(evaluation.get("bridge_receipts") == pending["bridge_receipts"], "canary bridge receipts drift")
    require(evaluation.get("target_p02b") == pending["target_p02b"], "canary target P02B drift")
    require(evaluation.get("target_query_plan") == pending["target_query_plan"], "canary query plan drift")
    require(evaluation.get("target_lease") == pending["target_lease"], "canary lease drift")
    require(evaluation.get("evaluator") == contract["evaluator"], "canary evaluator identity drift")
    require(
        evaluation.get("mixed_lineage_plan") == contract["mixed_lineage_plan"],
        "canary mixed-lineage plan drift",
    )
    require(
        evaluation.get("comparability_contract_sha256")
        == contract["comparability_contract_sha256"],
        "canary comparability contract drift",
    )
    evidence_path = Path(contract["evidence_path"])
    evidence_ref = external_file_ref(evidence_path)
    require(evaluation.get("canary_evidence") == evidence_ref, "canary evidence ref drift")
    evidence = load_json(evidence_path, "canary evidence")
    require(evidence.get("schema_version") == contract["evidence_schema"], "canary evidence schema drift")
    require(
        evidence.get("state") == "PASS"
        and evidence.get("cell_key") == "seml0:bridge-canary",
        "canary evidence state/cell drift",
    )
    require(evidence.get("backend_plan") == plan_ref, "canary evidence plan drift/replay")
    require(evidence.get("bridge_cell_done") == pending["bridge_cell_done"], "canary evidence cell drift")
    require(evidence.get("bridge_receipts") == pending["bridge_receipts"], "canary evidence receipts drift")
    require(
        evidence.get("mixed_lineage_plan") == contract["mixed_lineage_plan"]
        and evidence.get("contract_sha256") == contract["comparability_contract_sha256"],
        "canary evidence contract drift",
    )
    require(
        evidence.get("validated_result_receipt")
        == pending["bridge_receipts"]["validated_result"]
        and evidence.get("p31_receipt") == pending["bridge_receipts"]["p31"],
        "canary evidence direct receipt binding drift",
    )
    prepared_ref = pending["bridge_receipts"]["prepared_command"]
    prepared = load_json(Path(prepared_ref["path"]), "bridge prepared-command receipt")
    p31_ref = pending["bridge_receipts"]["p31"]
    p31 = load_json(Path(p31_ref["path"]), "bridge P31 receipt")
    topology_ref = pending["bridge_receipts"]["command_topology"]
    topology = load_json(Path(topology_ref["path"]), "bridge command topology")
    validated_receipt_ref = pending["bridge_receipts"]["validated_result"]
    validated_receipt = load_json(
        Path(validated_receipt_ref["path"]), "bridge validated-result receipt"
    )
    require(
        validated_receipt.get("schema_version")
        == contract["validated_output_binding"]["receipt_schema"]
        and validated_receipt.get("state") == "PASS"
        and validated_receipt.get("mode") == "production"
        and validated_receipt.get("synthetic_test_only") is False
        and validated_receipt.get("fixture_only") is False
        and validated_receipt.get("cell_key") == "seml0:bridge-canary"
        and validated_receipt.get("ordinal") == 1
        and validated_receipt.get("backend_plan_sha256") == plan_ref["sha256"]
        and validated_receipt.get("target_p02b") == pending["target_p02b"],
        "canary validated-result receipt drift",
    )
    require(
        p31.get("command_topology") == topology_ref
        and validated_receipt.get("command_topology") == topology_ref
        and topology.get("single_binary_invocation") is True
        and topology.get("warmup_measured_same_process") is True
        and topology.get("warmup_runs") == contract["legacy_protocol"]["warmup_passes"]
        and topology.get("measured_repeats") == contract["legacy_protocol"]["measured_passes"]
        and topology.get("process_lifetime") == contract["legacy_protocol"]["process_lifetime"],
        "canary command-topology chain drift",
    )
    request = load_json(Path(prepared["request"]["path"]), "bridge prepared request")
    require(
        request.get("timing", {}).get("per_query_timeout_ms")
        == topology.get("per_query_timeout_ms")
        == contract["legacy_protocol"]["per_query_timeout_ms"],
        "canary request/command deadline drift",
    )
    adapter_ref = verify_external_file_ref(
        validated_receipt.get("adapter_result"), "canary validated adapter result"
    )
    require(
        evidence.get("validated_result") == adapter_ref,
        "canary evidence/receipt adapter-result mismatch",
    )
    adapter = load_json(Path(adapter_ref["path"]), "canary validated adapter result")
    require(
        adapter.get("schema_version")
        == contract["validated_output_binding"]["adapter_schema"]
        and adapter.get("system_id") == "seml0"
        and adapter.get("cell_key") == "seml0:bridge-canary"
        and adapter.get("ordinal") == 1
        and adapter.get("backend_plan") == plan_ref
        and adapter.get("target_p02b") == pending["target_p02b"]
        and adapter.get("final_cell_root")
        == str(Path(pending["bridge_cell_done"]["path"]).parent.resolve()),
        "canary validated adapter identity/backlink drift",
    )
    request_ref = verify_external_file_ref(
        prepared.get("request"), "bridge prepared adapter request"
    )
    require(adapter.get("request") == request_ref, "canary adapter request backlink drift")
    p31_manifest_ref = verify_external_file_ref(
        p31.get("run_manifest"), "bridge P31 run manifest"
    )
    p31_manifest = load_json(Path(p31_manifest_ref["path"]), "bridge P31 run manifest")
    adapter_p31 = adapter.get("p31")
    require(
        type(adapter_p31) is dict
        and adapter_p31.get("receipt") == p31_ref
        and adapter_p31.get("run_manifest") == p31_manifest_ref
        and adapter_p31.get("host") == p31_manifest.get("host"),
        "canary adapter P31 backlink drift",
    )
    require(
        adapter_p31.get("command_topology") == topology_ref
        and adapter.get("process_lifetime_binding", {}).get("command_topology")
        == topology_ref,
        "canary adapter command-topology backlink drift",
    )
    require(
        adapter.get("per_query_timeout_ms")
        == contract["legacy_protocol"]["per_query_timeout_ms"],
        "canary validated result deadline drift",
    )
    comparability_ref = external_file_ref(Path(contract["comparability_path"]))
    require(
        evaluation.get("comparability_receipt") == comparability_ref,
        "canary comparability receipt ref drift",
    )
    comparability = load_json(Path(comparability_ref["path"]), "canary comparability receipt")
    mixed = load_json(
        Path(contract["mixed_lineage_plan"]["path"]), "formal mixed-lineage plan"
    )
    formal_contract = mixed["incremental_plan"][
        "bridge_canary_comparability_contract"
    ]
    expected_identity = set(formal_contract["identity_exact_match"])
    expected_correctness = set(formal_contract["correctness"])
    expected_performance = {
        "completed_qps",
        "latency_p50_us",
        "latency_p95_us",
        "latency_p99_us",
    }
    require(
        comparability.get("schema_version")
        == "cidr-e01-bridge-canary-comparability-receipt-v1"
        and comparability.get("state") == "PASS"
        and comparability.get("mixed_lineage_plan") == contract["mixed_lineage_plan"]
        and comparability.get("canary_evidence") == evidence_ref
        and comparability.get("contract_sha256") == contract["comparability_contract_sha256"]
        and comparability.get("failures") == []
        and type(comparability.get("identity_checks")) is dict
        and set(comparability["identity_checks"]) == expected_identity
        and all(
            type(row) is dict and row.get("state") == "PASS"
            for row in comparability["identity_checks"].values()
        )
        and type(comparability.get("correctness_checks")) is dict
        and set(comparability["correctness_checks"]) == expected_correctness
        and all(state == "PASS" for state in comparability["correctness_checks"].values())
        and type(comparability.get("performance_checks")) is dict
        and set(comparability["performance_checks"]) == expected_performance
        and all(
            type(row) is dict and row.get("state") == "PASS"
            for row in comparability["performance_checks"].values()
        )
        and comparability.get("normalizer_release") is True,
        "canary comparability did not release",
    )
    accepted_path = Path(contract["accepted_path"])
    accepted = {
        "schema_version": CANARY_ACCEPTED_SCHEMA,
        "state": "PASS",
        "campaign_root": str(root),
        "backend_plan": dict(plan_ref),
        "canary_pending": external_file_ref(Path(contract["pending_path"])),
        "canary_evaluation": evaluation_ref,
        "bridge_cell_done": pending["bridge_cell_done"],
        "resume_from_ordinal": 2,
        **FALSE_ELIGIBILITY,
    }
    if os.path.lexists(accepted_path):
        require(
            accepted_path.is_file()
            and not accepted_path.is_symlink()
            and load_json(accepted_path, "CANARY_ACCEPTED") == accepted,
            "CANARY_ACCEPTED drift/replay",
        )
    else:
        atomic_json(accepted_path, accepted)
    return evaluation_ref


def _validate_receipt(
    staging: Path,
    role: str,
    *,
    cell_key: str,
    ordinal: int,
    plan_sha: str,
    expected_mode: str,
    target_p02b: Optional[Mapping[str, Any]] = None,
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
    if role == "command_topology":
        require(
            value.get("single_binary_invocation") is True
            and value.get("warmup_measured_same_process") is True
            and value.get("process_model")
            == "single-storage-bench-process-warmup-and-measured-v1",
            "command topology proof invalid",
        )
        require(
            type(value.get("per_query_timeout_ms")) is int
            and value["per_query_timeout_ms"] > 0,
            "command topology deadline invalid",
        )
    if role == "correctness":
        require(value.get("mismatch_queries") == 0, "correctness mismatch")
        require(value.get("timeout_queries") == 0, "correctness timeout")
    if role == "cleanup":
        require(value.get("mutable_clone_removed") is True, "mutable clone cleanup required")
        clone = value.get("mutable_clone")
        require(type(clone) is str and Path(clone).is_absolute(), "cleanup clone path required")
        require(
            not os.path.lexists(clone),
            "cleanup claims removed clone that still lexists, including dangling symlink",
        )
    if expected_mode == "production" and role in {"store_clone", "p31", "validated_result", "cleanup"}:
        require(value.get("target_p02b") == target_p02b, f"{role}: target P02B backlink drift")
    return value


def finalize_staging_cell(
    staging: Path,
    final: Path,
    *,
    cell_key: str,
    ordinal: int,
    plan_sha: str,
    expected_mode: str,
    target_p02b: Optional[Mapping[str, Any]] = None,
    target_query_plan: Optional[Mapping[str, Any]] = None,
    target_lease: Optional[Mapping[str, Any]] = None,
    publish_done: bool = True,
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
            target_p02b=target_p02b,
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
        "target_p02b": target_p02b,
        "target_query_plan": target_query_plan,
        "target_lease": target_lease,
        **FALSE_ELIGIBILITY,
    }
    if publish_done:
        atomic_json(staging / "CELL-DONE.json", done)
    final.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(final)
    return done


def validate_adapter_provenance_chain(
    final: Path,
    *,
    provenance: Mapping[str, Any],
    adapter: Mapping[str, Any],
    validated_receipt: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    request_ref: Mapping[str, Any],
    request: Mapping[str, Any],
    prepared: Mapping[str, Any],
    topology: Mapping[str, Any],
    cell_key: str,
    ordinal: int,
    plan_sha: str,
    target_p02b: Mapping[str, Any],
    target_query_plan: Mapping[str, Any],
    adapter_tool: Mapping[str, Any],
) -> None:
    """Verify the complete observed provenance chain semantically.

    File SHA validation alone is insufficient: an internally consistent
    rewritten chain must still be rejected when any semantic identity differs
    from the frozen request, P31 receipts, target P02B bundle, or published
    cell paths.
    """
    expected_top_keys = {
        "schema_version",
        "mode",
        "variant",
        "process_lifetime",
        "request",
        "repo",
        "binary",
        "store",
        "truth",
        "sample_plan",
        "id_map",
        "p02b",
        "p31_wrapper",
        "command",
        "raw_artifacts",
        "split_phase_binding",
    }
    require(
        set(provenance) == expected_top_keys,
        f"{cell_key}: adapter provenance top-level keys drift",
    )
    require(
        provenance.get("schema_version") == SEML0_PROVENANCE_SCHEMA,
        f"{cell_key}: adapter provenance schema drift",
    )
    require(provenance.get("mode") == "formal", f"{cell_key}: provenance mode drift")
    require(
        request.get("execution_mode") == "formal",
        f"{cell_key}: request execution mode drift",
    )
    target_ref = verify_external_file_ref(target_p02b, f"{cell_key}: target P02B")
    target = load_json(Path(target_ref["path"]), f"{cell_key}: target P02B")
    require(
        target.get("schema_version") == TARGET_P02B_SCHEMA
        and target.get("state") == "PASS",
        f"{cell_key}: target P02B state/schema drift",
    )
    static_inputs = target.get("static_inputs")
    bound_inputs = static_inputs.get("bound_inputs") if type(static_inputs) is dict else None
    require(type(bound_inputs) is dict, f"{cell_key}: target bound inputs missing")
    variant = target.get("variant")
    require(
        type(variant) is str
        and provenance.get("variant") == variant,
        f"{cell_key}: provenance variant drift",
    )
    process_lifetime = request.get("process_lifetime")
    require(
        type(process_lifetime) is str
        and provenance.get("process_lifetime") == process_lifetime
        and topology.get("process_lifetime") == process_lifetime,
        f"{cell_key}: provenance process lifetime drift",
    )

    repo_root = bound_inputs.get("repo_root")
    require(
        type(repo_root) is dict and set(repo_root) == {"path"},
        f"{cell_key}: target repo-root binding drift",
    )
    expected_repo = {
        "root": str(Path(repo_root["path"]).resolve()),
        "head": static_inputs.get("repo_head"),
        "clean": True,
        "status_sha256": hashlib.sha256(b"").hexdigest(),
    }
    require(
        provenance.get("repo") == expected_repo,
        f"{cell_key}: provenance repository drift",
    )

    binary = verify_external_file_ref(
        bound_inputs.get("binary"), f"{cell_key}: target binary"
    )
    request_binary = request.get("binary")
    require(
        type(request_binary) is dict
        and request_binary.get("path") == binary["path"]
        and request_binary.get("sha256") == binary["sha256"]
        and provenance.get("binary") == binary,
        f"{cell_key}: provenance binary drift",
    )

    clone_ref = external_file_ref(final / RECEIPT_PATHS["store_clone"])
    clone = load_json(final / RECEIPT_PATHS["store_clone"], f"{cell_key}: store clone")
    store_manifest = verify_external_file_ref(
        static_inputs.get("store_manifest"), f"{cell_key}: store manifest"
    )
    expected_tree_sha = static_inputs.get("store_tree_sha256")
    require(
        type(expected_tree_sha) is str
        and clone.get("source_tree_sha256") == expected_tree_sha,
        f"{cell_key}: store tree SHA drift",
    )
    require(
        provenance.get("store")
        == {
            "tree_sha256": expected_tree_sha,
            "manifest": store_manifest,
            "clone_receipt": clone_ref,
            "mutable_clone_removed_before_cell_publication": True,
        },
        f"{cell_key}: provenance store drift",
    )

    truth = verify_external_file_ref(
        bound_inputs.get("truth"), f"{cell_key}: target truth"
    )
    request_truth = request.get("truth")
    require(
        type(request_truth) is dict
        and request_truth.get("path") == truth["path"]
        and request_truth.get("sha256") == truth["sha256"]
        and type(request_truth.get("query_count")) is int
        and request_truth["query_count"] > 0
        and provenance.get("truth") == truth,
        f"{cell_key}: provenance truth drift",
    )
    query_plan = verify_external_file_ref(
        target_query_plan, f"{cell_key}: target query plan"
    )
    require(
        provenance.get("sample_plan")
        == {**query_plan, "query_count": request_truth["query_count"]},
        f"{cell_key}: provenance sample-plan drift",
    )
    id_map = bound_inputs.get("id_map_dir")
    require(
        type(id_map) is dict
        and set(id_map) == {"path"}
        and provenance.get("id_map")
        == {
            "directory": str(Path(id_map["path"]).resolve()),
            "bound_by_target_p02b": target_ref,
        },
        f"{cell_key}: provenance id-map drift",
    )
    require(
        provenance.get("p02b") == {"target_bundle": target_ref},
        f"{cell_key}: provenance P02B drift",
    )
    p31_wrapper = verify_external_file_ref(
        bound_inputs.get("p31_wrapper"), f"{cell_key}: target P31 wrapper"
    )
    require(
        provenance.get("p31_wrapper") == p31_wrapper,
        f"{cell_key}: provenance P31 wrapper drift",
    )

    expected_raw_names = {
        "p10-raw-result.json",
        "p10-raw-observations.tsv",
        "p10-raw-phase-events.jsonl",
    }
    raw_artifacts = provenance.get("raw_artifacts")
    require(
        type(raw_artifacts) is dict and set(raw_artifacts) == expected_raw_names,
        f"{cell_key}: provenance raw artifact set drift",
    )
    for name in sorted(expected_raw_names):
        expected_ref = external_file_ref(
            final / "adapter-output" / "seml0-raw" / name
        )
        require(
            raw_artifacts[name] == expected_ref,
            f"{cell_key}: provenance raw artifact {name} drift",
        )

    binding = provenance.get("split_phase_binding")
    required_binding = {
        "schema_version",
        "backend_plan",
        "target_p02b",
        "request",
        "p31_receipt",
        "p31_run_manifest",
        "command_topology",
        "adapter_tool",
        "adapter_result",
        "validated_artifacts",
        "clone_receipt",
        "cell_key",
        "ordinal",
        "campaign_root",
        "final_cell_root",
    }
    require(
        type(binding) is dict and set(binding) == required_binding,
        f"{cell_key}: split-phase provenance binding keys drift",
    )
    require(
        binding["schema_version"] == SPLIT_PROVENANCE_SCHEMA,
        f"{cell_key}: split-phase provenance schema drift",
    )
    require(
        binding["cell_key"] == cell_key
        and binding["ordinal"] == ordinal
        and binding["final_cell_root"] == str(final.resolve())
        and binding["campaign_root"] == str(final.resolve().parents[1]),
        f"{cell_key}: split-phase provenance cell/attempt drift",
    )
    backend_ref = verify_external_file_ref(
        binding["backend_plan"], f"{cell_key}: provenance backend plan"
    )
    require(
        backend_ref["sha256"] == plan_sha,
        f"{cell_key}: provenance backend plan drift",
    )
    require(
        binding["target_p02b"] == target_ref,
        f"{cell_key}: provenance target P02B drift",
    )
    require(
        binding["request"] == request_ref
        and provenance.get("request") == request_ref
        and prepared.get("request") == request_ref,
        f"{cell_key}: provenance request drift",
    )
    p31_ref = external_file_ref(final / RECEIPT_PATHS["p31"])
    topology_ref = external_file_ref(final / RECEIPT_PATHS["command_topology"])
    p31_manifest_ref = verify_external_file_ref(
        load_json(final / RECEIPT_PATHS["p31"], f"{cell_key}: P31").get(
            "run_manifest"
        ),
        f"{cell_key}: P31 run manifest",
    )
    require(
        binding["p31_receipt"] == p31_ref
        and binding["p31_run_manifest"] == p31_manifest_ref
        and binding["command_topology"] == topology_ref
        and binding["clone_receipt"] == clone_ref,
        f"{cell_key}: provenance receipt backlink drift",
    )
    require(
        binding["adapter_tool"] == adapter_tool
        and verify_external_file_ref(
            binding["adapter_tool"], f"{cell_key}: provenance adapter tool"
        )
        == adapter_tool,
        f"{cell_key}: provenance adapter tool drift",
    )
    require(
        binding["adapter_result"] == artifacts.get("adapter-result.json"),
        f"{cell_key}: provenance adapter-result drift",
    )
    expected_validated_artifacts = {
        name: artifacts[name]
        for name in ("adapter-result.json", "query-observations.tsv", "phase-events.jsonl")
    }
    require(
        binding["validated_artifacts"] == expected_validated_artifacts,
        f"{cell_key}: provenance validated artifacts drift",
    )
    for label, ref in (
        ("request", binding["request"]),
        ("P31 receipt", binding["p31_receipt"]),
        ("P31 manifest", binding["p31_run_manifest"]),
        ("command topology", binding["command_topology"]),
        ("adapter result", binding["adapter_result"]),
        ("clone receipt", binding["clone_receipt"]),
        *(
            (f"validated artifact {name}", ref)
            for name, ref in binding["validated_artifacts"].items()
        ),
    ):
        actual = verify_external_file_ref(ref, f"{cell_key}: provenance {label}")
        require(
            path_within(Path(actual["path"]), final),
            f"{cell_key}: provenance {label} retained staging/outside path",
        )

    p31_manifest = load_json(Path(p31_manifest_ref["path"]), f"{cell_key}: P31 manifest")
    command = provenance.get("command")
    require(
        type(command) is dict
        and set(command)
        == {
            "argv",
            "argv_sha256",
            "invocations",
            "exit_code",
            "root_pid",
            "run_manifest",
            "command_topology",
        }
        and command.get("invocations") == 1
        and command.get("exit_code") == 0
        and command.get("argv") == topology.get("argv")
        and command.get("argv") == prepared.get("binary_argv")
        and command.get("argv_sha256") == topology.get("argv_sha256")
        and command.get("argv_sha256") == canonical_sha(command.get("argv"))
        and command.get("root_pid") == topology.get("root_pid")
        and command.get("root_pid") == p31_manifest.get("root_pid")
        and command.get("run_manifest") == p31_manifest_ref
        and command.get("command_topology") == topology_ref,
        f"{cell_key}: provenance observed command drift",
    )
    adapter_p31 = adapter.get("p31")
    require(
        type(adapter_p31) is dict
        and adapter_p31.get("receipt") == p31_ref
        and adapter_p31.get("run_manifest") == p31_manifest_ref
        and adapter_p31.get("command_topology") == topology_ref
        and adapter_p31.get("host") == p31_manifest.get("host")
        and adapter.get("process_lifetime_binding", {}).get("command_topology")
        == topology_ref,
        f"{cell_key}: validated adapter P31/provenance chain drift",
    )
    require(
        validated_receipt.get("request") == request_ref
        and validated_receipt.get("p31_receipt") == p31_ref
        and validated_receipt.get("command_topology") == topology_ref,
        f"{cell_key}: validated receipt/provenance chain drift",
    )


def validate_final_cell(
    final: Path,
    *,
    cell_key: str,
    ordinal: int,
    plan_sha: str,
    expected_mode: str,
    target_p02b: Optional[Mapping[str, Any]] = None,
    target_query_plan: Optional[Mapping[str, Any]] = None,
    target_lease: Optional[Mapping[str, Any]] = None,
    adapter_tool: Optional[Mapping[str, Any]] = None,
    pending_done: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    require(final.is_dir() and not final.is_symlink(), f"{cell_key}: final cell invalid")
    if pending_done is None:
        done = load_json(final / "CELL-DONE.json", f"{cell_key} CELL-DONE")
    else:
        require(
            not os.path.lexists(final / "CELL-DONE.json"),
            f"{cell_key}: CELL-DONE published before deep validation",
        )
        done = dict(pending_done)
    require(done.get("schema_version") == CELL_SCHEMA, f"{cell_key}: CELL-DONE schema drift")
    require(done.get("state") == "PASS", f"{cell_key}: CELL-DONE state drift")
    require(done.get("mode") == expected_mode, f"{cell_key}: mode drift")
    require(done.get("cell_key") == cell_key and done.get("ordinal") == ordinal, f"{cell_key}: identity drift")
    require(done.get("backend_plan_sha256") == plan_sha, f"{cell_key}: plan SHA drift")
    if expected_mode == "production":
        require(done.get("target_p02b") == target_p02b, f"{cell_key}: target P02B backlink drift")
        require(done.get("target_query_plan") == target_query_plan, f"{cell_key}: target query-plan backlink drift")
        require(done.get("target_lease") == target_lease, f"{cell_key}: target lease backlink drift")
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
            target_p02b=target_p02b,
        )
    staging = final.parent.parent / "staging" / final.name
    require(not os.path.lexists(staging), f"{cell_key}: stale staging root remains")
    if expected_mode == "production":
        validated_receipt = load_json(final / RECEIPT_PATHS["validated_result"], "validated result receipt")
        adapter_ref = verify_external_file_ref(
            validated_receipt.get("adapter_result"), f"{cell_key}: adapter result"
        )
        require(
            path_within(Path(adapter_ref["path"]), final),
            f"{cell_key}: adapter result outside final root",
        )
        adapter = load_json(Path(adapter_ref["path"]), f"{cell_key}: adapter result")
        prepared = load_json(final / RECEIPT_PATHS["prepared_command"], "prepared command")
        request_ref = verify_external_file_ref(
            prepared.get("request"), f"{cell_key}: prepared request"
        )
        request = load_json(Path(request_ref["path"]), f"{cell_key}: prepared request")
        topology = load_json(
            final / RECEIPT_PATHS["command_topology"], f"{cell_key}: command topology"
        )
        deadline = request.get("timing", {}).get("per_query_timeout_ms")
        require(
            type(deadline) is int
            and deadline > 0
            and topology.get("per_query_timeout_ms") == deadline
            and adapter.get("per_query_timeout_ms") == deadline,
            f"{cell_key}: request/command/result deadline drift",
        )
        artifacts = adapter.get("adapter_artifacts")
        require(type(artifacts) is dict and artifacts, f"{cell_key}: adapter artifacts missing")
        for name, ref in artifacts.items():
            actual = verify_external_file_ref(ref, f"{cell_key}: adapter artifact {name}")
            require(
                path_within(Path(actual["path"]), final),
                f"{cell_key}: adapter artifact outside final root",
            )
        provenance_ref = verify_external_file_ref(
            validated_receipt.get("adapter_provenance"),
            f"{cell_key}: validated-result adapter provenance",
        )
        require(
            provenance_ref == artifacts.get("adapter-provenance.json"),
            f"{cell_key}: provenance/validated-artifact ref drift",
        )
        provenance = load_json(
            Path(provenance_ref["path"]), f"{cell_key}: adapter provenance"
        )
        require(
            adapter.get("adapter_provenance") == provenance,
            f"{cell_key}: embedded/file provenance drift",
        )
        require(
            target_p02b is not None
            and target_query_plan is not None
            and adapter_tool is not None,
            f"{cell_key}: production provenance expectations missing",
        )
        validate_adapter_provenance_chain(
            final,
            provenance=provenance,
            adapter=adapter,
            validated_receipt=validated_receipt,
            artifacts=artifacts,
            request_ref=request_ref,
            request=request,
            prepared=prepared,
            topology=topology,
            cell_key=cell_key,
            ordinal=ordinal,
            plan_sha=plan_sha,
            target_p02b=target_p02b,
            target_query_plan=target_query_plan,
            adapter_tool=adapter_tool,
        )
    return done


def inspect_resume_root(
    root: Path,
    plan: Mapping[str, Any],
    plan_sha: str,
    *,
    expected_mode: str = "production",
) -> Dict[str, Any]:
    if not os.path.lexists(root):
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
            target_p02b=row.get("runtime", {}).get("target_p02b"),
            target_query_plan=row.get("runtime", {}).get("target_query_plan"),
            target_lease=row.get("runtime", {}).get("target_lease"),
            adapter_tool=row.get("runtime", {}).get("adapter_tool"),
        )
        completed += 1
    done_path = root / "MATRIX-DONE.json"
    if os.path.lexists(done_path):
        require(completed == 4, "premature MATRIX-DONE")
        done = load_json(done_path, "MATRIX-DONE")
        require(done.get("schema_version") == DONE_SCHEMA, "MATRIX-DONE schema drift")
        require(done.get("completed_cells") == 4, "MATRIX-DONE count drift")
        require(done.get("backend_plan_sha256") == plan_sha, "MATRIX-DONE plan drift")
        require(
            done.get("canary_evaluation")
            == external_file_ref(Path(plan["canary_checkpoint"]["evaluation_path"])),
            "MATRIX-DONE canary evaluation drift",
        )
        require(
            done.get("canary_accepted")
            == external_file_ref(Path(plan["canary_checkpoint"]["accepted_path"])),
            "MATRIX-DONE canary acceptance drift",
        )
    if expected_mode == "production" and completed == 0:
        require(
            not os.path.lexists(root / "CANARY-PENDING.json")
            and not os.path.lexists(root / "CANARY-ACCEPTED.json"),
            "canary checkpoint exists before bridge completion",
        )
    if expected_mode == "production" and completed > 1:
        require(
            os.path.lexists(root / "CANARY-ACCEPTED.json"),
            "post-bridge cells exist without accepted canary",
        )
    return {"state": "RESUME", "completed_cells": completed}


def _default_runner(argv: list[str], cwd: Path, stdout: Path, stderr: Path) -> int:
    with stdout.open("wb") as out, stderr.open("wb") as err:
        completed = subprocess.run(argv, cwd=cwd, stdout=out, stderr=err, check=False)
    return completed.returncode


def expected_matrix_done(
    root: Path,
    plan: Mapping[str, Any],
    plan_sha: str,
    canary_evaluation_ref: Mapping[str, Any],
) -> Dict[str, Any]:
    cell_refs = []
    for row in plan["cells"]:
        done_path = Path(row["final_cell_root"]) / "CELL-DONE.json"
        require(
            done_path.is_file() and not done_path.is_symlink(),
            f"{row['cell_key']}: CELL-DONE missing for matrix publication",
        )
        cell_refs.append(
            {
                "cell_key": row["cell_key"],
                "cell_done_sha256": sha256_file(done_path),
            }
        )
    return {
        "schema_version": DONE_SCHEMA,
        "state": "PASS",
        "mode": "production",
        "synthetic_test_only": False,
        "strict_serial": True,
        "completed_cells": 4,
        "backend_plan_sha256": plan_sha,
        "cells": cell_refs,
        "canary_evaluation": dict(canary_evaluation_ref),
        "canary_accepted": external_file_ref(
            Path(plan["canary_checkpoint"]["accepted_path"])
        ),
        **FALSE_ELIGIBILITY,
    }


def execute_production(
    plan_path: Path,
    *,
    runner: Callable[[list[str], Path, Path, Path], int] = _default_runner,
) -> Dict[str, Any]:
    plan = validate_backend_plan(plan_path.resolve())
    require(plan["state"] == "READY", "backend plan is not READY")
    root = Path(plan["campaign_root"]).resolve()
    plan_sha = sha256_file(plan_path.resolve())
    plan_ref = external_file_ref(plan_path.resolve())
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
            pending_path = Path(plan["canary_checkpoint"]["pending_path"])
            require(
                pending_path.is_file()
                and not pending_path.is_symlink()
                and load_json(pending_path, "CANARY_PENDING")
                == expected_canary_pending(root, plan, plan_ref),
                "completed campaign CANARY_PENDING drift",
            )
            pending = load_json(pending_path, "CANARY_PENDING")
            evaluation_ref = consume_canary_evaluation(
                root, plan, plan_ref, pending
            )
            require(
                evaluation_ref is not None,
                "completed campaign accepted canary evaluation missing",
            )
            expected_done = expected_matrix_done(
                root, plan, plan_sha, evaluation_ref
            )
            done_path = root / "MATRIX-DONE.json"
            if os.path.lexists(done_path):
                require(
                    done_path.is_file()
                    and not done_path.is_symlink()
                    and load_json(done_path, "MATRIX-DONE") == expected_done,
                    "MATRIX-DONE full-content drift",
                )
            else:
                atomic_json(done_path, expected_done)
            return expected_done
    canary_evaluation_ref: Optional[Dict[str, Any]] = None
    if completed == 1:
        pending = ensure_canary_pending(root, plan, plan_ref)
        canary_evaluation_ref = consume_canary_evaluation(root, plan, plan_ref, pending)
        if canary_evaluation_ref is None:
            return {
                "schema_version": CANARY_PENDING_SCHEMA,
                "state": "CANARY_PENDING",
                "completed_cells": 1,
                "backend_plan_sha256": plan_sha,
                "matrix_terminal": False,
                **FALSE_ELIGIBILITY,
            }
    if completed > 1:
        pending = ensure_canary_pending(root, plan, plan_ref)
        canary_evaluation_ref = consume_canary_evaluation(root, plan, plan_ref, pending)
        require(canary_evaluation_ref is not None, "accepted canary evaluation missing")
    rows_to_run = plan["cells"][completed : 1 if completed == 0 else None]
    for row in rows_to_run:
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
            unpublished_done = finalize_staging_cell(
                staging,
                final,
                cell_key=row["cell_key"],
                ordinal=row["ordinal"],
                plan_sha=plan_sha,
                expected_mode="production",
                target_p02b=row["runtime"]["target_p02b"],
                target_query_plan=row["runtime"]["target_query_plan"],
                target_lease=row["runtime"]["target_lease"],
                publish_done=False,
            )
            validate_final_cell(
                final,
                cell_key=row["cell_key"],
                ordinal=row["ordinal"],
                plan_sha=plan_sha,
                expected_mode="production",
                target_p02b=row["runtime"]["target_p02b"],
                target_query_plan=row["runtime"]["target_query_plan"],
                target_lease=row["runtime"]["target_lease"],
                adapter_tool=row["runtime"]["adapter_tool"],
                pending_done=unpublished_done,
            )
            atomic_json(final / "CELL-DONE.json", unpublished_done)
            if row["ordinal"] == 1:
                ensure_canary_pending(root, plan, plan_ref)
                return {
                    "schema_version": CANARY_PENDING_SCHEMA,
                    "state": "CANARY_PENDING",
                    "completed_cells": 1,
                    "backend_plan_sha256": plan_sha,
                    "matrix_terminal": False,
                    **FALSE_ELIGIBILITY,
                }
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
    require(canary_evaluation_ref is not None, "accepted canary evaluation missing")
    done = expected_matrix_done(root, plan, plan_sha, canary_evaluation_ref)
    atomic_json(root / "MATRIX-DONE.json", done)
    return done


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-plan", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = execute_production(args.backend_plan)
        print(
            json.dumps(
                {
                    "state": result["state"],
                    "completed_cells": result.get("completed_cells", 0),
                },
                sort_keys=True,
            )
        )
        return 0
    except (BackendError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
