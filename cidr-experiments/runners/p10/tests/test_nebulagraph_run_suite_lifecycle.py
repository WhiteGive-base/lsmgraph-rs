#!/usr/bin/env python3
"""Mock end-to-end gates for the run_suite-owned NebulaGraph lifecycle."""

from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

P10_DIR = Path(__file__).resolve().parents[1]
NEBULA_DIR = P10_DIR / "adapters" / "nebulagraph"
sys.path.insert(0, str(P10_DIR))
sys.path.insert(0, str(NEBULA_DIR))

import nebula_adapter as adapter  # noqa: E402
import run_suite  # noqa: E402
from p10_contract import (  # noqa: E402
    EXTERNAL_PROCESS_LIFETIME,
    TRUTH_DIGEST_ALGORITHM,
    ContractError,
    sha256_file,
)


class NebulaGraphRunSuiteLifecycleTest(unittest.TestCase):
    def _inputs(self, root: Path) -> tuple[dict, dict, list[dict[str, int]], Path]:
        dataset = root / "dataset.txt"
        dataset.write_text("1\n0 1 0\n", encoding="utf-8")
        truth = root / "truth.tsv"
        truth.write_text(
            "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash\n"
            "0\t1\t0\t1\t0\t0\n",
            encoding="utf-8",
        )
        store = root / "store"
        logs = root / "logs"
        store.mkdir()
        logs.mkdir()
        controller = NEBULA_DIR / "formal_cluster.py"
        system = {
            "id": "nebulagraph",
            "display_name": "NebulaGraph",
            "group": "client-server",
            "system_version": "NebulaGraph 3.8.0",
            "process_lifetime": EXTERNAL_PROCESS_LIFETIME,
            "binary": {"path": sys.executable, "sha256": sha256_file(Path(sys.executable))},
            "runtime_libraries": [],
            "store_roots": [{"label": "nebulagraph", "path": str(store), "sha256": "a" * 64}],
            "temp_roots": [{"label": "nebulagraph-logs", "path": str(logs)}],
            "service_lifecycle": "external-prestarted",
            "containers": ["nebula-graph-r01", "nebula-meta-r01", "nebula-storage-r01"],
            "extra_pids": [],
            "image_digests": [
                adapter.EXPECTED_IMAGES[role]["digest"]
                for role in ("graphd", "metad", "storaged")
            ],
            "adapter": {
                "path": str(NEBULA_DIR / "nebula_adapter.py"),
                "args": [
                    "--cluster-controller", str(controller),
                    "--cluster-controller-sha256", sha256_file(controller),
                ],
            },
        }
        suite = {
            "suite_id": "nebula-mock-e2e",
            "dataset": {"path": str(dataset), "sha256": sha256_file(dataset)},
            "truth": {
                "path": str(truth),
                "sha256": sha256_file(truth),
                "query_count": 1,
                "digest_algorithm": TRUTH_DIGEST_ALGORITHM,
            },
            "protocol": {
                "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
                "warmup_passes": 1,
                "measured_passes": 1,
                "concurrency": 1,
                "per_query_timeout_ms": 5000,
                "adapter_process_timeout_s": 30,
                "max_timeouts": 0,
            },
            "resources": {
                "device": "fixture",
                "data_mount": str(root),
                "interval_s": 1,
                "disk_interval_s": 1,
                "min_samples": 1,
            },
        }
        return suite, system, [{"query_index": 0, "edge_type": 1, "src": 0, "count": 1, "sum_hash": 0, "xor_hash": 0}], truth

    @staticmethod
    def _launched(system: dict, repeat_dir: Path) -> dict:
        lifecycle = system["active_nebulagraph_lifecycle"]
        return {
            **lifecycle,
            "sealed_admission_path": Path(lifecycle["receipt_paths"]["sealed_admission"]),
            "sealed_admission_sha256": "a" * 64,
            "sealed_admission": {"state": "PASS"},
            "preflight_path": Path(lifecycle["receipt_paths"]["preflight"]),
            "preflight_sha256": "b" * 64,
            "preflight": {"state": "PASS"},
            "start_path": Path(lifecycle["receipt_paths"]["start"]),
            "start_sha256": "c" * 64,
            "start": {"state": "PASS"},
        }

    @staticmethod
    def _stopped(system: dict, repeat_dir: Path, lifecycle: dict) -> dict:
        return {
            **lifecycle,
            "stop_path": Path(lifecycle["receipt_paths"]["stop"]),
            "stop_sha256": "d" * 64,
            "stop": {"state": "PASS"},
        }

    def test_mock_e2e_starts_before_p31_and_stops_before_validation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-run-suite-e2e-") as raw:
            root = Path(raw)
            suite, system, truth_rows, manifest = self._inputs(root)
            run_root = root / "run-id"
            run_root.mkdir()
            events: list[str] = []

            def launch(**kwargs):
                events.append("start")
                return self._launched(kwargs["system"], kwargs["repeat_dir"])

            def stop(**kwargs):
                events.append("stop")
                return self._stopped(kwargs["system"], kwargs["repeat_dir"], kwargs["lifecycle"])

            def run_p31(*args, **kwargs):
                events.append("p31")
                return subprocess.CompletedProcess(args=args, returncode=0)

            def validate_outputs(**kwargs):
                events.append("validate")
                return {"adapter_provenance": {}}

            def bind(*args, **kwargs):
                events.append("bind")

            with mock.patch.object(run_suite, "launch_nebulagraph_repeat", side_effect=launch), mock.patch.object(
                run_suite, "stop_nebulagraph_repeat", side_effect=stop
            ), mock.patch.object(run_suite, "p31_command", return_value=["mock-p31"]), mock.patch.object(
                run_suite.subprocess, "run", side_effect=run_p31
            ), mock.patch.object(run_suite, "read_p31_summary", return_value={"mock": True}), mock.patch.object(
                run_suite, "validate_adapter_outputs", side_effect=validate_outputs
            ), mock.patch.object(run_suite, "validate_nebulagraph_p31_binding", side_effect=bind):
                result = run_suite._execute_repeat_impl(
                    suite=suite,
                    system=system,
                    truth_rows=truth_rows,
                    repeat_index=1,
                    run_root=run_root,
                    resolved_manifest=manifest,
                    p31_wrapper=root / "p31.sh",
                    mode="formal",
                )
            self.assertEqual(events, ["start", "p31", "stop", "validate", "bind"])
            self.assertTrue(result["nebulagraph_lifecycle"]["graceful_stop"])
            request = run_suite.read_json(
                run_root / "systems/nebulagraph/repeat-01/adapter-request.json",
                "mock adapter request",
            )
            receipts = request["external_service"]["orchestrated_lifecycle"]["receipts"]
            self.assertEqual(receipts["start"], receipts["live_gate"])
            self.assertIn("stop", receipts)

    def test_p31_failure_still_runs_exact_lifecycle_stop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-run-suite-negative-") as raw:
            root = Path(raw)
            suite, system, truth_rows, manifest = self._inputs(root)
            run_root = root / "run-id"
            run_root.mkdir()
            stopped: list[dict] = []

            def launch(**kwargs):
                return self._launched(kwargs["system"], kwargs["repeat_dir"])

            def stop(**kwargs):
                stopped.append(kwargs["lifecycle"])
                return self._stopped(kwargs["system"], kwargs["repeat_dir"], kwargs["lifecycle"])

            with mock.patch.object(run_suite, "launch_nebulagraph_repeat", side_effect=launch), mock.patch.object(
                run_suite, "stop_nebulagraph_repeat", side_effect=stop
            ), mock.patch.object(run_suite, "p31_command", return_value=["mock-p31"]), mock.patch.object(
                run_suite.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(args=[], returncode=23),
            ):
                with self.assertRaisesRegex(ContractError, "P31/adapter exited 23"):
                    run_suite._execute_repeat_impl(
                        suite=suite,
                        system=system,
                        truth_rows=truth_rows,
                        repeat_index=1,
                        run_root=run_root,
                        resolved_manifest=manifest,
                        p31_wrapper=root / "p31.sh",
                        mode="formal",
                    )
            self.assertEqual(len(stopped), 1)

    def test_formal_adapter_rejects_missing_cluster_receipts(self) -> None:
        args = type("Args", (), {
            "cluster_controller": None,
            "cluster_controller_sha256": None,
            "cluster_preflight": None,
            "cluster_preflight_sha256": None,
            "cluster_start_receipt": None,
            "cluster_start_receipt_sha256": None,
        })()
        with self.assertRaisesRegex(ContractError, "requires controller, preflight, start"):
            adapter.consume_formal_cluster_lifecycle(
                args,
                {"execution_mode": "formal"},
                Path("runtime.json"),
                Path("store.json"),
            )

    def test_materializes_one_independent_nebulagraph_repeat_binding(self) -> None:
        system = {
            "id": "nebulagraph",
            "adapter": {
                "args": [
                    "--store-manifest", "/repeat-01/store.json",
                    "--store-manifest-sha256", "1" * 64,
                ]
            },
            "repeat_bindings": [
                {
                    "repeat_index": index,
                    "store_root": {
                        "label": "nebulagraph",
                        "path": f"/repeat-{index:02d}/store",
                        "sha256": "a" * 64,
                    },
                    "logs_root": {
                        "label": "nebulagraph-logs",
                        "path": f"/repeat-{index:02d}/logs",
                    },
                    "containers": {
                        "graphd": f"nebula-graph-r{index:02d}",
                        "metad": f"nebula-meta-r{index:02d}",
                        "storaged": f"nebula-storage-r{index:02d}",
                    },
                    "store_manifest": {
                        "path": f"/repeat-{index:02d}/store.json",
                        "sha256": str(index) * 64,
                    },
                }
                for index in (1, 2, 3)
            ],
        }
        effective = run_suite.materialize_nebulagraph_repeat_binding(system, 2)
        self.assertEqual("/repeat-02/store", effective["store_roots"][0]["path"])
        self.assertEqual("/repeat-02/logs", effective["temp_roots"][0]["path"])
        self.assertEqual(
            ["nebula-graph-r02", "nebula-meta-r02", "nebula-storage-r02"],
            effective["containers"],
        )
        args = effective["adapter"]["args"]
        self.assertEqual("/repeat-02/store.json", args[args.index("--store-manifest") + 1])
        self.assertEqual("2" * 64, args[args.index("--store-manifest-sha256") + 1])

    def test_missing_start_receipt_after_attempt_publishes_blocked_terminal_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-recovery-blocked-") as raw:
            root = Path(raw)
            controller = NEBULA_DIR / "formal_cluster.py"
            receipt_root = root / "receipts"
            receipt_root.mkdir()
            lifecycle = {
                "controller": {"path": str(controller), "sha256": sha256_file(controller)},
                "receipt_paths": {
                    "preflight": str(receipt_root / "preflight.json"),
                    "partial_start": str(receipt_root / "partial.json"),
                    "start_cleanup": str(receipt_root / "cleanup.json"),
                    "start": str(receipt_root / "start.json"),
                },
                "_start_attempted": True,
                "preflight_path": receipt_root / "preflight.json",
                "preflight_sha256": "a" * 64,
                "preflight": {"state": "PASS"},
                "_store_lock": {"fd": 123},
            }
            result = run_suite.recover_nebulagraph_failure(
                system={"binary": {"path": sys.executable}},
                repeat_dir=root,
                lifecycle=lifecycle,
                primary_failure=ContractError("mock timeout"),
            )
            self.assertEqual("BLOCKED", result["state"])
            self.assertEqual("START_ATTEMPT_WITHOUT_RESOURCE_IDS", result["disposition"])
            self.assertFalse(result["name_lookup_used"])

    def test_graceful_stop_failure_runs_recovery_and_still_fails_repeat(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-stop-recovery-") as raw:
            root = Path(raw)
            suite, system, truth_rows, manifest = self._inputs(root)
            run_root = root / "run-id"
            run_root.mkdir()
            recovered: list[dict] = []

            def launch(**kwargs):
                return self._launched(kwargs["system"], kwargs["repeat_dir"])

            def recover(**kwargs):
                recovered.append(kwargs["lifecycle"])
                return {"state": "PASS"}

            with mock.patch.object(run_suite, "launch_nebulagraph_repeat", side_effect=launch), mock.patch.object(
                run_suite, "stop_nebulagraph_repeat", side_effect=ContractError("mock stop failure")
            ), mock.patch.object(
                run_suite, "recover_nebulagraph_failure", side_effect=recover
            ), mock.patch.object(
                run_suite, "p31_command", return_value=["mock-p31"]
            ), mock.patch.object(
                run_suite.subprocess, "run", return_value=subprocess.CompletedProcess(args=[], returncode=0)
            ):
                with self.assertRaisesRegex(ContractError, "exact-ID recovery state=PASS"):
                    run_suite._execute_repeat_impl(
                        suite=suite,
                        system=system,
                        truth_rows=truth_rows,
                        repeat_index=1,
                        run_root=run_root,
                        resolved_manifest=manifest,
                        p31_wrapper=root / "p31.sh",
                        mode="formal",
                    )
            self.assertEqual(1, len(recovered))

    def test_three_repeat_result_lineage_requires_unique_clone_and_docker_ids(self) -> None:
        results = []
        for repeat in (1, 2, 3):
            containers = {
                role: {
                    "container_id": f"{repeat}{index}" * 32,
                }
                for index, role in enumerate(("metad", "storaged", "graphd"), start=1)
            }
            spec = {"run_id": "run-id", "repeat_index": repeat}
            sealed = {
                "validated": {"repeat_index": repeat},
                "lineage": {
                    "clone_repeat_index": repeat,
                    "clone_run_id": "run-id",
                    "clone_receipt": {"path": f"/repeat-{repeat}/clone.json"},
                    "clone_target": {"path": f"/repeat-{repeat}/store"},
                    "clone_source": {"path": "/golden", "sha256": "a" * 64},
                },
                "artifacts": {
                    "dataset": {"path": "/dense", "sha256": "b" * 64}
                },
                "store": {"tree": {"sha256": "c" * 64}},
            }
            results.append(
                {
                    "system_id": "nebulagraph",
                    "adapter_provenance": {
                        "cluster_lifecycle": {
                            "preflight": {"receipt": {"spec": spec}},
                            "start": {
                                "receipt": {
                                    "containers": containers,
                                    "network": {"network_id": str(repeat) * 64},
                                }
                            },
                        },
                        "sealed_admission": {"receipt": sealed},
                    },
                    "nebulagraph_lifecycle": {
                        "stop": {"path": f"/repeat-{repeat}/stop.json", "sha256": str(repeat) * 64}
                    },
                }
            )
        cluster_module = mock.Mock()
        cluster_module.validate_repeat_isolation.return_value = {"state": "PASS"}
        with mock.patch.object(run_suite, "_nebulagraph_cluster_module", return_value=cluster_module):
            run_suite.validate_nebulagraph_cross_repeat(results, 3, [1, 2, 3])
            cluster_module.validate_repeat_isolation.assert_called_once()
            results[2]["nebulagraph_lifecycle"]["stop"] = copy.deepcopy(
                results[0]["nebulagraph_lifecycle"]["stop"]
            )
            with self.assertRaisesRegex(ContractError, "lineage is not independent"):
                run_suite.validate_nebulagraph_cross_repeat(results, 3, [1, 2, 3])
            results[2]["nebulagraph_lifecycle"]["stop"] = {
                "path": "/repeat-3/stop.json", "sha256": "3" * 64
            }
            results[2]["adapter_provenance"]["cluster_lifecycle"]["start"]["receipt"]["network"] = {
                "network_id": "1" * 64
            }
            with self.assertRaisesRegex(ContractError, "Docker IDs are reused"):
                run_suite.validate_nebulagraph_cross_repeat(results, 3, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
