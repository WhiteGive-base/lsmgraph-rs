#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location("prep_driver", str(HERE / "prepare_correctness.py"))
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)
TRUTH = DRIVER.truth_builder
INVENTORY = DRIVER.inventory


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class CorrectnessDriverFixtureTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.binary = self.root / "lsmgraph-fixture"
        self.binary.write_bytes(b"fixture binary\n")
        self.binary.chmod(0o755)
        self.dataset = self.root / "dataset"
        self.dataset.mkdir()
        (self.dataset / "data.tsv").write_bytes(b"fixture\n")
        dataset_file_sha = sha256(self.dataset / "data.tsv")
        dataset_record = "file\0{}\0{}\0{}\n".format(
            "data.tsv", 8, dataset_file_sha
        ).encode("utf-8")
        self.dataset_sha = hashlib.sha256(dataset_record).hexdigest()
        self.dataset_manifest = self.root / "dataset-manifest.json"
        write_json(self.dataset_manifest, {
            "schema_version": "p02b-dataset-manifest-v1",
            "dataset_root": str(self.dataset),
            "dataset_sha256": self.dataset_sha,
            "hash_method": "sha256-tree-v1(relative-path,size,file-sha256)",
            "file_count": 1,
            "total_bytes": 8,
        })
        self.store = self.root / "pristine"
        self.store.mkdir()
        (self.store / "store.bin").write_bytes(b"store\n")
        self.store_manifest = self.root / "store-manifest.json"
        manifest = INVENTORY.scan_full_manifest(
            self.store, "sf10", self.dataset_sha, sha256(self.binary)
        )
        write_json(self.store_manifest, manifest)
        self.plan = self.root / "plan.json"
        self.plan_doc = {
            "version": 1,
            "entries": [{
                "edge_type": 1,
                "src_label": None,
                "dst_label": None,
                "samples": [{"src": 10, "degree": 1}, {"src": 20, "degree": 9}],
            }],
        }
        write_json(self.plan, self.plan_doc)
        self.property_plan = self.root / "property-plan.json"
        property_samples = []
        for src in range(1, 1001):
            degree = 2 if src <= 250 or 501 <= src <= 750 else 20
            property_samples.append({"src": src, "degree": degree})
        self.property_plan_doc = {
            "version": 1,
            "entries": [{
                "edge_type": None,
                "src_label": None,
                "dst_label": None,
                "samples": property_samples,
            }],
        }
        write_json(self.property_plan, self.property_plan_doc)
        self.references = {}
        for workload in DRIVER.WORKLOADS:
            self.references[workload] = self.root / ("reference-{}.json".format(workload))
            write_json(self.references[workload], self.reference_doc(workload, "A0"))
        self.profiles = self.root / "profiles.json"
        self.validator = self.root / "validate_profiles.py"
        self.builder = self.root / "build_correctness_pass.py"
        for path in (self.profiles, self.validator, self.builder):
            path.write_text("fixture\n", encoding="utf-8")
        self.work_root = self.root / "work"
        self.work_root.mkdir()
        self.output_root = self.root / "output"
        self.config = self.root / "config.json"
        self.candidate_plan = self.root / "candidate-plan.json"
        self.candidate_result = self.root / "candidate-result.json"
        candidate_plan_doc = {
            "version": 1,
            "source": "scan",
            "samples_per_edge_type": DRIVER.property_plan_builder.CANDIDATE_POOL_REQUESTED,
            "semantic_degree_hint": True,
            "force_signature": False,
            "src_label": None,
            "dst_label": None,
            "entries": [{
                "edge_type": None,
                "src_label": None,
                "dst_label": None,
                "candidate_edges_for_sampling": 10000,
                "candidate_sources_for_sampling": 1000,
                "samples": property_samples,
            }],
        }
        write_json(self.candidate_plan, candidate_plan_doc)
        candidate_result_doc = self.reference_doc("property-presence", "A0")
        candidate_result_doc.update({
            "data_dir": str(self.store),
            "snapshot": 2000,
            "sample_plan_in": str(self.candidate_plan),
            "workload_mode": "one_hop",
        })
        candidate_result_doc["benchmarks"][0]["workload_mode"] = "one_hop"
        write_json(self.candidate_result, candidate_result_doc)
        self.candidate_plan_generation = self.root / "candidate-plan-generation.json"
        self.candidate_plan_stderr = self.root / "candidate-plan.stderr"
        self.candidate_result_stderr = self.root / "candidate-result.stderr"
        write_json(self.candidate_plan_generation, {
            "data_dir": str(self.store),
            "snapshot": 2000,
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
            "benchmarks": [{
                "edge_type": None,
                "src_label": None,
                "dst_label": None,
                "candidate_edges_for_sampling": 10000,
                "candidate_sources_for_sampling": 1000,
                "semantic_degree_hint": False,
                "force_signature": False,
                "workload_mode": "one_hop",
                "property_predicate_mode": "none",
                "property_id": 0,
                "sampled_vertices": len(property_samples),
                "sampled_srcs": [sample["src"] for sample in property_samples],
                "sample_degrees": property_samples,
                "warmup_runs": 0,
                "repeats": 1,
                "emit_result_digests": False,
            }],
        })
        self.candidate_plan_stderr.write_bytes(b"")
        self.candidate_result_stderr.write_bytes(b"")
        generation_plan_argv, generation_result_argv = (
            DRIVER.property_plan_builder.candidate_generator.exact_commands(
                self.binary.resolve(), self.store.resolve(), self.candidate_plan.resolve()
            )
        )
        self.generation_receipt = self.root / "generation-receipt.json"
        write_json(self.generation_receipt, {
            "schema_version": 1,
            "state": "PASS",
            "gate": DRIVER.property_plan_builder.candidate_generator.GATE,
            "scale": "sf10",
            "property_id": 5,
            "candidate_pool_requested": DRIVER.property_plan_builder.CANDIDATE_POOL_REQUESTED,
            "sample_plan_role": "candidate_pool",
            "performance_eligible": False,
            "formal_plan_eligible": False,
            "downstream_input_eligible": True,
            "binary": {"path": str(self.binary), "sha256": sha256(self.binary)},
            "typed_plan": {"path": str(self.plan), "sha256": sha256(self.plan)},
            "store": {
                "path": str(self.store),
                "snapshot": 2000,
                "inventory_sha256_before": manifest["inventory_sha256"],
                "inventory_sha256_after": manifest["inventory_sha256"],
                "file_count": manifest["file_count"],
                "total_bytes": manifest["total_bytes"],
                "full_content_verified_before": True,
                "full_content_verified_after": True,
            },
            "dataset_sha256": self.dataset_sha,
            "pristine_inventory": {
                "path": str(self.store_manifest),
                "sha256": sha256(self.store_manifest),
                "inventory_sha256": manifest["inventory_sha256"],
                "store_path": str(self.store),
                "scale": "sf10",
                "dataset_sha256": self.dataset_sha,
                "binary_sha256": sha256(self.binary),
            },
            "commands": {
                "candidate_plan": {"argv": generation_plan_argv, "exit_code": 0},
                "candidate_result": {"argv": generation_result_argv, "exit_code": 0},
                "forbidden_environment": {
                    "SNB_SKIP_SEM_INDEX": "absent",
                    "SNB_SKIP_ADJ_CACHE": "absent",
                },
            },
            "artifacts": {
                "candidate_plan": {"path": str(self.candidate_plan), "sha256": sha256(self.candidate_plan)},
                "candidate_plan_generation": {"path": str(self.candidate_plan_generation), "sha256": sha256(self.candidate_plan_generation)},
                "candidate_plan_stderr": {"path": str(self.candidate_plan_stderr), "sha256": sha256(self.candidate_plan_stderr)},
                "candidate_result": {"path": str(self.candidate_result), "sha256": sha256(self.candidate_result)},
                "candidate_result_stderr": {"path": str(self.candidate_result_stderr), "sha256": sha256(self.candidate_result_stderr)},
            },
        })
        selected_samples = []
        for sample in property_samples:
            src = sample["src"]
            mixed = src <= 500
            selected_samples.append({
                "src": src,
                "degree": sample["degree"],
                "property_count": 1 if mixed else 0,
                "stratum": "mixed" if mixed else "zero",
                "degree_class": DRIVER.property_plan_builder.degree_class(sample["degree"]),
            })
        self.property_receipt = self.root / "property-plan.receipt.json"
        write_json(self.property_receipt, {
            "schema_version": 1,
            "state": "PASS",
            "gate": DRIVER.property_plan_builder.PROFILE,
            "scale": "sf10",
            "property_id": 5,
            "sampling_design": "balanced_diagnostic",
            "natural_prevalence_claim": False,
            "natural_prevalence_eligible": False,
            "selection_seed": DRIVER.property_plan_builder.SELECTION_SEED,
            "candidate_pool_requested": DRIVER.property_plan_builder.CANDIDATE_POOL_REQUESTED,
            "shuffle_algorithm": DRIVER.property_plan_builder.SHUFFLE_ALGORITHM,
            "sample_plan_role": "balanced_diagnostic",
            "formal_plan_eligible": True,
            "downstream_input_eligible": True,
            "performance_eligible": False,
            "thresholds": {
                "expected_queries": 1000,
                "min_positive": 500,
                "min_mixed": 500,
                "min_zero": 500,
            },
            "selected": {
                "queries": 1000,
                "unique_sources": 1000,
                "positive": 500,
                "mixed": 500,
                "zero": 500,
                "degree_classes": ["low", "medium"],
                "source_assignment_sha256": DRIVER.property_plan_builder.canonical_sha(selected_samples),
                "samples": selected_samples,
            },
            "generation_receipt": {"path": str(self.generation_receipt), "sha256": sha256(self.generation_receipt)},
            "store": {
                "path": str(self.store),
                "snapshot": 2000,
                "inventory_sha256": manifest["inventory_sha256"],
            },
            "dataset_sha256": self.dataset_sha,
            "pristine_inventory": {
                "path": str(self.store_manifest),
                "sha256": sha256(self.store_manifest),
                "inventory_sha256": manifest["inventory_sha256"],
                "store_path": str(self.store),
                "scale": "sf10",
                "dataset_sha256": self.dataset_sha,
                "binary_sha256": sha256(self.binary),
            },
            "binary": {"path": str(self.binary), "sha256": sha256(self.binary)},
            "candidate_plan": {"path": str(self.candidate_plan), "sha256": sha256(self.candidate_plan)},
            "candidate_result": {"path": str(self.candidate_result), "sha256": sha256(self.candidate_result)},
            "typed_plan": {"path": str(self.plan), "sha256": sha256(self.plan)},
            "output_plan": {"path": str(self.property_plan), "sha256": sha256(self.property_plan)},
        })
        workload_rows = {}
        for workload in DRIVER.WORKLOADS:
            plan_path = self.property_plan if workload == "property-presence" else self.plan
            workload_rows[workload] = {
                "sample_plan": str(plan_path),
                "sample_plan_sha256": sha256(plan_path),
                "reference_output": str(self.references[workload]),
                "reference_output_sha256": sha256(self.references[workload]),
                "reference_stage": "A0",
                "property_id": 5 if workload == "property-presence" else 0,
            }
            if workload == "property-presence":
                workload_rows[workload].update({
                    "candidate_pool_requested": DRIVER.property_plan_builder.CANDIDATE_POOL_REQUESTED,
                    "expected_queries": 1000,
                    "min_positive": 500,
                    "min_mixed": 500,
                    "min_zero": 500,
                    "stratified_plan_receipt": str(self.property_receipt),
                    "stratified_plan_receipt_sha256": sha256(self.property_receipt),
                })
        self.config_doc = {
            "schema_version": DRIVER.CONFIG_SCHEMA,
            "scale": "sf10",
            "binary": {"path": str(self.binary), "sha256": sha256(self.binary)},
            "dataset": {
                "path": str(self.dataset), "sha256": self.dataset_sha,
                "manifest": str(self.dataset_manifest),
                "manifest_sha256": sha256(self.dataset_manifest),
            },
            "pristine_store": {
                "path": str(self.store),
                "inventory_sha256": manifest["inventory_sha256"],
                "manifest": str(self.store_manifest),
                "manifest_sha256": sha256(self.store_manifest),
            },
            "p20": {
                "profiles": {"path": str(self.profiles), "sha256": sha256(self.profiles)},
                "profile_validator": {"path": str(self.validator), "sha256": sha256(self.validator)},
                "correctness_builder": {"path": str(self.builder), "sha256": sha256(self.builder)},
            },
            "runtime": {"cpuset": None, "worker_threads": 1, "csr_metadata_cache_entries": 16},
            "workloads": workload_rows,
        }
        write_json(self.config, self.config_doc)

    def tearDown(self):
        self.temporary.cleanup()

    def digest_samples(self, workload):
        mode = "presence" if workload == "property-presence" else "none"
        edge_type = None if workload == "property-presence" else 1
        if workload == "property-presence":
            result = []
            for sample in self.property_plan_doc["entries"][0]["samples"]:
                src = sample["src"]
                result.append({
                    "src": src,
                    "degree": sample["degree"],
                    "edge_type": edge_type,
                    "dst_label": None,
                    "property_predicate_mode": mode,
                    "result_count": 1 if src <= 500 else 0,
                    "result_digest": "{:016x}".format(src),
                })
            return result
        return [
            {"src": 10, "degree": 1, "edge_type": edge_type, "dst_label": None,
             "property_predicate_mode": mode, "result_count": 1,
             "result_digest": "1111111111111111"},
            {"src": 20, "degree": 9, "edge_type": edge_type, "dst_label": None,
             "property_predicate_mode": mode, "result_count": 2,
             "result_digest": "2222222222222222"},
        ]

    def reference_doc(self, workload, stage):
        mode = "presence" if workload == "property-presence" else "none"
        property_id = 5 if workload == "property-presence" else 0
        edge_type = None if workload == "property-presence" else 1
        plan_path = self.property_plan if workload == "property-presence" else self.plan
        plan_doc = self.property_plan_doc if workload == "property-presence" else self.plan_doc
        samples = self.digest_samples(workload)
        compact = [
            {"src": item["src"], "result_count": item["result_count"],
             "result_digest": item["result_digest"]}
            for item in samples
        ]
        folded = TRUTH.fold_entry_digest(compact)
        return {
            "query_control_stage": stage,
            "edge_type": edge_type,
            "edge_types": [],
            "src_label": None,
            "dst_label": None,
            "sample_plan_version": 1,
            "sample_plan_in": str(plan_path),
            "sample_plan_out": None,
            "scan_requested": False,
            "emit_result_digests": True,
            "workload_mode": "one_hop",
            "property_predicate_mode": mode,
            "property_id": property_id,
            "semantic_degree_hint": workload == "degree-stratified",
            "sample_plan_degree_hint": workload == "degree-stratified",
            "force_signature": False,
            "warmup_runs": 0,
            "repeats": 1,
            "benchmarks": [{
                "edge_type": edge_type, "src_label": None, "dst_label": None,
                "emit_result_digests": True, "workload_mode": "one_hop",
                "property_predicate_mode": mode, "property_id": property_id,
                "semantic_degree_hint": workload == "degree-stratified",
                "force_signature": False,
                "warmup_runs": 0,
                "repeats": 1,
                "sampled_srcs": [item["src"] for item in samples],
                "sampled_vertices": len(samples),
                "sample_degrees": plan_doc["entries"][0]["samples"],
                "entry_result_digest": folded,
                "result_digests": samples,
                "rounds": [{"kind": "measured", "round": 1}],
            }],
        }

    def completed(self, args, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    def fake_subprocess(self, command, **kwargs):
        if command and command[0] == "cp":
            shutil.copytree(str(self.store), command[-1], dirs_exist_ok=True)
            return self.completed(command)
        if "--resolve" in command:
            stage = command[command.index("--stage") + 1]
            workload = command[command.index("--workload") + 1]
            storage_args = [
                "storage-bench", "--l0-layout", "semantic-budgeted",
                "--query-control-stage", stage.lower(), "--workload-mode", "one-hop",
                "--emit-result-digests",
            ]
            if workload == "degree-stratified":
                storage_args.extend(["--semantic-degree-hint", "--sample-plan-degree-hint"])
            if workload == "property-presence":
                storage_args.extend(["--property-predicate-mode", "presence", "--property-id", "5"])
            value = {
                "scale": "sf10", "stage": {"id": stage, "l0_layout": "semantic-budgeted"},
                "mode": {"name": "correctness", "performance_eligible": False,
                         "max_query_streams": 1},
                "fixed_inputs": {"io_backend": "blocking"},
                "workload": {"name": workload}, "storage_bench_args": storage_args,
            }
            return self.completed(command, stdout=json.dumps(value))
        if command and command[0] == sys.executable and str(self.builder) in command:
            output = Path(command[command.index("--output") + 1])
            workload = command[command.index("--workload") + 1]
            truth_path = Path(command[command.index("--truth") + 1])
            sample_path = Path(command[command.index("--sample-plan") + 1])
            binary_path = Path(command[command.index("--binary") + 1])
            truth_doc = json.loads(truth_path.read_text(encoding="utf-8"))
            expected_queries = truth_doc["expected_queries"]
            gate = {
                "schema_version": 1, "state": "PASS", "correctness_only": True,
                "performance_eligible": False,
                "digest_schema": "storage-bench-result-digest-v1",
                "covered_stages": list(DRIVER.CANONICAL_STAGES["sf10"]),
                "expected_queries_per_stage": expected_queries,
                "checked": expected_queries * 7, "mismatches": 0,
                "workload": workload, "scale": "sf10",
                "truth_sha256": sha256(truth_path), "sample_plan_sha256": sha256(sample_path),
                "binary_sha256": sha256(binary_path), "dataset_sha256": self.dataset_sha,
            }
            write_json(output, gate)
            return self.completed(command, stdout=str(output) + "\n")
        stage = command[command.index("--query-control-stage") + 1].upper()
        workload = "property-presence" if "--property-predicate-mode" in command else (
            "degree-stratified" if "--semantic-degree-hint" in command else "typed-one-hop"
        )
        value = self.reference_doc(workload, stage)
        value["sample_plan_in"] = command[command.index("--sample-plan-in") + 1]
        return self.completed(command, stdout=json.dumps(value) + "\n")

    def test_full_fixture_builds_three_truths_and_twenty_one_raw_passes(self):
        args = SimpleNamespace(
            config=self.config,
            output_root=self.output_root,
            work_root=self.work_root,
            cleanup_clones=True,
        )
        with mock.patch.object(DRIVER.subprocess, "run", side_effect=self.fake_subprocess):
            code = DRIVER.run(args)
        self.assertEqual(code, 0)
        summary = json.loads((self.output_root / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["state"], "PASS")
        self.assertFalse(summary["performance_eligible"])
        self.assertEqual(set(summary["workloads"]), set(DRIVER.WORKLOADS))
        self.assertEqual(len(list((self.output_root / "raw").glob("*/*/PASS.json"))), 21)
        self.assertEqual(list(self.work_root.iterdir()), [])

    def test_observation_mismatch_is_fail_closed(self):
        reference = self.reference_doc("typed-one-hop", "A3")
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        truth = TRUTH.build_truth(
            "sf10", "typed-one-hop", 0, self.plan, sha256(self.plan),
            self.references["typed-one-hop"], sha256(self.references["typed-one-hop"]), "A0",
        )
        reference["benchmarks"][0]["result_digests"][0]["result_digest"] = "f" * 16
        with self.assertRaises((DRIVER.PrepError, TRUTH.TruthError)):
            DRIVER.compare_observation(reference, plan, self.plan, truth, "typed-one-hop", 0, "A3")

    def test_config_rejects_missing_workload(self):
        del self.config_doc["workloads"]["property-presence"]
        write_json(self.config, self.config_doc)
        with self.assertRaises(DRIVER.PrepError):
            DRIVER.load_config(self.config)

    def test_property_plan_must_be_independent(self):
        typed = self.config_doc["workloads"]["typed-one-hop"]
        prop = self.config_doc["workloads"]["property-presence"]
        prop["sample_plan"] = typed["sample_plan"]
        prop["sample_plan_sha256"] = typed["sample_plan_sha256"]
        write_json(self.config, self.config_doc)
        args = SimpleNamespace(
            config=self.config,
            output_root=self.output_root,
            work_root=self.work_root,
            cleanup_clones=True,
        )
        with self.assertRaisesRegex(DRIVER.PrepError, "independent sample plan"):
            DRIVER.run(args)
        self.assertFalse(self.output_root.exists())

    def test_property_thresholds_cannot_be_lowered_in_config(self):
        prop = self.config_doc["workloads"]["property-presence"]
        prop["min_mixed"] = 499
        write_json(self.config, self.config_doc)
        args = SimpleNamespace(
            config=self.config,
            output_root=self.output_root,
            work_root=self.work_root,
            cleanup_clones=True,
        )
        with self.assertRaisesRegex(TRUTH.TruthError, "canonical thresholds"):
            DRIVER.run(args)
        self.assertFalse(self.output_root.exists())

    def test_property_receipt_selected_count_tamper_is_rejected(self):
        receipt = json.loads(self.property_receipt.read_text(encoding="utf-8"))
        receipt["selected"]["mixed"] = 499
        write_json(self.property_receipt, receipt)
        prop = self.config_doc["workloads"]["property-presence"]
        prop["stratified_plan_receipt_sha256"] = sha256(self.property_receipt)
        write_json(self.config, self.config_doc)
        args = SimpleNamespace(
            config=self.config,
            output_root=self.output_root,
            work_root=self.work_root,
            cleanup_clones=True,
        )
        with self.assertRaisesRegex(DRIVER.PrepError, "selected mixed drift"):
            DRIVER.run(args)
        self.assertFalse(self.output_root.exists())

    def test_property_candidate_result_tamper_is_rejected(self):
        self.candidate_result.write_text("tampered\n", encoding="utf-8")
        args = SimpleNamespace(
            config=self.config,
            output_root=self.output_root,
            work_root=self.work_root,
            cleanup_clones=True,
        )
        with self.assertRaisesRegex(DRIVER.PrepError, "SHA-256 mismatch"):
            DRIVER.run(args)
        self.assertFalse(self.output_root.exists())

    def test_cli_does_not_write_failure_into_preexisting_output(self):
        self.output_root.mkdir()
        sentinel = self.output_root / "user-data.txt"
        sentinel.write_text("preserve\n", encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(HERE / "prepare_correctness.py"),
             "--config", str(self.config), "--output-root", str(self.output_root),
             "--work-root", str(self.work_root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertFalse((self.output_root / DRIVER.OWNERSHIP_MARKER).exists())
        self.assertFalse((self.output_root / "P20-PREP-FAILED.json").exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")

    def test_p20_tool_tamper_is_rejected(self):
        self.profiles.write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(DRIVER.PrepError):
            DRIVER.verify_p20(self.config_doc["p20"])

    def test_clone_cleanup_refuses_symlink_and_preserves_target(self):
        target = self.work_root / "real-clone"
        target.mkdir()
        (target / "sentinel").write_text("preserve\n", encoding="utf-8")
        linked = self.work_root / "linked-clone"
        try:
            linked.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest("symlink creation unavailable: {}".format(exc))
        with self.assertRaises(DRIVER.PrepError):
            DRIVER.safe_cleanup_clone(linked, self.work_root)
        self.assertEqual((target / "sentinel").read_text(encoding="utf-8"), "preserve\n")


if __name__ == "__main__":
    unittest.main()
