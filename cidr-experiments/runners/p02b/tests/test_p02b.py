#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from build_lineage_manifest import build_tree_manifest
from calculate_stability import OVERFLOW_BOUND_US, calculate_stability
from extract_run_metrics import extract_run_metrics
from p02b_common import GateError, sha256_file
from run_sf10_sentinel import validate_config, validate_lineage_manifest
from validate_clean_ready import validate_clean_ready
from validate_sentinel_result import validate_result
import run_sf10_sentinel as sentinel_runner


class StabilityTests(unittest.TestCase):
    BOUNDS = [100, 150000, 250000, 500000, OVERFLOW_BOUND_US]

    def make_metric(
        self,
        root: Path,
        index: int,
        qps: float,
        tail_gt_150: int = 170,
        tail_gt_250: int = 160,
        overflow: int = 0,
        mean_us: int = 1000,
    ) -> Path:
        count = 17000
        self.assertGreaterEqual(tail_gt_150, tail_gt_250)
        self.assertGreaterEqual(tail_gt_250, overflow)
        counts = [
            count - tail_gt_150 - 1,
            1,
            tail_gt_150 - tail_gt_250,
            tail_gt_250 - overflow,
            overflow,
        ]
        rank = 16830
        cumulative = 0
        p99_upper = None
        for bound, bucket_count in zip(self.BOUNDS, counts):
            cumulative += bucket_count
            if cumulative >= rank:
                p99_upper = float(bound)
                break
        self.assertIsNotNone(p99_upper)
        boundary_sha = hashlib.sha256(
            json.dumps(self.BOUNDS, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        path = root / "r{}-metrics.json".format(index)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "p02b-sentinel-run-metrics-v2",
                    "state": "PASS",
                    "run_index": index,
                    "run_id": "fixture-r{}".format(index),
                    "query_count": count,
                    "qps": qps,
                    "p99_upper_bound_us": p99_upper,
                    "measured_seconds": count / qps,
                    "histogram": {
                        "bounds_us": self.BOUNDS,
                        "counts": counts,
                        "boundary_sha256": boundary_sha,
                        "count": count,
                        "sum_us": count * mean_us,
                        "mean_us": float(mean_us),
                        "max_us": 400000,
                        "overflow_count": overflow,
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def calculate(paths: list[Path], qps_cv: float = 0.07, mean_cv: float = 0.07) -> dict:
        return calculate_stability(
            paths, 3, qps_cv, mean_cv, 99, 100, 150000, 250000, 3, True
        )

    def test_quantized_p99_jump_is_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0, 170),
                self.make_metric(root, 2, 101.0, 170),
                self.make_metric(root, 3, 99.5, 196),
            ]
            result = self.calculate(paths)
            self.assertEqual(result["state"], "PASS")
            self.assertTrue(result["qps"]["pass"])
            self.assertGreater(result["p99_upper_bound_us"]["cv"], 0.30)
            self.assertEqual(
                result["p99_upper_bound_us"]["admission_role"], "diagnostic_only"
            )
            self.assertEqual(
                result["derived_budgets"]["within_run_jitter_budget_count"], 39
            )
            self.assertEqual(
                result["derived_budgets"]["cross_run_range_budget_count"], 56
            )

    def test_stability_holds_when_qps_is_unstable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0),
                self.make_metric(root, 2, 120.0),
                self.make_metric(root, 3, 80.0),
            ]
            result = self.calculate(paths, qps_cv=0.03)
            self.assertEqual(result["state"], "HOLD")
            self.assertFalse(result["qps"]["pass"])

    def test_stability_holds_when_mean_storage_latency_is_unstable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0, mean_us=1000),
                self.make_metric(root, 2, 100.0, mean_us=1200),
                self.make_metric(root, 3, 100.0, mean_us=800),
            ]
            result = self.calculate(paths, mean_cv=0.03)
            self.assertEqual(result["state"], "HOLD")
            self.assertFalse(result["mean_storage_latency_us"]["pass"])

    def test_tail_hard_limits_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0, 170, 160),
                self.make_metric(root, 2, 100.0, 170, 160),
                self.make_metric(root, 3, 100.0, 171, 171),
            ]
            result = self.calculate(paths)
            self.assertEqual(result["state"], "HOLD")
            self.assertFalse(result["tail"]["upper_boundary"]["pass"])

            paths[2] = self.make_metric(root, 3, 100.0, 210, 160)
            result = self.calculate(paths)
            self.assertEqual(result["state"], "HOLD")
            self.assertFalse(result["tail"]["lower_boundary"]["pass"])

    def test_cross_run_range_budget_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0, 152, 100),
                self.make_metric(root, 2, 100.0, 152, 100),
                self.make_metric(root, 3, 100.0, 209, 100),
            ]
            result = self.calculate(paths)
            self.assertEqual(result["tail"]["lower_boundary"]["range_count"], 57)
            self.assertFalse(result["tail"]["lower_boundary"]["pass"])

            paths = [
                self.make_metric(root, 1, 100.0, 209, 100),
                self.make_metric(root, 2, 100.0, 209, 100),
                self.make_metric(root, 3, 100.0, 209, 157),
            ]
            result = self.calculate(paths)
            self.assertTrue(result["tail"]["lower_boundary"]["pass"])
            self.assertEqual(result["tail"]["upper_boundary"]["range_count"], 57)
            self.assertFalse(result["tail"]["upper_boundary"]["pass"])

    def test_overflow_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0),
                self.make_metric(root, 2, 100.0),
                self.make_metric(root, 3, 100.0, overflow=1),
            ]
            self.assertEqual(self.calculate(paths)["state"], "HOLD")

    def test_stability_rejects_duplicate_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0),
                self.make_metric(root, 2, 100.0),
                self.make_metric(root, 3, 100.0),
            ]
            value = json.loads(paths[2].read_text(encoding="utf-8"))
            value["run_id"] = "fixture-r2"
            paths[2].write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(GateError):
                self.calculate(paths)

    def test_stability_rejects_malformed_histogram_and_schema(self) -> None:
        mutations = (
            ("missing histogram", lambda value: value.pop("histogram")),
            (
                "count mismatch",
                lambda value: value["histogram"].__setitem__("count", 16999),
            ),
            (
                "boundary SHA mismatch",
                lambda value: value["histogram"].__setitem__(
                    "boundary_sha256", "0" * 64
                ),
            ),
            (
                "schema mismatch",
                lambda value: value.__setitem__(
                    "schema_version", "p02b-sentinel-run-metrics-v1"
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = [
                    self.make_metric(root, 1, 100.0),
                    self.make_metric(root, 2, 100.0),
                    self.make_metric(root, 3, 100.0),
                ]
                value = json.loads(paths[1].read_text(encoding="utf-8"))
                mutate(value)
                paths[1].write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(GateError):
                    self.calculate(paths)


class LineageManifestTests(unittest.TestCase):
    def test_tree_digest_is_deterministic_and_path_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "store"
            root.mkdir()
            (root / "a").write_text("one\n", encoding="utf-8")
            (root / "b").write_text("two\n", encoding="utf-8")
            first = build_tree_manifest(root, "store")
            second = build_tree_manifest(root, "store")
            self.assertEqual(first["store_path"], str(root.resolve()))
            self.assertNotIn("store_root", first)
            self.assertEqual(first["store_sha256"], second["store_sha256"])
            manifest_path = Path(temporary) / "store-manifest.json"
            manifest_path.write_text(json.dumps(first), encoding="utf-8")
            validated = validate_lineage_manifest(
                manifest_path,
                "p02b-store-manifest-v1",
                "store_path",
                "store_sha256",
                root,
            )
            self.assertEqual(validated["root"], str(root.resolve()))
            (root / "b").write_text("changed\n", encoding="utf-8")
            third = build_tree_manifest(root, "store")
            self.assertNotEqual(first["store_sha256"], third["store_sha256"])

            dataset = build_tree_manifest(root, "dataset")
            self.assertEqual(dataset["dataset_root"], str(root.resolve()))


class ConfigTests(unittest.TestCase):
    def test_quantization_aware_v4_config_is_accepted_and_legacy_is_rejected(self) -> None:
        config_dir = Path(__file__).resolve().parents[1] / "configs"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = config_dir / "sf10-seml0-short-gate-v4-quantization-aware.json"
            value = json.loads(source.read_text(encoding="utf-8"))
            value["p31"]["data_mount"] = str(root)
            path = root / "v4.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            validated = validate_config(path)
            self.assertEqual(
                validated["clean_ready"]["protocol_version"], "short-clean-window-v2"
            )
            self.assertEqual(validated["thresholds"]["qps_cv_max"], 0.07)
            self.assertEqual(validated["thresholds"]["mean_storage_latency_cv_max"], 0.07)
            self.assertEqual(validated["thresholds"]["quantile_numerator"], 99)
            self.assertEqual(validated["thresholds"]["lower_tail_bound_us"], 150000)
            self.assertEqual(validated["thresholds"]["upper_tail_bound_us"], 250000)

            legacy = json.loads(
                (config_dir / "sf10-seml0-short-gate-v3-relaxed-qps7.json").read_text(
                    encoding="utf-8"
                )
            )
            legacy["p31"]["data_mount"] = str(root)
            legacy_path = root / "legacy-v3.json"
            legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaises(GateError):
                validate_config(legacy_path)

    def test_short_v2_config_rejects_timing_drift(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "configs"
            / "sf10-seml0-short-gate-v4-quantization-aware.json"
        )
        value = json.loads(source.read_text(encoding="utf-8"))
        value["clean_ready"]["sample_interval_seconds"] = 30
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(GateError):
                validate_config(path)


class LeaseIssuanceStateMachineTests(unittest.TestCase):
    def test_lease_failure_preserves_p02b_pass_but_returns_three(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary).resolve()
            pass_path = run_dir / "PASS"
            pass_path.write_text('{"state":"PASS"}\n', encoding="utf-8")
            result = {
                "state": "PASS",
                "fixture_only": False,
                "clean_ready": {"protocol_version": "short-clean-window-v2"},
            }
            argv = [
                "run_sf10_sentinel.py",
                "--run-dir",
                str(run_dir),
                "--clean-ready",
                str(run_dir / "READY"),
                "--repo-root",
                str(run_dir),
                "--binary",
                str(run_dir / "binary"),
                "--dataset-manifest",
                str(run_dir / "dataset.json"),
                "--store",
                str(run_dir),
                "--store-manifest",
                str(run_dir / "store.json"),
                "--truth",
                str(run_dir / "truth.tsv"),
                "--query-plan",
                str(run_dir / "plan.json"),
                "--id-map-dir",
                str(run_dir),
                "--config",
                str(run_dir / "config.json"),
                "--batch-lease-output",
                str(run_dir / "lease.json"),
            ]
            with mock.patch.object(sentinel_runner, "run", return_value=result), mock.patch.object(
                sentinel_runner,
                "issue_batch_lease",
                side_effect=GateError("fixture lease failure"),
            ), mock.patch.object(sentinel_runner.sys, "argv", argv):
                return_code = sentinel_runner.main()
            self.assertEqual(return_code, 3)
            self.assertTrue(pass_path.is_file())
            self.assertFalse((run_dir / "FAILED").exists())
            failure = json.loads(
                (run_dir / "BATCH-LEASE-FAILED.json").read_text(encoding="utf-8")
            )
            self.assertEqual(failure["state"], "FAILED")
            self.assertIs(failure["p02b_pass_preserved"], True)


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
        self.write_json(
            query_plan,
            {
                "version": 1,
                "source": "shared-truth-tsv",
                "semantic_degree_hint": True,
                "force_signature": False,
                "entries": [
                    {
                        "edge_type": "knows",
                        "samples": [
                            {"src": source, "degree": 1} for source in range(6)
                        ],
                    }
                ],
            },
        )
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
        elapsed_values = [1000, 995, 1005]
        histogram_bounds = [100, 150000, 250000, 500000, OVERFLOW_BOUND_US]
        histogram_counts = [6, 0, 0, 0, 0]
        metric_paths = []
        repeats = []
        sentinel_run_id = self.run_dir.name
        for index, elapsed_ms in enumerate(elapsed_values, start=1):
            run_id = "{}-r{}".format(sentinel_run_id, index)
            repeat_root = self.run_dir / "repeats" / "r{}".format(index)
            p31_root = repeat_root / "p31"
            p31_root.mkdir(parents=True)
            command_stdout_path = p31_root / "command.stdout.log"
            self.write_json(
                command_stdout_path,
                {
                    "sample_plan_in": str(query_plan.resolve()),
                    "sample_plan_version": 1,
                    "warmup_runs": 1,
                    "repeats": 1,
                    "cache_state_before": {"fixture": "as-is"},
                    "cache_state_after": {"fixture": "warm"},
                    "benchmarks": [
                        {
                            "edge_type": "knows",
                            "warmup_runs": 1,
                            "repeats": 1,
                            "warmup_rounds": [{"kind": "warmup", "round": 1}],
                            "rounds": [
                                {
                                    "kind": "measured",
                                    "round": 1,
                                    "get_neighbors_elapsed_ms": elapsed_ms,
                                    "neighbor_metrics": {
                                        "storage": {
                                            "get_neighbors_ops": 6,
                                            "get_neighbors_latency": {
                                                "buckets": [
                                                    {
                                                        "upper_bound_us": bound,
                                                        "count": count,
                                                    }
                                                    for bound, count in zip(
                                                        histogram_bounds,
                                                        histogram_counts,
                                                    )
                                                ],
                                                "count": 6,
                                                "sum_us": 450,
                                                "max_us": 90,
                                            },
                                        }
                                    },
                                }
                            ],
                        }
                    ],
                },
            )
            metric_path = repeat_root / "run-metrics.json"
            metric_value = extract_run_metrics(
                command_stdout_path,
                query_plan,
                index,
                run_id,
                6,
                1,
                1,
                0.1,
            )
            self.write_json(metric_path, metric_value)
            metric_paths.append(metric_path)
            manifest_path = p31_root / "run-manifest.json"
            validation_path = p31_root / "validation.json"
            done_path = p31_root / "DONE"
            validation_value = {
                "schema_version": "cidr-run-manifest-v1",
                "state": "PASS",
                "errors": [],
                "warnings": [],
            }
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
                    "collector": {"require_aux_tools": True},
                    "disk_roots": [{"role": "store", "path": str(store.resolve())}],
                    "inputs": {},
                    "validation": validation_value,
                },
            )
            self.write_json(validation_path, validation_value)
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
                        "command_stdout": self.file_ref(command_stdout_path),
                    },
                }
            )

        stability = calculate_stability(
            metric_paths,
            3,
            0.07,
            0.07,
            99,
            100,
            150000,
            250000,
            3,
            True,
        )
        stability_path = self.run_dir / "stability-result.json"
        self.write_json(stability_path, stability)
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
        gate_contract = sentinel_runner.build_gate_contract(
            stability["policy"], stability_path
        )
        result_value = {
            "schema_version": "p02b-sf10-sentinel-result-v2",
            "state": "PASS",
            "fixture_only": True,
            "performance_eligible": False,
            "formal_gate_eligible": False,
            "downstream_release_eligible": False,
            "consumers": ["P10", "P20"],
            "task_id": "P02B-SF10-SENTINEL",
            "scale": "sf10",
            "run_id": sentinel_run_id,
            "started_at_utc": now,
            "completed_at_utc": now,
            "clean_ready": clean_ready,
            "gate_contract": gate_contract,
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

    def _rebuild_metric_chain(self, value: dict) -> None:
        metric_paths = [
            Path(repeat["metrics"]["path"]).resolve() for repeat in value["repeats"]
        ]
        stability = calculate_stability(
            metric_paths, 3, 0.07, 0.07, 99, 100, 150000, 250000, 3, True
        )
        stability_path = self.run_dir / "stability-result.json"
        self.write_json(stability_path, stability)
        value["stability"] = stability
        for repeat, metric_path in zip(value["repeats"], metric_paths):
            repeat["metrics"] = self.file_ref(metric_path)
        value["gate_contract"] = sentinel_runner.build_gate_contract(
            stability["policy"], stability_path
        )
        self._rewrite_result(value)

    def _install_v2_clean_ready(self) -> dict:
        artifact_names = (
            "READY",
            "COMPLETE",
            "classification.env",
            "STATE",
            "samples.tsv",
            "latest.tsv",
            "monitor_clean_window.sh",
        )
        artifact_dir = self.root / "v2-clean"
        artifact_dir.mkdir()
        artifacts = {}
        for name in artifact_names:
            path = artifact_dir / name
            path.write_text("{} fixture\n".format(name), encoding="utf-8")
            artifacts[name] = self.file_ref(path)
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        clean_ready = {
            "schema_version": "p02b-clean-ready-binding-v2",
            "state": "PASS",
            "protocol_version": "short-clean-window-v2",
            "run_id": "fixture-clean-v2",
            "ready_time": now,
            "age_seconds_at_binding": 0.0,
            "required_consecutive_samples": 5,
            "observed_consecutive_samples": 5,
            "source_v1_history_preserved": False,
            "git_head": self.repo_head,
            "host": self.host["hostname"],
            "timing": {
                "expected_interval_seconds": 60,
                "gap_tolerance_seconds": 15.0,
                "observed_gap_seconds": [60.0, 60.0, 60.0, 60.0],
                "maximum_observed_gap_seconds": 60.0,
                "gap_check_pass": True,
            },
            "artifacts": artifacts,
        }
        self._replace_clean_ready(clean_ready)
        return clean_ready

    def _replace_clean_ready(self, clean_ready: dict) -> None:
        binding = self.run_dir / "clean-ready-binding.json"
        self.write_json(binding, clean_ready)
        provenance = json.loads(self.provenance_path.read_text(encoding="utf-8"))
        provenance["clean_ready_binding_sha256"] = sha256_file(binding)
        self.write_json(self.provenance_path, provenance)
        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        result["clean_ready"] = clean_ready
        result["provenance"]["sha256"] = sha256_file(self.provenance_path)
        self._rewrite_result(result)

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

    def test_complete_short_v2_binding_is_accepted(self) -> None:
        self._install_v2_clean_ready()
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

    def test_quantization_gate_contract_is_returned_and_tamper_is_rejected(self) -> None:
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
        self.assertEqual(
            receipt["gate_contract"]["method"], "quantization-aware-tail-v1"
        )
        self.assertEqual(receipt["gate_contract"]["qps_cv_max"], 0.07)

        value = json.loads(self.result_path.read_text(encoding="utf-8"))
        value["gate_contract"]["qps_cv_max"] = 0.070001
        unsigned = dict(value["gate_contract"])
        unsigned.pop("contract_sha256")
        value["gate_contract"]["contract_sha256"] = sentinel_runner.canonical_sha256(
            unsigned
        )
        self._rewrite_result(value)
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

    def test_gate_contract_exact_key_drift_is_rejected(self) -> None:
        value = json.loads(self.result_path.read_text(encoding="utf-8"))
        value["gate_contract"]["method_id"] = value["gate_contract"].pop("method")
        unsigned = dict(value["gate_contract"])
        unsigned.pop("contract_sha256")
        value["gate_contract"]["contract_sha256"] = sentinel_runner.canonical_sha256(
            unsigned
        )
        self._rewrite_result(value)
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

    def test_gate_contract_tool_sha_drift_is_rejected(self) -> None:
        value = json.loads(self.result_path.read_text(encoding="utf-8"))
        value["gate_contract"]["tools"]["calculate_stability"]["sha256"] = "0" * 64
        unsigned = dict(value["gate_contract"])
        unsigned.pop("contract_sha256")
        value["gate_contract"]["contract_sha256"] = sentinel_runner.canonical_sha256(
            unsigned
        )
        self._rewrite_result(value)
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

    def test_bench_json_must_bind_exact_p31_command_stdout(self) -> None:
        value = json.loads(self.result_path.read_text(encoding="utf-8"))
        metric_paths = [
            Path(repeat["metrics"]["path"]).resolve() for repeat in value["repeats"]
        ]
        metric = json.loads(metric_paths[0].read_text(encoding="utf-8"))
        decoy = metric_paths[0].parent / "decoy-command.stdout.log"
        source_stdout = Path(metric["bench_json"]["path"])
        decoy.write_bytes(source_stdout.read_bytes())
        metric["bench_json"]["path"] = str(decoy.resolve())
        self.write_json(metric_paths[0], metric)

        self._rebuild_metric_chain(value)
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

    def test_metric_numeric_tamper_is_rejected_after_full_chain_resign(self) -> None:
        value = json.loads(self.result_path.read_text(encoding="utf-8"))
        metric_path = Path(value["repeats"][0]["metrics"]["path"])
        metric = json.loads(metric_path.read_text(encoding="utf-8"))
        original_bench_ref = dict(metric["bench_json"])
        metric["qps"] *= 1.001
        self.write_json(metric_path, metric)
        self._rebuild_metric_chain(value)
        resigned_metric = json.loads(metric_path.read_text(encoding="utf-8"))
        self.assertEqual(resigned_metric["bench_json"], original_bench_ref)
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

    def test_foreign_copied_p31_repeat_is_rejected(self) -> None:
        value = json.loads(self.result_path.read_text(encoding="utf-8"))
        source = Path(value["repeats"][0]["p31"]["run_dir"])
        foreign = self.root / "legacy-copied-p31"
        shutil.copytree(source, foreign)
        value["repeats"][0]["p31"] = {
            "run_dir": str(foreign.resolve()),
            "manifest": self.file_ref(foreign / "run-manifest.json"),
            "validation": self.file_ref(foreign / "validation.json"),
            "done": self.file_ref(foreign / "DONE"),
            "command_stdout": self.file_ref(foreign / "command.stdout.log"),
        }
        self._rewrite_result(value)
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

    def test_p31_warning_is_rejected_after_evidence_resign(self) -> None:
        value = json.loads(self.result_path.read_text(encoding="utf-8"))
        p31 = value["repeats"][0]["p31"]
        manifest_path = Path(p31["manifest"]["path"])
        validation_path = Path(p31["validation"]["path"])
        done_path = Path(p31["done"]["path"])
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        validation["warnings"] = ["fixture warning"]
        self.write_json(validation_path, validation)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["validation"] = validation
        self.write_json(manifest_path, manifest)
        done = json.loads(done_path.read_text(encoding="utf-8"))
        done["manifest_sha256"] = sha256_file(manifest_path)
        done["validation_sha256"] = sha256_file(validation_path)
        self.write_json(done_path, done)
        p31["manifest"] = self.file_ref(manifest_path)
        p31["validation"] = self.file_ref(validation_path)
        p31["done"] = self.file_ref(done_path)
        self._rewrite_result(value)
        with self.assertRaises(GateError):
            sentinel_runner.validate_p31_run(
                Path(p31["run_dir"]),
                "P02B-SF10-SENTINEL-r1",
                self.repo_head,
                self.root / "store",
                {},
                True,
            )
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

    def test_short_v2_gap_tamper_is_rejected_after_chain_resign(self) -> None:
        clean_ready = self._install_v2_clean_ready()
        clean_ready["timing"]["observed_gap_seconds"][2] = 12.0
        clean_ready["timing"]["maximum_observed_gap_seconds"] = 60.0
        self._replace_clean_ready(clean_ready)
        with self.assertRaises(GateError):
            validate_result(self.result_path, "P20", False)

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
        needle = '  "schema_version": "p02b-sf10-sentinel-result-v2",\n'
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
