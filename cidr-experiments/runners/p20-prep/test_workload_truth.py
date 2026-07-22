#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "build_workload_truth.py"
SPEC = importlib.util.spec_from_file_location("truth_builder", str(SCRIPT))
TRUTH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRUTH)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class WorkloadTruthTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.plan = self.root / "plan.json"
        self.plan_doc = {
            "version": 1,
            "source": "fixture",
            "entries": [
                {
                    "edge_type": 7,
                    "src_label": None,
                    "dst_label": None,
                    "samples": [{"src": 10, "degree": 1}, {"src": 20, "degree": 8}],
                }
            ],
        }
        self.plan.write_text(json.dumps(self.plan_doc, sort_keys=True) + "\n", encoding="utf-8")
        samples = [
            {"src": 10, "result_count": 1, "result_digest": "1111111111111111"},
            {"src": 20, "result_count": 2, "result_digest": "2222222222222222"},
        ]
        folded = TRUTH.fold_entry_digest(samples)
        full_samples = [
            dict(samples[0], degree=1, edge_type=7, dst_label=None, property_predicate_mode="none"),
            dict(samples[1], degree=8, edge_type=7, dst_label=None, property_predicate_mode="none"),
        ]
        self.reference = self.root / "reference.json"
        self.reference_doc = {
            "query_control_stage": "A0",
            "sample_plan_version": 1,
            "sample_plan_in": str(self.plan),
            "scan_requested": False,
            "emit_result_digests": True,
            "workload_mode": "one_hop",
            "property_predicate_mode": "none",
            "property_id": 0,
            "semantic_degree_hint": False,
            "sample_plan_degree_hint": False,
            "force_signature": False,
            "warmup_runs": 0,
            "repeats": 1,
            "benchmarks": [
                {
                    "edge_type": 7,
                    "src_label": None,
                    "dst_label": None,
                    "emit_result_digests": True,
                    "workload_mode": "one_hop",
                    "property_predicate_mode": "none",
                    "property_id": 0,
                    "semantic_degree_hint": False,
                    "force_signature": False,
                    "sampled_srcs": [10, 20],
                    "sampled_vertices": 2,
                    "sample_degrees": self.plan_doc["entries"][0]["samples"],
                    "entry_result_digest": folded,
                    "result_digests": full_samples,
                    "rounds": [
                        {
                            "kind": "measured",
                            "round": 1,
                        }
                    ],
                }
            ],
        }
        self.write_reference()
        self.output = self.root / "truth.json"

    def tearDown(self):
        self.temporary.cleanup()

    def write_reference(self):
        self.reference.write_text(
            json.dumps(self.reference_doc, sort_keys=True) + "\n", encoding="utf-8"
        )

    def command(self, workload="typed-one-hop", property_id=0):
        command = [
            sys.executable, str(SCRIPT),
            "--output", str(self.output),
            "--scale", "sf10",
            "--workload", workload,
            "--property-id", str(property_id),
            "--sample-plan", str(self.plan),
            "--sample-plan-sha256", sha256(self.plan),
            "--reference-output", str(self.reference),
            "--reference-output-sha256", sha256(self.reference),
            "--reference-stage", "A0",
        ]
        if workload == "property-presence":
            command.extend([
                "--expected-queries", "1000",
                "--min-positive", "500",
                "--min-mixed", "500",
                "--min-zero", "500",
            ])
        return command

    def write_balanced_property_fixture(self):
        plan_samples = []
        result_samples = []
        compact = []
        for src in range(1, 1001):
            degree = 2 if src <= 250 or 501 <= src <= 750 else 20
            count = 1 if src <= 500 else 0
            digest = "{:016x}".format(src)
            plan_samples.append({"src": src, "degree": degree})
            full = {
                "src": src,
                "degree": degree,
                "edge_type": None,
                "dst_label": None,
                "property_predicate_mode": "presence",
                "result_count": count,
                "result_digest": digest,
            }
            result_samples.append(full)
            compact.append({
                "src": src, "result_count": count, "result_digest": digest,
            })
        self.plan_doc["entries"][0]["edge_type"] = None
        self.plan_doc["entries"][0]["samples"] = plan_samples
        self.plan.write_text(
            json.dumps(self.plan_doc, sort_keys=True) + "\n", encoding="utf-8"
        )
        folded = TRUTH.fold_entry_digest(compact)
        self.reference_doc.update({
            "property_predicate_mode": "presence",
            "property_id": 17,
        })
        benchmark = self.reference_doc["benchmarks"][0]
        benchmark.update({
            "edge_type": None,
            "property_predicate_mode": "presence",
            "property_id": 17,
            "sampled_srcs": list(range(1, 1001)),
            "sampled_vertices": 1000,
            "sample_degrees": plan_samples,
            "entry_result_digest": folded,
            "result_digests": result_samples,
            "rounds": [{
                "kind": "measured",
                "round": 1,
            }],
        })
        self.write_reference()

    def test_builds_truth_and_recomputes_entry_fold(self):
        result = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        truth = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(truth["digest_schema"], "storage-bench-result-digest-v1")
        self.assertEqual(truth["expected_queries"], 2)
        self.assertEqual(truth["positive_result_samples"], 2)
        self.assertEqual(truth["entries"][0]["entry_result_digest"], TRUTH.fold_entry_digest(truth["entries"][0]["samples"]))

    def test_rejects_reference_sha_mismatch_without_output(self):
        command = self.command()
        command[command.index("--reference-output-sha256") + 1] = "0" * 64
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_rejects_entry_fold_mismatch(self):
        self.reference_doc["benchmarks"][0]["entry_result_digest"] = "0" * 16
        self.write_reference()
        result = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not fold", result.stderr)

    def test_rejects_vacuous_property_truth(self):
        benchmark = self.reference_doc["benchmarks"][0]
        self.plan_doc["entries"][0]["edge_type"] = None
        self.plan.write_text(
            json.dumps(self.plan_doc, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.reference_doc["property_predicate_mode"] = "presence"
        self.reference_doc["property_id"] = 17
        benchmark["edge_type"] = None
        benchmark["property_predicate_mode"] = "presence"
        benchmark["property_id"] = 17
        for sample in benchmark["result_digests"]:
            sample["result_count"] = 0
            sample["edge_type"] = None
            sample["property_predicate_mode"] = "presence"
        compact = [
            {"src": sample["src"], "result_count": 0, "result_digest": sample["result_digest"]}
            for sample in benchmark["result_digests"]
        ]
        folded = TRUTH.fold_entry_digest(compact)
        benchmark["entry_result_digest"] = folded
        self.write_reference()
        result = subprocess.run(
            self.command("property-presence", 17),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("vacuous", result.stderr)

    def test_property_plan_rejects_typed_edge_filter(self):
        benchmark = self.reference_doc["benchmarks"][0]
        self.reference_doc["property_predicate_mode"] = "presence"
        self.reference_doc["property_id"] = 17
        benchmark["property_predicate_mode"] = "presence"
        benchmark["property_id"] = 17
        for sample in benchmark["result_digests"]:
            sample["property_predicate_mode"] = "presence"
        self.write_reference()
        result = subprocess.run(
            self.command("property-presence", 17),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("edge_type=null", result.stderr)

    def test_balanced_property_truth_freezes_all_registered_thresholds(self):
        self.write_balanced_property_fixture()
        result = subprocess.run(
            self.command("property-presence", 17),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        truth = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(truth["expected_queries"], 1000)
        self.assertEqual(truth["positive_result_samples"], 500)
        self.assertEqual(truth["mixed_result_samples"], 500)
        self.assertEqual(truth["zero_result_samples"], 500)
        self.assertEqual(truth["min_positive"], 500)
        self.assertEqual(truth["min_mixed"], 500)
        self.assertEqual(truth["min_zero"], 500)
        self.assertGreaterEqual(len(truth["degree_classes"]), 2)
        self.assertFalse(truth["natural_prevalence_claim"])

    def test_property_thresholds_cannot_be_silently_lowered(self):
        self.write_balanced_property_fixture()
        command = self.command("property-presence", 17)
        command[command.index("--min-mixed") + 1] = "499"
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("canonical thresholds", result.stderr)
        self.assertFalse(self.output.exists())

    def test_property_truth_rejects_499_zero_samples(self):
        self.write_balanced_property_fixture()
        benchmark = self.reference_doc["benchmarks"][0]
        sample = benchmark["result_digests"][-1]
        sample["result_count"] = sample["degree"]
        compact = [
            {
                "src": item["src"],
                "result_count": item["result_count"],
                "result_digest": item["result_digest"],
            }
            for item in benchmark["result_digests"]
        ]
        folded = TRUTH.fold_entry_digest(compact)
        benchmark["entry_result_digest"] = folded
        self.write_reference()
        result = subprocess.run(
            self.command("property-presence", 17),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("below min_zero", result.stderr)
        self.assertFalse(self.output.exists())

    def test_output_is_never_overwritten(self):
        self.output.write_text("preserve\n", encoding="utf-8")
        result = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "preserve\n")

    def test_rejects_symlink_reference(self):
        linked = self.root / "reference-link.json"
        try:
            linked.symlink_to(self.reference)
        except OSError as exc:
            self.skipTest("symlink creation unavailable: {}".format(exc))
        command = self.command()
        command[command.index("--reference-output") + 1] = str(linked)
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
