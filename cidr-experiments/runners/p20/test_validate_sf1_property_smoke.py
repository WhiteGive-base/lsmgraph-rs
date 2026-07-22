#!/usr/bin/env python3

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "property_gate", str(HERE / "validate_sf1_property_smoke.py")
)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class PropertySmokeGateTest(unittest.TestCase):
    def property_plan(self):
        return {
            "version": 1,
            "entries": [
                {
                    "edge_type": None,
                    "samples": [
                        {"src": 1, "degree": 3},
                        {"src": 2, "degree": 2},
                        {"src": 3, "degree": 1},
                    ],
                }
            ],
        }

    def property_result(self):
        samples = self.property_plan()["entries"][0]["samples"]
        digests = [
            {"src": 1, "result_count": 1, "result_digest": "1" * 16, "degree": 3, "edge_type": None, "property_predicate_mode": "presence"},
            {"src": 2, "result_count": 2, "result_digest": "2" * 16, "degree": 2, "edge_type": None, "property_predicate_mode": "presence"},
            {"src": 3, "result_count": 0, "result_digest": "3" * 16, "degree": 1, "edge_type": None, "property_predicate_mode": "presence"},
        ]
        return {
            "data_dir": str(HERE),
            "snapshot": 1,
            "sample_plan_in": str(Path(__file__).resolve()),
            "sample_plan_version": 1,
            "scan_requested": False,
            "emit_result_digests": True,
            "property_predicate_mode": "presence",
            "property_id": 5,
            "workload_mode": "one_hop",
            "benchmarks": [
                {
                    "edge_type": None,
                    "property_predicate_mode": "presence",
                    "property_id": 5,
                    "sample_degrees": samples,
                    "sampled_srcs": [1, 2, 3],
                    "sampled_vertices": 3,
                    "result_digests": [
                        dict(item, degree=sample["degree"])
                        for item, sample in zip(digests, samples)
                    ],
                }
            ],
        }

    def test_accepts_positive_negative_and_mixed_property_population(self):
        plan = self.property_plan()
        entries, total = GATE.validate_plan(plan, True)
        self.assertEqual(total, 3)
        stats = GATE.validate_result(
            self.property_result(),
            Path(__file__).resolve(),
            entries,
            5,
            True,
            HERE.resolve(),
            1,
        )
        self.assertEqual(stats["positive_queries"], 2)
        self.assertEqual(stats["zero_queries"], 1)
        self.assertEqual(stats["mixed_queries"], 1)
        self.assertEqual(stats["positive_records"], 3)
        self.assertEqual(stats["absent_records_lower_bound"], 3)

    def test_rejects_typed_filter_in_property_plan(self):
        plan = self.property_plan()
        plan["entries"][0]["edge_type"] = 1
        with self.assertRaisesRegex(GATE.GateError, "edge_type=null"):
            GATE.validate_plan(plan, True)

    def test_rejects_store_without_both_bitmap_populations(self):
        receipt = {
            "property_materialization": {
                "profile": "snb-knows-creation-date-v1",
                "property_id": 5,
                "edge_type": 1,
                "materialized_property_values": 4,
                "topology_only_directed_edges": 3,
                "property_bitmap_nonzero_segments_per_store": [2],
                "property_bitmap_zero_segments_per_store": [0],
            }
        }
        with self.assertRaisesRegex(GATE.GateError, "exact-negative"):
            GATE.validate_import(receipt, 5)

    def test_rejects_symlink_in_any_input_path_component(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            (real / "value.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
            linked = root / "linked"
            try:
                linked.symlink_to(real, target_is_directory=True)
            except OSError as error:
                self.skipTest("symlink creation unavailable: {}".format(error))
            with self.assertRaisesRegex(GATE.GateError, "symlink component"):
                GATE.load_json(linked / "value.json", "fixture")


if __name__ == "__main__":
    unittest.main()
