#!/usr/bin/env python3

import importlib.util
import json
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("aggregate_figure2", HERE / "aggregate_figure2.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def precondition(stage):
    if stage == "A3":
        return {
            "kind": "fixed-trace-adaptation-control",
            "trace": "shared-training-trace",
            "training_runs": 1,
            "training_start_cache_state": "fresh-clone-process-start",
            "adaptation_prestate": "post-identical-training-trace",
            "feedback": "disabled",
            "feedback_compactions": 0,
        }
    if stage == "A4":
        return {
            "kind": "fixed-trace-feedback-adaptation",
            "trace": "shared-training-trace",
            "training_runs": 1,
            "training_start_cache_state": "fresh-clone-process-start",
            "adaptation_prestate": "post-identical-training-trace",
            "feedback": "enabled",
            "feedback_compactions": 1,
        }
    return {"kind": "none"}


def collapsed(stage, repeat, mode):
    sha = "a" * 64
    value = {
        "experiment_id": "P20",
        "task_id": "task-{}-{}-{}".format(stage, repeat, mode),
        "run_id": "run-{}-{}-{}".format(stage, repeat, mode),
        "repeat_index": str(repeat),
        "scale": "sf10",
        "stage": stage,
        "mode": mode,
        "workload": "typed-one-hop",
        "property_predicate_mode": "none",
        "property_id": "0",
        "performance_eligible": "true" if mode == "latency" else "false",
        "concurrency": "1",
        "worker_threads": "1",
        "cpuset": "0",
        "host_fingerprint_sha256": sha,
        "git_sha": "git",
        "feature_switches_json": json.dumps(
            {
                "stage": stage,
                "query_cpu_phase_instrumentation": mode == "cpu-phase",
            },
            sort_keys=True,
        ),
        "pre_measurement_json": json.dumps(precondition(stage), sort_keys=True),
        "warmup_runs": "0",
        "training_runs": "1" if stage in {"A3", "A4"} else "0",
        "training_feedback_compactions": "1" if stage == "A4" else "0",
        "cache_state_before_json": json.dumps(
            {"run": "{}-{}-{}".format(stage, repeat, mode)}
        ),
        "cache_state_after_json": json.dumps(
            {"run": "{}-{}-{}-after".format(stage, repeat, mode)}
        ),
        "binary_sha256": sha,
        "dataset_sha256": "b" * 64,
        "sample_plan_sha256": "c" * 64,
        "truth_sha256": "d" * 64,
        "correctness_pass_sha256": "e" * 64,
        "profiles_sha256": "f" * 64,
        "p20_runner_sha256": "1" * 64,
        "p20_summarizer_sha256": "2" * 64,
        "p31_wrapper_sha256": "3" * 64,
        "pristine_store_sha256": "4" * 64,
        "pristine_store_manifest_sha256": "5" * 64,
        "current_digest_pass": "1",
        "current_digest_mismatches": "0",
        "measured_operations": 10,
        "neighbor_edges": 20,
        "candidate_segments_total": 30 + repeat,
        "routed_segments_total": 20,
        "body_read_segments_total": 10,
        "logical_read_bytes_total": 2000,
        "logical_body_bytes_total": 1000,
        "query_cpu_query_setup_ns": 100,
        "query_cpu_metadata_admission_ns": 200,
        "query_cpu_routing_index_ns": 300,
        "query_cpu_body_decode_filter_ns": 400,
        "query_cpu_mvcc_result_ns": 500,
        "query_cpu_phase_sum_ns": 1500,
        "query_cpu_clock_failures": 0,
        "query_cpu_total_ns": 1800 if mode == "cpu-phase" else "",
        "latency_p99_us": 100 + repeat,
        "latency_histogram_json": "{}",
        "summary_path": "/fixture",
        "summary_sha256": ("6" if mode == "latency" else "7") * 64,
        "admission_protocol": "legacy-p02b-admission-v1",
        "p02b_sentinel_result_sha256": "8" * 64,
        "p02b_pass_marker_sha256": "9" * 64,
        "p02b_provenance_sha256": "a" * 64,
        "p02b_validator_sha256": "b" * 64,
        "p02b_admission_sha256": "c" * 64,
        "batch_lease_sha256": "",
        "batch_gate_tool_sha256": "",
        "batch_lease_admission_sha256": "",
        "batch_lease_pre_p31_sha256": "",
        "p31_integrity_guard_status_sha256": "",
        "p31_integrity_guard_samples_sha256": "",
        "p31_integrity_guard_ready_sha256": "",
        "p31_command_release_sha256": "",
    }
    return value


class Figure2AggregatorTest(unittest.TestCase):
    def canonical_inputs(self):
        values = []
        for stage in MODULE.CANONICAL_STAGES["sf10"]:
            for repeat in (1, 2, 3):
                values.append(collapsed(stage, repeat, "latency"))
                if stage != "A6":
                    values.append(collapsed(stage, repeat, "cpu-phase"))
        return values

    def test_pairs_modes_and_aggregates_only_independent_runs(self):
        run_rows = MODULE.build_run_rows(self.canonical_inputs(), 3)
        self.assertEqual(len(run_rows), 21)
        a2 = next(
            row for row in run_rows if row["ablation_stage"] == "A2" and row["repeat_index"] == "1"
        )
        self.assertEqual(a2["cpu_total_ns"], 1800)
        self.assertEqual(a2["latency_p99_us"], 101)
        aggregate = MODULE.build_aggregate_rows(run_rows)
        self.assertEqual(len(aggregate), 7)
        a6 = next(row for row in aggregate if row["ablation_stage"] == "A6")
        self.assertEqual(a6["cpu_total_ns_per_op_mean"], "")
        self.assertEqual(a6["independent_runs"], 3)

    def test_missing_paired_mode_fails_closed(self):
        values = self.canonical_inputs()
        values = [
            value
            for value in values
            if not (
                value["stage"] == "A2"
                and value["repeat_index"] == "2"
                and value["mode"] == "cpu-phase"
            )
        ]
        with self.assertRaises(MODULE.AggregateError):
            MODULE.build_run_rows(values, 3)

    def test_paired_modes_reject_non_instrumentation_feature_drift(self):
        values = self.canonical_inputs()
        target = next(
            value
            for value in values
            if value["stage"] == "A2"
            and value["repeat_index"] == "1"
            and value["mode"] == "cpu-phase"
        )
        switches = json.loads(target["feature_switches_json"])
        switches["stage"] = "drift"
        target["feature_switches_json"] = json.dumps(switches, sort_keys=True)
        with self.assertRaises(MODULE.AggregateError):
            MODULE.build_run_rows(values, 3)

    def test_paired_modes_require_expected_instrumentation_polarity(self):
        values = self.canonical_inputs()
        target = next(
            value
            for value in values
            if value["stage"] == "A2"
            and value["repeat_index"] == "1"
            and value["mode"] == "cpu-phase"
        )
        switches = json.loads(target["feature_switches_json"])
        switches["query_cpu_phase_instrumentation"] = False
        target["feature_switches_json"] = json.dumps(switches, sort_keys=True)
        with self.assertRaises(MODULE.AggregateError):
            MODULE.build_run_rows(values, 3)

    def test_v2_pair_accepts_distinct_per_run_guard_evidence(self):
        values = self.canonical_inputs()
        for index, value in enumerate(values):
            value["admission_protocol"] = "short-clean-window-v2"
            for field in (
                "p02b_sentinel_result_sha256",
                "p02b_pass_marker_sha256",
                "p02b_provenance_sha256",
                "p02b_validator_sha256",
                "p02b_admission_sha256",
            ):
                value[field] = ""
            value["batch_lease_sha256"] = "d" * 64
            value["batch_gate_tool_sha256"] = "e" * 64
            for offset, field in enumerate(
                (
                    "batch_lease_admission_sha256",
                    "batch_lease_pre_p31_sha256",
                    "p31_integrity_guard_status_sha256",
                    "p31_integrity_guard_samples_sha256",
                    "p31_integrity_guard_ready_sha256",
                    "p31_command_release_sha256",
                )
            ):
                value[field] = "{:064x}".format(index * 10 + offset + 1)
        run_rows = MODULE.build_run_rows(values, 3)
        self.assertEqual(len(run_rows), 21)

    def test_v2_pair_rejects_different_batch_lease(self):
        values = self.canonical_inputs()
        for value in values:
            value["admission_protocol"] = "short-clean-window-v2"
            for field in (
                "p02b_sentinel_result_sha256",
                "p02b_pass_marker_sha256",
                "p02b_provenance_sha256",
                "p02b_validator_sha256",
                "p02b_admission_sha256",
            ):
                value[field] = ""
            for field in (
                "batch_lease_sha256",
                "batch_gate_tool_sha256",
                "batch_lease_admission_sha256",
                "batch_lease_pre_p31_sha256",
                "p31_integrity_guard_status_sha256",
                "p31_integrity_guard_samples_sha256",
                "p31_integrity_guard_ready_sha256",
                "p31_command_release_sha256",
            ):
                value[field] = "d" * 64
        target = next(
            value
            for value in values
            if value["stage"] == "A2"
            and value["repeat_index"] == "1"
            and value["mode"] == "cpu-phase"
        )
        target["batch_lease_sha256"] = "f" * 64
        with self.assertRaises(MODULE.AggregateError):
            MODULE.build_run_rows(values, 3)

    def test_entry_histograms_are_merged_before_run_p99(self):
        base = {field: "x" for field in MODULE.RUN_CONSTANTS}
        base.update(
            {
                "mode": "latency",
                "benchmark_entry_index": "0",
                "query_cpu_total_ns": "",
                "p02b_sentinel_result_sha256": "8" * 64,
                "p02b_pass_marker_sha256": "9" * 64,
                "p02b_provenance_sha256": "a" * 64,
                "p02b_validator_sha256": "b" * 64,
            }
        )
        for field in MODULE.ADDITIVE:
            base[field] = "0"
        base["measured_operations"] = "2"
        first = dict(base)
        first["get_neighbors_latency_histogram_json"] = json.dumps(
            {
                "count": 2,
                "sum_us": 20,
                "buckets": [
                    {"upper_bound_us": 10, "count": 2},
                    {"upper_bound_us": 100, "count": 0},
                ],
            }
        )
        second = dict(base)
        second["benchmark_entry_index"] = "1"
        second["get_neighbors_latency_histogram_json"] = json.dumps(
            {
                "count": 2,
                "sum_us": 200,
                "buckets": [
                    {"upper_bound_us": 10, "count": 0},
                    {"upper_bound_us": 100, "count": 2},
                ],
            }
        )
        merged = MODULE.collapse_summary(Path(__file__), [first, second])
        self.assertEqual(merged["measured_operations"], 4)
        self.assertEqual(merged["latency_p99_us"], 100)

    def test_paired_modes_reject_different_p02b_gate(self):
        values = self.canonical_inputs()
        cpu = next(
            value
            for value in values
            if value["stage"] == "A2"
            and value["repeat_index"] == "1"
            and value["mode"] == "cpu-phase"
        )
        cpu["p02b_sentinel_result_sha256"] = "0" * 64
        with self.assertRaises(MODULE.AggregateError):
            MODULE.build_run_rows(values, 3)

    def test_one_aggregation_batch_cannot_mix_legacy_and_v2(self):
        values = self.canonical_inputs()
        item = values[0]
        item["admission_protocol"] = "short-clean-window-v2"
        for field in (
            "p02b_sentinel_result_sha256",
            "p02b_pass_marker_sha256",
            "p02b_provenance_sha256",
            "p02b_validator_sha256",
            "p02b_admission_sha256",
        ):
            item[field] = ""
        for field in (
            "batch_lease_sha256",
            "batch_gate_tool_sha256",
            "batch_lease_admission_sha256",
            "batch_lease_pre_p31_sha256",
            "p31_integrity_guard_status_sha256",
            "p31_integrity_guard_samples_sha256",
            "p31_integrity_guard_ready_sha256",
            "p31_command_release_sha256",
        ):
            item[field] = "d" * 64
        with self.assertRaises(MODULE.AggregateError):
            MODULE.build_run_rows(values, 3)


if __name__ == "__main__":
    unittest.main()
