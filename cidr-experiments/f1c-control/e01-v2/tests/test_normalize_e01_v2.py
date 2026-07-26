#!/usr/bin/env python3
"""Negative/positive tests for the isolated E01 v2 normalizer.

The fixture is synthetic and lives only in TemporaryDirectory.  It never
touches a frozen raw root and is not evidence for any Figure 1 claim.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
normalizer = importlib.import_module("normalize_e01_v2")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(root: Path, relative: str, value: Dict[str, Any]) -> Dict[str, Any]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(payload)
    return {"path": relative, "sha256": _sha(payload), "size_bytes": len(payload)}


def _write_text(root: Path, relative: str, payload: str) -> Dict[str, Any]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    data = payload.encode()
    path.write_bytes(data)
    return {"path": relative, "sha256": _sha(data), "size_bytes": len(data)}


def _ref_sha(ref: Dict[str, Any]) -> str:
    return str(ref["sha256"])


def build_fixture(root: Path) -> Tuple[Path, Dict[str, Any]]:
    host = "a" * 64
    git = "b" * 40
    input_sha = "c" * 64
    trace_sha = "d" * 64
    gate_contract_sha = "e" * 64
    seml0_binary = "f" * 64
    protocol = {
        "systems": [
            {"system_key": "seml0", "display_name": "SemL0", "variant": "budg-b64"},
            {"system_key": "seml0-naive", "display_name": "SemL0-naive", "variant": "naive"},
            {"system_key": "livegraph", "display_name": "LiveGraph", "variant": "baseline"},
            {"system_key": "aster", "display_name": "Aster RocksGraph", "variant": "baseline"},
            {"system_key": "tugraph", "display_name": "TuGraph", "variant": "baseline"},
            {"system_key": "nebulagraph", "display_name": "NebulaGraph", "variant": "baseline"},
            {"system_key": "neo4j", "display_name": "Neo4j", "variant": "baseline"},
        ],
        "repeat_indices": [1, 2, 3],
        "dataset_id": "sf10-typed-neighbor-v2",
        "scale_factor": 10,
        "vertex_count": 1000000,
        "directed_edge_count": 10000000,
        "property_count": 0,
        "query_count": 1700,
        "input_sha256": input_sha,
        "workload_id": "typed-neighbor-s50-v2",
        "query_trace_sha256": trace_sha,
        "seed": 42,
        "cache_state": "warm",
        "concurrency": 1,
        "interface_scope": "typed-neighbor-dense-id-v1",
        "host_fingerprint": host,
        "harness_git_sha": git,
    }

    gate = _write_json(
        root,
        "admission/gate.json",
        {
            "schema_version": "p02b-sf10-sentinel-result-v2",
            "state": "PASS",
            "fixture_only": False,
            "formal_gate_eligible": True,
            "downstream_release_eligible": True,
        },
    )
    lease = _write_json(
        root,
        "admission/lease.json",
        {
            "schema_version": "cidr-batch-lease-v2",
            "state": "PASS",
            "scope": "single-host-single-head-formal-batch",
            "classification": {
                "performance_eligible": False,
                "purpose": "downstream-admission-only",
            },
            "issued_at_utc": "2026-01-01T00:00:00Z",
            "expires_at_utc": "2026-01-02T00:00:00Z",
            "duration_seconds": 86400,
            "consumers": ["P10", "P20"],
            "identity": {
                "host": {"fingerprint_sha256": host},
                "repo": {"head": git, "clean_at_issue": True},
                "binary": {"sha256": seml0_binary},
            },
            "p02b": {
                "result": {"path": gate["path"], "sha256": _ref_sha(gate)},
                "gate_contract": {"contract_sha256": gate_contract_sha},
                "gate_contract_sha256": gate_contract_sha,
            },
        },
    )
    marker = _write_json(
        root,
        "admission/lease.PASS.json",
        {
            "schema_version": "cidr-batch-lease-marker-v2",
            "state": "PASS",
            "lease": lease["path"],
            "lease_sha256": _ref_sha(lease),
        },
    )
    all_run_keys = list(normalizer.RUN_KEYS)
    runs = []
    system_builds: Dict[str, str] = {}

    for ordinal, run_key in enumerate(all_run_keys):
        system_key, repeat_token = run_key.split(":")
        repeat = int(repeat_token[1:])
        underlying = normalizer.UNDERLYING_SYSTEM.get(system_key, system_key)
        display_name = "SemL0" if system_key == "seml0-naive" else normalizer.SYSTEM_NAMES[system_key]
        variant = protocol["systems"][normalizer.SYSTEM_KEYS.index(system_key)]["variant"]
        if system_key in {"seml0", "seml0-naive"}:
            binary = seml0_binary
        else:
            binary = system_builds.setdefault(
                system_key, ("%064x" % (ordinal + 100))[0:64]
            )
        version = "fixture-engine-v2" if system_key in {"seml0", "seml0-naive"} else f"{system_key}-fixture-v2"
        run_id = f"e01-test-{system_key}-r{repeat}"
        timestamp = (datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc) + timedelta(seconds=ordinal)).isoformat().replace("+00:00", "Z")
        completed = 1700
        measurement = 20.0 + ordinal / 100.0
        qps = completed / measurement
        p50, p95, p99 = 100.0 + ordinal, 1000.0 + ordinal, 2000.0 + ordinal
        metrics = {
            "latency_p50_us": p50,
            "latency_p95_us": p95,
            "latency_p99_us": p99,
            "completed_queries": completed,
            "timeout_queries": 0,
            "query_count": 1700,
            "measurement_s": measurement,
            "warmup_s": 5.0,
            "completed_qps": qps,
            "offered_qps": 100.0,
            "load_wall_s": 30.0 + ordinal,
            "final_disk_bytes": 1000000 + ordinal,
            "expected_digest_sha256": "1" * 64,
            "actual_digest_sha256": "1" * 64,
            "digest_pass": True,
            "mismatch_count": 0,
        }

        prefix = f"assets/{system_key}/r{repeat}"
        request = _write_json(
            root,
            f"{prefix}/adapter-request.json",
            {
                "schema_version": "cidr-p10-adapter-request-v2",
                "execution_mode": "formal",
                "system_id": underlying,
                "repeat_index": repeat,
                "run_id": run_id,
            },
        )
        provenance = _write_json(
            root,
            f"{prefix}/adapter-provenance.json",
            {
                "schema_version": "cidr-test-provenance-v2",
                "mode": "formal",
                "variant": variant,
                "repo": {"head": git},
                "e01_binding": {
                    "dataset_id": protocol["dataset_id"],
                    "input_sha256": input_sha,
                    "workload_id": protocol["workload_id"],
                    "query_trace_sha256": trace_sha,
                    "seed": protocol["seed"],
                    "cache_state": protocol["cache_state"],
                    "concurrency": protocol["concurrency"],
                    "interface_scope": protocol["interface_scope"],
                    "host_fingerprint": host,
                    "git_sha": git,
                    "binary_sha256": binary,
                    "system_version": version,
                },
            },
        )
        p31_manifest_value = {
            "schema_version": "cidr-run-manifest-v1",
            "state": "PASS",
            "command_exit_code": 0,
            "collector_exit_code": 0,
            "performance_eligible_declared": True,
            "validation": {
                "schema_version": "cidr-run-manifest-v1",
                "state": "PASS",
                "errors": [],
                "warnings": [],
            },
            "host": {"fingerprint_sha256": host},
            "repo": {"git_sha": git},
            "inputs": {
                "dataset": {"sha256": input_sha},
                "query_or_trace": {"sha256": trace_sha},
            },
            "integrity_guard": {
                "state": "PASS",
                "consumer": "P10",
                "lease_sha256": _ref_sha(lease),
            },
        }
        p31_manifest = _write_json(root, f"{prefix}/p31/run-manifest.json", p31_manifest_value)
        p31_validation = _write_json(
            root,
            f"{prefix}/p31/validation.json",
            {
                "schema_version": "cidr-run-manifest-v1",
                "state": "PASS",
                "errors": [],
                "warnings": [],
            },
        )
        p31_done = _write_json(
            root,
            f"{prefix}/p31/DONE",
            {
                "manifest_sha256": _ref_sha(p31_manifest),
                "state": "PASS",
                "validation_sha256": _ref_sha(p31_validation),
            },
        )
        admission = _write_json(
            root,
            f"{prefix}/batch-lease-admission.json",
            {
                "schema_version": "cidr-p10-repeat-batch-admission-v2",
                "state": "PASS",
                "protocol": "batch-lease-v2",
                "consumer": "P10",
                "receipt": {
                    "schema_version": "cidr-batch-lease-admission-v2",
                    "state": "PASS",
                    "consumer": "P10",
                    "lease_sha256": _ref_sha(lease),
                    "host": {"fingerprint_sha256": host},
                    "repo_head": git,
                    "remaining_seconds": 3600.0,
                },
                "lease": {"path": lease["path"], "sha256": _ref_sha(lease)},
            },
        )
        cost = _write_json(
            root,
            f"{prefix}/cost-receipt.json",
            {
                "schema_version": "cidr-e01-cost-receipt-v2",
                "state": "PASS",
                "run_key": run_key,
                "load_wall_s": metrics["load_wall_s"],
                "final_disk_bytes": metrics["final_disk_bytes"],
                "accounting_rule": "direct-import-and-sealed-tree-v2",
                "inferred": False,
            },
        )
        validated = _write_json(
            root,
            f"{prefix}/validated-result.json",
            {
                "schema_version": "cidr-p10-validated-repeat-v1",
                "system_id": underlying,
                "display_name": display_name,
                "repeat_index": repeat,
                "system_version": version,
                "interface_scope": protocol["interface_scope"],
                "timeout_queries": 0,
                "completed_queries": completed,
                "mismatch_queries": 0,
                "expected_digest_sha256": "1" * 64,
                "actual_digest_sha256": "1" * 64,
                "latency_p50_us": p50,
                "latency_p95_us": p95,
                "latency_p99_us": p99,
                "measurement_s": measurement,
                "warmup_s": 5.0,
                "qps": qps,
                "request": {"path": request["path"], "sha256": _ref_sha(request)},
                "p31": {
                    "manifest_sha256": _ref_sha(p31_manifest),
                    "validation_sha256": _ref_sha(p31_validation),
                    "done_sha256": _ref_sha(p31_done),
                },
            },
        )
        runs.append(
            {
                "run_key": run_key,
                "run_id": run_id,
                "system_key": system_key,
                "display_name": normalizer.SYSTEM_NAMES[system_key],
                "variant": variant,
                "repeat_index": repeat,
                "timestamp_utc": timestamp,
                "host_fingerprint": host,
                "git_sha": git,
                "binary_sha256": binary,
                "system_version": version,
                "classification": {
                    "execution_mode": "formal",
                    "conditional_waiver": False,
                    "diagnostic_only": False,
                    "formal_eligible": True,
                    "performance_eligible": True,
                    "paper_claim_eligible": False,
                },
                "outcome": {
                    "state": "PASS",
                    "superseded": False,
                    "failed_marker_present": False,
                },
                "metrics": metrics,
                "artifacts": {
                    "validated_result": validated,
                    "adapter_request": request,
                    "adapter_provenance": provenance,
                    "p31_manifest": p31_manifest,
                    "p31_validation": p31_validation,
                    "p31_done": p31_done,
                    "batch_admission": admission,
                    "cost_receipt": cost,
                },
            }
        )

    manifest = {
        "schema_version": "cidr-e01-formal-manifest-v2",
        "experiment_id": "E01",
        "campaign_id": "TEST-ONLY-e01-v2",
        "created_at_utc": "2026-01-01T01:00:00Z",
        "classification": {
            "execution_mode": "formal",
            "conditional_waiver": False,
            "diagnostic_only": False,
            "formal_eligible": True,
            "performance_eligible": True,
            "paper_claim_eligible": False,
        },
        "protocol": protocol,
        "admission_epochs": [
            {
                "epoch_id": "test-epoch-1",
                "run_keys": all_run_keys,
                "gate_result": gate,
                "batch_lease": lease,
                "batch_lease_marker": marker,
            }
        ],
        "runs": runs,
    }
    manifest_path = root / "E01-formal-manifest-v2.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path, manifest


class E01V2ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="e01-v2-test-")
        self.root = Path(self.temporary.name)
        self.manifest_path, self.manifest = build_fixture(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_manifest(self, value: Dict[str, Any]) -> None:
        self.manifest_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _replace_run_asset(
        self, value: Dict[str, Any], run_index: int, role: str, payload: Dict[str, Any]
    ) -> None:
        old_ref = value["runs"][run_index]["artifacts"][role]
        new_ref = _write_json(self.root, old_ref["path"], payload)
        value["runs"][run_index]["artifacts"][role] = new_ref

    def test_valid_21_run_dry_run_is_paper_claim_closed(self) -> None:
        result = normalizer.normalize_manifest(
            self.manifest_path, None, normalizer.SCHEMA_PATH
        )
        self.assertEqual(result["receipt"]["row_count"], 21)
        self.assertFalse(result["receipt"]["output_eligibility"]["paper_claim_eligible"])

    def test_conditional_manifest_is_rejected(self) -> None:
        value = copy.deepcopy(self.manifest)
        value["classification"]["execution_mode"] = "conditional"
        self._write_manifest(value)
        with self.assertRaises(normalizer.ContractError):
            normalizer.normalize_manifest(self.manifest_path, None, normalizer.SCHEMA_PATH)

    def test_missing_naive_repeat_is_rejected(self) -> None:
        value = copy.deepcopy(self.manifest)
        value["runs"] = value["runs"][:-1]
        self._write_manifest(value)
        with self.assertRaises(normalizer.ContractError):
            normalizer.normalize_manifest(self.manifest_path, None, normalizer.SCHEMA_PATH)

    def test_duplicate_run_id_is_rejected(self) -> None:
        value = copy.deepcopy(self.manifest)
        value["runs"][1]["run_id"] = value["runs"][0]["run_id"]
        self._write_manifest(value)
        with self.assertRaises(normalizer.ContractError):
            normalizer.normalize_manifest(self.manifest_path, None, normalizer.SCHEMA_PATH)

    def test_trace_drift_in_p31_is_rejected(self) -> None:
        value = copy.deepcopy(self.manifest)
        asset = value["runs"][0]["artifacts"]["p31_manifest"]
        path = self.root / asset["path"]
        p31 = json.loads(path.read_text(encoding="utf-8"))
        p31["inputs"]["query_or_trace"]["sha256"] = "9" * 64
        payload = (json.dumps(p31, sort_keys=True, separators=(",", ":")) + "\n").encode()
        path.write_bytes(payload)
        with self.assertRaises(normalizer.ContractError):
            normalizer.normalize_manifest(self.manifest_path, None, normalizer.SCHEMA_PATH)

    def test_cache_drift_in_provenance_binding_is_rejected(self) -> None:
        value = copy.deepcopy(self.manifest)
        ref = value["runs"][0]["artifacts"]["adapter_provenance"]
        provenance = json.loads((self.root / ref["path"]).read_text(encoding="utf-8"))
        provenance["e01_binding"]["cache_state"] = "cold"
        self._replace_run_asset(value, 0, "adapter_provenance", provenance)
        self._write_manifest(value)
        with self.assertRaises(normalizer.ContractError):
            normalizer.normalize_manifest(self.manifest_path, None, normalizer.SCHEMA_PATH)

    def test_binary_drift_between_run_and_binding_is_rejected(self) -> None:
        value = copy.deepcopy(self.manifest)
        value["runs"][0]["binary_sha256"] = "8" * 64
        self._write_manifest(value)
        with self.assertRaises(normalizer.ContractError):
            normalizer.normalize_manifest(self.manifest_path, None, normalizer.SCHEMA_PATH)

    def test_p31_failed_state_is_rejected(self) -> None:
        value = copy.deepcopy(self.manifest)
        ref = value["runs"][0]["artifacts"]["p31_manifest"]
        p31 = json.loads((self.root / ref["path"]).read_text(encoding="utf-8"))
        p31["state"] = "FAILED"
        self._replace_run_asset(value, 0, "p31_manifest", p31)
        self._write_manifest(value)
        with self.assertRaises(normalizer.ContractError):
            normalizer.normalize_manifest(self.manifest_path, None, normalizer.SCHEMA_PATH)

    def test_asset_sha_drift_is_rejected(self) -> None:
        value = copy.deepcopy(self.manifest)
        asset = value["runs"][0]["artifacts"]["cost_receipt"]
        path = self.root / asset["path"]
        path.write_text(path.read_text(encoding="utf-8").replace("PASS", "FAIL"), encoding="utf-8")
        with self.assertRaises(normalizer.ContractError):
            normalizer.normalize_manifest(self.manifest_path, None, normalizer.SCHEMA_PATH)

    def test_bad_qps_is_rejected_before_output(self) -> None:
        value = copy.deepcopy(self.manifest)
        value["runs"][0]["metrics"]["completed_qps"] *= 2
        self._write_manifest(value)
        output = self.root / "out"
        with self.assertRaises(normalizer.ContractError):
            normalizer.normalize_manifest(self.manifest_path, output, normalizer.SCHEMA_PATH)
        self.assertFalse(output.exists())

    def test_legacy_inventory_snapshot_matches_frozen_source(self) -> None:
        source = ROOT.parents[1] / "l6-control" / "sample-l5" / "L5-CONDITIONAL-COMBINED.json"
        snapshot = ROOT / "existing-evidence-inventory.json"
        snapshot_value = json.loads(snapshot.read_text(encoding="utf-8"))
        # The Linux detached engineering worktree intentionally does not copy
        # the untracked legacy evidence summary.  Structural checks still run;
        # when the source is present (the local audit workspace), compare every
        # recorded SHA against it.
        if not source.exists():
            self.assertEqual(snapshot_value["observed"]["repeat_count"], 18)
            self.assertEqual(snapshot_value["eligibility"]["formal_eligible"], False)
            self.assertEqual(snapshot_value["eligibility"]["performance_eligible"], False)
            self.assertEqual(snapshot_value["eligibility"]["paper_claim_eligible"], False)
            return
        source_value = json.loads(source.read_text(encoding="utf-8"))
        self.assertEqual(snapshot_value["source"]["sha256"], _sha(source.read_bytes()))
        self.assertEqual(snapshot_value["observed"]["repeat_count"], 18)
        self.assertEqual(len(snapshot_value["observed"]["cells"]), 18)
        for snap, raw in zip(snapshot_value["observed"]["cells"], source_value["repeats"]):
            self.assertEqual(snap["cell_key"], f"{raw['system_id']}:r{raw['repeat_index']}")
            self.assertEqual(
                snap["validated_result_sha256"],
                raw["validated_result"]["sha256"],
            )
            for snap_key, raw_key in (
                ("p31_validation_sha256", "p31_validation"),
                ("p31_done_sha256", "p31_done"),
                ("failed_marker_sha256", "source_root_failed_marker_preserved"),
            ):
                raw_ref = raw.get(raw_key)
                self.assertEqual(snap[snap_key], raw_ref.get("sha256") if raw_ref else None)
        self.assertEqual(snapshot_value["eligibility"]["formal_eligible"], False)
        self.assertEqual(snapshot_value["eligibility"]["performance_eligible"], False)
        self.assertEqual(snapshot_value["eligibility"]["paper_claim_eligible"], False)


if __name__ == "__main__":
    unittest.main()
