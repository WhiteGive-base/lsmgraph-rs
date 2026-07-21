#!/usr/bin/env python3

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
GENERATOR = HERE / "build_correctness_pass.py"
STAGES = ["A0", "A1", "A2", "A3", "A4", "A5", "A6"]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CorrectnessPassGeneratorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.binary = self.root / "binary"
        self.binary.write_bytes(b"binary\n")
        self.dataset = self.root / "dataset"
        self.dataset.write_bytes(b"dataset\n")
        self.sample = self.root / "sample.json"
        self.sample.write_text('{"version":1}\n', encoding="utf-8")
        self.truth = self.root / "truth.json"
        self.truth.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "digest_schema": "storage-bench-result-digest-v1",
                    "scale": "sf10",
                    "workload": "typed-one-hop",
                    "sample_plan_sha256": sha256(self.sample),
                    "property_predicate_mode": "none",
                    "property_id": 0,
                    "entries": [
                        {
                            "edge_type": 1,
                            "src_label": 1,
                            "dst_label": 2,
                            "entry_result_digest": "0123456789abcdef",
                            "samples": [
                                {
                                    "src": 7,
                                    "result_count": 2,
                                    "result_digest": "1111111111111111",
                                }
                            ],
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.observations = []
        for stage in STAGES:
            path = self.root / (stage + ".json")
            path.write_text(
                json.dumps(
                    {
                        "query_control_stage": stage,
                        "sample_plan_version": 1,
                        "scan_requested": False,
                        "emit_result_digests": True,
                        "property_predicate_mode": "none",
                        "property_id": 0,
                        "benchmarks": [
                            {
                                "edge_type": 1,
                                "src_label": 1,
                                "dst_label": 2,
                                "entry_result_digest": "0123456789abcdef",
                                "result_digests": [
                                    {
                                        "src": 7,
                                        "result_count": 2,
                                        "result_digest": "1111111111111111",
                                    }
                                ],
                            }
                        ],
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self.observations.append(path)
        self.output = self.root / "PASS.json"

    def tearDown(self):
        self.temporary.cleanup()

    def command(self):
        command = [
            sys.executable,
            str(GENERATOR),
            "--output",
            str(self.output),
            "--scale",
            "sf10",
            "--workload",
            "typed-one-hop",
            "--binary",
            str(self.binary),
            "--binary-sha256",
            sha256(self.binary),
            "--dataset",
            str(self.dataset),
            "--dataset-sha256",
            sha256(self.dataset),
            "--sample-plan",
            str(self.sample),
            "--sample-plan-sha256",
            sha256(self.sample),
            "--truth",
            str(self.truth),
            "--truth-sha256",
            sha256(self.truth),
        ]
        for stage, path in zip(STAGES, self.observations):
            command.extend(["--observation", "{}={}".format(stage, path)])
        return command

    def test_builds_external_full_stage_pass(self):
        result = subprocess.run(self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        gate = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(gate["covered_stages"], STAGES)
        self.assertEqual(gate["expected_queries_per_stage"], 1)
        self.assertEqual(gate["checked"], 7)
        self.assertEqual(gate["mismatches"], 0)
        self.assertFalse(gate["performance_eligible"])

    def test_digest_mismatch_fails_without_output(self):
        raw = json.loads(self.observations[3].read_text(encoding="utf-8"))
        raw["benchmarks"][0]["result_digests"][0]["result_digest"] = "f" * 16
        self.observations[3].write_text(json.dumps(raw) + "\n", encoding="utf-8")
        result = subprocess.run(self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest mismatch", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
