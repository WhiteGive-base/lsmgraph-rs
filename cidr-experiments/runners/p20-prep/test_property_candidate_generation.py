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


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SCRIPT = HERE / "generate_property_candidate_pool.py"
SPEC = importlib.util.spec_from_file_location("candidate_generator", str(SCRIPT))
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)
INVENTORY = GENERATOR.inventory


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class PropertyCandidateGenerationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.store = self.root / "store"
        self.store.mkdir()
        (self.store / "store.bin").write_bytes(b"frozen store\n")
        self.typed_plan = self.root / "typed-plan.json"
        write_json(self.typed_plan, {"version": 1, "entries": [{"edge_type": 1}]})
        self.fixture = self.root / "fixture_binary.py"
        self.fixture.write_text(
            """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if not args or args[0] != 'storage-bench':
    raise SystemExit(9)
def value(flag):
    return args[args.index(flag) + 1]
store = Path(value('--data-dir')).resolve()
if '--sample-plan-out' in args:
    plan = Path(value('--sample-plan-out')).resolve()
    samples = [{'src': 1, 'degree': 2}]
    plan_doc = {
        'version': 1, 'source': 'scan', 'samples_per_edge_type': 300000,
        'semantic_degree_hint': True, 'force_signature': False,
        'src_label': None, 'dst_label': None,
        'entries': [{'edge_type': None, 'src_label': None, 'dst_label': None,
                     'candidate_edges_for_sampling': 2,
                     'candidate_sources_for_sampling': 1,
                     'samples': samples}]
    }
    plan.write_text(json.dumps(plan_doc, sort_keys=True) + '\\n', encoding='utf-8')
    generation = {
        'data_dir': str(store), 'snapshot': 123,
        'edge_type': None, 'edge_types': [], 'src_label': None, 'dst_label': None,
        'semantic_degree_hint': False, 'force_signature': False,
        'emit_result_digests': False, 'sample_plan_degree_hint': True,
        'workload_mode': 'one_hop', 'property_predicate_mode': 'none',
        'property_id': 0, 'warmup_runs': 0, 'repeats': 1,
        'sample_plan_in': None, 'sample_plan_out': str(plan),
        'sample_plan_version': 1, 'scan_requested': True,
        'benchmarks': [{
            'edge_type': None, 'src_label': None, 'dst_label': None,
            'candidate_edges_for_sampling': 2,
            'candidate_sources_for_sampling': 1,
            'semantic_degree_hint': False, 'force_signature': False,
            'workload_mode': 'one_hop', 'property_predicate_mode': 'none',
            'property_id': 0, 'sampled_vertices': 1, 'sampled_srcs': [1],
            'sample_degrees': samples, 'warmup_runs': 0, 'repeats': 1,
            'emit_result_digests': False,
        }],
    }
    if os.environ.get('P20_FIXTURE_PLAN_OUTPUT_DRIFT') == '1':
        generation['scan_requested'] = False
    print(json.dumps(generation))
else:
    if os.environ.get('P20_FIXTURE_MUTATE') == '1':
        (store / 'store.bin').write_bytes(b'mutated store\\n')
    print(json.dumps({
        'data_dir': str(store), 'snapshot': 123,
        'sample_plan_in': str(Path(value('--sample-plan-in')).resolve())
    }))
""",
            encoding="utf-8",
        )
        if os.name == "nt":
            self.binary = self.root / "fixture-lsmgraph.cmd"
            self.binary.write_text(
                '@"{}" "{}" %*\n'.format(sys.executable, self.fixture),
                encoding="utf-8",
            )
        else:
            self.binary = self.fixture
        self.binary.chmod(0o755)
        self.dataset_sha = "d" * 64
        self.output = self.root / "output"

    def tearDown(self):
        self.temporary.cleanup()

    def make_inventory(self):
        manifest = INVENTORY.scan_full_manifest(
            self.store, "sf10", self.dataset_sha, sha256(self.binary)
        )
        path = self.root / "inventory.json"
        write_json(path, manifest)
        return path

    def command(self, scale="sf1", inventory_path=None):
        command = [
            sys.executable, str(SCRIPT),
            "--scale", scale,
            "--property-id", "5",
            "--binary", str(self.binary),
            "--binary-sha256", sha256(self.binary),
            "--store", str(self.store),
            "--snapshot", "123",
            "--dataset-sha256", self.dataset_sha,
            "--typed-plan", str(self.typed_plan),
            "--typed-plan-sha256", sha256(self.typed_plan),
            "--output-root", str(self.output),
        ]
        if inventory_path is not None:
            command.extend([
                "--inventory-manifest", str(inventory_path),
                "--inventory-manifest-sha256", sha256(inventory_path),
            ])
        return command

    def test_sf1_generation_binds_exact_commands_and_full_store_identity(self):
        completed = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        receipt = json.loads(
            (self.output / "generation-receipt.json").read_text(encoding="utf-8")
        )
        self.assertFalse(receipt["performance_eligible"])
        self.assertFalse(receipt["downstream_input_eligible"])
        self.assertIsNone(receipt["pristine_inventory"])
        self.assertEqual(
            receipt["store"]["inventory_sha256_before"],
            receipt["store"]["inventory_sha256_after"],
        )
        expected_plan, expected_result = GENERATOR.exact_commands(
            self.binary.resolve(), self.store.resolve(),
            (self.output / "candidate-plan.json").resolve(),
        )
        self.assertEqual(receipt["commands"]["candidate_plan"]["argv"], expected_plan)
        self.assertEqual(receipt["commands"]["candidate_result"]["argv"], expected_result)
        self.assertEqual(receipt["commands"]["candidate_plan"]["exit_code"], 0)
        self.assertEqual(receipt["commands"]["candidate_result"]["exit_code"], 0)

    def test_sf10_requires_and_binds_verified_pristine_inventory(self):
        manifest = self.make_inventory()
        completed = subprocess.run(
            self.command(scale="sf10", inventory_path=manifest),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        receipt = json.loads(
            (self.output / "generation-receipt.json").read_text(encoding="utf-8")
        )
        self.assertTrue(receipt["downstream_input_eligible"])
        self.assertFalse(receipt["performance_eligible"])
        self.assertEqual(receipt["pristine_inventory"]["sha256"], sha256(manifest))
        self.assertEqual(
            receipt["pristine_inventory"]["inventory_sha256"],
            json.loads(manifest.read_text(encoding="utf-8"))["inventory_sha256"],
        )

    def test_store_content_mutation_is_fail_closed(self):
        environment = os.environ.copy()
        environment["P20_FIXTURE_MUTATE"] = "1"
        completed = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=environment,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("store", completed.stderr.lower())
        self.assertFalse((self.output / "generation-receipt.json").exists())
        self.assertTrue((self.output / "FAILED.json").is_file())

    def test_plan_generation_output_field_drift_is_fail_closed(self):
        environment = os.environ.copy()
        environment["P20_FIXTURE_PLAN_OUTPUT_DRIFT"] = "1"
        completed = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=environment,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("scan_requested drift", completed.stderr)
        self.assertFalse((self.output / "generation-receipt.json").exists())
        self.assertTrue((self.output / "FAILED.json").is_file())

    def test_sf10_rejects_inventory_bound_to_another_binary(self):
        manifest = self.make_inventory()
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        doc["binary_sha256"] = "0" * 64
        write_json(manifest, doc)
        completed = subprocess.run(
            self.command(scale="sf10", inventory_path=manifest),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("binary_sha256 drift", completed.stderr)
        self.assertFalse(self.output.exists())

    def test_output_root_is_never_overwritten(self):
        self.output.mkdir()
        sentinel = self.output / "user.txt"
        sentinel.write_text("preserve\n", encoding="utf-8")
        completed = subprocess.run(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
        self.assertFalse((self.output / "generation-receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
