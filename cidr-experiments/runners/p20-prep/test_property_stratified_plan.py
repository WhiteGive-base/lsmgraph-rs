#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SCRIPT = HERE / "build_property_stratified_plan.py"
SPEC = importlib.util.spec_from_file_location("property_plan_builder", str(SCRIPT))
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class PropertyStratifiedPlanTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.store = self.root / "store"
        self.store.mkdir()
        (self.store / "store.bin").write_bytes(b"frozen store\n")
        self.binary = self.root / "lsmgraph"
        self.binary.write_bytes(b"fixture binary\n")
        self.binary.chmod(0o755)
        self.candidate_plan = self.root / "candidate-plan.json"
        self.candidate_result = self.root / "candidate-result.json"
        self.typed_plan = self.root / "typed-plan.json"
        self.output_plan = self.root / "formal-property-plan.json"
        self.output_receipt = self.root / "formal-property-plan.receipt.json"
        samples = []
        self.counts = {}
        for src in range(1, 501):
            degree = 2 if src <= 250 else 20
            samples.append({"src": src, "degree": degree})
            self.counts[src] = 1
        for src in range(501, 1001):
            degree = 3 if src <= 750 else 30
            samples.append({"src": src, "degree": degree})
            self.counts[src] = 0
        for src in range(1001, 1011):
            samples.append({"src": src, "degree": 4})
            self.counts[src] = 4
        self.plan_doc = {
            "version": 1,
            "source": "scan",
            "samples_per_edge_type": BUILDER.CANDIDATE_POOL_REQUESTED,
            "semantic_degree_hint": True,
            "force_signature": False,
            "src_label": None,
            "dst_label": None,
            "entries": [{
                "edge_type": None,
                "src_label": None,
                "dst_label": None,
                "candidate_edges_for_sampling": 10000,
                "candidate_sources_for_sampling": len(samples),
                "samples": samples,
            }],
        }
        write_json(self.candidate_plan, self.plan_doc)
        self.typed_doc = {
            "version": 1,
            "source": "scan",
            "samples_per_edge_type": 1,
            "semantic_degree_hint": True,
            "force_signature": False,
            "src_label": None,
            "dst_label": None,
            "entries": [{
                "edge_type": 1,
                "src_label": None,
                "dst_label": None,
                "candidate_edges_for_sampling": 1,
                "candidate_sources_for_sampling": 1,
                "samples": [{"src": 1, "degree": 1}],
            }],
        }
        write_json(self.typed_plan, self.typed_doc)
        self.write_result()
        self.plan_generation = self.root / "candidate-plan-generation.json"
        self.plan_stderr = self.root / "candidate-plan-generation.stderr"
        self.result_stderr = self.root / "candidate-result.stderr"
        self.write_plan_generation()
        self.plan_stderr.write_bytes(b"")
        self.result_stderr.write_bytes(b"")
        self.generation_receipt = self.root / "generation-receipt.json"
        self.inventory_manifest = self.root / "inventory.json"
        self.dataset_sha = "d" * 64
        self.write_generation_receipt("sf10")

    def tearDown(self):
        self.temporary.cleanup()

    def write_result(self):
        samples = self.plan_doc["entries"][0]["samples"]
        digests = []
        compact = []
        for sample in samples:
            src = sample["src"]
            result_digest = "{:016x}".format(src)
            digest = {
                "src": src,
                "degree": sample["degree"],
                "edge_type": None,
                "dst_label": None,
                "property_predicate_mode": "presence",
                "result_count": self.counts[src],
                "result_digest": result_digest,
            }
            digests.append(digest)
            compact.append({
                "src": src,
                "result_count": self.counts[src],
                "result_digest": result_digest,
            })
        folded = BUILDER.fold_entry_digest(compact)
        benchmark = {
            "edge_type": None,
            "src_label": None,
            "dst_label": None,
            "emit_result_digests": True,
            "workload_mode": "one_hop",
            "property_predicate_mode": "presence",
            "property_id": 5,
            "semantic_degree_hint": False,
            "force_signature": False,
            "sampled_vertices": len(samples),
            "sampled_srcs": [sample["src"] for sample in samples],
            "sample_degrees": samples,
            "warmup_runs": 0,
            "repeats": 1,
            "entry_result_digest": folded,
            "result_digests": digests,
            "rounds": [{
                "kind": "measured",
                "round": 1,
            }],
        }
        self.result_doc = {
            "data_dir": str(self.store),
            "snapshot": 123,
            "edge_type": None,
            "edge_types": [],
            "src_label": None,
            "dst_label": None,
            "sample_plan_version": 1,
            "sample_plan_in": str(self.candidate_plan),
            "sample_plan_out": None,
            "scan_requested": False,
            "emit_result_digests": True,
            "workload_mode": "one_hop",
            "property_predicate_mode": "presence",
            "property_id": 5,
            "semantic_degree_hint": False,
            "sample_plan_degree_hint": False,
            "force_signature": False,
            "warmup_runs": 0,
            "repeats": 1,
            "benchmarks": [benchmark],
        }
        write_json(self.candidate_result, self.result_doc)

    def write_plan_generation(self):
        entry = self.plan_doc["entries"][0]
        samples = entry["samples"]
        benchmark = {
            "edge_type": None,
            "src_label": None,
            "dst_label": None,
            "candidate_edges_for_sampling": entry["candidate_edges_for_sampling"],
            "candidate_sources_for_sampling": entry["candidate_sources_for_sampling"],
            "semantic_degree_hint": False,
            "force_signature": False,
            "workload_mode": "one_hop",
            "property_predicate_mode": "none",
            "property_id": 0,
            "sampled_vertices": len(samples),
            "sampled_srcs": [sample["src"] for sample in samples],
            "sample_degrees": samples,
            "warmup_runs": 0,
            "repeats": 1,
            "emit_result_digests": False,
        }
        write_json(self.plan_generation, {
            "data_dir": str(self.store),
            "snapshot": 123,
            "edge_type": None,
            "edge_types": [],
            "src_label": None,
            "dst_label": None,
            "semantic_degree_hint": False,
            "force_signature": False,
            "emit_result_digests": False,
            "sample_plan_degree_hint": True,
            "workload_mode": "one_hop",
            "property_predicate_mode": "none",
            "property_id": 0,
            "warmup_runs": 0,
            "repeats": 1,
            "sample_plan_in": None,
            "sample_plan_out": str(self.candidate_plan),
            "sample_plan_version": 1,
            "scan_requested": True,
            "benchmarks": [benchmark],
        })

    def write_generation_receipt(self, scale="sf10"):
        self.write_plan_generation()
        binary_sha = sha256(self.binary)
        identity = BUILDER.inventory.scan_full_manifest(
            self.store, scale, self.dataset_sha, binary_sha
        )
        if scale == "sf10":
            write_json(self.inventory_manifest, identity)
            pristine = {
                "path": str(self.inventory_manifest),
                "sha256": sha256(self.inventory_manifest),
                "inventory_sha256": identity["inventory_sha256"],
                "store_path": str(self.store),
                "scale": "sf10",
                "dataset_sha256": self.dataset_sha,
                "binary_sha256": binary_sha,
            }
        else:
            pristine = None
        plan_command, result_command = BUILDER.candidate_generator.exact_commands(
            self.binary.resolve(), self.store.resolve(), self.candidate_plan.resolve()
        )
        receipt = {
            "schema_version": 1,
            "state": "PASS",
            "gate": BUILDER.candidate_generator.GATE,
            "scale": scale,
            "property_id": 5,
            "candidate_pool_requested": BUILDER.CANDIDATE_POOL_REQUESTED,
            "sample_plan_role": "candidate_pool",
            "performance_eligible": False,
            "formal_plan_eligible": False,
            "downstream_input_eligible": scale == "sf10",
            "binary": {"path": str(self.binary), "sha256": binary_sha},
            "typed_plan": {"path": str(self.typed_plan), "sha256": sha256(self.typed_plan)},
            "store": {
                "path": str(self.store),
                "snapshot": 123,
                "inventory_sha256_before": identity["inventory_sha256"],
                "inventory_sha256_after": identity["inventory_sha256"],
                "file_count": identity["file_count"],
                "total_bytes": identity["total_bytes"],
                "full_content_verified_before": True,
                "full_content_verified_after": True,
            },
            "dataset_sha256": self.dataset_sha,
            "pristine_inventory": pristine,
            "commands": {
                "candidate_plan": {"argv": plan_command, "exit_code": 0},
                "candidate_result": {"argv": result_command, "exit_code": 0},
                "forbidden_environment": {
                    "SNB_SKIP_SEM_INDEX": "absent",
                    "SNB_SKIP_ADJ_CACHE": "absent",
                },
            },
            "artifacts": {
                "candidate_plan": {"path": str(self.candidate_plan), "sha256": sha256(self.candidate_plan)},
                "candidate_plan_generation": {"path": str(self.plan_generation), "sha256": sha256(self.plan_generation)},
                "candidate_plan_stderr": {"path": str(self.plan_stderr), "sha256": sha256(self.plan_stderr)},
                "candidate_result": {"path": str(self.candidate_result), "sha256": sha256(self.candidate_result)},
                "candidate_result_stderr": {"path": str(self.result_stderr), "sha256": sha256(self.result_stderr)},
            },
        }
        write_json(self.generation_receipt, receipt)

    def command(self, output_plan=None, output_receipt=None):
        return [
            sys.executable, str(SCRIPT),
            "--generation-receipt", str(self.generation_receipt),
            "--generation-receipt-sha256", sha256(self.generation_receipt),
            "--output-plan", str(output_plan or self.output_plan),
            "--output-receipt", str(output_receipt or self.output_receipt),
        ]

    def test_builds_balanced_deterministic_plan(self):
        completed = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        plan = json.loads(self.output_plan.read_text(encoding="utf-8"))
        receipt = json.loads(self.output_receipt.read_text(encoding="utf-8"))
        selected = plan["entries"][0]["samples"]
        self.assertEqual(len(selected), 1000)
        self.assertEqual(len({row["src"] for row in selected}), 1000)
        self.assertTrue(all(row["edge_type"] is None for row in plan["entries"]))
        self.assertEqual(receipt["selected"]["mixed"], 500)
        self.assertEqual(receipt["selected"]["zero"], 500)
        self.assertEqual(receipt["selected"]["positive"], 500)
        self.assertGreaterEqual(len(receipt["selected"]["degree_classes"]), 2)
        self.assertEqual(receipt["output_plan"]["sha256"], sha256(self.output_plan))
        self.assertTrue(receipt["formal_plan_eligible"])
        self.assertTrue(receipt["downstream_input_eligible"])
        self.assertFalse(receipt["performance_eligible"])
        self.assertFalse(receipt["natural_prevalence_claim"])

        second_plan = self.root / "second-plan.json"
        second_receipt = self.root / "second-receipt.json"
        repeated = subprocess.run(
            self.command(output_plan=second_plan, output_receipt=second_receipt),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(self.output_plan.read_bytes(), second_plan.read_bytes())

    def test_sf1_is_never_formal_or_performance_eligible(self):
        self.write_generation_receipt("sf1")
        completed = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        receipt = json.loads(self.output_receipt.read_text(encoding="utf-8"))
        self.assertFalse(receipt["formal_plan_eligible"])
        self.assertFalse(receipt["performance_eligible"])
        self.assertEqual(receipt["sample_plan_role"], "calibration_only")

    def test_insufficient_mixed_reports_actual_distribution_without_outputs(self):
        for src in range(1, 11):
            self.counts[src] = 0
        self.write_result()
        self.write_generation_receipt()
        completed = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(completed.returncode, 2)
        diagnostic = json.loads(completed.stderr)
        self.assertEqual(diagnostic["observed_pool"]["mixed"], 490)
        self.assertEqual(diagnostic["observed_pool"]["zero"], 510)
        self.assertFalse(self.output_plan.exists())
        self.assertFalse(self.output_receipt.exists())

    def test_rejects_duplicate_candidate_source(self):
        samples = self.plan_doc["entries"][0]["samples"]
        samples[-1]["src"] = samples[0]["src"]
        write_json(self.candidate_plan, self.plan_doc)
        self.write_generation_receipt()
        completed = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("duplicate source", completed.stderr)
        self.assertFalse(self.output_plan.exists())

    def test_rejects_candidate_plan_substituted_for_typed_plan(self):
        self.typed_plan.write_bytes(self.candidate_plan.read_bytes())
        self.write_generation_receipt()
        completed = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("concrete edge type", completed.stderr)

    def test_rejects_result_snapshot_drift(self):
        self.result_doc["snapshot"] = 124
        write_json(self.candidate_result, self.result_doc)
        self.write_generation_receipt()
        completed = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("snapshot binding drift", completed.stderr)

    def test_rejects_generation_output_field_drift(self):
        generation = json.loads(self.plan_generation.read_text(encoding="utf-8"))
        generation["scan_requested"] = False
        write_json(self.plan_generation, generation)
        receipt = json.loads(self.generation_receipt.read_text(encoding="utf-8"))
        receipt["artifacts"]["candidate_plan_generation"]["sha256"] = sha256(
            self.plan_generation
        )
        write_json(self.generation_receipt, receipt)
        completed = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("scan_requested drift", completed.stderr)
        self.assertFalse(self.output_plan.exists())

    def test_never_overwrites_outputs(self):
        self.output_plan.write_text("preserve\n", encoding="utf-8")
        completed = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.output_plan.read_text(encoding="utf-8"), "preserve\n")
        self.assertFalse(self.output_receipt.exists())

    def test_pair_write_rolls_back_first_publish_if_second_publish_fails(self):
        first = self.root / "transaction-first.json"
        second = self.root / "transaction-second.json"
        real_link = BUILDER.os.link
        calls = {"count": 0}

        def fail_second(source, destination):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("fixture second publish failure")
            return real_link(source, destination)

        with mock.patch.object(BUILDER.os, "link", side_effect=fail_second):
            with self.assertRaises(OSError):
                BUILDER.write_pair_new(first, b"first\n", second, b"second\n")
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertFalse(first.with_name(first.name + ".tmp").exists())
        self.assertFalse(second.with_name(second.name + ".tmp").exists())

    def test_rejects_symlink_input_component(self):
        link = self.root / "store-link"
        try:
            link.symlink_to(self.store, target_is_directory=True)
        except OSError as exc:
            self.skipTest("symlink creation unavailable: {}".format(exc))
        command = self.command()
        receipt = json.loads(self.generation_receipt.read_text(encoding="utf-8"))
        receipt["store"]["path"] = str(link)
        write_json(self.generation_receipt, receipt)
        command = self.command()
        completed = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("symlink component", completed.stderr)
        self.assertFalse(self.output_plan.exists())


if __name__ == "__main__":
    unittest.main()
