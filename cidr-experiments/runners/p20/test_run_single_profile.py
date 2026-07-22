#!/usr/bin/env python3

import csv
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


HERE = Path(__file__).resolve().parent
RUNNER = HERE / "run_single_profile.py"
SUMMARIZER = HERE / "summarize_profile.py"
FIXTURES = HERE / "tests"
SPEC = importlib.util.spec_from_file_location("run_single_profile", RUNNER)
assert SPEC and SPEC.loader
RUNNER_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER_MODULE)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "size_bytes": path.stat().st_size, "sha256": sha256(path)}


def canonical_inventory_sha(files):
    payload = json.dumps(
        sorted(files, key=lambda value: value["path"]),
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SingleProfileRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "fixture.txt").write_text("fixture repo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "fixture@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "P20 Fixture"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "fixture.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-q", "-m", "fixture"], check=True
        )
        self.repo_head = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        self.mount = self.root / "data-mount"
        self.mount.mkdir()
        self.dataset = self.root / "dataset.bin"
        self.dataset.write_bytes(b"dataset-v1\n")
        self.sample_plan = self.root / "sample-plan.json"
        self.sample_plan.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "fixture",
                    "entries": [
                        {
                            "edge_type": 1,
                            "samples": [{"src": 1, "degree": 4}, {"src": 2, "degree": 5}],
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.pristine = self.root / "pristine-store"
        self.pristine.mkdir()
        (self.pristine / "store.bin").write_bytes(b"immutable-pristine\n")
        self.binary = self.root / "fixture-benchmark"
        shutil.copy2(FIXTURES / "fixture_benchmark.py", self.binary)
        self.binary.chmod(0o755)
        self.truth = self.root / "truth.json"
        self.truth.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "digest_schema": "storage-bench-result-digest-v1",
                    "scale": "sf10",
                    "workload": "typed-one-hop",
                    "sample_plan_sha256": sha256(self.sample_plan),
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
                                    "src": 1,
                                    "result_count": 4,
                                    "result_digest": "1111111111111111",
                                },
                                {
                                    "src": 2,
                                    "result_count": 5,
                                    "result_digest": "2222222222222222",
                                },
                            ],
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        store_files = [
            {
                "path": "store.bin",
                "size_bytes": (self.pristine / "store.bin").stat().st_size,
                "sha256": sha256(self.pristine / "store.bin"),
            }
        ]
        self.store_inventory_sha = canonical_inventory_sha(store_files)
        self.store_manifest = self.root / "pristine-store-manifest.json"
        self.store_manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "FROZEN",
                    "inventory_schema": "p20-store-inventory-v1",
                    "store_path": str(self.pristine),
                    "scale": "sf10",
                    "l0_layout": "semantic-budgeted",
                    "dataset_sha256": sha256(self.dataset),
                    "binary_sha256": sha256(self.binary),
                    "inventory_sha256": self.store_inventory_sha,
                    "files": store_files,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.p31 = self.root / "fixture-p31"
        shutil.copy2(FIXTURES / "fixture_p31.py", self.p31)
        self.p31.chmod(0o755)
        self.correctness = self.root / "correctness-pass.json"
        self.write_correctness()
        self.sentinel_dir = self.root / "p02b-sentinel"
        self.sentinel = self.sentinel_dir / "sentinel-result.json"
        self.write_sentinel()
        self.output = self.root / "output"
        self.stage = self.root / "stage-store"

    def tearDown(self):
        self.temporary.cleanup()

    def write_correctness(self, **changes):
        value = {
            "schema_version": 1,
            "state": "PASS",
            "correctness_only": True,
            "performance_eligible": False,
            "scale": "sf10",
            "workload": "typed-one-hop",
            "digest_schema": "storage-bench-result-digest-v1",
            "covered_stages": ["A0", "A1", "A2", "A3", "A4", "A5", "A6"],
            "expected_queries_per_stage": 2,
            "checked": 14,
            "mismatches": 0,
            "binary_sha256": sha256(self.binary),
            "dataset_sha256": sha256(self.dataset),
            "truth_sha256": sha256(self.truth),
            "sample_plan_sha256": sha256(self.sample_plan),
        }
        value.update(changes)
        self.correctness.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def write_sentinel(self, **changes):
        self.sentinel_dir.mkdir(parents=True, exist_ok=True)
        p02b_dataset = self.root / "p02b-dataset"
        p02b_dataset.mkdir(exist_ok=True)
        (p02b_dataset / "dataset.tsv").write_text("fixture\n", encoding="utf-8")
        dataset_manifest = self.root / "p02b-dataset-manifest.json"
        dataset_content_sha = sha256(p02b_dataset / "dataset.tsv")
        dataset_manifest.write_text(
            json.dumps(
                {
                    "schema_version": "p02b-dataset-manifest-v1",
                    "dataset_root": str(p02b_dataset.resolve()),
                    "dataset_sha256": dataset_content_sha,
                    "hash_method": "fixture-single-file-sha256",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        sentinel_store_manifest = self.root / "p02b-store-manifest.json"
        sentinel_store_manifest.write_text(
            json.dumps(
                {
                    "schema_version": "p02b-store-manifest-v1",
                    "store_path": str(self.pristine.resolve()),
                    "store_sha256": self.store_inventory_sha,
                    "hash_method": "p20-store-inventory-v1",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = self.root / "p02b-config.json"
        config.write_text('{"schema_version":"p02b-sf10-sentinel-config-v1"}\n', encoding="utf-8")
        id_map = self.root / "p02b-id-map"
        id_map.mkdir(exist_ok=True)
        dense = id_map / "dense-to-original.tsv"
        original = id_map / "original-to-dense.tsv"
        dense.write_text("dense_id\toriginal_id\n0\t100\n", encoding="utf-8")
        original.write_text("original_id\tdense_id\n100\t0\n", encoding="utf-8")
        id_map_manifest = id_map / "id-map-manifest.json"
        id_map_manifest.write_text(
            json.dumps(
                {
                    "format": "seml0-shared-id-map",
                    "format_version": 1,
                    "status": "PASS",
                    "formal_pass": True,
                    "verification_complete": True,
                    "mapping_hash": "0123456789abcdef",
                    "mapping_hash_algorithm": "fnv1a64-le-dense-original-v1",
                    "dense_to_original": {"sha256": sha256(dense)},
                    "original_to_dense": {"sha256": sha256(original)},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        formal_pass = id_map / "FORMAL-PASS"
        formal_pass.write_text(
            "id-map-manifest.json sha256 {}\n".format(sha256(id_map_manifest)),
            encoding="utf-8",
        )
        checksums = id_map / "SHA256SUMS"
        checksums.write_text("fixture checksums\n", encoding="utf-8")
        file_references = {
            "binary": file_ref(self.binary),
            "truth": file_ref(self.truth),
            "query_plan": file_ref(self.sample_plan),
            "config": file_ref(config),
            "dataset_manifest": file_ref(dataset_manifest),
            "store_manifest": file_ref(sentinel_store_manifest),
            "id_map_manifest": file_ref(id_map_manifest),
            "p31_wrapper": file_ref(self.p31),
        }
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ready = self.root / "p02b-READY"
        ready.write_text("readiness_gate=PASS\n", encoding="utf-8")
        clean_ready = {
            "schema_version": "p02b-clean-ready-binding-v1",
            "state": "PASS",
            "run_id": "fixture-clean",
            "ready_time": now,
            "age_seconds_at_binding": 0.0,
            "required_consecutive_samples": 10,
            "observed_consecutive_samples": 10,
            "git_head": self.repo_head,
            "host": socket.gethostname(),
            "artifacts": {"READY": file_ref(ready)},
        }
        clean_binding = self.sentinel_dir / "clean-ready-binding.json"
        clean_binding.write_text(json.dumps(clean_ready, sort_keys=True) + "\n", encoding="utf-8")
        regenerated_plan = self.sentinel_dir / "regenerated-query-plan.json"
        regenerated_plan.write_bytes(self.sample_plan.read_bytes())
        shared_truth_result = self.sentinel_dir / "shared-truth-result.json"
        shared_truth_result.write_text('{"state":"PASS"}\n', encoding="utf-8")
        host = {"hostname": socket.gethostname(), "fingerprint_sha256": "f" * 64}
        repeats = []
        stability_runs = []
        qps_values = [100.0, 101.0, 99.0]
        p99_values = [1000.0, 1005.0, 995.0]
        for index, (qps, p99) in enumerate(zip(qps_values, p99_values), start=1):
            run_id = "p02b-fixture-r{}".format(index)
            repeat_dir = self.sentinel_dir / "repeats" / "r{}".format(index)
            p31_dir = repeat_dir / "p31"
            p31_dir.mkdir(parents=True, exist_ok=True)
            metrics_path = repeat_dir / "run-metrics.json"
            metrics_value = {
                "schema_version": "p02b-sentinel-run-metrics-v1",
                "state": "PASS",
                "run_index": index,
                "run_id": run_id,
                "query_count": 1700,
                "qps": qps,
                "p99_us": p99,
                "measured_seconds": 30.0,
            }
            metrics_path.write_text(json.dumps(metrics_value, sort_keys=True) + "\n", encoding="utf-8")
            stability_runs.append(
                {
                    "path": str(metrics_path.resolve()),
                    "sha256": sha256(metrics_path),
                    "run_index": index,
                    "run_id": run_id,
                    "query_count": 1700,
                    "qps": qps,
                    "p99_us": p99,
                    "measured_seconds": 30.0,
                }
            )
            manifest_path = p31_dir / "run-manifest.json"
            validation_path = p31_dir / "validation.json"
            done_path = p31_dir / "DONE"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": "cidr-run-manifest-v1",
                        "resource_schema_version": "cidr-resource-v1",
                        "state": "PASS",
                        "run_id": run_id,
                        "task_id": "P02B-SF10-SENTINEL-r{}".format(index),
                        "performance_eligible_declared": False,
                        "repo": {"git_sha": self.repo_head, "dirty": False},
                        "host": host,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            validation_path.write_text('{"state":"PASS"}\n', encoding="utf-8")
            done_path.write_text(
                json.dumps(
                    {
                        "state": "PASS",
                        "manifest_sha256": sha256(manifest_path),
                        "validation_sha256": sha256(validation_path),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            repeats.append(
                {
                    "run_index": index,
                    "run_id": run_id,
                    "metrics": file_ref(metrics_path),
                    "p31": {
                        "run_dir": str(p31_dir.resolve()),
                        "manifest": file_ref(manifest_path),
                        "validation": file_ref(validation_path),
                        "done": file_ref(done_path),
                    },
                }
            )

        def cv_metric(values, maximum):
            mean = statistics.mean(values)
            stdev = statistics.stdev(values)
            return {
                "values": values,
                "mean": mean,
                "sample_stdev": stdev,
                "cv": stdev / mean,
                "maximum_cv": maximum,
                "pass": True,
            }

        stability = {
            "schema_version": "p02b-sentinel-cv-v1",
            "state": "PASS",
            "method": "sample_standard_deviation_over_arithmetic_mean",
            "independent_process_runs": 3,
            "query_count_per_run": 1700,
            "qps": cv_metric(qps_values, 0.03),
            "p99_us": cv_metric(p99_values, 0.05),
            "runs": stability_runs,
        }
        provenance = {
            "schema_version": "p02b-sentinel-provenance-v1",
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "repo": {
                "root": str(self.repo.resolve()),
                "head": self.repo_head,
                "dirty": False,
                "status_lines": [],
            },
            "fixture_mode": False,
            "files": file_references,
            "dataset": {
                "manifest": file_references["dataset_manifest"],
                "root": str(p02b_dataset.resolve()),
                "content_sha256": dataset_content_sha,
                "hash_method": "fixture-single-file-sha256",
            },
            "store": {
                "manifest": file_references["store_manifest"],
                "root": str(self.pristine.resolve()),
                "content_sha256": self.store_inventory_sha,
                "hash_method": "p20-store-inventory-v1",
            },
            "id_map": {
                "path": str(id_map.resolve()),
                "manifest": file_references["id_map_manifest"],
                "formal_pass": file_ref(formal_pass),
                "checksums": file_ref(checksums),
                "mapping_hash": "0123456789abcdef",
                "mapping_hash_algorithm": "fnv1a64-le-dense-original-v1",
                "dense_to_original_sha256": sha256(dense),
                "original_to_dense_sha256": sha256(original),
            },
            "query_plan_summary": {
                "version": 1,
                "source": "shared-truth-tsv",
                "entries": 1,
                "queries": 1700,
                "semantic_degree_hint": True,
                "force_signature": False,
                "sha256": sha256(self.sample_plan),
            },
            "clean_ready_binding_sha256": sha256(clean_binding),
        }
        provenance_path = self.sentinel_dir / "provenance.json"
        provenance_path.write_text(json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8")
        fixture_only = bool(changes.get("fixture_only", False))
        value = {
            "schema_version": "p02b-sf10-sentinel-result-v1",
            "state": "PASS",
            "fixture_only": fixture_only,
            "performance_eligible": False,
            "formal_gate_eligible": not fixture_only,
            "downstream_release_eligible": not fixture_only,
            "consumers": ["P10", "P20"],
            "task_id": "P02B-SF10-SENTINEL",
            "scale": "sf10",
            "run_id": "p02b-fixture",
            "started_at_utc": now,
            "completed_at_utc": now,
            "clean_ready": clean_ready,
            "protocol": {
                "independent_process_runs": 3,
                "expected_queries": 1700,
                "warmup_runs": 1,
                "measured_repeats": 1,
                "minimum_measured_seconds_per_run": 30.0,
                "cache_policy": "no-drop-caches;independent-process;in-process-warmup;os-cache-as-is",
                "cpu": {"housekeeping_cpuset": "0", "formal_cpuset": "1", "threads": 1},
                "io_backend": "blocking",
                "l0_layout": "semantic-budgeted",
                "semantic_degree_hint": True,
                "force_signature": False,
                "p31": {
                    "device": "fixture-device",
                    "data_mount": str(self.mount.resolve()),
                    "interval_seconds": 1.0,
                    "disk_interval_seconds": 15.0,
                    "min_samples": 10,
                    "require_aux_tools": True,
                },
            },
            "provenance": {
                "path": str(provenance_path.resolve()),
                "sha256": sha256(provenance_path),
                "repo_head": self.repo_head,
                "binary_sha256": sha256(self.binary),
                "truth_sha256": sha256(self.truth),
                "query_plan_sha256": sha256(self.sample_plan),
                "store_sha256": self.store_inventory_sha,
                "dataset_sha256": dataset_content_sha,
                "config_sha256": sha256(config),
            },
            "correctness": {
                "state": "PASS",
                "checked": 1700,
                "mismatches": 0,
                "total_neighbors": 1700,
                "mapping_hash": "0123456789abcdef",
                "result": file_ref(shared_truth_result),
                "regenerated_query_plan": file_ref(regenerated_plan),
            },
            "stability": stability,
            "repeats": repeats,
        }
        value.update(changes)
        self.sentinel.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        for marker_name in ("PASS", "FIXTURE-PASS", "FAILED"):
            marker_path = self.sentinel_dir / marker_name
            if marker_path.exists():
                marker_path.unlink()
        marker_name = "FIXTURE-PASS" if value.get("fixture_only") is True else "PASS"
        (self.sentinel_dir / marker_name).write_text(
            json.dumps(
                {
                    "state": "PASS",
                    "fixture_only": value.get("fixture_only") is True,
                    "result": str(self.sentinel.resolve()),
                    "result_sha256": sha256(self.sentinel),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def command(
        self, extra=None, output=None, stage=None, correctness_sha=None, include_sentinel=True
    ):
        allowed = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else [0, 1]
        if len(allowed) < 2:
            self.skipTest("P20 fixture requires two allowed CPUs")
        cpuset = str(allowed[0])
        housekeeping_cpuset = str(allowed[1])
        command = [
            sys.executable,
            str(RUNNER),
            "--output-root",
            str(output or self.output),
            "--stage-store",
            str(stage or self.stage),
            "--pristine-store",
            str(self.pristine),
            "--pristine-store-sha256",
            self.store_inventory_sha,
            "--pristine-store-manifest",
            str(self.store_manifest),
            "--pristine-store-manifest-sha256",
            sha256(self.store_manifest),
            "--task-id",
            "P20-fixture",
            "--run-id",
            "fixture-r1",
            "--repeat-index",
            "1",
            "--repo-root",
            str(self.repo),
            "--p31-wrapper",
            str(self.p31),
            "--binary",
            str(self.binary),
            "--binary-sha256",
            sha256(self.binary),
            "--dataset",
            str(self.dataset),
            "--dataset-sha256",
            sha256(self.dataset),
            "--sample-plan",
            str(self.sample_plan),
            "--sample-plan-sha256",
            sha256(self.sample_plan),
            "--truth",
            str(self.truth),
            "--truth-sha256",
            sha256(self.truth),
            "--correctness-pass",
            str(self.correctness),
            "--correctness-pass-sha256",
            correctness_sha or sha256(self.correctness),
            "--scale",
            "sf10",
            "--stage",
            "A2",
            "--mode",
            "cpu-phase",
            "--workload",
            "typed-one-hop",
            "--performance-eligible",
            "false",
            "--data-mount",
            str(self.mount),
            "--device",
            "fixture-device",
            "--min-samples",
            "10",
            "--worker-threads",
            "1",
            "--cpuset",
            cpuset,
            "--housekeeping-cpuset",
            housekeeping_cpuset,
        ]
        if include_sentinel:
            command.extend(
                [
                    "--legacy-v1-admission",
                    "--p02b-sentinel-result",
                    str(self.sentinel),
                    "--p02b-sentinel-result-sha256",
                    sha256(self.sentinel),
                ]
            )
        if extra:
            command.extend(extra)
        return command

    def invoke(self, env=None, **kwargs):
        return subprocess.run(
            self.command(**kwargs),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    def rehash_p31_raw(self):
        raw_path = self.output / "p31" / "command.stdout.log"
        manifest_path = self.output / "p31" / "run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["command.stdout.log"] = {
            "size_bytes": raw_path.stat().st_size,
            "sha256": sha256(raw_path),
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        done = self.output / "p31" / "DONE"
        done_doc = json.loads(done.read_text(encoding="utf-8"))
        done_doc["manifest_sha256"] = sha256(manifest_path)
        done.write_text(json.dumps(done_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def test_fixture_smoke_emits_fixed_summary_and_array_invocation(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        expected_files = {
            "resolved-profile.json",
            "invocation-config.json",
            "command.json",
            "inputs.sha256.tsv",
            "correctness-pass.json",
            "p02b-admission.json",
            "stage-store-provenance.json",
            "stage-store-post-state.json",
            "summary.tsv",
            "P20-PASS.json",
        }
        self.assertTrue(expected_files <= {path.name for path in self.output.iterdir()})
        self.assertTrue((self.output / "p31" / "DONE").is_file())
        self.assertFalse((self.output / "p31" / "FAILED").exists())
        with (self.output / "summary.tsv").open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["query_cpu_query_setup_ns"], "101")
        self.assertEqual(rows[0]["query_cpu_phase_sum_ns"], "1515")
        self.assertEqual(rows[0]["query_cpu_total_ns"], "2000")
        self.assertEqual(rows[0]["measured_operations"], "2")
        self.assertEqual(rows[0]["current_digest_pass"], "1")
        self.assertEqual(rows[0]["repeat_index"], "1")
        self.assertEqual(rows[0]["performance_eligible"], "false")
        command = json.loads((self.output / "command.json").read_text(encoding="utf-8"))
        self.assertEqual(command["invocation_kind"], "argv-no-shell")
        self.assertEqual(command["p31_argv"][command["p31_argv"].index("--") + 1 :], command["benchmark_argv"])
        self.assertIn("taskset", command["benchmark_argv"])
        p31_capture = json.loads((self.output / "p31" / "p31-argv.json").read_text(encoding="utf-8"))
        self.assertNotIn("--allow-missing-aux-tools", p31_capture["flags"])
        self.assertIn("false", p31_capture["argv"])
        self.assertEqual((self.stage / "store.bin").read_bytes(), b"immutable-pristine\n")
        (self.stage / "store.bin").write_bytes(b"stage-mutated\n")
        self.assertEqual((self.pristine / "store.bin").read_bytes(), b"immutable-pristine\n")

    @unittest.skipUnless(os.environ.get("P20_REAL_P31_WRAPPER"), "real P31 fixture smoke not requested")
    def test_real_p31_wrapper_with_sleeping_fixture_only(self):
        command = self.command(extra=["--disk-interval", "1", "--min-samples", "10"])
        command[command.index("--p31-wrapper") + 1] = os.environ["P20_REAL_P31_WRAPPER"]
        command[command.index("--data-mount") + 1] = "/data"
        command[command.index("--device") + 1] = "nvme1n1"
        environment = os.environ.copy()
        environment["P20_FIXTURE_SLEEP"] = "12"
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.output / "p31" / "DONE").is_file())
        self.assertTrue((self.output / "summary.tsv").is_file())

    def test_existing_empty_or_nonempty_output_is_rejected_before_clone(self):
        for name, nonempty in (("empty", False), ("nonempty", True)):
            output = self.root / name
            output.mkdir()
            if nonempty:
                (output / "occupied").write_text("x", encoding="utf-8")
            stage = self.root / (name + "-stage")
            result = self.invoke(output=output, stage=stage)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not already exist", result.stderr)
            self.assertFalse(stage.exists())

    def test_existing_stage_store_is_rejected(self):
        self.stage.mkdir()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stage store must not already exist", result.stderr)
        self.assertFalse(self.output.exists())

    def test_unknown_runtime_override_and_auto_compact_are_rejected(self):
        cases = [
            ["--runtime-override", "mystery=1"],
            ["--runtime-override", "auto-compact=1"],
            ["--runtime-override", "repeats=2"],
        ]
        for index, extra in enumerate(cases):
            output = self.root / ("reject-output-{}".format(index))
            stage = self.root / ("reject-stage-{}".format(index))
            result = self.invoke(extra=extra, output=output, stage=stage)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
            self.assertFalse(stage.exists())

    def test_mode_performance_mismatch_is_rejected(self):
        result = self.invoke(extra=["--performance-eligible", "true"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disagrees with resolved mode", result.stderr)
        self.assertFalse(self.stage.exists())

    def test_formal_mode_derives_true_and_forbids_missing_aux_tools(self):
        command = self.command()
        command[command.index("--mode") + 1] = "latency"
        command[command.index("--performance-eligible") + 1] = "true"
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        p31_capture = json.loads((self.output / "p31" / "p31-argv.json").read_text(encoding="utf-8"))
        perf_index = p31_capture["argv"].index("--performance-eligible")
        self.assertEqual(p31_capture["argv"][perf_index + 1], "true")
        self.assertNotIn("--allow-missing-aux-tools", p31_capture["flags"])

        blocked_output = self.root / "formal-blocked-output"
        blocked_stage = self.root / "formal-blocked-stage"
        blocked = self.command(output=blocked_output, stage=blocked_stage)
        blocked[blocked.index("--mode") + 1] = "latency"
        blocked[blocked.index("--performance-eligible") + 1] = "true"
        blocked.append("--allow-missing-aux-tools")
        result = subprocess.run(blocked, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot allow missing", result.stderr)
        self.assertFalse(blocked_output.exists())
        self.assertFalse(blocked_stage.exists())

    def test_correctness_sha_and_coverage_fail_closed(self):
        self.write_correctness(binary_sha256="0" * 64)
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("correctness evidence binary SHA-256", result.stderr)
        self.assertFalse(self.stage.exists())
        self.write_correctness(covered_stages=["A1"], checked=2)
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("canonical stage list", result.stderr)

    def test_summarizer_rejects_missing_done_and_missing_query_setup_field(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        done = self.output / "p31" / "DONE"
        saved_done = done.read_bytes()
        done.unlink()
        summary = subprocess.run(
            [sys.executable, str(SUMMARIZER), "--run-root", str(self.output)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(summary.returncode, 0)
        self.assertIn("DONE is missing", summary.stderr)
        done.write_bytes(saved_done)
        raw_path = self.output / "p31" / "command.stdout.log"
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        del raw["benchmarks"][0]["rounds"][0]["query_cpu_query_setup_ns"]
        raw_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
        self.rehash_p31_raw()
        summary = subprocess.run(
            [sys.executable, str(SUMMARIZER), "--run-root", str(self.output)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(summary.returncode, 0)
        self.assertIn("query_cpu_query_setup_ns", summary.stderr)

    def test_summarizer_rejects_failed_marker_even_with_done(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.output / "p31" / "FAILED").write_text('{"state":"FAILED"}\n', encoding="utf-8")
        summary = subprocess.run(
            [sys.executable, str(SUMMARIZER), "--run-root", str(self.output)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(summary.returncode, 0)
        self.assertIn("FAILED exists", summary.stderr)

    def test_current_run_digest_must_match_truth(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        raw_path = self.output / "p31" / "command.stdout.log"
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        raw["benchmarks"][0]["result_digests"][0]["result_digest"] = "f" * 16
        raw_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
        self.rehash_p31_raw()
        result = subprocess.run(
            [sys.executable, str(SUMMARIZER), "--run-root", str(self.output)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("current sample digest mismatches truth", result.stderr)

    def test_pristine_store_content_must_match_tree_manifest(self):
        changed = bytearray((self.pristine / "store.bin").read_bytes())
        changed[0] ^= 1
        (self.pristine / "store.bin").write_bytes(changed)
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from frozen inventory", result.stderr)
        self.assertFalse(self.stage.exists())

    def test_forbidden_semantic_bypass_environment_is_rejected(self):
        environment = os.environ.copy()
        environment["SNB_SKIP_SEM_INDEX"] = "1"
        result = self.invoke(env=environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("forbidden benchmark environment", result.stderr)
        self.assertFalse(self.stage.exists())

    def test_correctness_mode_is_an_explicit_external_phase(self):
        command = self.command()
        command[command.index("--mode") + 1] = "correctness"
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("consumes an external correctness PASS", result.stderr)
        self.assertFalse(self.stage.exists())

    def test_every_measured_mode_requires_formal_p02b_result(self):
        command = self.command(include_sentinel=False)
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--p02b-sentinel-result", result.stderr)
        self.assertFalse(self.stage.exists())

    def test_old_flat_p20_sentinel_schema_is_rejected_before_clone(self):
        self.sentinel.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "PASS",
                    "purpose": "p20-clean-window-sentinel",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sentinel result key drift", result.stderr)
        self.assertFalse(self.stage.exists())

    def test_p02b_caller_hash_and_collection_floor_fail_before_clone(self):
        wrong_hash = self.command()
        wrong_hash[wrong_hash.index("--p02b-sentinel-result-sha256") + 1] = "0" * 64
        result = subprocess.run(
            wrong_hash, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("p02b_sentinel_result SHA-256 mismatch", result.stderr)
        self.assertFalse(self.stage.exists())

        low_samples = self.command()
        low_samples[low_samples.index("--min-samples") + 1] = "9"
        result = subprocess.run(
            low_samples, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("min_samples >= 10", result.stderr)
        self.assertFalse(self.stage.exists())

        overlap = self.command()
        overlap[overlap.index("--housekeeping-cpuset") + 1] = overlap[
            overlap.index("--cpuset") + 1
        ]
        result = subprocess.run(
            overlap, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cpusets must be disjoint", result.stderr)
        self.assertFalse(self.stage.exists())

    def test_p02b_consumer_marker_and_provenance_tamper_fail_closed(self):
        cases = []
        self.write_sentinel(consumers=["P10"])
        cases.append(("consumer", "canonical consumer list drift"))
        for index, (_, expected_message) in enumerate(cases):
            result = self.invoke(
                output=self.root / "tamper-output-{}".format(index),
                stage=self.root / "tamper-stage-{}".format(index),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected_message, result.stderr)

        self.write_sentinel()
        marker = self.sentinel_dir / "PASS"
        marker_value = json.loads(marker.read_text(encoding="utf-8"))
        marker_value["result_sha256"] = "0" * 64
        marker.write_text(json.dumps(marker_value) + "\n", encoding="utf-8")
        result = self.invoke(
            output=self.root / "marker-output", stage=self.root / "marker-stage"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("marker", result.stderr)

        self.write_sentinel()
        provenance = self.sentinel_dir / "provenance.json"
        provenance.write_text(provenance.read_text(encoding="utf-8") + " ", encoding="utf-8")
        result = self.invoke(
            output=self.root / "provenance-output", stage=self.root / "provenance-stage"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provenance", result.stderr)

    def test_cpu_phase_binds_p02b_gate_for_figure_pair(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        with (self.output / "summary.tsv").open("r", encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual(row["p02b_sentinel_result_sha256"], sha256(self.sentinel))
        self.assertEqual(row["performance_eligible"], "false")

    def test_summary_rejects_p02b_marker_toctou(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = self.sentinel_dir / "PASS"
        marker.write_text(marker.read_text(encoding="utf-8") + " ", encoding="utf-8")
        summary = subprocess.run(
            [sys.executable, str(SUMMARIZER), "--run-root", str(self.output)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(summary.returncode, 0)
        self.assertIn("p02b_pass_marker", summary.stderr)


class AdmissionProtocolUnitTest(unittest.TestCase):
    def test_explicit_legacy_and_complete_v2_are_accepted_but_mixing_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            gate = repo / "cidr-experiments" / "runners" / "batch_gate_v2.py"
            gate.parent.mkdir(parents=True)
            gate.write_text("# fixture\n", encoding="utf-8")
            lease = repo / "lease.json"
            lease.write_text("{}\n", encoding="utf-8")
            legacy = SimpleNamespace(
                legacy_v1_admission=True,
                batch_lease=None,
                batch_gate_tool=None,
                p02b_sentinel_result=repo / "result.json",
                p02b_sentinel_result_sha256="a" * 64,
            )
            self.assertEqual(
                RUNNER_MODULE.resolve_admission_mode(legacy, repo)["protocol_version"],
                "legacy-p02b-admission-v1",
            )
            v2 = SimpleNamespace(
                legacy_v1_admission=False,
                batch_lease=lease,
                batch_gate_tool=gate,
                p02b_sentinel_result=None,
                p02b_sentinel_result_sha256=None,
            )
            self.assertEqual(
                RUNNER_MODULE.resolve_admission_mode(v2, repo)["protocol_version"],
                "short-clean-window-v2",
            )
            v2.p02b_sentinel_result = repo / "result.json"
            with self.assertRaises(RUNNER_MODULE.RunnerError):
                RUNNER_MODULE.resolve_admission_mode(v2, repo)

    def test_p31_guard_evidence_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            evidence = {}
            mapping = {
                "release": "command-release.json",
                "ready": "READY.json",
                "status": "status.json",
                "samples": "integrity-samples.tsv",
            }
            for name, filename in mapping.items():
                path = root / filename
                path.write_text(name + "\n", encoding="utf-8")
                evidence[name] = file_ref(path)
            lease_row = {"sha256": "1" * 64}
            gate_row = {"sha256": "2" * 64}
            binary_row = {"sha256": "3" * 64}
            validation = {
                "state": "PASS",
                "integrity_guard": {
                    "schema_version": "cidr-p31-batch-integrity-v2",
                    "state": "PASS",
                    "consumer": "P20",
                    "lease_sha256": lease_row["sha256"],
                    "gate_tool_sha256": gate_row["sha256"],
                    "anchor_binary_sha256": binary_row["sha256"],
                    "evidence": evidence,
                },
            }
            (root / "validation.json").write_text(
                json.dumps(validation), encoding="utf-8"
            )
            guard, rows = RUNNER_MODULE.validate_p31_integrity_guard(
                root, lease_row, gate_row, binary_row
            )
            self.assertEqual(guard["state"], "PASS")
            self.assertEqual(len(rows), 4)
            (root / "status.json").write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(RUNNER_MODULE.RunnerError):
                RUNNER_MODULE.validate_p31_integrity_guard(
                    root, lease_row, gate_row, binary_row
                )


if __name__ == "__main__":
    unittest.main()
