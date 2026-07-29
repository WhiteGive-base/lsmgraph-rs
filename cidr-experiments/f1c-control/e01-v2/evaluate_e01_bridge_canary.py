#!/usr/bin/env python3
"""Evaluate and seal the pre-registered E01 SemL0 bridge canary contract."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import statistics
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import build_e01_mixed_lineage as mixed
import inventory_e01_seml0_assets as assets
import run_e01_incremental_production as production


EVIDENCE_SCHEMA = "cidr-e01-bridge-canary-evidence-v1"
CHECKPOINT_EVIDENCE_SCHEMA = "cidr-e01-bridge-canary-evidence-v2"
RECEIPT_SCHEMA = "cidr-e01-bridge-canary-comparability-receipt-v1"
CHECKPOINT_SCHEMA = "cidr-e01-bridge-canary-checkpoint-receipt-v2"
METRICS = ("completed_qps", "latency_p50_us", "latency_p95_us", "latency_p99_us")
FULL_V2_TOP_LEVEL_GROUPS = (
    "schema_version",
    "state",
    "cell_key",
    "backend_plan",
    "bridge_cell_done",
    "bridge_receipts",
    "mixed_lineage_plan",
    "contract_sha256",
    "validated_result_receipt",
    "validated_result",
    "p31_receipt",
    "classification",
    "identity",
    "protocol",
    "correctness",
    "metrics",
)
FULL_V2_SECTION_KEYS = {
    "classification": (
        "formal_eligible",
        "performance_eligible",
        "paper_claim_eligible",
    ),
    "identity": (
        "logical_dataset_id",
        "truth_sha256",
        "host_fingerprint",
    ),
    "protocol": (
        "query_count",
        "interface_scope",
        "concurrency",
        "warmup_passes",
        "measured_passes",
        "per_query_timeout_ms",
        "clock",
        "timing_boundary",
    ),
    "correctness": (
        "completed_queries",
        "timeout_queries",
        "mismatch_queries",
        "expected_digest_sha256",
        "actual_digest_sha256",
    ),
    "metrics": METRICS,
}
FULL_V2_SOURCES = {
    "classification": "validated production receipt chain",
    "identity.logical_dataset_id": "mixed_lineage_plan.logical_dataset_identity.logical_dataset_id",
    "identity.truth_sha256": "mixed_lineage_plan.logical_dataset_identity.truth_sha256",
    "identity.host_fingerprint": "validated_result.p31.host.fingerprint_sha256",
    "protocol": "validated_result exact protocol fields",
    "correctness": "validated_result cross-checked with correctness receipt and request truth",
    "metrics": "validated_result exact measured metrics",
}
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}
_CHECKPOINT_BIND_TOKEN = object()


class CanaryError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CanaryError(message)


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
        raise CanaryError(f"{label}: cannot read JSON: {exc}") from exc
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


def _contract(plan: Mapping[str, Any]) -> Dict[str, Any]:
    contract = plan.get("incremental_plan", {}).get(
        "bridge_canary_comparability_contract"
    )
    require(type(contract) is dict, "canary contract missing")
    body = dict(contract)
    declared = body.pop("contract_sha256", None)
    actual = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    require(declared == actual, "canary contract SHA drift")
    require(contract.get("frozen_before_timing") is True, "contract not frozen")
    return contract


def _positive(value: Any, label: str) -> float:
    require(
        type(value) in (int, float) and not isinstance(value, bool) and value > 0,
        f"{label}: positive number required",
    )
    return float(value)


def _expected_identity(plan: Mapping[str, Any]) -> Dict[str, Any]:
    seml0 = [
        cell for cell in plan["legacy_cells"] if cell["cell_key"] in (
            "seml0:r1",
            "seml0:r2",
            "seml0:r3",
        )
    ]
    require(len(seml0) == 3, "legacy SemL0 reference set drift")
    first = seml0[0]
    protocol = first["protocol"]
    return {
        "logical_dataset_id": plan["logical_dataset_identity"]["logical_dataset_id"],
        "truth_sha256": plan["logical_dataset_identity"]["truth_sha256"],
        "query_count": protocol["query_count"],
        "interface_scope": protocol["interface_scope"],
        "concurrency": protocol["concurrency"],
        "warmup_passes": protocol["warmup_passes"],
        "measured_passes": protocol["measured_passes"],
        "per_query_timeout_ms": protocol["per_query_timeout_ms"],
        "clock": protocol["clock"],
        "timing_boundary": protocol["timing_boundary"],
        "host_fingerprint": first["identity"]["host_fingerprint"],
    }


def full_v2_contract() -> Dict[str, Any]:
    return {
        "top_level_groups": list(FULL_V2_TOP_LEVEL_GROUPS),
        "section_keys": {
            key: list(value) for key, value in FULL_V2_SECTION_KEYS.items()
        },
        "sources": dict(FULL_V2_SOURCES),
        "writer": "evaluator-receipt-bound-o_excl-v1",
    }


def _validate_full_v2_shape(evidence: Mapping[str, Any]) -> None:
    require(
        set(evidence) == set(FULL_V2_TOP_LEVEL_GROUPS),
        "checkpoint evidence top-level groups drift",
    )
    for section, keys in FULL_V2_SECTION_KEYS.items():
        value = evidence.get(section)
        require(type(value) is dict, f"checkpoint evidence {section} required")
        require(
            set(value) == set(keys),
            f"checkpoint evidence {section} keys drift",
        )


def evaluate(
    plan_path: Path,
    evidence_path: Path,
    *,
    expected_evidence_schema: str = EVIDENCE_SCHEMA,
) -> Dict[str, Any]:
    plan_ref = file_ref(plan_path, "mixed-lineage plan")
    plan = load_json(plan_path, "mixed-lineage plan")
    require(plan.get("schema_version") == mixed.OUTPUT_SCHEMA, "mixed plan schema drift")
    contract = _contract(plan)
    evidence_ref = file_ref(evidence_path, "canary evidence")
    evidence = load_json(evidence_path, "canary evidence")
    require(
        evidence.get("schema_version") == expected_evidence_schema,
        "evidence schema drift",
    )
    if expected_evidence_schema == CHECKPOINT_EVIDENCE_SCHEMA:
        _validate_full_v2_shape(evidence)
    require(evidence.get("state") == "PASS", "canary evidence must PASS")
    require(evidence.get("cell_key") == "seml0:bridge-canary", "canary cell key drift")
    require(
        evidence.get("contract_sha256") == contract["contract_sha256"],
        "evidence contract SHA drift",
    )
    classification = evidence.get("classification")
    require(type(classification) is dict, "canary classification required")
    require(classification.get("formal_eligible") is True, "fresh formal canary required")
    require(classification.get("performance_eligible") is True, "performance canary required")
    require(classification.get("paper_claim_eligible") is False, "paper claim must stay closed")
    validated_ref = verify_ref(
        evidence.get("validated_result"), "canary validated result"
    )
    validated = load_json(Path(validated_ref["path"]), "canary validated result")
    p31_ref = verify_ref(evidence.get("p31_receipt"), "canary P31 receipt")
    p31_receipt = load_json(Path(p31_ref["path"]), "canary P31 receipt")
    require(p31_receipt.get("state") == "PASS", "canary P31 receipt must PASS")

    identity = evidence.get("identity")
    protocol = evidence.get("protocol")
    correctness = evidence.get("correctness")
    metrics = evidence.get("metrics")
    require(all(type(item) is dict for item in (identity, protocol, correctness, metrics)), "evidence sections required")
    require(validated.get("system_id") == "seml0", "validated result system drift")
    request_backlink = validated.get("request")
    require(type(request_backlink) is dict, "validated result request backlink required")
    request_ref = file_ref(
        Path(str(request_backlink.get("path", ""))), "canary adapter request"
    )
    require(
        request_backlink.get("sha256") == request_ref["sha256"],
        "canary adapter request backlink drift",
    )
    request = load_json(Path(request_ref["path"]), "canary adapter request")
    truth = request.get("truth")
    require(type(truth) is dict, "canary adapter request truth binding required")
    validated_metric_map = {
        "completed_qps": "qps",
        "latency_p50_us": "latency_p50_us",
        "latency_p95_us": "latency_p95_us",
        "latency_p99_us": "latency_p99_us",
    }
    for evidence_key, validated_key in validated_metric_map.items():
        require(
            metrics.get(evidence_key) == validated.get(validated_key),
            f"canary evidence/validated metric drift: {evidence_key}",
        )
    validated_correctness_map = {
        "completed_queries": "completed_queries",
        "timeout_queries": "timeout_queries",
        "mismatch_queries": "mismatch_queries",
        "expected_digest_sha256": "expected_digest_sha256",
        "actual_digest_sha256": "actual_digest_sha256",
    }
    for evidence_key, validated_key in validated_correctness_map.items():
        require(
            correctness.get(evidence_key) == validated.get(validated_key),
            f"canary evidence/validated correctness drift: {evidence_key}",
        )
    for key in (
        "query_count",
        "interface_scope",
        "concurrency",
        "warmup_passes",
        "measured_passes",
        "per_query_timeout_ms",
        "clock",
        "timing_boundary",
    ):
        require(
            protocol.get(key) == validated.get(key),
            f"canary evidence/validated protocol drift: {key}",
        )
    validated_p31 = validated.get("p31")
    require(type(validated_p31) is dict, "validated P31 binding required")
    require(
        identity.get("host_fingerprint")
        == validated_p31.get("host", {}).get("fingerprint_sha256"),
        "canary evidence/validated host drift",
    )
    require(
        identity.get("truth_sha256") == truth.get("sha256"),
        "canary evidence/request truth drift",
    )
    candidate_binding = {**identity, **protocol}
    expected = _expected_identity(plan)
    identity_checks = {}
    for key in contract["identity_exact_match"]:
        passed = candidate_binding.get(key) == expected.get(key)
        identity_checks[key] = {
            "state": "PASS" if passed else "FAILED",
            "expected": expected.get(key),
            "actual": candidate_binding.get(key),
        }
    correctness_expected = contract["correctness"]
    correctness_checks = {
        "completed_queries": correctness.get("completed_queries")
        == correctness_expected["completed_queries"],
        "timeout_queries": correctness.get("timeout_queries")
        == correctness_expected["timeout_queries"],
        "mismatch_queries": correctness.get("mismatch_queries")
        == correctness_expected["mismatch_queries"],
        "expected_digest_equals_actual": (
            type(correctness.get("expected_digest_sha256")) is str
            and correctness.get("expected_digest_sha256")
            == correctness.get("actual_digest_sha256")
            == plan["logical_dataset_identity"]["expected_digest_sha256"]
        ),
    }
    legacy = [
        cell for cell in plan["legacy_cells"] if cell["cell_key"] in contract["legacy_reference_keys"]
    ]
    require(len(legacy) == 3, "legacy reference keys missing")
    baseline = {
        "completed_qps": statistics.median(cell["metrics"]["qps"] for cell in legacy),
        "latency_p50_us": statistics.median(
            cell["metrics"]["latency_p50_us"] for cell in legacy
        ),
        "latency_p95_us": statistics.median(
            cell["metrics"]["latency_p95_us"] for cell in legacy
        ),
        "latency_p99_us": statistics.median(
            cell["metrics"]["latency_p99_us"] for cell in legacy
        ),
    }
    rule = contract["performance_rule"]
    bounds = {
        "completed_qps": (rule["completed_qps_min"], rule["completed_qps_max"]),
        "latency_p50_us": (rule["latency_p50_min"], rule["latency_p50_max"]),
        "latency_p95_us": (rule["latency_p95_min"], rule["latency_p95_max"]),
        "latency_p99_us": (rule["latency_p99_min"], rule["latency_p99_max"]),
    }
    performance = {}
    for name in METRICS:
        candidate = _positive(metrics.get(name), f"metrics.{name}")
        ratio = candidate / float(baseline[name])
        lower, upper = bounds[name]
        performance[name] = {
            "state": "PASS" if lower <= ratio <= upper else "FAILED",
            "legacy_median": baseline[name],
            "fresh_canary": candidate,
            "ratio": ratio,
            "lower_inclusive": lower,
            "upper_inclusive": upper,
        }
    failures = [
        f"identity.{key}" for key, check in identity_checks.items()
        if check["state"] != "PASS"
    ]
    failures += [
        f"correctness.{key}" for key, passed in correctness_checks.items() if not passed
    ]
    failures += [
        f"performance.{key}" for key, check in performance.items()
        if check["state"] != "PASS"
    ]
    return {
        "schema_version": RECEIPT_SCHEMA,
        "state": "PASS" if not failures else "FAILED",
        "experiment_id": "E01",
        "canary_cell_key": "seml0:bridge-canary",
        "legacy_reference_keys": contract["legacy_reference_keys"],
        "pre_registered_rule": True,
        "contract_sha256": contract["contract_sha256"],
        "mixed_lineage_plan": plan_ref,
        "canary_evidence": evidence_ref,
        "identity_checks": identity_checks,
        "correctness_checks": {
            key: "PASS" if passed else "FAILED"
            for key, passed in correctness_checks.items()
        },
        "performance_checks": performance,
        "failures": failures,
        "normalizer_release": not failures,
        "failure_policy": contract["failure_policy"],
        "original_artifacts_modified": False,
        **FALSE_ELIGIBILITY,
    }


def _iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_checkpoint_evidence(
    *,
    backend_plan_path: Path,
    campaign_root: Path,
    mixed_plan_path: Path,
    evidence_path: Path,
) -> Dict[str, Any]:
    """Derive the complete v2 evidence object from the retained receipt chain.

    This function does not write.  The CLI owns the single O_EXCL publication.
    """
    backend_plan_path = backend_plan_path.resolve()
    backend = production.validate_backend_plan(backend_plan_path)
    require(backend.get("state") == "READY", "checkpoint backend plan must be READY")
    backend_ref = file_ref(backend_plan_path, "checkpoint backend plan")
    root = campaign_root.resolve()
    require(root == Path(backend["campaign_root"]).resolve(), "checkpoint campaign root drift")
    contract = backend["canary_checkpoint"]
    require(
        contract.get("full_v2_contract") == full_v2_contract(),
        "checkpoint full-v2 evidence contract drift",
    )
    mixed_plan_path = mixed_plan_path.resolve()
    mixed_ref = file_ref(mixed_plan_path, "checkpoint mixed-lineage plan")
    require(
        mixed_ref == contract["mixed_lineage_plan"],
        "checkpoint mixed-lineage plan drift",
    )
    mixed_plan = load_json(mixed_plan_path, "checkpoint mixed-lineage plan")
    require(
        mixed_plan.get("schema_version") == mixed.OUTPUT_SCHEMA,
        "checkpoint mixed-lineage schema drift",
    )
    formal_contract = _contract(mixed_plan)
    require(
        formal_contract["contract_sha256"]
        == contract["comparability_contract_sha256"],
        "checkpoint comparability contract drift",
    )
    require(
        evidence_path.absolute() == Path(contract["evidence_path"]).absolute(),
        "checkpoint evidence path drift",
    )
    require(
        not os.path.lexists(evidence_path.absolute()),
        "checkpoint evidence path occupied",
    )
    resume = production.inspect_resume_root(
        root, backend, backend_ref["sha256"], expected_mode="production"
    )
    require(
        resume == {"state": "RESUME", "completed_cells": 1},
        "checkpoint requires exact bridge-only prefix",
    )
    pending_path = Path(contract["pending_path"])
    pending = load_json(pending_path, "CANARY_PENDING")
    require(
        pending == production.expected_canary_pending(
            root, backend, backend_ref
        ),
        "CANARY_PENDING drift/replay",
    )
    bridge = backend["cells"][0]
    bridge_final = Path(bridge["final_cell_root"]).resolve()
    bridge_done = production.validate_final_cell(
        bridge_final,
        cell_key=bridge["cell_key"],
        ordinal=bridge["ordinal"],
        plan_sha=backend_ref["sha256"],
        expected_mode="production",
        target_p02b=bridge["runtime"]["target_p02b"],
        target_query_plan=bridge["runtime"]["target_query_plan"],
        target_lease=bridge["runtime"]["target_lease"],
        adapter_tool=bridge["runtime"]["adapter_tool"],
    )
    require(bridge_done.get("state") == "PASS", "checkpoint bridge must PASS")
    require(
        bridge_done.get("mode") == "production"
        and bridge_done.get("synthetic_test_only") is False
        and bridge_done.get("fixture_only") is False,
        "checkpoint bridge must be production/non-synthetic",
    )
    receipts = pending["bridge_receipts"]
    require(
        set(receipts) == set(production.RECEIPT_PATHS),
        "checkpoint bridge receipt roles drift",
    )
    for role, receipt_ref in receipts.items():
        verify_ref(receipt_ref, f"checkpoint bridge receipt {role}")
    prepared = load_json(
        Path(receipts["prepared_command"]["path"]), "bridge prepared-command receipt"
    )
    correctness_receipt = load_json(
        Path(receipts["correctness"]["path"]), "bridge correctness receipt"
    )
    p31_receipt = load_json(Path(receipts["p31"]["path"]), "bridge P31 receipt")
    validated_receipt = load_json(
        Path(receipts["validated_result"]["path"]), "bridge validated-result receipt"
    )
    for label, value in (
        ("prepared-command", prepared),
        ("correctness", correctness_receipt),
        ("P31", p31_receipt),
        ("validated-result", validated_receipt),
    ):
        require(
            value.get("state") == "PASS"
            and value.get("mode") == "production"
            and value.get("synthetic_test_only") is False
            and value.get("fixture_only") is False,
            f"bridge {label} must be production/non-synthetic PASS",
        )
        require(
            value.get("cell_key") == "seml0:bridge-canary"
            and value.get("ordinal") == 1
            and value.get("backend_plan_sha256") == backend_ref["sha256"],
            f"bridge {label} identity drift",
        )
    require(
        p31_receipt.get("timing_generated") is True,
        "bridge P31 must contain real timing",
    )
    validated_ref = verify_ref(
        validated_receipt.get("adapter_result"), "bridge validated adapter result"
    )
    validated = load_json(Path(validated_ref["path"]), "bridge validated adapter result")
    require(
        validated.get("schema_version") == "cidr-p10-validated-repeat-v1"
        and validated.get("system_id") == "seml0"
        and validated.get("cell_key") == "seml0:bridge-canary"
        and validated.get("ordinal") == 1,
        "bridge validated result identity drift",
    )
    require(validated.get("backend_plan") == backend_ref, "bridge validated plan drift")
    require(
        validated.get("target_p02b") == pending["target_p02b"],
        "bridge validated target drift",
    )
    require(
        validated.get("final_cell_root") == str(bridge_final),
        "bridge validated final root drift",
    )
    request_ref = verify_ref(prepared.get("request"), "bridge prepared adapter request")
    require(validated.get("request") == request_ref, "bridge request backlink drift")
    request = load_json(Path(request_ref["path"]), "bridge prepared adapter request")
    truth = request.get("truth")
    require(type(truth) is dict, "bridge request truth binding required")
    identity = {
        "logical_dataset_id": mixed_plan["logical_dataset_identity"][
            "logical_dataset_id"
        ],
        "truth_sha256": mixed_plan["logical_dataset_identity"]["truth_sha256"],
        "host_fingerprint": validated.get("p31", {})
        .get("host", {})
        .get("fingerprint_sha256"),
    }
    require(
        identity["truth_sha256"] == truth.get("sha256"),
        "bridge request/mixed truth drift",
    )
    protocol = {
        key: validated.get(key) for key in FULL_V2_SECTION_KEYS["protocol"]
    }
    correctness = {
        key: validated.get(key) for key in FULL_V2_SECTION_KEYS["correctness"]
    }
    require(
        correctness_receipt.get("query_count") == protocol["query_count"]
        == correctness["completed_queries"],
        "bridge correctness query count drift",
    )
    require(
        correctness_receipt.get("timeout_queries") == correctness["timeout_queries"]
        and correctness_receipt.get("mismatch_queries")
        == correctness["mismatch_queries"],
        "bridge correctness receipt/result drift",
    )
    require(
        correctness["expected_digest_sha256"]
        == correctness["actual_digest_sha256"]
        == mixed_plan["logical_dataset_identity"]["expected_digest_sha256"],
        "bridge correctness digest drift",
    )
    metrics = {
        "completed_qps": validated.get("qps"),
        "latency_p50_us": validated.get("latency_p50_us"),
        "latency_p95_us": validated.get("latency_p95_us"),
        "latency_p99_us": validated.get("latency_p99_us"),
    }
    evidence = {
        "schema_version": CHECKPOINT_EVIDENCE_SCHEMA,
        "state": "PASS",
        "cell_key": "seml0:bridge-canary",
        "backend_plan": backend_ref,
        "bridge_cell_done": pending["bridge_cell_done"],
        "bridge_receipts": receipts,
        "mixed_lineage_plan": mixed_ref,
        "contract_sha256": formal_contract["contract_sha256"],
        "validated_result_receipt": receipts["validated_result"],
        "validated_result": validated_ref,
        "p31_receipt": receipts["p31"],
        "classification": {
            "formal_eligible": True,
            "performance_eligible": True,
            "paper_claim_eligible": False,
        },
        "identity": identity,
        "protocol": protocol,
        "correctness": correctness,
        "metrics": metrics,
    }
    _validate_full_v2_shape(evidence)
    return evidence


def _validate_comparability_receipt(
    value: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    evidence_ref: Mapping[str, Any],
) -> None:
    mixed_plan = load_json(
        Path(contract["mixed_lineage_plan"]["path"]), "formal mixed-lineage plan"
    )
    formal_contract = _contract(mixed_plan)
    require(
        formal_contract["contract_sha256"]
        == contract["comparability_contract_sha256"],
        "comparability frozen contract drift",
    )
    expected_identity = set(formal_contract["identity_exact_match"])
    expected_correctness = set(formal_contract["correctness"])
    expected_performance = set(METRICS)
    require(value.get("schema_version") == RECEIPT_SCHEMA, "comparability schema drift")
    require(value.get("state") == "PASS", "comparability receipt must PASS")
    require(
        value.get("mixed_lineage_plan") == contract["mixed_lineage_plan"],
        "comparability mixed-plan drift",
    )
    require(value.get("canary_evidence") == evidence_ref, "comparability evidence drift")
    require(
        value.get("contract_sha256") == contract["comparability_contract_sha256"],
        "comparability contract drift",
    )
    require(value.get("failures") == [], "comparability failures must be empty")
    identity = value.get("identity_checks")
    correctness = value.get("correctness_checks")
    performance = value.get("performance_checks")
    require(
        type(identity) is dict and set(identity) == expected_identity,
        "comparability identity checks required",
    )
    require(
        all(type(row) is dict and row.get("state") == "PASS" for row in identity.values()),
        "comparability identity check failed",
    )
    require(
        type(correctness) is dict
        and set(correctness) == expected_correctness
        and all(state == "PASS" for state in correctness.values()),
        "comparability correctness check failed",
    )
    require(
        type(performance) is dict
        and set(performance) == expected_performance
        and all(
            type(row) is dict and row.get("state") == "PASS"
            for row in performance.values()
        ),
        "comparability performance check failed",
    )
    require(value.get("normalizer_release") is True, "comparability did not release")


def validate_checkpoint_inputs(
    *,
    backend_plan_path: Path,
    campaign_root: Path,
    mixed_plan_path: Path,
    evidence_path: Path,
) -> Dict[str, Any]:
    backend_plan_path = backend_plan_path.resolve()
    backend = production.validate_backend_plan(backend_plan_path)
    require(backend.get("state") == "READY", "checkpoint backend plan must be READY")
    backend_ref = file_ref(backend_plan_path, "checkpoint backend plan")
    root = campaign_root.resolve()
    require(root == Path(backend["campaign_root"]).resolve(), "checkpoint campaign root drift")
    contract = backend["canary_checkpoint"]
    require(
        contract.get("full_v2_contract") == full_v2_contract(),
        "checkpoint full-v2 evidence contract drift",
    )
    require(
        file_ref(mixed_plan_path.resolve(), "checkpoint mixed-lineage plan")
        == contract["mixed_lineage_plan"],
        "checkpoint mixed-lineage plan drift",
    )
    require(
        evidence_path.absolute() == Path(contract["evidence_path"]).absolute(),
        "checkpoint evidence path drift",
    )
    resume = production.inspect_resume_root(
        root, backend, backend_ref["sha256"], expected_mode="production"
    )
    require(
        resume == {"state": "RESUME", "completed_cells": 1},
        "checkpoint requires exact bridge-only prefix",
    )
    pending_path = Path(contract["pending_path"])
    pending_ref = file_ref(pending_path, "CANARY_PENDING")
    pending = load_json(pending_path, "CANARY_PENDING")
    require(
        pending == production.expected_canary_pending(root, backend, backend_ref),
        "CANARY_PENDING drift/replay",
    )
    evidence_ref = file_ref(evidence_path, "checkpoint canary evidence")
    evidence = load_json(evidence_path, "checkpoint canary evidence")
    _validate_full_v2_shape(evidence)
    require(
        evidence.get("schema_version") == CHECKPOINT_EVIDENCE_SCHEMA,
        "checkpoint evidence schema drift",
    )
    require(
        evidence.get("state") == "PASS"
        and evidence.get("cell_key") == "seml0:bridge-canary",
        "checkpoint evidence state/cell drift",
    )
    require(evidence.get("backend_plan") == backend_ref, "checkpoint evidence plan drift")
    require(
        evidence.get("bridge_cell_done") == pending["bridge_cell_done"],
        "checkpoint evidence cell drift",
    )
    require(
        evidence.get("bridge_receipts") == pending["bridge_receipts"],
        "checkpoint evidence receipt binding drift",
    )
    require(
        evidence.get("mixed_lineage_plan") == contract["mixed_lineage_plan"],
        "checkpoint evidence mixed-plan drift",
    )
    require(
        evidence.get("contract_sha256") == contract["comparability_contract_sha256"],
        "checkpoint evidence contract drift",
    )
    require(
        type(evidence.get("bridge_receipts")) is dict,
        "checkpoint evidence receipt binding required",
    )
    require(
        set(evidence["bridge_receipts"]) == set(production.RECEIPT_PATHS),
        "checkpoint evidence receipt roles drift",
    )
    require(
        evidence.get("validated_result_receipt")
        == pending["bridge_receipts"]["validated_result"],
        "checkpoint evidence validated-result receipt drift",
    )
    require(
        evidence.get("p31_receipt") == pending["bridge_receipts"]["p31"],
        "checkpoint evidence P31 drift",
    )
    bridge = backend["cells"][0]
    bridge_final = Path(bridge["final_cell_root"]).resolve()
    bridge_done = production.validate_final_cell(
        bridge_final,
        cell_key=bridge["cell_key"],
        ordinal=bridge["ordinal"],
        plan_sha=backend_ref["sha256"],
        expected_mode="production",
        target_p02b=bridge["runtime"]["target_p02b"],
        target_query_plan=bridge["runtime"]["target_query_plan"],
        target_lease=bridge["runtime"]["target_lease"],
        adapter_tool=bridge["runtime"]["adapter_tool"],
    )
    require(
        file_ref(bridge_final / "CELL-DONE.json", "bridge CELL-DONE")
        == pending["bridge_cell_done"]
        and bridge_done.get("state") == "PASS",
        "checkpoint bridge deep-validation publication drift",
    )
    expected_receipt_base = {
        "state": "PASS",
        "mode": "production",
        "synthetic_test_only": False,
        "fixture_only": False,
        "cell_key": "seml0:bridge-canary",
        "ordinal": 1,
        "backend_plan_sha256": backend_ref["sha256"],
    }
    prepared_ref = pending["bridge_receipts"]["prepared_command"]
    prepared = load_json(Path(prepared_ref["path"]), "bridge prepared-command receipt")
    p31_ref = pending["bridge_receipts"]["p31"]
    p31 = load_json(Path(p31_ref["path"]), "bridge P31 receipt")
    topology_ref = pending["bridge_receipts"]["command_topology"]
    topology = load_json(Path(topology_ref["path"]), "bridge command-topology receipt")
    validated_receipt_ref = pending["bridge_receipts"]["validated_result"]
    validated_receipt = load_json(
        Path(validated_receipt_ref["path"]), "bridge validated-result receipt"
    )
    for label, receipt, schema in (
        (
            "prepared-command",
            prepared,
            "cidr-e01-incremental-prepared-command-receipt-v1",
        ),
        ("P31", p31, "cidr-e01-incremental-p31-receipt-v1"),
        (
            "command-topology",
            topology,
            "cidr-e01-incremental-command-topology-receipt-v1",
        ),
        (
            "validated-result",
            validated_receipt,
            "cidr-e01-incremental-validated-result-receipt-v1",
        ),
    ):
        require(receipt.get("schema_version") == schema, f"bridge {label} schema drift")
        require(
            all(receipt.get(key) == value for key, value in expected_receipt_base.items()),
            f"bridge {label} identity/state drift",
        )
    require(
        p31.get("target_p02b") == pending["target_p02b"]
        and validated_receipt.get("target_p02b") == pending["target_p02b"],
        "bridge validated/P31 target drift",
    )
    require(
        p31.get("command_topology") == topology_ref
        and validated_receipt.get("command_topology") == topology_ref
        and topology.get("single_binary_invocation") is True
        and topology.get("warmup_measured_same_process") is True
        and topology.get("warmup_runs") == contract["legacy_protocol"]["warmup_passes"]
        and topology.get("measured_repeats") == contract["legacy_protocol"]["measured_passes"]
        and topology.get("process_lifetime") == contract["legacy_protocol"]["process_lifetime"],
        "bridge command-topology chain drift",
    )
    adapter_ref = verify_ref(
        validated_receipt.get("adapter_result"), "checkpoint validated adapter result"
    )
    require(
        evidence.get("validated_result") == adapter_ref,
        "checkpoint evidence/receipt adapter-result mismatch",
    )
    adapter = load_json(Path(adapter_ref["path"]), "checkpoint validated adapter result")
    require(
        adapter.get("schema_version") == "cidr-p10-validated-repeat-v1"
        and adapter.get("system_id") == "seml0"
        and adapter.get("cell_key") == "seml0:bridge-canary"
        and adapter.get("ordinal") == 1,
        "checkpoint adapter result schema/cell drift",
    )
    require(adapter.get("backend_plan") == backend_ref, "checkpoint adapter plan drift")
    require(
        adapter.get("target_p02b") == pending["target_p02b"],
        "checkpoint adapter target drift",
    )
    require(
        adapter.get("final_cell_root")
        == str(Path(pending["bridge_cell_done"]["path"]).parent.resolve())
        == str(Path(bridge["final_cell_root"]).resolve()),
        "checkpoint adapter final-cell drift",
    )
    request_ref = verify_ref(prepared.get("request"), "bridge prepared adapter request")
    require(adapter.get("request") == request_ref, "checkpoint adapter request drift")
    request = load_json(Path(request_ref["path"]), "bridge prepared adapter request")
    require(
        request.get("timing", {}).get("per_query_timeout_ms")
        == topology.get("per_query_timeout_ms")
        == adapter.get("per_query_timeout_ms")
        == validated_receipt.get("per_query_timeout_ms")
        == contract["legacy_protocol"]["per_query_timeout_ms"],
        "checkpoint request/command/result deadline drift",
    )
    p31_manifest_ref = verify_ref(p31.get("run_manifest"), "bridge P31 run manifest")
    p31_manifest = load_json(Path(p31_manifest_ref["path"]), "bridge P31 run manifest")
    adapter_p31 = adapter.get("p31")
    require(
        type(adapter_p31) is dict
        and adapter_p31.get("receipt") == p31_ref
        and adapter_p31.get("run_manifest") == p31_manifest_ref
        and adapter_p31.get("host") == p31_manifest.get("host"),
        "checkpoint adapter P31 backlink drift",
    )
    require(
        adapter_p31.get("command_topology") == topology_ref
        and adapter.get("process_lifetime_binding", {}).get("command_topology")
        == topology_ref,
        "checkpoint adapter command-topology backlink drift",
    )
    artifacts = adapter.get("adapter_artifacts")
    final_root = Path(bridge["final_cell_root"]).resolve()
    staging_root = Path(bridge["staging_cell_root"]).resolve()
    require(not os.path.lexists(staging_root), "checkpoint stale staging root remains")
    require(type(artifacts) is dict and artifacts, "checkpoint adapter artifacts missing")
    for name, ref in artifacts.items():
        actual = verify_ref(ref, f"checkpoint adapter artifact {name}")
        require(
            production.path_within(Path(actual["path"]), final_root),
            f"checkpoint adapter artifact {name} outside final root",
        )
    return {
        "backend": backend,
        "backend_ref": backend_ref,
        "root": root,
        "contract": contract,
        "pending": pending,
        "pending_ref": pending_ref,
        "evidence": evidence,
        "evidence_ref": evidence_ref,
    }


def _bind_production_checkpoint(
    comparability_path: Path,
    backend_plan_path: Path,
    campaign_root: Path,
    *,
    expected_comparability: Mapping[str, Any],
    bind_token: object,
) -> Dict[str, Any]:
    require(
        bind_token is _CHECKPOINT_BIND_TOKEN,
        "checkpoint bind is internal to the evaluator transaction",
    )
    backend_plan_path = backend_plan_path.resolve()
    backend_plan = production.validate_backend_plan(backend_plan_path)
    contract = backend_plan["canary_checkpoint"]
    checkpoint = validate_checkpoint_inputs(
        backend_plan_path=backend_plan_path,
        campaign_root=campaign_root,
        mixed_plan_path=Path(contract["mixed_lineage_plan"]["path"]),
        evidence_path=Path(contract["evidence_path"]),
    )
    backend_ref = checkpoint["backend_ref"]
    root = checkpoint["root"]
    require(not os.path.lexists(root / "MATRIX-DONE.json"), "checkpoint cannot consume MATRIX-DONE")
    require(not os.path.lexists(root / "MATRIX-FAILED.json"), "failed campaign cannot be evaluated")
    pending_ref = checkpoint["pending_ref"]
    pending = checkpoint["pending"]
    evaluator_ref = file_ref(Path(__file__).resolve(), "canary evaluator")
    require(
        evaluator_ref == backend_plan["canary_checkpoint"]["evaluator"],
        "runtime evaluator differs from backend plan",
    )
    lease_ref = verify_ref(pending["target_lease"], "bridge target lease")
    lease = load_json(Path(lease_ref["path"]), "bridge target lease")
    expires = lease.get("expires_at_utc") or lease.get("expires_at")
    require(type(expires) is str and _iso(expires) > dt.datetime.now(dt.timezone.utc), "bridge lease expired")
    gate_ref = production.external_file_ref(
        Path(backend_plan["campaign_gates"]["backend_arming"]["path"])
    )
    comparability_path = comparability_path.resolve()
    require(
        comparability_path == Path(contract["comparability_path"]).resolve(),
        "comparability receipt path drift",
    )
    comparability_ref = file_ref(comparability_path, "comparability receipt")
    comparability = load_json(comparability_path, "comparability receipt")
    require(
        comparability == expected_comparability,
        "published comparability differs from evaluator transaction",
    )
    evidence_ref = checkpoint["evidence_ref"]
    _validate_comparability_receipt(
        comparability, contract=contract, evidence_ref=evidence_ref
    )
    bridge_receipts = pending["bridge_receipts"]
    state = "PASS"
    return {
        "schema_version": CHECKPOINT_SCHEMA,
        "state": state,
        "campaign_root": str(root),
        "backend_plan": backend_ref,
        "arming_gate": gate_ref,
        "canary_pending": pending_ref,
        "bridge_cell_done": pending["bridge_cell_done"],
        "bridge_receipts": bridge_receipts,
        "runner_receipts": {
            "prepared_command": bridge_receipts["prepared_command"],
            "store_clone": bridge_receipts["store_clone"],
        },
        "p31_receipt": bridge_receipts["p31"],
        "correctness_receipt": bridge_receipts["correctness"],
        "finalize_receipts": {
            "validated_result": bridge_receipts["validated_result"],
            "fairness": bridge_receipts["fairness"],
        },
        "cleanup_receipt": bridge_receipts["cleanup"],
        "target_p02b": pending["target_p02b"],
        "target_query_plan": pending["target_query_plan"],
        "target_lease": pending["target_lease"],
        "lease_expires_at_utc": expires,
        "evaluator": evaluator_ref,
        "mixed_lineage_plan": contract["mixed_lineage_plan"],
        "comparability_contract_sha256": contract[
            "comparability_contract_sha256"
        ],
        "canary_evidence": evidence_ref,
        "comparability_receipt": comparability_ref,
        "campaign_state": {
            "completed_cells": 1,
            "strict_prefix": True,
            "matrix_done": False,
            "matrix_failed": False,
        },
        "resume_authorized": state == "PASS",
        "original_artifacts_modified": False,
        **FALSE_ELIGIBILITY,
    }


def atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path = path.absolute()
    require(
        not os.path.lexists(path),
        f"refusing to overwrite/replace retained receipt, including dangling symlink: {path}",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def run_production_checkpoint(
    *,
    backend_plan_path: Path,
    campaign_root: Path,
    mixed_plan_path: Path,
    evidence_path: Path,
    evaluation_path: Path,
) -> Dict[str, Any]:
    backend = production.validate_backend_plan(backend_plan_path.resolve())
    contract = backend["canary_checkpoint"]
    require(
        evaluation_path.absolute()
        == Path(contract["evaluation_path"]).absolute(),
        "checkpoint output path drift",
    )
    comparability_path = Path(contract["comparability_path"])
    require(
        not os.path.lexists(comparability_path),
        "checkpoint comparability path occupied",
    )
    require(
        not os.path.lexists(evaluation_path.absolute()),
        "checkpoint evaluation path occupied",
    )
    evidence = build_checkpoint_evidence(
        backend_plan_path=backend_plan_path,
        campaign_root=campaign_root,
        mixed_plan_path=mixed_plan_path,
        evidence_path=evidence_path,
    )
    atomic_write(evidence_path, evidence)
    checkpoint = validate_checkpoint_inputs(
        backend_plan_path=backend_plan_path,
        campaign_root=campaign_root,
        mixed_plan_path=mixed_plan_path,
        evidence_path=evidence_path,
    )
    comparability = evaluate(
        mixed_plan_path.resolve(),
        evidence_path.resolve(),
        expected_evidence_schema=CHECKPOINT_EVIDENCE_SCHEMA,
    )
    require(
        comparability.get("state") == "PASS",
        "checkpoint comparability did not PASS",
    )
    atomic_write(comparability_path, comparability)
    receipt = _bind_production_checkpoint(
        comparability_path,
        backend_plan_path.resolve(),
        campaign_root.resolve(),
        expected_comparability=comparability,
        bind_token=_CHECKPOINT_BIND_TOKEN,
    )
    atomic_write(evaluation_path, receipt)
    return receipt


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mixed-plan", type=Path, required=True)
    parser.add_argument("--canary-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--backend-plan", type=Path)
    parser.add_argument("--campaign-root", type=Path)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        require(
            (args.backend_plan is None) == (args.campaign_root is None),
            "backend-plan and campaign-root must be provided together",
        )
        if args.backend_plan is not None:
            require(args.output is not None, "checkpoint mode requires output")
            receipt = run_production_checkpoint(
                backend_plan_path=args.backend_plan,
                campaign_root=args.campaign_root,
                mixed_plan_path=args.mixed_plan,
                evidence_path=args.canary_evidence,
                evaluation_path=args.output,
            )
        else:
            receipt = evaluate(args.mixed_plan.resolve(), args.canary_evidence.resolve())
        if args.output is None:
            print(json.dumps(receipt, indent=2, sort_keys=True))
        elif args.backend_plan is None:
            atomic_write(args.output, receipt)
        if args.output is not None:
            print(
                json.dumps(
                    {"state": receipt["state"], "output": str(args.output.resolve())}
                )
            )
        return 0 if receipt["state"] == "PASS" else 3
    except (CanaryError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
