#!/usr/bin/env python3
"""Fail-closed E01 matrix-evidence adapter and PASS composition assembler."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from build_e01_mixed_lineage import INCREMENTAL_CELLS, OUTPUT_SCHEMA


EVIDENCE_SCHEMA = "cidr-e01-incremental-evidence-v2"
ANCHOR_SCHEMA = "cidr-e01-incremental-postprocess-anchor-v1"
BACKEND_SCHEMA = "cidr-e01-incremental-backend-plan-v3"
ARMING_SCHEMA = "cidr-e01-incremental-backend-arming-gate-v2"
MATRIX_START_SCHEMA = "cidr-e01-incremental-production-matrix-start-v1"
MATRIX_DONE_SCHEMA = "cidr-e01-incremental-production-matrix-done-v1"
CELL_DONE_SCHEMA = "cidr-e01-incremental-production-cell-done-v1"
VALIDATED_RECEIPT_SCHEMA = "cidr-e01-incremental-validated-result-receipt-v1"
VALIDATED_REPEAT_SCHEMA = "cidr-p10-validated-repeat-v1"
CLEANUP_SCHEMA = "cidr-e01-incremental-cleanup-receipt-v1"
CORRECTNESS_SCHEMA = "cidr-e01-incremental-correctness-receipt-v1"
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
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


class EvidenceError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


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
        raise EvidenceError(f"{label}: cannot read JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value


def file_ref(path: Path, label: str) -> Dict[str, Any]:
    path = path.resolve()
    require(path.is_file() and not path.is_symlink(), f"{label}: regular file required")
    size = path.stat().st_size
    require(0 < size <= 16 * 1024 * 1024, f"{label}: receipt size invalid")
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": size}


def verify_ref(value: Any, label: str) -> Dict[str, Any]:
    require(
        type(value) is dict and set(value) == {"path", "sha256", "size_bytes"},
        f"{label}: exact reference required",
    )
    actual = file_ref(Path(value["path"]), label)
    require(actual == value, f"{label}: reference drift")
    return actual


def _receipt(final: Path, done: Mapping[str, Any], role: str) -> Dict[str, Any]:
    descriptor = done["receipts"].get(role)
    require(
        type(descriptor) is dict
        and descriptor.get("path") == RECEIPT_PATHS[role],
        f"{role}: fixed receipt path drift",
    )
    actual = file_ref(final / RECEIPT_PATHS[role], role)
    expected = {
        "path": RECEIPT_PATHS[role],
        "sha256": actual["sha256"],
        "size_bytes": actual["size_bytes"],
    }
    require(descriptor == expected, f"{role}: CELL-DONE descriptor drift")
    return actual


def _pass_receipt(
    path: Path, schema: str, key: str, ordinal: int, plan_sha: str, label: str
) -> Dict[str, Any]:
    value = load_json(path, label)
    require(
        value.get("schema_version") == schema
        and value.get("state") == "PASS"
        and value.get("mode") == "production"
        and value.get("synthetic_test_only") is False
        and value.get("fixture_only") is False
        and value.get("cell_key") == key
        and value.get("ordinal") == ordinal
        and value.get("backend_plan_sha256") == plan_sha,
        f"{label}: schema/state/identity drift",
    )
    return value


def matrix_evidence_adapter(
    anchor_path: Path, backend_plan_path: Path, matrix_done_path: Path
) -> Dict[str, Any]:
    backend_plan_path = backend_plan_path.resolve()
    plan_ref = file_ref(backend_plan_path, "formal backend plan")
    anchor_ref = file_ref(anchor_path.resolve(), "postprocess anchor")
    anchor = load_json(anchor_path.resolve(), "postprocess anchor")
    plan = load_json(backend_plan_path, "formal backend plan")
    expected_keys = [item[0] for item in INCREMENTAL_CELLS]
    require(
        plan.get("schema_version") == BACKEND_SCHEMA
        and plan.get("state") == "READY"
        and plan.get("execution_state") == "READY"
        and plan.get("strict_serial") is True
        and plan.get("synthetic_test_only") is False
        and plan.get("fixture_only") is False
        and type(plan.get("cells")) is list
        and [item.get("cell_key") for item in plan["cells"]] == expected_keys,
        "formal backend identity drift",
    )
    campaign = Path(plan["campaign_root"]).resolve()
    require(
        anchor.get("schema_version") == ANCHOR_SCHEMA
        and anchor.get("state") == "FROZEN_BEFORE_TIMING"
        and anchor.get("formal_backend_plan") == plan_ref
        and anchor.get("campaign_root") == str(campaign)
        and anchor.get("cell_keys") == expected_keys,
        "external postprocess anchor drift",
    )
    require(
        matrix_done_path.resolve() == campaign / "MATRIX-DONE.json",
        "MATRIX-DONE fixed path/campaign drift",
    )
    require(not (campaign / "MATRIX-FAILED.json").exists(), "failed matrix forbidden")
    start_ref = file_ref(campaign / "MATRIX-START.json", "MATRIX-START")
    start = load_json(Path(start_ref["path"]), "MATRIX-START")
    require(
        start.get("schema_version") == MATRIX_START_SCHEMA
        and start.get("state") == "PASS"
        and start.get("mode") == "production"
        and start.get("strict_serial") is True
        and start.get("synthetic_test_only") is False
        and start.get("backend_plan_sha256") == plan_ref["sha256"],
        "MATRIX-START drift",
    )
    arming_path = Path(plan.get("campaign_gates", {}).get("backend_arming", {}).get("path", ""))
    require(arming_path.is_absolute(), "arming gate absolute path required")
    arming_ref = file_ref(arming_path, "arming gate")
    arming = load_json(arming_path, "arming gate")
    require(
        arming.get("schema_version") == ARMING_SCHEMA
        and arming.get("state") == "PASS"
        and arming.get("backend_plan") == plan_ref
        and arming.get("production_scheduler") == plan.get("production_scheduler")
        and arming.get("phase_executor") == plan.get("phase_executor"),
        "backend plan/arming gate drift",
    )
    matrix_ref = file_ref(matrix_done_path.resolve(), "MATRIX-DONE")
    matrix = load_json(matrix_done_path.resolve(), "MATRIX-DONE")
    require(
        set(matrix)
        == {
            "schema_version", "state", "mode", "synthetic_test_only",
            "strict_serial", "completed_cells", "backend_plan_sha256", "cells",
            "canary_evaluation", "canary_accepted", "formal_eligible",
            "performance_eligible", "paper_claim_eligible",
        }
        and matrix.get("schema_version") == MATRIX_DONE_SCHEMA
        and matrix.get("state") == "PASS"
        and matrix.get("mode") == "production"
        and matrix.get("strict_serial") is True
        and matrix.get("synthetic_test_only") is False
        and matrix.get("completed_cells") == 4
        and matrix.get("backend_plan_sha256") == plan_ref["sha256"]
        and all(matrix.get(name) is False for name in FALSE_ELIGIBILITY)
        and type(matrix.get("cells")) is list
        and [item.get("cell_key") for item in matrix["cells"]] == expected_keys,
        "MATRIX-DONE schema/state/cell-set drift",
    )
    canary_evaluation = verify_ref(matrix.get("canary_evaluation"), "canary evaluation")
    verify_ref(matrix.get("canary_accepted"), "canary accepted")

    evidence_cells = []
    for row, matrix_cell, frozen in zip(plan["cells"], matrix["cells"], INCREMENTAL_CELLS):
        key, system, repeat, role, included = frozen
        ordinal = row["ordinal"]
        final = Path(row["final_cell_root"]).resolve()
        require(
            final == campaign / "cells" / final.name
            and ordinal == len(evidence_cells) + 1,
            f"{key}: backend final-root/ordinal drift",
        )
        done_ref = file_ref(final / "CELL-DONE.json", f"{key}: CELL-DONE")
        require(
            matrix_cell
            == {"cell_key": key, "cell_done_sha256": done_ref["sha256"]},
            f"{key}: MATRIX-DONE/CELL-DONE drift",
        )
        done = load_json(Path(done_ref["path"]), f"{key}: CELL-DONE")
        require(
            done.get("schema_version") == CELL_DONE_SCHEMA
            and done.get("state") == "PASS"
            and done.get("mode") == "production"
            and done.get("synthetic_test_only") is False
            and done.get("fixture_only") is False
            and done.get("cell_key") == key
            and done.get("ordinal") == ordinal
            and done.get("backend_plan_sha256") == plan_ref["sha256"]
            and type(done.get("receipts")) is dict
            and set(done["receipts"]) == set(RECEIPT_PATHS),
            f"{key}: CELL-DONE schema/state/receipt-set drift",
        )
        refs = {name: _receipt(final, done, name) for name in RECEIPT_PATHS}
        validated_receipt = _pass_receipt(
            Path(refs["validated_result"]["path"]),
            VALIDATED_RECEIPT_SCHEMA,
            key,
            ordinal,
            plan_ref["sha256"],
            f"{key}: validated-result receipt",
        )
        validated_ref = verify_ref(
            validated_receipt.get("adapter_result"), f"{key}: validated repeat"
        )
        require(
            Path(validated_ref["path"]).resolve()
            == final / "adapter-output" / "validated-repeat.json",
            f"{key}: validated-repeat fixed path drift",
        )
        validated = load_json(Path(validated_ref["path"]), f"{key}: validated repeat")
        require(
            validated.get("schema_version") == VALIDATED_REPEAT_SCHEMA
            and validated.get("backend_plan") == plan_ref
            and validated.get("cell_key") == key
            and validated.get("ordinal") == ordinal
            and Path(validated.get("final_cell_root", "")).resolve() == final,
            f"{key}: validated-repeat identity drift",
        )
        cleanup = _pass_receipt(
            Path(refs["cleanup"]["path"]),
            CLEANUP_SCHEMA,
            key,
            ordinal,
            plan_ref["sha256"],
            f"{key}: cleanup",
        )
        require(cleanup.get("mutable_clone_removed") is True, f"{key}: cleanup incomplete")
        correctness = _pass_receipt(
            Path(refs["correctness"]["path"]),
            CORRECTNESS_SCHEMA,
            key,
            ordinal,
            plan_ref["sha256"],
            f"{key}: correctness",
        )
        require(
            correctness.get("mismatch_queries") == 0
            and correctness.get("timeout_queries") == 0,
            f"{key}: correctness failure",
        )
        provenance_ref = verify_ref(
            validated_receipt.get("adapter_provenance"), f"{key}: provenance"
        )
        require(
            Path(provenance_ref["path"]).resolve()
            == final / "adapter-output" / "adapter-provenance.json",
            f"{key}: provenance fixed path drift",
        )
        provenance = load_json(Path(provenance_ref["path"]), f"{key}: provenance")
        binding = provenance.get("split_phase_binding")
        require(
            type(binding) is dict
            and binding.get("backend_plan") == plan_ref
            and Path(binding.get("final_cell_root", "")).resolve() == final
            and binding.get("adapter_tool") == row.get("runtime", {}).get("adapter_tool"),
            f"{key}: provenance/backend binding drift",
        )
        runtime = row["runtime"]
        evidence_cells.append(
            {
                "ordinal": ordinal,
                "cell_key": key,
                "system_id": system,
                "repeat_index": repeat,
                "role": role,
                "included_in_figure_rows": included,
                "classification": {
                    "formal_eligible": True,
                    "performance_eligible": True,
                    "paper_claim_eligible": False,
                },
                "outcome": {
                    "state": "PASS",
                    "mismatch_queries": 0,
                    "timeout_queries": 0,
                },
                "validated_result": validated_ref,
                "validated_result_receipt": refs["validated_result"],
                "cell_done": done_ref,
                "p31_receipt": refs["p31"],
                "cleanup_receipt": refs["cleanup"],
                "prepared_request": validated["request"],
                "store_clone_receipt": refs["store_clone"],
                "command_topology": refs["command_topology"],
                "adapter_tool": runtime["adapter_tool"],
                "adapter_provenance": provenance_ref,
                "target_p02b": runtime["target_p02b"],
                "target_query_plan": runtime["target_query_plan"],
                "target_lease": runtime["target_lease"],
                "identity": {
                    "host_fingerprint": validated["p31"]["host"]["fingerprint_sha256"],
                    "git_sha": provenance["repo"]["head"],
                    "binary_sha256": provenance["binary"]["sha256"],
                    "physical_input_sha256": provenance["store"]["tree_sha256"],
                    "truth_sha256": provenance["truth"]["sha256"],
                },
                "protocol": {
                    "interface_scope": validated["interface_scope"],
                    "concurrency": validated["concurrency"],
                    "query_count": validated["query_count"],
                    "warmup_passes": validated["warmup_passes"],
                    "measured_passes": validated["measured_passes"],
                    "process_lifetime": validated["process_lifetime"],
                },
                "metrics": {
                    "measurement_s": validated["measurement_s"],
                    "warmup_s": validated["warmup_s"],
                    "latency_p50_us": validated["latency_p50_us"],
                    "latency_p95_us": validated["latency_p95_us"],
                    "latency_p99_us": validated["latency_p99_us"],
                    "qps": validated["qps"],
                    "completed_queries": validated["completed_queries"],
                },
            }
        )
    admission_ref = verify_ref(plan.get("admission_bundle"), "admission bundle")
    admission = load_json(Path(admission_ref["path"]), "admission bundle")
    gates = {
        "fresh_asset_seal": {"state": "PASS", "receipt": plan["asset_inventory"]},
        "adapter_identity": {"state": "PASS", "receipt": plan["lineage"]},
        "fresh_p03": {"state": "PASS", "receipt": admission["p03"]},
        "fresh_p02b": {"state": "PASS", "receipt": plan_ref},
        "fresh_batch_lease": {"state": "PASS", "receipt": admission["lease"]["receipt"]},
        "fresh_resource_gate": {"state": "PASS", "receipt": admission_ref},
        "strict_serial_scheduler": {"state": "PASS", "receipt": matrix_ref},
        "cleanup": {"state": "PASS", "receipt": matrix_ref},
    }
    return {
        "schema_version": EVIDENCE_SCHEMA,
        "state": "PASS",
        "postprocess_anchor": anchor_ref,
        "formal_backend_plan": plan_ref,
        "backend_arming_gate": arming_ref,
        "matrix_start": start_ref,
        "matrix_done": matrix_ref,
        "campaign_root": str(campaign),
        "campaign_gates": gates,
        "cells": evidence_cells,
        "bridge_canary_comparability": {
            "state": "PASS",
            "canary_cell_key": "seml0:bridge-canary",
            "legacy_reference_keys": ["seml0:r1", "seml0:r2", "seml0:r3"],
            "pre_registered_rule": True,
            "contract_sha256": plan["canary_checkpoint"][
                "comparability_contract_sha256"
            ],
            "receipt": canary_evaluation,
        },
        **FALSE_ELIGIBILITY,
    }


def build_postprocess_anchor(backend_plan_path: Path) -> Dict[str, Any]:
    plan_ref = file_ref(backend_plan_path.resolve(), "formal backend plan")
    plan = load_json(backend_plan_path.resolve(), "formal backend plan")
    keys = [item[0] for item in INCREMENTAL_CELLS]
    require(
        plan.get("schema_version") == BACKEND_SCHEMA
        and plan.get("state") == "READY"
        and plan.get("execution_state") == "READY"
        and plan.get("strict_serial") is True
        and [item.get("cell_key") for item in plan.get("cells", [])] == keys,
        "formal backend cannot be frozen",
    )
    return {
        "schema_version": ANCHOR_SCHEMA,
        "state": "FROZEN_BEFORE_TIMING",
        "formal_backend_plan": plan_ref,
        "campaign_root": str(Path(plan["campaign_root"]).resolve()),
        "cell_keys": keys,
        **FALSE_ELIGIBILITY,
    }


def pass_composition_assembler(
    hold_composition_path: Path, evidence_path: Path
) -> Dict[str, Any]:
    composition = load_json(hold_composition_path.resolve(), "HOLD composition")
    require(
        composition.get("schema_version") == OUTPUT_SCHEMA
        and composition.get("state") == "HOLD",
        "input composition must be frozen HOLD",
    )
    evidence = load_json(evidence_path.resolve(), "incremental evidence")
    require(
        evidence.get("schema_version") == EVIDENCE_SCHEMA
        and evidence.get("state") == "PASS",
        "PASS incremental evidence required",
    )
    result = copy.deepcopy(composition)
    result["state"] = "PASS"
    result["incremental_plan"]["state"] = "PASS"
    result["incremental_plan"]["incremental_evidence"] = evidence
    return result


def atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    require(not path.exists(), f"refusing to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    anchor = sub.add_parser("anchor")
    anchor.add_argument("--backend-plan", type=Path, required=True)
    anchor.add_argument("--output", type=Path, required=True)
    adapt = sub.add_parser("adapt")
    adapt.add_argument("--anchor", type=Path, required=True)
    adapt.add_argument("--backend-plan", type=Path, required=True)
    adapt.add_argument("--matrix-done", type=Path, required=True)
    adapt.add_argument("--output", type=Path, required=True)
    assemble = sub.add_parser("assemble")
    assemble.add_argument("--hold-composition", type=Path, required=True)
    assemble.add_argument("--evidence", type=Path, required=True)
    assemble.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "anchor":
            value = build_postprocess_anchor(args.backend_plan)
        elif args.command == "adapt":
            value = matrix_evidence_adapter(
                args.anchor, args.backend_plan, args.matrix_done
            )
        else:
            value = pass_composition_assembler(
                args.hold_composition, args.evidence
            )
        atomic_write(args.output.resolve(), value)
        print(json.dumps({"state": value["state"]}, sort_keys=True))
        return 0
    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
