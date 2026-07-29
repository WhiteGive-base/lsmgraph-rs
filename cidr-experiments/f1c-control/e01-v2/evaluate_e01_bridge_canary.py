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
RECEIPT_SCHEMA = "cidr-e01-bridge-canary-comparability-receipt-v1"
CHECKPOINT_SCHEMA = "cidr-e01-bridge-canary-checkpoint-receipt-v2"
METRICS = ("completed_qps", "latency_p50_us", "latency_p95_us", "latency_p99_us")
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


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
        "clock": protocol["clock"],
        "timing_boundary": protocol["timing_boundary"],
        "host_fingerprint": first["identity"]["host_fingerprint"],
    }


def evaluate(plan_path: Path, evidence_path: Path) -> Dict[str, Any]:
    plan_ref = file_ref(plan_path, "mixed-lineage plan")
    plan = load_json(plan_path, "mixed-lineage plan")
    require(plan.get("schema_version") == mixed.OUTPUT_SCHEMA, "mixed plan schema drift")
    contract = _contract(plan)
    evidence_ref = file_ref(evidence_path, "canary evidence")
    evidence = load_json(evidence_path, "canary evidence")
    require(evidence.get("schema_version") == EVIDENCE_SCHEMA, "evidence schema drift")
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


def bind_production_checkpoint(
    comparability: Mapping[str, Any],
    backend_plan_path: Path,
    campaign_root: Path,
) -> Dict[str, Any]:
    backend_plan_path = backend_plan_path.resolve()
    backend_plan = production.validate_backend_plan(backend_plan_path)
    require(backend_plan.get("state") == "READY", "checkpoint backend plan must be READY")
    backend_ref = file_ref(backend_plan_path, "checkpoint backend plan")
    root = campaign_root.resolve()
    require(root == Path(backend_plan["campaign_root"]).resolve(), "checkpoint campaign root drift")
    require(not os.path.lexists(root / "MATRIX-DONE.json"), "checkpoint cannot consume MATRIX-DONE")
    require(not os.path.lexists(root / "MATRIX-FAILED.json"), "failed campaign cannot be evaluated")
    resume = production.inspect_resume_root(
        root, backend_plan, backend_ref["sha256"], expected_mode="production"
    )
    require(resume == {"state": "RESUME", "completed_cells": 1}, "checkpoint requires exact bridge-only prefix")
    expected_pending = production.expected_canary_pending(root, backend_plan, backend_ref)
    pending_path = Path(backend_plan["canary_checkpoint"]["pending_path"])
    pending_ref = file_ref(pending_path, "CANARY_PENDING")
    pending = load_json(pending_path, "CANARY_PENDING")
    require(pending == expected_pending, "CANARY_PENDING drift/replay")
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
    bridge_receipts = pending["bridge_receipts"]
    state = "PASS" if (
        comparability.get("state") == "PASS"
        and comparability.get("normalizer_release") is True
    ) else "FAILED_RETAINED"
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
        "comparability": dict(comparability),
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
        receipt = evaluate(args.mixed_plan.resolve(), args.canary_evidence.resolve())
        require(
            (args.backend_plan is None) == (args.campaign_root is None),
            "backend-plan and campaign-root must be provided together",
        )
        if args.backend_plan is not None:
            require(args.output is not None, "checkpoint mode requires output")
            backend = production.validate_backend_plan(args.backend_plan.resolve())
            expected_output = Path(backend["canary_checkpoint"]["evaluation_path"]).absolute()
            require(args.output.absolute() == expected_output, "checkpoint output path drift")
            receipt = bind_production_checkpoint(
                receipt, args.backend_plan.resolve(), args.campaign_root.resolve()
            )
        if args.output is None:
            print(json.dumps(receipt, indent=2, sort_keys=True))
        else:
            atomic_write(args.output, receipt)
            print(json.dumps({"state": receipt["state"], "output": str(args.output.resolve())}))
        return 0 if receipt["state"] == "PASS" else 3
    except (CanaryError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
