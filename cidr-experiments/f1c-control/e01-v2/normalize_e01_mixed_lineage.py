#!/usr/bin/env python3
"""Fail-closed normalizer for a completed E01 mixed-lineage composition.

Pending plans are valid engineering artifacts but are never normalizer input.
Output is allowed only after four fresh cells, all campaign gates, and the
SemL0 bridge-canary comparability gate are explicitly PASS.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from build_e01_mixed_lineage import (
    INCREMENTAL_CELLS,
    LEGACY_KEYS,
    OUTPUT_SCHEMA,
    CompositionError,
    read_json,
    sha256_file,
)


TSV_COLUMNS = (
    "experiment_id",
    "run_key",
    "system_key",
    "repeat_index",
    "lineage_class",
    "plot_role",
    "formal_eligible",
    "performance_eligible",
    "paper_claim_eligible",
    "host_fingerprint",
    "git_sha",
    "binary_sha256",
    "physical_input_sha256",
    "truth_sha256",
    "interface_scope",
    "concurrency",
    "query_count",
    "warmup_passes",
    "measured_passes",
    "measurement_s",
    "warmup_s",
    "latency_p50_us",
    "latency_p95_us",
    "latency_p99_us",
    "completed_qps",
    "completed_queries",
    "timeout_queries",
    "mismatch_queries",
    "validated_result_sha256",
    "p31_evidence_sha256",
)
REQUIRED_GATES = (
    "fresh_asset_seal",
    "adapter_identity",
    "fresh_p03",
    "fresh_p02b",
    "fresh_batch_lease",
    "fresh_resource_gate",
    "strict_serial_scheduler",
    "cleanup",
)
TARGET_P02B_SCHEMA = "cidr-e01-target-specific-p02b-v1"
TARGET_VARIANTS = {"budg-b64", "naive"}


def _asset_ref(value: Any, label: str, *, verify: bool = True) -> Dict[str, Any]:
    if type(value) is not dict or set(value) != {"path", "sha256", "size_bytes"}:
        raise CompositionError(f"{label}: exact asset reference required")
    path = Path(value["path"])
    if type(value["path"]) is not str or not Path(value["path"]).is_absolute():
        raise CompositionError(f"{label}: absolute path required")
    if type(value["sha256"]) is not str or len(value["sha256"]) != 64:
        raise CompositionError(f"{label}: SHA-256 required")
    if type(value["size_bytes"]) is not int or value["size_bytes"] < 0:
        raise CompositionError(f"{label}: nonnegative size required")
    if verify:
        if not path.is_file() or path.is_symlink():
            raise CompositionError(f"{label}: regular receipt file required")
        if path.stat().st_size != value["size_bytes"] or sha256_file(path) != value["sha256"]:
            raise CompositionError(f"{label}: frozen receipt drift")
    return dict(value)


def _legacy_rows(value: Mapping[str, Any]) -> List[Dict[str, Any]]:
    cells = value.get("legacy_cells")
    if type(cells) is not list or len(cells) != 18:
        raise CompositionError("legacy_cells must contain 18 rows")
    if [cell.get("cell_key") for cell in cells] != list(LEGACY_KEYS):
        raise CompositionError("legacy cell order/key set drift")
    rows = []
    for cell in cells:
        key = cell["cell_key"]
        if cell.get("lineage_class") != "legacy_validated":
            raise CompositionError(f"{key}: lineage class drift")
        classification = cell.get("classification")
        if type(classification) is not dict:
            raise CompositionError(f"{key}: classification object required")
        for name in ("formal_eligible", "performance_eligible", "paper_claim_eligible"):
            if classification.get(name) is not False:
                raise CompositionError(f"{key}: legacy {name} must remain false")
        if classification.get("formal_upgrade_performed") is not False:
            raise CompositionError(f"{key}: legacy formal upgrade is forbidden")
        receipts = cell.get("receipts")
        if type(receipts) is not dict:
            raise CompositionError(f"{key}: receipts required")
        validated = _asset_ref(receipts.get("validated_result"), f"{key}.validated_result")
        bridge = receipts.get("p31_bridge")
        if type(bridge) is not dict or bridge.get("state") != "PASS":
            raise CompositionError(f"{key}: P31 bridge PASS required")
        if bridge.get("mode") == "direct_pass_receipts":
            p31 = _asset_ref(bridge.get("validation"), f"{key}.p31.validation")
            _asset_ref(bridge.get("done"), f"{key}.p31.done")
        elif bridge.get("mode") == "read_only_post_timing_revalidation":
            p31 = _asset_ref(
                bridge.get("conditional_revalidation"), f"{key}.p31.revalidation"
            )
            _asset_ref(
                bridge.get("post_timing_revalidation"), f"{key}.p31.post_timing"
            )
            if bridge.get("original_artifacts_modified") is not False:
                raise CompositionError(f"{key}: original artifacts were modified")
        else:
            raise CompositionError(f"{key}: unsupported P31 bridge")
        identity = cell["identity"]
        protocol = cell["protocol"]
        metrics = cell["metrics"]
        rows.append(
            {
                "experiment_id": "E01",
                "run_key": key,
                "system_key": cell["system_id"],
                "repeat_index": cell["repeat_index"],
                "lineage_class": "legacy_validated",
                "plot_role": "figure_row",
                "formal_eligible": False,
                "performance_eligible": False,
                "paper_claim_eligible": False,
                "host_fingerprint": identity["host_fingerprint"],
                "git_sha": identity["git_sha"],
                "binary_sha256": identity["binary_sha256"],
                "physical_input_sha256": identity["physical_input_sha256"],
                "truth_sha256": identity["truth_sha256"],
                "interface_scope": protocol["interface_scope"],
                "concurrency": protocol["concurrency"],
                "query_count": protocol["query_count"],
                "warmup_passes": protocol["warmup_passes"],
                "measured_passes": protocol["measured_passes"],
                "measurement_s": metrics["measurement_s"],
                "warmup_s": metrics["warmup_s"],
                "latency_p50_us": metrics["latency_p50_us"],
                "latency_p95_us": metrics["latency_p95_us"],
                "latency_p99_us": metrics["latency_p99_us"],
                "completed_qps": metrics["qps"],
                "completed_queries": metrics["completed_queries"],
                "timeout_queries": metrics["timeout_queries"],
                "mismatch_queries": metrics["mismatch_queries"],
                "validated_result_sha256": validated["sha256"],
                "p31_evidence_sha256": p31["sha256"],
            }
        )
    return rows


def _fresh_rows(value: Mapping[str, Any]) -> List[Dict[str, Any]]:
    plan = value.get("incremental_plan")
    if type(plan) is not dict:
        raise CompositionError("incremental_plan object required")
    if plan.get("state") != "PASS":
        raise CompositionError("pre-output gate: incremental plan is not PASS")
    contract = plan.get("bridge_canary_comparability_contract")
    if type(contract) is not dict:
        raise CompositionError("pre-output gate: bridge canary contract is absent")
    contract_body = dict(contract)
    contract_sha = contract_body.pop("contract_sha256", None)
    actual_contract_sha = hashlib.sha256(
        json.dumps(contract_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if contract_sha != actual_contract_sha:
        raise CompositionError("pre-output gate: bridge canary contract SHA drift")
    if (
        contract.get("schema_version")
        != "cidr-e01-bridge-canary-comparability-contract-v1"
        or contract.get("frozen_before_timing") is not True
        or contract.get("candidate_cell_key") != "seml0:bridge-canary"
        or contract.get("legacy_reference_keys")
        != ["seml0:r1", "seml0:r2", "seml0:r3"]
    ):
        raise CompositionError("pre-output gate: bridge canary contract identity drift")
    evidence = plan.get("incremental_evidence")
    if type(evidence) is not dict or evidence.get("state") != "PASS":
        raise CompositionError("pre-output gate: fresh incremental evidence is absent")
    gates = evidence.get("campaign_gates")
    if type(gates) is not dict or set(gates) != set(REQUIRED_GATES):
        raise CompositionError("pre-output gate: exact fresh campaign gate set required")
    for gate in REQUIRED_GATES:
        item = gates[gate]
        if type(item) is not dict or item.get("state") != "PASS":
            raise CompositionError(f"pre-output gate: {gate} is not PASS")
        _asset_ref(item.get("receipt"), f"campaign_gates.{gate}")
    p02b_ref = _asset_ref(gates["fresh_p02b"]["receipt"], "campaign_gates.fresh_p02b")
    p02b_gate = read_json(Path(p02b_ref["path"]), "campaign_gates.fresh_p02b")
    target_refs = p02b_gate.get("target_p02b")
    if type(target_refs) is not dict or set(target_refs) != TARGET_VARIANTS:
        raise CompositionError("fresh_p02b: exact budg-b64/naive target map required")
    target_bundles = {}
    for variant in sorted(TARGET_VARIANTS):
        ref = _asset_ref(target_refs[variant], f"fresh_p02b.{variant}")
        bundle = read_json(Path(ref["path"]), f"fresh_p02b.{variant}")
        if (
            bundle.get("schema_version") != TARGET_P02B_SCHEMA
            or bundle.get("state") != "PASS"
            or bundle.get("variant") != variant
            or bundle.get("store_unchanged") is not True
            or bundle.get("store_pre") != bundle.get("store_post")
            or bundle.get("store_pre", {}).get("full_tree_hash_performed") is not True
        ):
            raise CompositionError(f"fresh_p02b.{variant}: target bundle drift")
        _asset_ref(bundle.get("lease"), f"fresh_p02b.{variant}.lease")
        _asset_ref(bundle.get("static_inputs", {}).get("query_plan"), f"fresh_p02b.{variant}.query_plan")
        target_bundles[variant] = bundle
    cells = evidence.get("cells")
    expected_keys = [item[0] for item in INCREMENTAL_CELLS]
    if type(cells) is not list or [cell.get("cell_key") for cell in cells] != expected_keys:
        raise CompositionError("pre-output gate: exact four-cell evidence order required")
    rows = []
    for cell, expected in zip(cells, INCREMENTAL_CELLS):
        key, system, repeat, role, included = expected
        if (
            cell.get("system_id") != system
            or cell.get("repeat_index") != repeat
            or cell.get("role") != role
            or cell.get("included_in_figure_rows") is not included
        ):
            raise CompositionError(f"{key}: incremental cell identity/role drift")
        classification = cell.get("classification")
        if type(classification) is not dict:
            raise CompositionError(f"{key}: classification required")
        for name in ("formal_eligible", "performance_eligible"):
            if classification.get(name) is not True:
                raise CompositionError(f"{key}: fresh {name}=true required")
        if classification.get("paper_claim_eligible") is not False:
            raise CompositionError(f"{key}: paper claim must remain closed")
        outcome = cell.get("outcome")
        if type(outcome) is not dict or outcome.get("state") != "PASS":
            raise CompositionError(f"{key}: PASS outcome required")
        if outcome.get("mismatch_queries") != 0 or outcome.get("timeout_queries") != 0:
            raise CompositionError(f"{key}: correctness failure")
        validated = _asset_ref(cell.get("validated_result"), f"{key}.validated_result")
        p31 = _asset_ref(cell.get("p31_receipt"), f"{key}.p31_receipt")
        _asset_ref(cell.get("cleanup_receipt"), f"{key}.cleanup_receipt")
        identity = cell.get("identity")
        expected_variant = "budg-b64" if key == "seml0:bridge-canary" else "naive"
        expected_bundle_ref = target_refs[expected_variant]
        expected_bundle = target_bundles[expected_variant]
        if cell.get("target_p02b") != expected_bundle_ref:
            raise CompositionError(f"{key}: target P02B backlink drift")
        if cell.get("target_query_plan") != expected_bundle["static_inputs"]["query_plan"]:
            raise CompositionError(f"{key}: target query-plan backlink drift")
        if cell.get("target_lease") != expected_bundle["lease"]:
            raise CompositionError(f"{key}: target lease backlink drift")
        if type(identity) is not dict or identity.get("physical_input_sha256") != target_bundles[expected_variant]["store_pre"]["sha256"]:
            raise CompositionError(f"{key}: target P02B/store identity drift")
        if included:
            metrics = cell.get("metrics")
            protocol = cell.get("protocol")
            if not all(type(item) is dict for item in (metrics, identity, protocol)):
                raise CompositionError(f"{key}: metrics/identity/protocol required")
            rows.append(
                {
                    "experiment_id": "E01",
                    "run_key": key,
                    "system_key": system,
                    "repeat_index": repeat,
                    "lineage_class": "fresh_incremental_formal",
                    "plot_role": "figure_row",
                    "formal_eligible": True,
                    "performance_eligible": True,
                    "paper_claim_eligible": False,
                    "host_fingerprint": identity["host_fingerprint"],
                    "git_sha": identity["git_sha"],
                    "binary_sha256": identity["binary_sha256"],
                    "physical_input_sha256": identity["physical_input_sha256"],
                    "truth_sha256": identity["truth_sha256"],
                    "interface_scope": protocol["interface_scope"],
                    "concurrency": protocol["concurrency"],
                    "query_count": protocol["query_count"],
                    "warmup_passes": protocol["warmup_passes"],
                    "measured_passes": protocol["measured_passes"],
                    "measurement_s": metrics["measurement_s"],
                    "warmup_s": metrics["warmup_s"],
                    "latency_p50_us": metrics["latency_p50_us"],
                    "latency_p95_us": metrics["latency_p95_us"],
                    "latency_p99_us": metrics["latency_p99_us"],
                    "completed_qps": metrics["qps"],
                    "completed_queries": metrics["completed_queries"],
                    "timeout_queries": outcome["timeout_queries"],
                    "mismatch_queries": outcome["mismatch_queries"],
                    "validated_result_sha256": validated["sha256"],
                    "p31_evidence_sha256": p31["sha256"],
                }
            )
    canary = evidence.get("bridge_canary_comparability")
    if type(canary) is not dict or canary.get("state") != "PASS":
        raise CompositionError("pre-output gate: bridge canary comparability is not PASS")
    if canary.get("canary_cell_key") != "seml0:bridge-canary":
        raise CompositionError("bridge canary key drift")
    if canary.get("legacy_reference_keys") != ["seml0:r1", "seml0:r2", "seml0:r3"]:
        raise CompositionError("bridge canary legacy reference set drift")
    if canary.get("pre_registered_rule") is not True:
        raise CompositionError("bridge canary rule was not pre-registered")
    if canary.get("contract_sha256") != contract_sha:
        raise CompositionError("bridge canary result does not bind the frozen contract")
    canary_ref = _asset_ref(
        canary.get("receipt"), "bridge_canary_comparability.receipt"
    )
    canary_receipt = read_json(
        Path(canary_ref["path"]), "bridge_canary_comparability.receipt"
    )
    if (
        canary_receipt.get("state") != "PASS"
        or canary_receipt.get("contract_sha256") != contract_sha
    ):
        raise CompositionError("bridge canary receipt did not pass the frozen contract")
    if len(rows) != 3:
        raise CompositionError("exactly three fresh Figure rows required")
    return rows


def normalize(composition_path: Path, out_dir: Optional[Path]) -> Dict[str, Any]:
    composition_path = composition_path.resolve()
    value = read_json(composition_path, "mixed-lineage composition")
    if value.get("schema_version") != OUTPUT_SCHEMA:
        raise CompositionError("mixed-lineage composition schema drift")
    classification = value.get("classification")
    if type(classification) is not dict:
        raise CompositionError("composition classification required")
    for name in ("formal_eligible", "performance_eligible", "paper_claim_eligible"):
        if classification.get(name) is not False:
            raise CompositionError(f"mixed composition {name} must remain false")
    if classification.get("legacy_formal_upgrade_performed") is not False:
        raise CompositionError("legacy formal upgrade is forbidden")
    if value.get("state") != "PASS":
        raise CompositionError("pre-output gate: composition is not PASS")
    rows = _legacy_rows(value) + _fresh_rows(value)
    if len(rows) != 21 or sum(row["lineage_class"] == "legacy_validated" for row in rows) != 18:
        raise CompositionError("mixed output must be exactly legacy18 + fresh3")
    receipt = {
        "schema_version": "cidr-e01-mixed-lineage-normalization-receipt-v1",
        "state": "PASS",
        "experiment_id": "E01",
        "source_composition_sha256": sha256_file(composition_path),
        "row_count": 21,
        "legacy_row_count": 18,
        "fresh_row_count": 3,
        "canary_timing_cell_count": 1,
        "classification": {
            "lineage": "mixed_legacy_validated_plus_fresh_incremental",
            "formal_eligible": False,
            "performance_eligible": False,
            "paper_claim_eligible": False,
            "strict_all_formal_claim": False,
            "mixed_lineage_disclosure_required": True,
        },
        "checks": {
            "legacy_receipt_sha_and_p31_bridge": "PASS",
            "logical_dataset_identity_disclosed": "PASS",
            "fresh_campaign_gates": "PASS",
            "four_fresh_cells": "PASS",
            "bridge_canary_comparability": "PASS",
            "legacy_formal_upgrade": "NOT_PERFORMED",
            "paper_claim_upgrade": "NOT_PERFORMED",
        },
    }
    if out_dir is None:
        return {"rows": rows, "receipt": receipt}
    out_dir = out_dir.resolve()
    if out_dir.exists():
        raise CompositionError(f"refusing to overwrite output directory: {out_dir}")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".e01-mixed-normalize-", dir=str(out_dir.parent)))
    try:
        tsv = temporary / "E01-mixed-lineage-results.tsv"
        with tsv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TSV_COLUMNS, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        receipt["results_tsv"] = {
            "path": tsv.name,
            "sha256": sha256_file(tsv),
            "size_bytes": tsv.stat().st_size,
        }
        receipt_path = temporary / "normalization-receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (temporary / "SHA256SUMS").write_text(
            f"{sha256_file(tsv)}  {tsv.name}\n"
            f"{sha256_file(receipt_path)}  {receipt_path.name}\n",
            encoding="utf-8",
        )
        os.replace(str(temporary), str(out_dir))
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"rows": rows, "receipt": receipt}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--composition", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.dry_run and args.out_dir is not None:
            raise CompositionError("--dry-run cannot be combined with --out-dir")
        result = normalize(args.composition, None if args.dry_run else args.out_dir)
        print(json.dumps({"state": "PASS", "row_count": len(result["rows"])}, sort_keys=True))
        return 0
    except (CompositionError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
