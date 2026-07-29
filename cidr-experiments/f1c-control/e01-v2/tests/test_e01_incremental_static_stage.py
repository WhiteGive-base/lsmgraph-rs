#!/usr/bin/env python3
"""Static asset/scheduler/canary tests for the four-cell E01 increment."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_e01_mixed_lineage as mixed
import evaluate_e01_bridge_canary as evaluator
import inventory_e01_seml0_assets as inventory
import run_e01_incremental_matrix as matrix
import seal_e01_incremental_light_assets as light_seals
import build_e01_incremental_command_plan as command_plan
from test_e01_mixed_lineage import make_fixture, write_json


def write_value(path: Path, value: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def ref(path: Path) -> Dict[str, Any]:
    return matrix.file_ref(path, path.name)


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_plan(root: Path) -> Tuple[Path, Dict[str, Any]]:
    source, dataset, dense = make_fixture(root)
    value = mixed.build_composition(
        source, dataset, dense, created_at_utc="2026-07-27T00:00:00Z"
    )
    return write_value(root / "mixed-plan.json", value), value


def make_asset_fixture(
    root: Path, plan_path: Path, plan: Dict[str, Any]
) -> Tuple[Path, Path, Path, Path]:
    binary = root / "assets/lsmgraph"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"synthetic binary identity placeholder\n")
    binary.chmod(0o555)
    binary_sha = next(
        cell["identity"]["binary_sha256"]
        for cell in plan["legacy_cells"]
        if cell["cell_key"] == "seml0:r1"
    )
    for cell in plan["legacy_cells"]:
        if cell["system_id"] != "seml0":
            continue
        request_path = Path(cell["identity"]["adapter_request"]["path"])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["binary"] = {"path": str(binary.resolve()), "sha256": binary_sha}
        write_value(request_path, request)
        cell["identity"]["adapter_request"] = ref(request_path)
    write_value(plan_path, plan)
    adapter = root / "assets/seml0_adapter.py"
    adapter.write_text(
        "VARIANTS = {\n"
        "    'naive': ('naive', False),\n"
        "    'budg-b64': ('semantic-budgeted', True),\n"
        "}\n",
        encoding="utf-8",
    )
    current_root = root / "stores/budg-b64"
    naive_root = root / "stores/naive"
    current_root.mkdir(parents=True)
    naive_root.mkdir(parents=True)
    current = write_value(
        root / "manifests/current.json",
        {
            "schema_version": inventory.STORE_SCHEMA,
            "store_root": str(current_root.resolve()),
            "store_sha256": "1" * 64,
            "file_count": 10,
            "total_bytes": 100,
            "hash_method": "sha256-tree-v1(relative-path,size,file-sha256)",
        },
    )
    naive = write_value(
        root / "manifests/naive.json",
        {
            "schema_version": inventory.STORE_SCHEMA,
            "store_root": str(naive_root.resolve()),
            "store_sha256": "2" * 64,
            "file_count": 11,
            "total_bytes": 101,
            "hash_method": "sha256-tree-v1(relative-path,size,file-sha256)",
        },
    )
    return adapter, current, naive, binary


def make_inventory(
    root: Path, plan_path: Path, plan: Dict[str, Any]
) -> Tuple[Path, Dict[str, Any]]:
    adapter, current, naive, _ = make_asset_fixture(root, plan_path, plan)
    value = inventory.inventory(plan_path, adapter, current, naive)
    return write_value(root / "asset-inventory.json", value), value


def make_ready_synthetic_spec(
    root: Path, hold: Dict[str, Any], plan_path: Path
) -> Tuple[Path, Path]:
    value = copy.deepcopy(hold)
    value["state"] = "READY"
    value["execution_state"] = "READY"
    value["synthetic_test_only"] = True
    value["blockers"] = []
    synthetic_inventory = write_value(
        root / "synthetic/inventory.json",
        {"state": "PASS", "synthetic_test_only": True, "production_ready": False},
    )
    value["asset_compatibility_inventory"] = ref(synthetic_inventory)
    command_plan = write_value(
        root / "synthetic/command-plan.json",
        {"state": "PASS", "synthetic_test_only": True, "adapter_invoked": False},
    )
    value["adapter_command_plan"] = ref(command_plan)
    for key in matrix.GATE_KEYS:
        gate = write_value(
            root / f"synthetic/gates/{key}.json",
            {
                "state": "PASS",
                "synthetic_test_only": True,
                "fixture_only": True,
                "adapter_invoked": False,
                "timing_generated": False,
            },
        )
        value["campaign_gates"][key] = ref(gate)
    result_root = root / "synthetic-result"
    value["campaign_root"] = str(result_root.resolve())
    return write_value(root / "synthetic/spec.json", value), result_root


def make_canary_evidence(
    root: Path, plan: Dict[str, Any], plan_path: Path
) -> Path:
    contract = plan["incremental_plan"]["bridge_canary_comparability_contract"]
    legacy = [
        cell for cell in plan["legacy_cells"] if cell["system_id"] == "seml0"
    ]
    first = legacy[0]
    metrics = {
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
    request = write_value(
        root / "canary/adapter-request.json",
        {
            "state": "PASS",
            "system_id": "seml0",
            "truth": {
                "path": "/fixture/truth.tsv",
                "sha256": plan["logical_dataset_identity"]["truth_sha256"],
            },
            "timing": {
                "per_query_timeout_ms": first["protocol"]["per_query_timeout_ms"]
            },
        },
    )
    validated = write_value(
        root / "canary/validated-result.json",
        {
            "state": "PASS",
            "system_id": "seml0",
            "request": {"path": str(request.resolve()), "sha256": ref(request)["sha256"]},
            "qps": metrics["completed_qps"],
            "latency_p50_us": metrics["latency_p50_us"],
            "latency_p95_us": metrics["latency_p95_us"],
            "latency_p99_us": metrics["latency_p99_us"],
            "completed_queries": 1700,
            "timeout_queries": 0,
            "mismatch_queries": 0,
            "expected_digest_sha256": plan["logical_dataset_identity"][
                "expected_digest_sha256"
            ],
            "actual_digest_sha256": plan["logical_dataset_identity"][
                "expected_digest_sha256"
            ],
            **{
                key: first["protocol"][key]
                for key in (
                    "query_count",
                    "interface_scope",
                    "concurrency",
                    "warmup_passes",
                    "measured_passes",
                    "per_query_timeout_ms",
                    "clock",
                    "timing_boundary",
                )
            },
            "p31": {
                "host": {
                    "fingerprint_sha256": first["identity"]["host_fingerprint"]
                }
            },
        },
    )
    p31 = write_value(root / "canary/p31.json", {"state": "PASS"})
    value = {
        "schema_version": evaluator.EVIDENCE_SCHEMA,
        "state": "PASS",
        "cell_key": "seml0:bridge-canary",
        "contract_sha256": contract["contract_sha256"],
        "classification": {
            "formal_eligible": True,
            "performance_eligible": True,
            "paper_claim_eligible": False,
        },
        "validated_result": ref(validated),
        "p31_receipt": ref(p31),
        "identity": {
            "logical_dataset_id": plan["logical_dataset_identity"]["logical_dataset_id"],
            "truth_sha256": plan["logical_dataset_identity"]["truth_sha256"],
            "host_fingerprint": first["identity"]["host_fingerprint"],
        },
        "protocol": {
            key: first["protocol"][key]
            for key in (
                "query_count",
                "interface_scope",
                "concurrency",
                "warmup_passes",
                "measured_passes",
                "per_query_timeout_ms",
                "clock",
                "timing_boundary",
            )
        },
        "correctness": {
            "completed_queries": 1700,
            "timeout_queries": 0,
            "mismatch_queries": 0,
            "expected_digest_sha256": plan["logical_dataset_identity"][
                "expected_digest_sha256"
            ],
            "actual_digest_sha256": plan["logical_dataset_identity"][
                "expected_digest_sha256"
            ],
        },
        "metrics": metrics,
    }
    return write_value(root / "canary/evidence.json", value)


def make_light_assets(
    root: Path,
    plan_path: Path,
    plan: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Path]]:
    adapter, current_store, naive_store, binary = make_asset_fixture(
        root, plan_path, plan
    )
    repo = root / "adapter-repo"
    repo.mkdir()
    adapter_in_repo = repo / "seml0_adapter.py"
    shutil.copy2(adapter, adapter_in_repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "seml0_adapter.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture adapter"], check=True)
    inventory_value = inventory.inventory(
        plan_path, adapter_in_repo, current_store, naive_store
    )
    inventory_value["engine_binary"]["candidate_sha256"] = file_sha(binary)
    inventory_path = write_value(root / "light/asset-inventory.json", inventory_value)
    truth = root / "light/truth.tsv"
    truth.parent.mkdir(parents=True, exist_ok=True)
    truth.write_text("fixture truth\n", encoding="utf-8")
    plan["logical_dataset_identity"]["truth_sha256"] = file_sha(truth)
    write_value(plan_path, plan)
    dataset_manifest = root / "lineage/sf10-dataset-manifest.json"
    dense_summary = root / "lineage/convert-summary.json"
    shared_plan = write_value(
        root / "light/shared-plan.json",
        {
            "entries": [
                {
                    "edge_type": index,
                    "samples": [{"src": sample, "degree": 1} for sample in range(50)],
                }
                for index in range(34)
            ]
        },
    )
    id_map_dir = root / "light/id-map"
    id_map = write_value(
        id_map_dir / "id-map-manifest.json",
        {
            "status": "PASS",
            "vertex_count": 29987835,
            "mapping_hash": "fixture-map",
        },
    )
    preflight = write_value(
        root / "light/preflight.json",
        {
            "truth_rows": 1700,
            "truth_tsv": str(truth.resolve()),
            "id_map_dir": str(id_map_dir.resolve()),
            "verification": {
                "status": "PASS",
                "checked": 1700,
                "mismatches": 0,
            },
        },
    )
    adapter_receipt = light_seals.adapter_identity(
        adapter_in_repo,
        repo,
        plan_path,
        "2026-07-27T00:00:00Z",
    )
    binary_receipt = light_seals.binary_file_seal(
        binary,
        inventory_path,
        "2026-07-27T00:00:00Z",
    )
    lineage_receipt = light_seals.lineage_seal(
        plan_path,
        dataset_manifest,
        dense_summary,
        shared_plan,
        preflight,
        truth,
        id_map,
        "2026-07-27T00:00:00Z",
    )
    paths = {
        "repo": repo,
        "adapter": adapter_in_repo,
        "binary": binary,
        "inventory": inventory_path,
        "current_store": current_store,
        "naive_store": naive_store,
        "p31": write_value(root / "light/p31-wrapper.sh", {"state": "fixture"}),
        "p02b_validator": write_value(root / "light/p02b-validator.py", {"state": "fixture"}),
        "adapter_receipt": write_value(root / "light/adapter-identity.json", adapter_receipt),
        "binary_receipt": write_value(root / "light/binary-seal.json", binary_receipt),
        "lineage_receipt": write_value(root / "light/lineage-seal.json", lineage_receipt),
    }
    return adapter_receipt, binary_receipt, lineage_receipt, paths


class IncrementalStaticStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="e01-incremental-static-")
        self.root = Path(self.temporary.name)
        self.plan_path, self.plan = make_plan(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_asset_inventory_is_hold_without_hashing_binary_or_store_trees(self) -> None:
        _, value = make_inventory(self.root, self.plan_path, self.plan)
        self.assertEqual(value["state"], "HOLD")
        self.assertFalse(value["production_ready"])
        self.assertFalse(value["engine_binary"]["binary_rehashed_now"])
        self.assertTrue(all(not item["tree_rehashed_now"] for item in value["stores"]))
        self.assertEqual(
            value["adapter"]["supported_variant_bindings"]["naive"]["l0_layout"],
            "naive",
        )

    def test_asset_inventory_rejects_adapter_variant_drift(self) -> None:
        adapter, current, naive, _ = make_asset_fixture(
            self.root, self.plan_path, self.plan
        )
        adapter.write_text("VARIANTS = {'naive': ('schema', False)}\n", encoding="utf-8")
        with self.assertRaises(inventory.InventoryError):
            inventory.inventory(self.plan_path, adapter, current, naive)

    def test_hold_production_preflight_rejects_before_root(self) -> None:
        inventory_path, _ = make_inventory(self.root, self.plan_path, self.plan)
        result_root = self.root / "formal-root-must-not-exist"
        hold = matrix.build_hold_spec(
            self.plan_path, inventory_path, result_root
        )
        spec = write_value(self.root / "hold-spec.json", hold)
        preflight = matrix.production_preflight(spec, result_root)
        self.assertEqual(preflight["state"], "BLOCKED")
        self.assertFalse(preflight["result_root_created"])
        self.assertFalse(result_root.exists())
        self.assertIn("executor.process_invocation=NOT_IMPLEMENTED", preflight["blockers"])

    def test_synthetic_four_cell_scheduler_resumes_strict_serial_prefix(self) -> None:
        inventory_path, _ = make_inventory(self.root, self.plan_path, self.plan)
        hold = matrix.build_hold_spec(
            self.plan_path, inventory_path, self.root / "unused-root"
        )
        spec, result_root = make_ready_synthetic_spec(
            self.root, hold, self.plan_path
        )
        partial = matrix.run_synthetic(spec, result_root, stop_after=2)
        self.assertEqual(partial["completed_cells"], 2)
        final = matrix.run_synthetic(spec, result_root)
        self.assertEqual(final["completed_cells"], 4)
        self.assertTrue((result_root / "MATRIX-DONE.json").is_file())
        self.assertFalse(final["timing_generated"])

    def test_synthetic_resume_rejects_unknown_cell(self) -> None:
        inventory_path, _ = make_inventory(self.root, self.plan_path, self.plan)
        hold = matrix.build_hold_spec(
            self.plan_path, inventory_path, self.root / "unused-root"
        )
        spec, result_root = make_ready_synthetic_spec(
            self.root, hold, self.plan_path
        )
        matrix.run_synthetic(spec, result_root, stop_after=1)
        (result_root / "cells/unknown").mkdir()
        with self.assertRaises(matrix.MatrixError):
            matrix.run_synthetic(spec, result_root)

    def test_synthetic_resume_rejects_corrupt_cell_done(self) -> None:
        inventory_path, _ = make_inventory(self.root, self.plan_path, self.plan)
        hold = matrix.build_hold_spec(
            self.plan_path, inventory_path, self.root / "unused-root"
        )
        spec, result_root = make_ready_synthetic_spec(
            self.root, hold, self.plan_path
        )
        matrix.run_synthetic(spec, result_root, stop_after=1)
        done = next((result_root / "cells").glob("*/CELL-DONE.json"))
        done.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(matrix.MatrixError):
            matrix.run_synthetic(spec, result_root)

    def test_canary_evaluator_passes_exact_legacy_median(self) -> None:
        evidence = make_canary_evidence(self.root, self.plan, self.plan_path)
        receipt = evaluator.evaluate(self.plan_path, evidence)
        self.assertEqual(receipt["state"], "PASS")
        self.assertTrue(receipt["normalizer_release"])
        self.assertEqual(receipt["failures"], [])

    def test_canary_evaluator_rejects_qps_outside_frozen_bound(self) -> None:
        evidence = make_canary_evidence(self.root, self.plan, self.plan_path)
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value["metrics"]["completed_qps"] *= 0.5
        validated_path = Path(value["validated_result"]["path"])
        validated = json.loads(validated_path.read_text(encoding="utf-8"))
        validated["qps"] = value["metrics"]["completed_qps"]
        write_value(validated_path, validated)
        value["validated_result"] = ref(validated_path)
        write_value(evidence, value)
        receipt = evaluator.evaluate(self.plan_path, evidence)
        self.assertEqual(receipt["state"], "FAILED")
        self.assertIn("performance.completed_qps", receipt["failures"])
        self.assertFalse(receipt["normalizer_release"])

    def test_canary_evaluator_rejects_identity_and_digest_drift(self) -> None:
        evidence = make_canary_evidence(self.root, self.plan, self.plan_path)
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value["identity"]["host_fingerprint"] = "9" * 64
        value["correctness"]["actual_digest_sha256"] = "8" * 64
        validated_path = Path(value["validated_result"]["path"])
        validated = json.loads(validated_path.read_text(encoding="utf-8"))
        validated["p31"]["host"]["fingerprint_sha256"] = "9" * 64
        validated["actual_digest_sha256"] = "8" * 64
        write_value(validated_path, validated)
        value["validated_result"] = ref(validated_path)
        write_value(evidence, value)
        receipt = evaluator.evaluate(self.plan_path, evidence)
        self.assertEqual(receipt["state"], "FAILED")
        self.assertIn("identity.host_fingerprint", receipt["failures"])
        self.assertIn("correctness.expected_digest_equals_actual", receipt["failures"])

    def test_canary_evaluator_rejects_wrapper_metric_not_in_validated_result(self) -> None:
        evidence = make_canary_evidence(self.root, self.plan, self.plan_path)
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value["metrics"]["completed_qps"] *= 0.99
        write_value(evidence, value)
        with self.assertRaises(evaluator.CanaryError):
            evaluator.evaluate(self.plan_path, evidence)

    def test_canary_receipt_refuses_overwrite(self) -> None:
        evidence = make_canary_evidence(self.root, self.plan, self.plan_path)
        receipt = evaluator.evaluate(self.plan_path, evidence)
        output = self.root / "canary/receipt.json"
        evaluator.atomic_write(output, receipt)
        with self.assertRaises(evaluator.CanaryError):
            evaluator.atomic_write(output, receipt)

    def test_real_asset_inventory_snapshot_is_hold_without_tree_rehash(self) -> None:
        snapshot = ROOT / "E01-seml0-asset-compatibility-inventory-v1.json"
        if not snapshot.exists():
            self.skipTest("real read-only asset snapshot is not installed")
        value = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual(value["state"], "HOLD")
        self.assertFalse(value["production_ready"])
        self.assertFalse(value["engine_binary"]["binary_rehashed_now"])
        self.assertEqual(
            [item["candidate_tree_sha256"] for item in value["stores"]],
            [
                "ae77255c03c40d9d7e55071374ab3adc1dc67942f9443ad7d97dacacbe78c8b5",
                "133e2ab535dd2c915d93e6e5ded65295151e199ec7268609f0a3e2f65387ddd2",
            ],
        )
        self.assertTrue(all(not item["tree_rehashed_now"] for item in value["stores"]))

    def test_real_four_cell_spec_snapshot_is_hold_and_root_unallocated(self) -> None:
        snapshot = ROOT / "E01-incremental-4cell-production-spec-HOLD-v1.json"
        if not snapshot.exists():
            self.skipTest("real HOLD production spec is not installed")
        value = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual(value["state"], "HOLD")
        self.assertEqual(value["execution_state"], "BLOCKED")
        self.assertEqual([cell["cell_key"] for cell in value["cells"]], list(matrix.RUN_KEYS))
        self.assertTrue(all(item is None for item in value["campaign_gates"].values()))
        self.assertIsNone(value["adapter_command_plan"])
        self.assertFalse(Path(value["campaign_root"]).exists())

    def test_real_light_asset_bundle_is_fresh_small_scope_only(self) -> None:
        bundle = ROOT / "E01-incremental-light-seals-v1"
        if not bundle.exists():
            self.skipTest("real incremental light-seal bundle is not installed")
        expected = {}
        for line in (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            digest, name = line.split("  ", 1)
            expected[name] = digest
        self.assertEqual(
            set(expected),
            {
                "adapter-artifact-identity.json",
                "binary-file-seal.json",
                "dataset-trace-truth-lineage-seal.json",
            },
        )
        for name, digest in expected.items():
            self.assertEqual(file_sha(bundle / name), digest)
        adapter = json.loads(
            (bundle / "adapter-artifact-identity.json").read_text(encoding="utf-8")
        )
        binary = json.loads(
            (bundle / "binary-file-seal.json").read_text(encoding="utf-8")
        )
        lineage = json.loads(
            (bundle / "dataset-trace-truth-lineage-seal.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(adapter["state"], "PASS")
        self.assertTrue(adapter["fresh_identity_captured"])
        self.assertTrue(adapter["repo"]["clean_at_capture"])
        self.assertEqual(
            adapter["repo"]["head"], "84401bc4a3da3724a0eca9dcc46bcb2823ca1ee7"
        )
        self.assertEqual(binary["bytes_read_now"], 8_256_256)
        self.assertTrue(binary["content_hashed_now"])
        self.assertFalse(lineage["large_content_rehashed_now"])
        self.assertTrue(lineage["lineage_only"])
        self.assertFalse(lineage["physical_byte_equivalence_claimed"])
        for value in (adapter, binary, lineage):
            self.assertFalse(value["formal_eligible"])
            self.assertFalse(value["performance_eligible"])
            self.assertFalse(value["paper_claim_eligible"])

    def test_real_command_plan_and_v2_spec_remain_pre_root_hold(self) -> None:
        command_path = ROOT / "E01-incremental-adapter-command-plan-v1.json"
        spec_path = ROOT / "E01-incremental-4cell-production-spec-HOLD-v2.json"
        if not command_path.exists() or not spec_path.exists():
            self.skipTest("real command-plan/HOLD-v2 snapshots are not installed")
        command = command_plan.validate(command_path)
        self.assertEqual(command["state"], "HOLD")
        self.assertEqual(command["execution_state"], "BLOCKED")
        self.assertFalse(command["adapter_invoked"])
        self.assertFalse(command["timing_generated"])
        self.assertEqual([cell["cell_key"] for cell in command["cells"]], list(matrix.RUN_KEYS))
        self.assertTrue(all(cell["command_argv"] is None for cell in command["cells"]))
        self.assertTrue(
            all(cell["unresolved_arguments"]["fresh_store_seal"] is None for cell in command["cells"])
        )
        spec = matrix.validate_spec(spec_path)
        self.assertEqual(spec["state"], "HOLD")
        self.assertEqual(spec["execution_state"], "BLOCKED")
        self.assertEqual(
            spec["adapter_command_plan"]["sha256"], file_sha(command_path)
        )
        self.assertTrue(all(value is None for value in spec["campaign_gates"].values()))
        self.assertFalse(Path(spec["campaign_root"]).exists())

    def test_light_asset_seals_hash_only_binary_and_small_lineage(self) -> None:
        adapter, binary, lineage, _ = make_light_assets(
            self.root, self.plan_path, self.plan
        )
        self.assertEqual(adapter["state"], "PASS")
        self.assertTrue(binary["content_hashed_now"])
        self.assertLess(binary["bytes_read_now"], light_seals.MAX_BINARY_BYTES)
        self.assertFalse(lineage["large_content_rehashed_now"])
        self.assertEqual(
            lineage["checks"]["dense_dataset_content_rehash"], "NOT_PERFORMED"
        )
        self.assertFalse(adapter["formal_eligible"])
        self.assertFalse(binary["performance_eligible"])
        self.assertFalse(lineage["paper_claim_eligible"])

    def test_adapter_identity_requires_clean_repo(self) -> None:
        _, _, _, paths = make_light_assets(self.root, self.plan_path, self.plan)
        (paths["repo"] / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(light_seals.SealError):
            light_seals.adapter_identity(
                paths["adapter"],
                paths["repo"],
                self.plan_path,
                "2026-07-27T00:00:00Z",
            )

    def test_binary_file_seal_rejects_declared_sha_drift(self) -> None:
        _, _, _, paths = make_light_assets(self.root, self.plan_path, self.plan)
        value = json.loads(paths["inventory"].read_text(encoding="utf-8"))
        value["engine_binary"]["candidate_sha256"] = "0" * 64
        write_value(paths["inventory"], value)
        with self.assertRaises(light_seals.SealError):
            light_seals.binary_file_seal(
                paths["binary"],
                paths["inventory"],
                "2026-07-27T00:00:00Z",
            )

    def test_four_cell_command_plan_binds_immediate_assets_but_holds_p02b(self) -> None:
        _, _, _, paths = make_light_assets(self.root, self.plan_path, self.plan)
        campaign_root = self.root / "future-formal-root"
        value = command_plan.build(
            mixed_plan_path=self.plan_path,
            asset_inventory_path=paths["inventory"],
            adapter_identity_path=paths["adapter_receipt"],
            binary_seal_path=paths["binary_receipt"],
            lineage_seal_path=paths["lineage_receipt"],
            p31_wrapper_path=paths["p31"],
            p02b_validator_path=paths["p02b_validator"],
            current_store_manifest_path=paths["current_store"],
            naive_store_manifest_path=paths["naive_store"],
            campaign_root=campaign_root,
        )
        self.assertEqual(command_plan.validate(value)["state"], "HOLD")
        self.assertEqual([cell["cell_key"] for cell in value["cells"]], list(matrix.RUN_KEYS))
        self.assertTrue(all(cell["command_argv"] is None for cell in value["cells"]))
        self.assertTrue(
            all(
                cell["unresolved_arguments"]["--p02b-result"] is None
                for cell in value["cells"]
            )
        )
        self.assertFalse(campaign_root.exists())

    def test_hold_spec_can_bind_hold_command_plan_without_enabling_execution(self) -> None:
        _, _, _, paths = make_light_assets(self.root, self.plan_path, self.plan)
        campaign_root = self.root / "future-formal-root"
        command = command_plan.build(
            mixed_plan_path=self.plan_path,
            asset_inventory_path=paths["inventory"],
            adapter_identity_path=paths["adapter_receipt"],
            binary_seal_path=paths["binary_receipt"],
            lineage_seal_path=paths["lineage_receipt"],
            p31_wrapper_path=paths["p31"],
            p02b_validator_path=paths["p02b_validator"],
            current_store_manifest_path=paths["current_store"],
            naive_store_manifest_path=paths["naive_store"],
            campaign_root=campaign_root,
        )
        command_path = write_value(self.root / "light/command-plan.json", command)
        hold = matrix.build_hold_spec(
            self.plan_path,
            paths["inventory"],
            campaign_root,
            command_path,
        )
        self.assertIsNotNone(hold["adapter_command_plan"])
        self.assertEqual(hold["state"], "HOLD")
        self.assertEqual(hold["execution_state"], "BLOCKED")
        self.assertFalse(campaign_root.exists())


if __name__ == "__main__":
    unittest.main()
