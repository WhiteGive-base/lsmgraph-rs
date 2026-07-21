#!/usr/bin/env python3

import csv
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
RUNNER = HERE / "run_single_profile.py"
SUMMARIZER = HERE / "summarize_profile.py"
FIXTURES = HERE / "tests"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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
        self.sentinel = self.root / "clean-window-sentinel.json"
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
        value = {
            "schema_version": 1,
            "state": "PASS",
            "purpose": "p20-clean-window-sentinel",
            "performance_eligible": False,
            "gate_mode": "seml0",
            "scale": "sf10",
            "host": socket.gethostname(),
            "completed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "observations": 3,
            "qps_cv": 0.02,
            "p99_cv": 0.04,
            "monitor_ready_sha256": "9" * 64,
            "binary_sha256": sha256(self.binary),
            "dataset_sha256": sha256(self.dataset),
            "sample_plan_sha256": sha256(self.sample_plan),
            "truth_sha256": sha256(self.truth),
            "pristine_store_sha256": self.store_inventory_sha,
        }
        value.update(changes)
        self.sentinel.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def command(self, extra=None, output=None, stage=None, correctness_sha=None):
        cpuset = str(min(os.sched_getaffinity(0))) if hasattr(os, "sched_getaffinity") else None
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
            "--allow-missing-aux-tools",
            "--worker-threads",
            "1",
        ]
        if cpuset is not None:
            command.extend(["--cpuset", cpuset])
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
        self.assertIn("--allow-missing-aux-tools", p31_capture["flags"])
        self.assertIn("false", p31_capture["argv"])
        self.assertEqual((self.stage / "store.bin").read_bytes(), b"immutable-pristine\n")
        (self.stage / "store.bin").write_bytes(b"stage-mutated\n")
        self.assertEqual((self.pristine / "store.bin").read_bytes(), b"immutable-pristine\n")

    @unittest.skipUnless(os.environ.get("P20_REAL_P31_WRAPPER"), "real P31 fixture smoke not requested")
    def test_real_p31_wrapper_with_sleeping_fixture_only(self):
        command = self.command(
            extra=["--disk-interval", "1", "--min-samples", "2"]
        )
        command[command.index("--p31-wrapper") + 1] = os.environ["P20_REAL_P31_WRAPPER"]
        command[command.index("--data-mount") + 1] = "/data"
        command[command.index("--device") + 1] = "nvme1n1"
        environment = os.environ.copy()
        environment["P20_FIXTURE_SLEEP"] = "4"
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
        self.write_sentinel()
        command = self.command()
        command[command.index("--mode") + 1] = "latency"
        command[command.index("--performance-eligible") + 1] = "true"
        command.remove("--allow-missing-aux-tools")
        command.extend(
            [
                "--clean-window-sentinel",
                str(self.sentinel),
                "--clean-window-sentinel-sha256",
                sha256(self.sentinel),
            ]
        )
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

    def test_formal_mode_requires_valid_clean_window_sentinel(self):
        command = self.command()
        command[command.index("--mode") + 1] = "latency"
        command[command.index("--performance-eligible") + 1] = "true"
        command.remove("--allow-missing-aux-tools")
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires a clean-window sentinel", result.stderr)

        self.write_sentinel(qps_cv=0.031)
        command.extend(
            [
                "--clean-window-sentinel",
                str(self.sentinel),
                "--clean-window-sentinel-sha256",
                sha256(self.sentinel),
            ]
        )
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("qps_cv exceeds", result.stderr)

    def test_cpu_phase_can_bind_same_clean_window_for_figure_pair(self):
        self.write_sentinel()
        result = self.invoke(
            extra=[
                "--clean-window-sentinel",
                str(self.sentinel),
                "--clean-window-sentinel-sha256",
                sha256(self.sentinel),
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with (self.output / "summary.tsv").open("r", encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual(row["clean_window_sentinel_sha256"], sha256(self.sentinel))
        self.assertEqual(row["performance_eligible"], "false")


if __name__ == "__main__":
    unittest.main()
