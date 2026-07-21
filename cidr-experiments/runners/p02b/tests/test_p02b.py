#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import json
import statistics
import tempfile
import unittest
from pathlib import Path

from build_lineage_manifest import build_tree_manifest
from calculate_cv import calculate_cv
from p02b_common import GateError, sha256_file
from validate_clean_ready import validate_clean_ready
from validate_sentinel_result import validate_result


class CvTests(unittest.TestCase):
    def make_metric(self, root: Path, index: int, qps: float, p99: float) -> Path:
        path = root / "r{}-metrics.json".format(index)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "p02b-sentinel-run-metrics-v1",
                    "state": "PASS",
                    "run_index": index,
                    "run_id": "fixture-r{}".format(index),
                    "query_count": 60,
                    "qps": qps,
                    "p99_us": p99,
                    "measured_seconds": 1.0,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_cv_passes_with_stable_independent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0, 1000.0),
                self.make_metric(root, 2, 101.0, 1005.0),
                self.make_metric(root, 3, 99.5, 995.0),
            ]
            result = calculate_cv(paths, 3, 0.03, 0.05)
            self.assertEqual(result["state"], "PASS")
            self.assertTrue(result["qps"]["pass"])
            self.assertTrue(result["p99_us"]["pass"])

    def test_cv_holds_when_qps_is_unstable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0, 1000.0),
                self.make_metric(root, 2, 120.0, 1000.0),
                self.make_metric(root, 3, 80.0, 1000.0),
            ]
            result = calculate_cv(paths, 3, 0.03, 0.05)
            self.assertEqual(result["state"], "HOLD")
            self.assertFalse(result["qps"]["pass"])

    def test_cv_rejects_duplicate_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0, 1000.0),
                self.make_metric(root, 2, 100.0, 1000.0),
                self.make_metric(root, 3, 100.0, 1000.0),
            ]
            value = json.loads(paths[2].read_text(encoding="utf-8"))
            value["run_id"] = "fixture-r2"
            paths[2].write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(GateError):
                calculate_cv(paths, 3, 0.03, 0.05)


class LineageManifestTests(unittest.TestCase):
    def test_tree_digest_is_deterministic_and_path_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "store"
            root.mkdir()
            (root / "a").write_text("one\n", encoding="utf-8")
            (root / "b").write_text("two\n", encoding="utf-8")
            first = build_tree_manifest(root, "store")
            second = build_tree_manifest(root, "store")
            self.assertEqual(first["store_sha256"], second["store_sha256"])
            (root / "b").write_text("changed\n", encoding="utf-8")
            third = build_tree_manifest(root, "store")
            self.assertNotEqual(first["store_sha256"], third["store_sha256"])


class CleanReadyTests(unittest.TestCase):
    def make_ready(self, root: Path, stopped: bool = False) -> Path:
        p03 = root / "P03-CLEAN-WINDOW-MONITOR"
        run_dir = p03 / "raw" / "fixture-clean"
        run_dir.mkdir(parents=True)
        monitor = p03 / "monitor_clean_window.sh"
        monitor.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        classification = {
            "performance_eligible": "false",
            "purpose": "clean_window_readiness_only",
            "gate_mode": "seml0",
            "run_id": "fixture-clean",
            "script_sha256": sha256_file(monitor),
            "git_head": "0" * 40,
            "host": "fixture",
            "ready_samples": "3",
        }
        (run_dir / "classification.env").write_text(
            "".join("{}={}\n".format(key, value) for key, value in classification.items()),
            encoding="utf-8",
        )
        header = (
            "timestamp\tsample\tmetric_pass\tservice_pass\tsample_pass\tstreak\t"
            "reasons\tgate_mode\n"
        )
        rows = "".join(
            "{}\t{}\t1\t1\t1\t{}\tnone\tseml0\n".format(now, index, index)
            for index in range(1, 4)
        )
        (run_dir / "samples.tsv").write_text(header + rows, encoding="utf-8")
        (run_dir / "latest.tsv").write_text(header + rows.splitlines(True)[-1], encoding="utf-8")
        (run_dir / "STATE").write_text(
            "performance_eligible=false\nsample_pass=1\nstreak=3\nrequired_streak=3\nreasons=none\n",
            encoding="utf-8",
        )
        (run_dir / "COMPLETE").write_text("run_id=fixture-clean\n", encoding="utf-8")
        (run_dir / "READY").write_text(
            "performance_eligible=false\n"
            "readiness_gate=PASS\n"
            "gate_mode=seml0\n"
            "run_id=fixture-clean\n"
            "ready_time={}\n"
            "samples=3\n"
            "consecutive_passes=3\n"
            "latest_sample={}\n".format(now, (run_dir / "latest.tsv").resolve()),
            encoding="utf-8",
        )
        if stopped:
            (run_dir / "STOPPED").write_text("state=stopped\n", encoding="utf-8")
        return run_dir / "READY"

    def test_ready_binding_passes_and_hashes_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = self.make_ready(Path(temporary))
            result = validate_clean_ready(ready, 300, 3)
            self.assertEqual(result["state"], "PASS")
            self.assertEqual(result["observed_consecutive_samples"], 3)
            self.assertIn("monitor_clean_window.sh", result["artifacts"])

    def test_ready_binding_rejects_stopped_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = self.make_ready(Path(temporary), stopped=True)
            with self.assertRaises(GateError):
                validate_clean_ready(ready, 300, 3)

    def test_ready_binding_rejects_tampered_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = self.make_ready(Path(temporary))
            samples = ready.parent / "samples.tsv"
            text = samples.read_text(encoding="utf-8").replace(
                "\t3\t1\t1\t1\t3\t", "\t3\t1\t1\t0\t0\t"
            )
            samples.write_text(text, encoding="utf-8")
            with self.assertRaises(GateError):
                validate_clean_ready(ready, 300, 3)


class SentinelResultTests(unittest.TestCase):
    """Exercise the consumer contract against a complete on-disk P02B fixture."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.repo_head = "1" * 40
        self.binary = self.root / "fixture-binary"
        self.binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.binary_sha = sha256_file(self.binary)
        self.host = {"hostname": "fixture-host", "fingerprint_sha256": "a" * 64}
        self._build_fixture()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def file_ref(path: Path, **extra: object) -> dict:
        value = {
            "path": str(path.resolve()),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        value.update(extra)
        return value

    def _build_fixture(self) -> None:
        dataset = self.root / "dataset"
        store = self.root / "store"
        dataset.mkdir()
        store.mkdir()
        dataset_file = dataset / "data.tsv"
        store_file = store / "store.bin"
        dataset_file.write_text("fixture dataset\n", encoding="utf-8")
        store_file.write_text("fixture store\n", encoding="utf-8")
        dataset_sha = sha256_file(dataset_file)
        store_sha = sha256_file(store_file)
        dataset_manifest = self.root / "dataset-manifest.json"
        store_manifest = self.root / "store-manifest.json"
        self.write_json(
            dataset_manifest,
            {
                "schema_version": "p02b-dataset-manifest-v1",
                "dataset_root": str(dataset),
                "dataset_sha256": dataset_sha,
                "hash_method": "fixture-single-file-sha256",
            },
        )
        self.write_json(
            store_manifest,
            {
                "schema_version": "p02b-store-manifest-v1",
                "store_path": str(store),
                "store_sha256": store_sha,
                "hash_method": "fixture-single-file-sha256",
            },
        )

        truth = self.root / "truth.tsv"
        query_plan = self.root / "query-plan.json"
        config = self.root / "config.json"
        p31_wrapper = self.root / "p31-wrapper.sh"
        truth.write_text("fixture truth\n", encoding="utf-8")
        query_plan.write_text('{"plan":1}\n', encoding="utf-8")
        config.write_text('{"fixture":true}\n', encoding="utf-8")
        p31_wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

        id_map = self.root / "id-map"
        id_map.mkdir()
        dense = id_map / "dense-to-original.tsv"
        original = id_map / "original-to-dense.tsv"
        dense.write_text("dense_id\toriginal_id\n0\t100\n", encoding="utf-8")
        original.write_text("original_id\tdense_id\n100\t0\n", encoding="utf-8")
        id_manifest = id_map / "id-map-manifest.json"
        id_manifest_value = {
            "format": "seml0-shared-id-map",
            "format_version": 1,
            "status": "PASS",
            "formal_pass": True,
            "verification_complete": True,
            "mapping_hash": "0123456789abcdef",
            "mapping_hash_algorithm": "fnv1a64-le-dense-original-v1",
            "dense_to_original": {"sha256": sha256_file(dense)},
            "original_to_dense": {"sha256": sha256_file(original)},
        }
        self.write_json(id_manifest, id_manifest_value)
        formal_pass = id_map / "FORMAL-PASS"
        formal_pass.write_text(
            "id-map-manifest.json sha256 {}\n".format(sha256_file(id_manifest)),
            encoding="utf-8",
        )
        checksums = id_map / "SHA256SUMS"
        checksums.write_text("fixture checksums\n", encoding="utf-8")

        clean_artifact = self.root / "READY"
        clean_artifact.write_text("readiness_gate=PASS\n", encoding="utf-8")
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        clean_ready = {
            "schema_version": "p02b-clean-ready-binding-v1",
            "state": "PASS",
            "run_id": "fixture-clean",
            "ready_time": now,
            "age_seconds_at_binding": 0.0,
            "required_consecutive_samples": 3,
            "observed_consecutive_samples": 3,
            "git_head": self.repo_head,
            "host": self.host["hostname"],
            "artifacts": {"READY": self.file_ref(clean_artifact)},
        }
        clean_binding = self.run_dir / "clean-ready-binding.json"
        self.write_json(clean_binding, clean_ready)

        generated_plan = self.run_dir / "regenerated-query-plan.json"
        shared_truth_result = self.run_dir / "shared-truth-result.json"
        generated_plan.write_bytes(query_plan.read_bytes())
        self.write_json(shared_truth_result, {"state": "PASS"})

        protocol = {
            "independent_process_runs": 3,
            "expected_queries": 6,
            "warmup_runs": 1,
            "measured_repeats": 1,
            "minimum_measured_seconds_per_run": 0.1,
            "cache_policy": "no-drop-caches;independent-process;in-process-warmup;os-cache-as-is",
            "cpu": {
                "housekeeping_cpuset": "0",
                "formal_cpuset": "1",
                "threads": 1,
            },
            "io_backend": "blocking",
            "l0_layout": "schema",
            "semantic_degree_hint": True,
            "force_signature": False,
            "p31": {
                "device": "fixture-device",
                "data_mount": str(self.root),
                "interval_seconds": 0.2,
                "disk_interval_seconds": 0.5,
                "min_samples": 2,
                "require_aux_tools": True,
            },
        }
        qps_values = [100.0, 101.0, 99.0]
        p99_values = [1000.0, 1005.0, 995.0]
        stability_runs = []
        repeats = []
        for index, (qps, p99) in enumerate(zip(qps_values, p99_values), start=1):
            run_id = "fixture-r{}".format(index)
            repeat_root = self.run_dir / "repeats" / "r{}".format(index)
            p31_root = repeat_root / "p31"
            p31_root.mkdir(parents=True)
            metric_path = repeat_root / "run-metrics.json"
            metric_value = {
                "schema_version": "p02b-sentinel-run-metrics-v1",
                "state": "PASS",
                "run_index": index,
                "run_id": run_id,
                "query_count": 6,
                "qps": qps,
                "p99_us": p99,
                "measured_seconds": 1.0,
            }
            self.write_json(metric_path, metric_value)
            stability_runs.append(
                {
                    "path": str(metric_path),
                    "sha256": sha256_file(metric_path),
                    "run_index": index,
                    "run_id": run_id,
                    "query_count": 6,
                    "qps": qps,
                    "p99_us": p99,
                    "measured_seconds": 1.0,
                }
            )
            manifest_path = p31_root / "run-manifest.json"
            validation_path = p31_root / "validation.json"
            done_path = p31_root / "DONE"
            self.write_json(
                manifest_path,
                {
                    "schema_version": "cidr-run-manifest-v1",
                    "resource_schema_version": "cidr-resource-v1",
                    "state": "PASS",
                    "run_id": run_id,
                    "task_id": "P02B-SF10-SENTINEL-r{}".format(index),
                    "performance_eligible_declared": False,
                    "repo": {"git_sha": self.repo_head, "dirty": False},
                    "host": self.host,
                },
            )
            self.write_json(validation_path, {"state": "PASS"})
            self.write_json(
                done_path,
                {
                    "state": "PASS",
                    "manifest_sha256": sha256_file(manifest_path),
                    "validation_sha256": sha256_file(validation_path),
                },
            )
            repeats.append(
                {
                    "run_index": index,
                    "run_id": run_id,
                    "metrics": self.file_ref(metric_path),
                    "p31": {
                        "run_dir": str(p31_root),
                        "manifest": self.file_ref(manifest_path),
                        "validation": self.file_ref(validation_path),
                        "done": self.file_ref(done_path),
                    },
                }
            )

        def cv_metric(values: list, maximum: float) -> dict:
            mean = statistics.mean(values)
            stdev = statistics.stdev(values)
            cv = stdev / mean
            return {
                "values": values,
                "mean": mean,
                "sample_stdev": stdev,
                "cv": cv,
                "maximum_cv": maximum,
                "pass": True,
            }

        stability = {
            "schema_version": "p02b-sentinel-cv-v1",
            "state": "PASS",
            "method": "sample_standard_deviation_over_arithmetic_mean",
            "independent_process_runs": 3,
            "query_count_per_run": 6,
            "qps": cv_metric(qps_values, 0.03),
            "p99_us": cv_metric(p99_values, 0.05),
            "runs": stability_runs,
        }
        files = {
            "binary": self.file_ref(self.binary),
            "truth": self.file_ref(truth, queries=6),
            "query_plan": self.file_ref(query_plan),
            "config": self.file_ref(config),
            "dataset_manifest": self.file_ref(dataset_manifest),
            "store_manifest": self.file_ref(store_manifest),
            "id_map_manifest": self.file_ref(id_manifest),
            "p31_wrapper": self.file_ref(p31_wrapper),
        }
        provenance_value = {
            "schema_version": "p02b-sentinel-provenance-v1",
            "created_at_utc": now,
            "repo": {
                "root": str(self.repo),
                "head": self.repo_head,
                "dirty": False,
                "status_lines": [],
            },
            "fixture_mode": True,
            "files": files,
            "dataset": {
                "manifest": files["dataset_manifest"],
                "root": str(dataset),
                "content_sha256": dataset_sha,
                "hash_method": "fixture-single-file-sha256",
            },
            "store": {
                "manifest": files["store_manifest"],
                "root": str(store),
                "content_sha256": store_sha,
                "hash_method": "fixture-single-file-sha256",
            },
            "id_map": {
                "path": str(id_map),
                "manifest": self.file_ref(id_manifest),
                "formal_pass": self.file_ref(formal_pass),
                "checksums": self.file_ref(checksums),
                "mapping_hash": "0123456789abcdef",
                "mapping_hash_algorithm": "fnv1a64-le-dense-original-v1",
                "dense_to_original_sha256": sha256_file(dense),
                "original_to_dense_sha256": sha256_file(original),
            },
            "query_plan_summary": {
                "version": 1,
                "source": "shared-truth-tsv",
                "entries": 1,
                "queries": 6,
                "semantic_degree_hint": True,
                "force_signature": False,
                "sha256": sha256_file(query_plan),
            },
            "clean_ready_binding_sha256": sha256_file(clean_binding),
        }
        self.provenance_path = self.run_dir / "provenance.json"
        self.write_json(self.provenance_path, provenance_value)
        self.result_path = self.run_dir / "sentinel-result.json"
        result_value = {
            "schema_version": "p02b-sf10-sentinel-result-v1",
            "state": "PASS",
            "fixture_only": True,
            "performance_eligible": False,
            "formal_gate_eligible": False,
            "downstream_release_eligible": False,
            "consumers": ["P10", "P20"],
            "task_id": "P02B-SF10-SENTINEL",
            "scale": "sf10",
            "run_id": "fixture-sentinel",
            "started_at_utc": now,
            "completed_at_utc": now,
            "clean_ready": clean_ready,
            "protocol": protocol,
            "provenance": {
                "path": str(self.provenance_path),
                "sha256": sha256_file(self.provenance_path),
                "repo_head": self.repo_head,
                "binary_sha256": self.binary_sha,
                "truth_sha256": sha256_file(truth),
                "query_plan_sha256": sha256_file(query_plan),
                "store_sha256": store_sha,
                "dataset_sha256": dataset_sha,
                "config_sha256": sha256_file(config),
            },
            "correctness": {
                "state": "PASS",
                "checked": 6,
                "mismatches": 0,
                "total_neighbors": 6,
                "mapping_hash": "0123456789abcdef",
                "result": self.file_ref(shared_truth_result),
                "regenerated_query_plan": self.file_ref(generated_plan),
            },
            "stability": stability,
            "repeats": repeats,
        }
        self.write_json(self.result_path, result_value)
        self.marker_path = self.run_dir / "FIXTURE-PASS"
        self._resign_marker()

    def _resign_marker(self) -> None:
        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.write_json(
            self.marker_path,
            {
                "state": "PASS",
                "fixture_only": result["fixture_only"],
                "result": str(self.result_path),
                "result_sha256": sha256_file(self.result_path),
            },
        )

    def _rewrite_result(self, value: dict) -> None:
        self.write_json(self.result_path, value)
        self._resign_marker()

    def test_complete_fixture_releases_p20_nonformal_consumer(self) -> None:
        receipt = validate_result(
            self.result_path,
            "P20",
            False,
            expected_repo_root=self.repo,
            expected_repo_head=self.repo_head,
            expected_binary_sha256=self.binary_sha,
            max_age_seconds=3600,
        )
        self.assertEqual(receipt["state"], "PASS")
        self.assertEqual(receipt["scope"], "host-global")
        self.assertEqual(receipt["host"], self.host)
        self.assertEqual(receipt["pass_marker_sha256"], sha256_file(self.marker_path))
        self.assertEqual(receipt["provenance_sha256"], sha256_file(self.provenance_path))

    def test_fixture_cannot_release_formal_p20(self) -> None:
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", True)

    def test_consumer_tamper_is_rejected_even_after_marker_resign(self) -> None:
        value = json.loads(self.result_path.read_text(encoding="utf-8"))
        value["consumers"] = ["P20"]
        self._rewrite_result(value)
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

    def test_marker_tamper_and_opposite_marker_are_rejected(self) -> None:
        marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        marker["result_sha256"] = "0" * 64
        self.write_json(self.marker_path, marker)
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)
        self._resign_marker()
        self.write_json(self.run_dir / "PASS", marker)
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

    def test_provenance_tamper_is_rejected_even_after_chain_resign(self) -> None:
        provenance = json.loads(self.provenance_path.read_text(encoding="utf-8"))
        provenance["files"]["binary"]["size_bytes"] += 1
        self.write_json(self.provenance_path, provenance)
        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        result["provenance"]["sha256"] = sha256_file(self.provenance_path)
        self._rewrite_result(result)
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

    def test_duplicate_json_key_is_rejected_even_after_marker_resign(self) -> None:
        text = self.result_path.read_text(encoding="utf-8")
        needle = '  "schema_version": "p02b-sf10-sentinel-result-v1",\n'
        self.result_path.write_text(text.replace(needle, needle + needle, 1), encoding="utf-8")
        self._resign_marker()
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

    def test_loose_boolean_integer_type_is_rejected(self) -> None:
        value = json.loads(self.result_path.read_text(encoding="utf-8"))
        value["correctness"]["mismatches"] = False
        self._rewrite_result(value)
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

if __name__ == "__main__":
    unittest.main()
