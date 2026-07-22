#!/usr/bin/env python3
"""Static formal-request and template gates for the NebulaGraph P10 adapter."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

P10_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = P10_DIR.parents[2]
NEBULA_DIR = P10_DIR / "adapters" / "nebulagraph"
sys.path.insert(0, str(P10_DIR))
sys.path.insert(0, str(NEBULA_DIR))

import nebula_adapter as adapter  # noqa: E402
from p10_contract import (  # noqa: E402
    CLOCK_NAME,
    CONTRACT_VERSION,
    EXTERNAL_PROCESS_LIFETIME,
    INTERFACE_SCOPE,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    TRUTH_DIGEST_ALGORITHM,
    ContractError,
    sha256_file,
)
from run_suite import build_request  # noqa: E402


class NebulaGraphFormalRequestTest(unittest.TestCase):
    def _request(self, root: Path) -> tuple[dict, Path]:
        dataset = root / "edges-dense.txt"
        dataset.write_text("1\n0 1 0\n", encoding="utf-8")
        truth = root / "truth.tsv"
        truth.write_text(
            "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash\n"
            "0\t1\t0\t1\t0\t0\n",
            encoding="utf-8",
        )
        store = root / "store"
        store.mkdir()
        logs = root / "logs"
        logs.mkdir()
        receipts = root / "lifecycle"
        receipts.mkdir()
        controller = NEBULA_DIR / "formal_cluster.py"
        binary = Path(sys.executable).resolve()
        containers = ["nebula-r01-graph", "nebula-r01-meta", "nebula-r01-storage"]
        image_digests = [
            adapter.EXPECTED_IMAGES[role]["digest"]
            for role in ("graphd", "metad", "storaged")
        ]
        suite = {
            "suite_id": "nebulagraph-formal-request-static",
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
            },
        }
        system = {
            "id": "nebulagraph",
            "group": "client-server",
            "system_version": "NebulaGraph 3.8.0",
            "process_lifetime": EXTERNAL_PROCESS_LIFETIME,
            "binary": {"path": str(binary), "sha256": sha256_file(binary)},
            "runtime_libraries": [],
            "store_roots": [
                {"label": "nebulagraph", "path": str(store), "sha256": "a" * 64}
            ],
            "service_lifecycle": "external-prestarted",
            "containers": containers,
            "extra_pids": [],
            "image_digests": image_digests,
            "active_nebulagraph_lifecycle": {
                "controller": {
                    "path": str(controller.resolve()),
                    "sha256": sha256_file(controller),
                },
                "receipt_paths": {
                    "store_lock": str((receipts / "store.lock").resolve()),
                    "sealed_admission": str((receipts / "sealed.json").resolve()),
                    "preflight": str((receipts / "preflight.json").resolve()),
                    "partial_start": str((receipts / "partial.json").resolve()),
                    "start_cleanup": str((receipts / "cleanup.json").resolve()),
                    "start": str((receipts / "start.json").resolve()),
                    "live_gate": str((receipts / "start.json").resolve()),
                    "stop": str((receipts / "stop.json").resolve()),
                },
                "logs_root": str(logs.resolve()),
            },
        }
        request = build_request(suite, system, 1, "static-run", "formal")
        request_path = root / "adapter-request.json"
        request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        return request, request_path

    def test_authoritative_run_suite_request_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-formal-request-") as raw:
            root = Path(raw)
            request, request_path = self._request(root)
            with mock.patch.object(
                adapter, "FORMAL_DATASET_SHA256", request["dataset"]["sha256"]
            ):
                with mock.patch.object(
                    adapter, "FORMAL_TRUTH_SHA256", request["truth"]["sha256"]
                ):
                    with mock.patch.object(adapter, "FORMAL_QUERY_COUNT", 1):
                        parsed, rows, *_ = adapter.validate_request(
                            request_path, "nebulagraph"
                        )
            self.assertEqual(len(rows), 1)
            self.assertEqual(parsed["schema_version"], request["schema_version"])
            self.assertEqual(parsed["external_service"], request["external_service"])
            self.assertEqual(parsed["interface_scope"], INTERFACE_SCOPE)
            self.assertEqual(parsed["contract_version"], CONTRACT_VERSION)
            self.assertEqual(parsed["timing"]["clock"], CLOCK_NAME)
            self.assertEqual(parsed["timing"]["timing_boundary"], TIMING_BOUNDARY)
            self.assertEqual(
                parsed["timing"]["sequence_digest_algorithm"],
                SEQUENCE_DIGEST_ALGORITHM,
            )

    def test_external_image_role_order_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-formal-request-drift-") as raw:
            root = Path(raw)
            request, request_path = self._request(root)
            request["external_service"]["image_digests"].reverse()
            request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
            with mock.patch.object(
                adapter, "FORMAL_DATASET_SHA256", request["dataset"]["sha256"]
            ):
                with mock.patch.object(
                    adapter, "FORMAL_TRUTH_SHA256", request["truth"]["sha256"]
                ):
                    with mock.patch.object(adapter, "FORMAL_QUERY_COUNT", 1):
                        with self.assertRaisesRegex(
                            ContractError, "image digests/order drift"
                        ):
                            adapter.validate_request(request_path, "nebulagraph")

    def test_formal_p02b_requires_complete_sha_bound_argv(self) -> None:
        args = argparse.Namespace(
            p02b_result=None,
            p02b_result_sha256=None,
            p02b_validator=None,
            p02b_validator_sha256=None,
            p02b_binary=None,
            p02b_binary_sha256=None,
            p02b_max_age_seconds=21600,
        )
        with self.assertRaisesRegex(ContractError, "complete P02B"):
            adapter.consume_p02b(args, formal=True)

    def test_formal_p02b_binds_repo_binary_host_and_truth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-p02b-static-") as raw:
            root = Path(raw)
            truth = root / "truth.tsv"
            truth.write_text("truth\n", encoding="utf-8")
            binary = root / "lsmgraph"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            validator = root / "validator"
            validator.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            validator.chmod(0o755)
            marker = root / "PASS"
            marker.write_text("pass\n", encoding="utf-8")
            raw_manifest = root / "dataset-manifest.json"
            raw_manifest.write_text(
                json.dumps({"schema_version": "p02b-dataset-manifest-v1"}) + "\n",
                encoding="utf-8",
            )
            canonical = root / "provenance.json"
            canonical.write_text(
                json.dumps(
                    {
                        "files": {
                            "truth": {
                                "path": str(truth.resolve()),
                                "sha256": sha256_file(truth),
                            },
                            "dataset_manifest": {
                                "path": str(raw_manifest.resolve()),
                                "sha256": sha256_file(raw_manifest),
                            },
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            head = "b" * 40
            result = root / "sentinel-result.json"
            result.write_text(
                json.dumps(
                    {
                        "provenance": {
                            "repo_head": head,
                            "binary_sha256": sha256_file(binary),
                            "truth_sha256": sha256_file(truth),
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            host = {"hostname": "test-host", "fingerprint_sha256": "c" * 64}
            admission = {
                "state": "PASS",
                "consumer": "P10",
                "formal_required": True,
                "fixture_only": False,
                "scale": "sf10",
                "repo_root": str(root.resolve()),
                "repo_head": head,
                "binary_sha256": sha256_file(binary),
                "sentinel_result": str(result.resolve()),
                "sentinel_result_sha256": sha256_file(result),
                "host": host,
                "pass_marker": str(marker.resolve()),
                "pass_marker_sha256": sha256_file(marker),
                "provenance": str(canonical.resolve()),
                "provenance_sha256": sha256_file(canonical),
            }
            args = argparse.Namespace(
                p02b_result=result,
                p02b_result_sha256=sha256_file(result),
                p02b_validator=validator,
                p02b_validator_sha256=sha256_file(validator),
                p02b_binary=binary,
                p02b_binary_sha256=sha256_file(binary),
                p02b_max_age_seconds=21600,
            )
            request = {
                "truth": {"path": str(truth.resolve()), "sha256": sha256_file(truth)}
            }
            repo = {"root": str(root.resolve()), "head": head, "clean": True}
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(admission), stderr=""
            )
            with mock.patch.object(adapter, "p31_host_facts", return_value=host):
                with mock.patch.object(
                    adapter.subprocess, "run", return_value=completed
                ) as invoked:
                    receipt = adapter.consume_p02b(args, True, request, repo)
            self.assertEqual(receipt["state"], "PASS")
            command = invoked.call_args.args[0]
            self.assertIn("--expected-repo-root", command)
            self.assertIn("--expected-repo-head", command)
            self.assertIn("--expected-binary-sha256", command)
            self.assertIn("--max-age-seconds", command)

    def test_formal_template_has_no_silent_identity_placeholder(self) -> None:
        template = json.loads(
            (NEBULA_DIR / "formal-system.template.json").read_text(encoding="utf-8")
        )
        self.assertEqual(template["adapter"]["sha256"], "REPLACE_ADAPTER_SHA256")
        self.assertEqual(template["binary"]["path"], "/usr/bin/python3.8")
        self.assertTrue(all("REPLACE_RUN_TOKEN-r01" in name for name in template["containers"]))
        self.assertIn("repeat-01", template["store_roots"][0]["path"])
        args = template["adapter"]["args"]
        self.assertIn("repeat-01", args[args.index("--store-manifest") + 1])
        bindings = template["repeat_bindings"]
        self.assertEqual([1, 2, 3], [binding["repeat_index"] for binding in bindings])
        self.assertEqual(3, len({binding["network"] for binding in bindings}))
        self.assertEqual(3, len({binding["graph_port"] for binding in bindings}))
        self.assertEqual(
            9,
            len({name for binding in bindings for name in binding["containers"].values()}),
        )
        for option in (
            "--p02b-result-sha256",
            "--p02b-binary",
            "--p02b-binary-sha256",
            "--p02b-max-age-seconds",
            "--repo-root",
            "--cluster-controller",
            "--cluster-controller-sha256",
        ):
            self.assertIn(option, args)
        self.assertEqual(template["temp_roots"][0]["label"], "nebulagraph-logs")
        self.assertEqual(
            adapter.FORMAL_DATASET_SHA256,
            "9727fed3d710a3f0897b1ee7379411a091e0568bef05902de8b1787efc39d258",
        )


if __name__ == "__main__":
    unittest.main()
