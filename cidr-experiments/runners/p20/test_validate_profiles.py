#!/usr/bin/env python3

import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("validate_profiles", HERE / "validate_profiles.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ProfileValidationTest(unittest.TestCase):
    def setUp(self):
        self.doc = MODULE.load_profiles(HERE / "profiles.json")

    def test_canonical_document_passes(self):
        MODULE.validate(self.doc)

    def test_feature_drift_fails_closed(self):
        changed = copy.deepcopy(self.doc)
        changed["stages"][2]["features"][2] = True
        with self.assertRaises(MODULE.ProfileError):
            MODULE.validate(changed)

    def test_illegal_a5_layout_fails_closed(self):
        changed = copy.deepcopy(self.doc)
        changed["stages"][5]["l0_layout"] = "naive"
        with self.assertRaises(MODULE.ProfileError):
            MODULE.validate(changed)

    def test_a0_cannot_switch_to_naive_layout(self):
        changed = copy.deepcopy(self.doc)
        changed["stages"][0]["l0_layout"] = "naive"
        with self.assertRaises(MODULE.ProfileError):
            MODULE.validate(changed)

    def test_unknown_key_fails_closed(self):
        changed = copy.deepcopy(self.doc)
        changed["stages"][0]["surprise"] = True
        with self.assertRaises(MODULE.ProfileError):
            MODULE.validate(changed)

    def test_duplicate_json_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicate.json"
            path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
            with self.assertRaises(MODULE.ProfileError):
                MODULE.load_profiles(path)

    def test_workload_cannot_override_control_arguments(self):
        changed = copy.deepcopy(self.doc)
        changed["workloads"]["typed-one-hop"]["args"].append("--l0-layout=naive")
        with self.assertRaises(MODULE.ProfileError):
            MODULE.validate(changed)

    def test_mode_or_fixed_input_value_drift_fails_closed(self):
        changed = copy.deepcopy(self.doc)
        changed["modes"]["correctness"]["performance_eligible"] = True
        with self.assertRaises(MODULE.ProfileError):
            MODULE.validate(changed)
        changed = copy.deepcopy(self.doc)
        changed["fixed_inputs"]["io_backend"] = "uring"
        with self.assertRaises(MODULE.ProfileError):
            MODULE.validate(changed)

    def test_cpu_phase_a6_is_rejected(self):
        with self.assertRaises(MODULE.ProfileError):
            MODULE.resolve(
                self.doc,
                scale="sf10",
                stage_id="A6",
                mode_name="cpu-phase",
                workload_name="typed-one-hop",
                bindings={},
            )

    def test_sf30_noncanonical_stage_is_rejected(self):
        with self.assertRaises(MODULE.ProfileError):
            MODULE.resolve(
                self.doc,
                scale="sf30",
                stage_id="A1",
                mode_name="latency",
                workload_name="typed-one-hop",
                bindings={},
            )

    def test_property_binding_is_required_and_substituted(self):
        with self.assertRaises(MODULE.ProfileError):
            MODULE.resolve(
                self.doc,
                scale="sf10",
                stage_id="A3",
                mode_name="latency",
                workload_name="property-presence",
                bindings={},
            )
        resolved = MODULE.resolve(
            self.doc,
            scale="sf10",
            stage_id="A3",
            mode_name="latency",
            workload_name="property-presence",
            bindings={"PROPERTY_ID": "17"},
        )
        self.assertIn("17", resolved["storage_bench_args"])
        self.assertNotIn("--automatic-maintenance", resolved["storage_bench_args"])

    def test_a6_latency_enables_automatic_maintenance(self):
        resolved = MODULE.resolve(
            self.doc,
            scale="sf10",
            stage_id="A6",
            mode_name="latency",
            workload_name="typed-one-hop",
            bindings={},
        )
        self.assertIn("--automatic-maintenance", resolved["storage_bench_args"])
        self.assertEqual(
            resolved["storage_bench_args"][-2:], ["--training-runs", "1"]
        )
        self.assertNotIn("--query-cpu-phases", resolved["storage_bench_args"])

    def test_a4_training_and_compaction_are_same_process_arguments(self):
        resolved = MODULE.resolve(
            self.doc,
            scale="sf10",
            stage_id="A4",
            mode_name="latency",
            workload_name="typed-one-hop",
            bindings={},
        )
        args = resolved["storage_bench_args"]
        self.assertIn("--training-runs", args)
        self.assertIn("--training-feedback-compactions", args)
        self.assertNotIn("--automatic-maintenance", args)

    def test_a3_a4_share_training_prestate_but_only_a4_compacts(self):
        a3 = MODULE.resolve(
            self.doc,
            scale="sf10",
            stage_id="A3",
            mode_name="latency",
            workload_name="typed-one-hop",
            bindings={},
        )
        a4 = MODULE.resolve(
            self.doc,
            scale="sf10",
            stage_id="A4",
            mode_name="latency",
            workload_name="typed-one-hop",
            bindings={},
        )
        for resolved in (a3, a4):
            self.assertEqual(
                resolved["stage"]["pre_measurement"]["training_start_cache_state"],
                "fresh-clone-process-start",
            )
            self.assertIn("--training-runs", resolved["storage_bench_args"])
        self.assertNotIn("--training-feedback-compactions", a3["storage_bench_args"])
        self.assertIn("--training-feedback-compactions", a4["storage_bench_args"])

        changed = copy.deepcopy(self.doc)
        changed["stages"][3]["pre_measurement"]["adaptation_prestate"] = "different"
        with self.assertRaises(MODULE.ProfileError):
            MODULE.validate(changed)


if __name__ == "__main__":
    unittest.main()
