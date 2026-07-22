#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import run_suite
from p10_contract import ContractError, read_p31_summary, sha256_file


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class BatchLeaseV2Test(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.lease = self.root / "lease.json"
        self.gate = self.root / "batch_gate_v2.py"
        self.binary = self.root / "binary"
        self.lease.write_text("{}\n", encoding="utf-8")
        self.gate.write_text("# fixture\n", encoding="utf-8")
        self.binary.write_text("binary\n", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def receipt(self):
        return {
            "schema_version": "cidr-batch-lease-admission-v2",
            "state": "PASS",
            "consumer": "P10",
            "lease": str(self.lease),
            "lease_sha256": sha256_file(self.lease),
            "binary_sha256": sha256_file(self.binary),
            "repo_head": "1" * 40,
            "issued_at_utc": "2026-07-22T00:00:00Z",
            "expires_at_utc": "2026-07-23T00:00:00Z",
            "remaining_seconds": 3600.0,
            "host": {"hostname": "fixture", "fingerprint_sha256": "2" * 64},
        }

    def test_receipt_identity_is_fail_closed(self):
        completed = SimpleNamespace(
            returncode=0, stdout=json.dumps(self.receipt()), stderr=""
        )
        with mock.patch("run_suite.subprocess.run", return_value=completed):
            receipt, command = run_suite.validate_batch_lease(
                self.lease, self.gate, self.binary
            )
        self.assertEqual(receipt["state"], "PASS")
        self.assertIn("validate-lease", command)
        invalid = self.receipt()
        invalid["remaining_seconds"] = 0
        completed.stdout = json.dumps(invalid)
        with mock.patch("run_suite.subprocess.run", return_value=completed):
            with self.assertRaises(ContractError):
                run_suite.validate_batch_lease(self.lease, self.gate, self.binary)

    def test_p31_done_binds_guard_validation(self):
        run_dir = self.root / "p31"
        run_dir.mkdir()
        manifest = {
            "state": "PASS",
            "performance_eligible_declared": False,
            "summary": {
                "resources": {
                    "peak_rss_bytes": 1,
                    "peak_pss_bytes": 1,
                    "process_user_cpu_s": 1.0,
                    "process_sys_cpu_s": 1.0,
                    "process_read_bytes": 1,
                    "process_write_bytes": 1,
                },
                "disk": {"peak_store_total_bytes": 1, "peak_temp_bytes": 0},
                "integrity_guard": {"state": "PASS"},
            },
        }
        validation = {"state": "PASS", "integrity_guard": {"state": "PASS"}}
        write_json(run_dir / "run-manifest.json", manifest)
        write_json(run_dir / "validation.json", validation)
        write_json(
            run_dir / "DONE",
            {
                "state": "PASS",
                "manifest_sha256": sha256_file(run_dir / "run-manifest.json"),
                "validation_sha256": sha256_file(run_dir / "validation.json"),
            },
        )
        summary = read_p31_summary(run_dir, performance_eligible=False)
        self.assertEqual(
            summary["validation_sha256"], sha256_file(run_dir / "validation.json")
        )
        validation["integrity_guard"]["state"] = "FAILED"
        write_json(run_dir / "validation.json", validation)
        with self.assertRaises(ContractError):
            read_p31_summary(run_dir, performance_eligible=False)

    def test_livegraph_p31_command_unions_named_inputs_dataset_mode_and_batch_guard(self):
        resolved_manifest = self.root / "resolved.json"
        resolved_manifest.write_text("{}\n", encoding="utf-8")
        suite = {
            "resources": {
                "device": "nvme1n1",
                "data_mount": "/data",
                "interval_s": 1,
                "disk_interval_s": 15,
                "min_samples": 2,
            },
            "dataset": {"path": "/data/dataset", "sha256": "3" * 64},
            "truth": {"path": "/data/truth.tsv", "sha256": "4" * 64},
            "protocol": {"adapter_process_timeout_s": 60},
        }
        system = {
            "id": "livegraph",
            "binary": {"path": "/bin/worker", "sha256": "5" * 64},
            "adapter": {"path": "/bin/adapter", "args": []},
            "store_roots": [{"label": "store", "path": "/data/store"}],
            "temp_roots": [],
            "containers": [],
            "extra_pids": [1234],
        }
        admission = {
            "protocol": "batch-lease-v2",
            "lease": self.lease,
            "gate_tool": self.gate,
            "anchor_binary": self.binary,
        }
        with mock.patch("run_suite.shutil.which", return_value="/usr/bin/timeout"):
            command = run_suite.p31_command(
                suite=suite,
                system=system,
                repeat_index=1,
                repeat_dir=self.root / "repeat",
                request_path=self.root / "request.json",
                adapter_output=self.root / "adapter-output",
                resolved_manifest=resolved_manifest,
                p31_wrapper=self.root / "p31.sh",
                mode="formal",
                extra_adapter_args=["--worker-pid", "1234"],
                named_inputs=["worker=/proc/1234/exe=" + "6" * 64],
                admission=admission,
            )
        delimiter = command.index("--")
        wrapper_args = command[:delimiter]
        for flag in (
            "--input",
            "--dataset-sha256-mode",
            "--batch-lease",
            "--batch-gate-tool",
            "--batch-consumer",
            "--batch-anchor-binary",
        ):
            self.assertEqual(wrapper_args.count(flag), 1, flag)
        self.assertEqual(
            wrapper_args[wrapper_args.index("--dataset-sha256-mode") + 1],
            "declared-no-read-v1",
        )
        self.assertEqual(wrapper_args[wrapper_args.index("--batch-consumer") + 1], "P10")


if __name__ == "__main__":
    unittest.main()
