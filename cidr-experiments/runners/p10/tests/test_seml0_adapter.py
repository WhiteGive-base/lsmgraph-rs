#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

P10_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = P10_DIR.parents[2]
ADAPTER = P10_DIR / "adapters" / "seml0_adapter.py"
FIXTURE_BINARY = Path(__file__).resolve().parent / "fixture_seml0_binary.py"
P02B_VALIDATOR = P10_DIR.parent / "p02b" / "validate_sentinel_result.py"
P31_WRAPPER = P10_DIR.parent / "p31" / "run_with_resources.sh"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class SemL0AdapterTests(unittest.TestCase):
    maxDiff = None

    def prepare(self, root: Path) -> list[str]:
        store = root / "store"
        store.mkdir()
        store_file = store / "fixture.store"
        store_file.write_text("fixture store\n", encoding="utf-8")
        store_tree_sha = sha256(store_file)
        store_manifest = root / "store-manifest.json"
        write_json(
            store_manifest,
            {
                "schema_version": "p02b-store-manifest-v1",
                "store_path": str(store.resolve()),
                "store_sha256": store_tree_sha,
                "hash_method": "fixture-single-file-sha256",
            },
        )

        truth = root / "truth.tsv"
        truth.write_text(
            "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash\n"
            "0\t7\t0\t2\t10\t20\n"
            "1\t-7\t1\t1\t11\t21\n",
            encoding="utf-8",
        )
        plan = root / "sample-plan.json"
        write_json(
            plan,
            {
                "version": 1,
                "source": "shared-truth-tsv",
                "samples_per_edge_type": 1,
                "semantic_degree_hint": False,
                "force_signature": False,
                "src_label": None,
                "dst_label": None,
                "entries": [
                    {
                        "edge_type": 7,
                        "src_label": None,
                        "dst_label": None,
                        "candidate_edges_for_sampling": 2,
                        "candidate_sources_for_sampling": 1,
                        "samples": [{"src": 100, "degree": 2}],
                    },
                    {
                        "edge_type": -7,
                        "src_label": None,
                        "dst_label": None,
                        "candidate_edges_for_sampling": 1,
                        "candidate_sources_for_sampling": 1,
                        "samples": [{"src": 200, "degree": 1}],
                    },
                ],
            },
        )
        id_map = root / "id-map"
        id_map.mkdir()
        dense = id_map / "dense-to-original.tsv"
        original = id_map / "original-to-dense.tsv"
        dense.write_text("dense_id\toriginal_id\n0\t100\n1\t200\n2\t300\n", encoding="utf-8")
        original.write_text("original_id\tdense_id\n100\t0\n200\t1\n300\t2\n", encoding="utf-8")
        id_manifest = id_map / "id-map-manifest.json"
        write_json(
            id_manifest,
            {
                "format": "seml0-shared-id-map",
                "format_version": 1,
                "vertex_count": 3,
                "mapping_hash_algorithm": "fnv1a64-le-dense-original-v1",
                "mapping_hash": "fixture",
                "dense_to_original": {"path": dense.name, "sha256": sha256(dense)},
                "original_to_dense": {"path": original.name, "sha256": sha256(original)},
            },
        )
        request = root / "request.json"
        write_json(
            request,
            {
                "schema_version": "cidr-p10-adapter-request-v1",
                "contract_version": "cidr-typed-neighbor-adapter-v1",
                "suite_id": "fixture",
                "run_id": "fixture",
                "system_id": "seml0",
                "group": "embedded",
                "system_version": "fixture-seml0",
                "interface_scope": "typed-neighbor-dense-id-v1",
                "repeat_index": 1,
                "truth": {
                    "path": str(truth.resolve()),
                    "sha256": sha256(truth),
                    "query_count": 2,
                    "digest_algorithm": "mix64-dense-dst-count-sum-xor-v1",
                },
                "timing": {
                    "timing_boundary": "typed-neighbor-call-plus-result-materialization-and-digest-v1",
                    "clock": "CLOCK_MONOTONIC",
                    "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
                    "process_reuse_between_phases": True,
                    "warmup_passes": 1,
                    "measured_passes": 1,
                    "concurrency": 1,
                    "per_query_timeout_ms": 1000,
                    "sequence_digest_algorithm": "sha256-pass-query-count-sum-xor-v1",
                },
            },
        )
        p02b_result = root / "sentinel-result.json"
        write_json(
            p02b_result,
            {
                "schema_version": "p02b-sf10-sentinel-result-v1",
                "state": "PASS",
                "fixture_only": True,
                "performance_eligible": False,
                "formal_gate_eligible": False,
                "downstream_release_eligible": False,
                "consumers": ["P10", "P20"],
                "scale": "fixture",
                "protocol": {"expected_queries": 2},
                "provenance": {
                    "repo_head": "0" * 40,
                    "binary_sha256": sha256(FIXTURE_BINARY),
                    "truth_sha256": sha256(truth),
                    "query_plan_sha256": sha256(plan),
                    "store_sha256": store_tree_sha,
                },
                "correctness": {"state": "PASS", "mismatches": 0},
                "stability": {"state": "PASS", "qps": {"pass": True}, "p99_us": {"pass": True}},
            },
        )
        write_json(
            root / "FIXTURE-PASS",
            {
                "state": "PASS",
                "fixture_only": True,
                "result": str(p02b_result.resolve()),
                "result_sha256": sha256(p02b_result),
            },
        )
        output = root / "output"
        return [
            sys.executable,
            "-B",
            str(ADAPTER),
            "--mode", "fixture",
            "--variant", "schema",
            "--binary", str(FIXTURE_BINARY),
            "--binary-sha256", sha256(FIXTURE_BINARY),
            "--data-dir", str(store),
            "--store-manifest", str(store_manifest),
            "--store-manifest-sha256", sha256(store_manifest),
            "--store-tree-sha256", store_tree_sha,
            "--sample-plan", str(plan),
            "--sample-plan-sha256", sha256(plan),
            "--truth", str(truth),
            "--truth-sha256", sha256(truth),
            "--id-map-dir", str(id_map),
            "--id-map-manifest-sha256", sha256(id_manifest),
            "--p02b-result", str(p02b_result),
            "--p02b-result-sha256", sha256(p02b_result),
            "--p02b-validator", str(P02B_VALIDATOR),
            "--p02b-validator-sha256", sha256(P02B_VALIDATOR),
            "--p31-wrapper", str(P31_WRAPPER),
            "--p31-wrapper-sha256", sha256(P31_WRAPPER),
            "--repo-root", str(REPO_ROOT),
            "--request", str(request),
            "--output-dir", str(output),
        ]

    def invoke(self, command: list[str], expected: int = 0) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(command, text=True, capture_output=True, check=False, env=environment)
        self.assertEqual(completed.returncode, expected, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
        return completed

    def test_fixture_adapter_uses_one_storage_bench_process_and_publishes_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-seml0-adapter-") as temporary:
            root = Path(temporary)
            command = self.prepare(root)
            self.invoke(command)
            output = root / "output"
            self.assertEqual((output / "fixture-invocations.txt").read_text(encoding="utf-8"), "1")
            self.assertTrue((output / "adapter-result.json").is_file())
            self.assertTrue((output / "query-observations.tsv").is_file())
            self.assertTrue((output / "phase-events.jsonl").is_file())
            provenance = json.loads((output / "adapter-provenance.json").read_text(encoding="utf-8"))
            self.assertEqual(provenance["command"]["invocations"], 1)
            self.assertEqual(provenance["variant"], "schema")

    def test_p02b_plan_lineage_mismatch_fails_before_binary_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-seml0-lineage-") as temporary:
            root = Path(temporary)
            command = self.prepare(root)
            result_path = root / "sentinel-result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["provenance"]["query_plan_sha256"] = "f" * 64
            write_json(result_path, result)
            marker = json.loads((root / "FIXTURE-PASS").read_text(encoding="utf-8"))
            marker["result_sha256"] = sha256(result_path)
            write_json(root / "FIXTURE-PASS", marker)
            index = command.index("--p02b-result-sha256") + 1
            command[index] = sha256(result_path)
            completed = self.invoke(command, expected=2)
            self.assertIn("query plan differs", completed.stderr)
            self.assertFalse((root / "output" / "fixture-invocations.txt").exists())


if __name__ == "__main__":
    unittest.main()
