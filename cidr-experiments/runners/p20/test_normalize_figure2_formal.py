#!/usr/bin/env python3

import argparse
import csv
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "normalize_figure2_formal", HERE / "normalize_figure2_formal.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def sha256(path):
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_tsv(path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=columns, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def switches(stage):
    index = int(stage[1:])
    names = [
        "exact_evidence_admission",
        "semantic_routing",
        "budgeted_degree_promotion",
        "feedback_priority",
        "semantic_compaction",
        "automatic_lifecycle_control",
    ]
    return json.dumps(
        {name: feature_index < index for feature_index, name in enumerate(names)},
        sort_keys=True,
        separators=(",", ":"),
    )


class Fixture:
    def __init__(self, root, latency_candidate_total=30):
        self.root = root
        self.latency_candidate_total = latency_candidate_total
        self.dataset_sha = "b" * 64
        self.sample_sha = "c" * 64
        self.truth_sha = "d" * 64
        self.binary_sha = "a" * 64
        self.profile_sha = "e" * 64
        self.host_sha = "f" * 64
        self.git_sha = "1" * 40
        self.summaries = []
        self.manifests = []
        self.run_rows = []
        self._build_runs()
        self.run_input = root / "figure2-runs.tsv"
        write_tsv(self.run_input, MODULE.aggregate.RUN_COLUMNS, self.run_rows)
        self.metadata = root / "figure2-formal-metadata.json"
        self.write_metadata()
        self.output = root / "F2.tsv"

    def _manifest(self, run_id, index):
        path = self.root / ("manifest-{}.json".format(index))
        write_json(
            path,
            {
                "schema_version": "cidr-run-manifest-v1",
                "state": "PASS",
                "run_id": run_id,
                "performance_eligible_declared": True,
                "started_at_utc": "2026-07-23T{:02d}:00:00Z".format(index % 24),
                "host": {"fingerprint_sha256": self.host_sha},
                "repo": {"git_sha": self.git_sha},
                "inputs": {
                    "binary": {"sha256": self.binary_sha},
                    "dataset": {"sha256": self.dataset_sha},
                    "truth": {"sha256": self.truth_sha},
                    "query_or_trace": {"sha256": self.sample_sha},
                },
            },
        )
        self.manifests.append(path)
        return sha256(path)

    def _summary(self, stage, repeat, mode, manifest_sha, index):
        stage_index = int(stage[1:])
        run_id = "{}-{}-r{}".format(mode, stage, repeat)
        path = self.root / ("summary-{}-{}-r{}.tsv".format(mode, stage, repeat))
        p99 = 100 + stage_index
        histogram = json.dumps(
            {
                "count": 10,
                "sum_us": p99 * 10,
                "buckets": [{"upper_bound_us": p99, "count": 10}],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        query_cpu = mode == "cpu-phase"
        row = {field: "" for field in MODULE.SUMMARY_REQUIRED}
        row.update(
            {
                "summary_schema_version": "2",
                "run_id": run_id,
                "repeat_index": str(repeat),
                "benchmark_entry_index": "0",
                "scale": "sf10",
                "stage": stage,
                "mode": mode,
                "workload": "typed-one-hop",
                "property_predicate_mode": "none",
                "property_id": "0",
                "performance_eligible": "false" if query_cpu else "true",
                "concurrency": "1",
                "warmup_runs": "1",
                "host_fingerprint_sha256": self.host_sha,
                "git_sha": self.git_sha,
                "feature_switches_json": switches(stage),
                "current_digest_pass": "1",
                "current_digest_mismatches": "0",
                "binary_sha256": self.binary_sha,
                "dataset_sha256": self.dataset_sha,
                "sample_plan_sha256": self.sample_sha,
                "truth_sha256": self.truth_sha,
                "p31_manifest_sha256": manifest_sha,
                "elapsed_ms": "2000",
                "measured_operations": "10",
                "candidate_segments_total": str(
                    self.latency_candidate_total if not query_cpu else 30
                ),
                "body_read_segments_total": "10",
                "logical_body_bytes_total": "1000",
                "get_neighbors_latency_histogram_json": histogram,
                "query_cpu_query_setup_ns": "100" if query_cpu else "0",
                "query_cpu_metadata_admission_ns": "200" if query_cpu else "0",
                "query_cpu_routing_index_ns": "300" if query_cpu else "0",
                "query_cpu_body_decode_filter_ns": "400" if query_cpu else "0",
                "query_cpu_mvcc_result_ns": "500" if query_cpu else "0",
                "query_cpu_total_ns": "1800" if query_cpu else "",
            }
        )
        columns = sorted(MODULE.SUMMARY_REQUIRED)
        write_tsv(path, columns, [row])
        self.summaries.append(path)
        return run_id, sha256(path), p99, histogram

    def _build_runs(self):
        serial = 0
        for stage in MODULE.aggregate.CANONICAL_STAGES["sf10"]:
            for repeat in (1, 2, 3):
                serial += 1
                latency_run_id = "latency-{}-r{}".format(stage, repeat)
                manifest_sha = self._manifest(latency_run_id, serial)
                latency_id, latency_sha, p99, histogram = self._summary(
                    stage, repeat, "latency", manifest_sha, serial
                )
                cpu_id = ""
                cpu_sha = ""
                if stage != "A6":
                    cpu_id, cpu_sha, _, _ = self._summary(
                        stage, repeat, "cpu-phase", "9" * 64, serial
                    )
                row = {field: "" for field in MODULE.aggregate.RUN_COLUMNS}
                row.update(
                    {
                        "figure2_schema_version": "1",
                        "scale": "sf10",
                        "ablation_stage": stage,
                        "workload": "typed-one-hop",
                        "property_predicate_mode": "none",
                        "property_id": "0",
                        "repeat_index": str(repeat),
                        "latency_run_id": latency_id,
                        "cpu_phase_run_id": cpu_id,
                        "feature_switches": switches(stage),
                        "pre_measurement_json": "{}",
                        "latency_p99_us": str(p99),
                        "candidate_segments_total": "30",
                        "body_read_segments_total": "10",
                        "measured_operations": "10",
                        "body_read_bytes_total": "1000",
                        "device_read_bytes_total": "",
                        "cpu_query_setup_ns": "" if stage == "A6" else "100",
                        "cpu_admission_ns": "" if stage == "A6" else "200",
                        "cpu_routing_ns": "" if stage == "A6" else "300",
                        "cpu_body_decode_filter_ns": "" if stage == "A6" else "400",
                        "cpu_mvcc_result_ns": "" if stage == "A6" else "500",
                        "cpu_total_ns": "" if stage == "A6" else "1800",
                        "latency_histogram_json": histogram,
                        "binary_sha256": self.binary_sha,
                        "dataset_sha256": self.dataset_sha,
                        "sample_plan_sha256": self.sample_sha,
                        "truth_sha256": self.truth_sha,
                        "correctness_pass_sha256": "2" * 64,
                        "profiles_sha256": self.profile_sha,
                        "pristine_store_sha256": "3" * 64,
                        "pristine_store_manifest_sha256": "4" * 64,
                        "admission_protocol": "short-clean-window-v2",
                        "latency_summary_sha256": latency_sha,
                        "cpu_phase_summary_sha256": cpu_sha,
                    }
                )
                self.run_rows.append(row)

    def metadata_value(self):
        return {
            "schema_version": MODULE.METADATA_SCHEMA,
            "run_input_sha256": sha256(self.run_input),
            "selection": {
                "scale": "sf10",
                "workload": "typed-one-hop",
                "property_predicate_mode": "none",
                "property_id": "0",
            },
            "common": {
                "experiment_id": "E03",
                "system": "SemL0",
                "dataset_id": "snb-sf10-v1",
                "vertex_count": 100,
                "directed_edge_count": 1000,
                "property_count": None,
                "workload_id": "p20-typed-one-hop-v1",
                "seed": 7,
                "cache_state": "warm",
                "warmup_s": 1.5,
            },
            "property_cohort": "not_applicable",
        }

    def write_metadata(self, transform=None):
        value = self.metadata_value()
        if transform is not None:
            transform(value)
        write_json(self.metadata, value)

    def args(self):
        return argparse.Namespace(
            run_input=self.run_input,
            summary=self.summaries,
            p31_manifest=self.manifests,
            metadata=self.metadata,
            output=self.output,
        )


class Figure2FormalNormalizerTests(unittest.TestCase):
    def test_normalizes_only_proven_fields_and_explicit_frozen_metadata(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            self.assertEqual(MODULE.run(fixture.args()), 0)
            with fixture.output.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(len(rows), 21)
            a0 = rows[0]
            self.assertEqual(a0["experiment_id"], "E03")
            self.assertEqual(a0["timestamp_utc"], "2026-07-23T01:00:00Z")
            self.assertEqual(a0["measurement_s"], "2")
            self.assertEqual(a0["input_sha256"], fixture.dataset_sha)
            self.assertEqual(a0["query_trace_sha256"], fixture.sample_sha)
            self.assertEqual(a0["variant"], "A0")
            self.assertEqual(a0["digest_pass"], "true")
            self.assertEqual(a0["mismatch_count"], "0")
            self.assertEqual(rows[-1]["cpu_total_ns"], "")
            self.assertEqual(
                a0["normalization_metadata_sha256"], sha256(fixture.metadata)
            )

    def test_metadata_must_bind_exact_run_input(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            fixture.write_metadata(
                lambda value: value.update({"run_input_sha256": "0" * 64})
            )
            with self.assertRaisesRegex(MODULE.NormalizeError, "does not bind"):
                MODULE.run(fixture.args())

    def test_missing_explicit_campaign_fact_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))

            def remove_warmup(value):
                del value["common"]["warmup_s"]

            fixture.write_metadata(remove_warmup)
            with self.assertRaisesRegex(MODULE.NormalizeError, "common fields drift"):
                MODULE.run(fixture.args())

    def test_balanced_property_cohort_is_canonical(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))

            def property_metadata(value):
                value["selection"].update(
                    {
                        "workload": "property-presence",
                        "property_predicate_mode": "presence",
                        "property_id": "5",
                    }
                )
                value["common"]["property_count"] = 100
                value["property_cohort"] = "balanced_mixed_zero"

            fixture.write_metadata(property_metadata)
            loaded = MODULE.load_metadata(
                fixture.metadata, sha256(fixture.run_input)
            )
            self.assertEqual(loaded["property_cohort"], "balanced_mixed_zero")

    def test_summary_metric_cannot_be_replaced_by_metadata_or_run_tsv(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw), latency_candidate_total=31)
            with self.assertRaisesRegex(
                MODULE.NormalizeError, "latency candidate_segments_total drift"
            ):
                MODULE.run(fixture.args())

    def test_p31_start_timestamp_is_required(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            first = fixture.manifests[0]
            value = json.loads(first.read_text(encoding="utf-8"))
            del value["started_at_utc"]
            write_json(first, value)
            # Rebind the first summary and aggregate row to the modified manifest/summary.
            new_manifest_sha = sha256(first)
            summary = fixture.summaries[0]
            with summary.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
                columns = list(rows[0])
            rows[0]["p31_manifest_sha256"] = new_manifest_sha
            write_tsv(summary, columns, rows)
            fixture.run_rows[0]["latency_summary_sha256"] = sha256(summary)
            write_tsv(fixture.run_input, MODULE.aggregate.RUN_COLUMNS, fixture.run_rows)
            fixture.write_metadata()
            with self.assertRaisesRegex(MODULE.NormalizeError, "started_at_utc"):
                MODULE.run(fixture.args())


if __name__ == "__main__":
    unittest.main()
