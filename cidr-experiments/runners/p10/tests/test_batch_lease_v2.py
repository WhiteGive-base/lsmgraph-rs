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


def file_ref(path):
    path = Path(path).resolve()
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


class BatchLeaseV2Test(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.lease = self.root / "lease.json"
        self.gate = self.root / "batch_gate_v2.py"
        self.binary = self.root / "binary"
        self.gate.write_text("# fixture\n", encoding="utf-8")
        self.binary.write_text("binary\n", encoding="utf-8")
        p02b_root = self.root / "p02b"
        p02b_root.mkdir()
        run_root = p02b_root / "P02B-fixture"
        run_root.mkdir()
        validator = p02b_root / "validate_sentinel_result.py"
        extractor = p02b_root / "extract_run_metrics.py"
        calculator = p02b_root / "calculate_stability.py"
        for path in (validator, extractor, calculator):
            path.write_text("# fixture\n", encoding="utf-8")
        stability = {
            "schema_version": "p02b-sentinel-stability-v2",
            "state": "PASS",
            "method": "quantization-aware-tail-v1",
        }
        stability_path = run_root / "stability-result.json"
        write_json(stability_path, stability)
        unsigned_contract = {
            "schema_version": "p02b-gate-contract-v1",
            "method": "quantization-aware-tail-v1",
            "quantile": {"numerator": 99, "denominator": 100},
            "tail_bounds_us": {"lower": 150000, "upper": 250000},
            "sigma_multiplier": 3,
            "qps_cv_max": 0.07,
            "mean_storage_latency_cv_max": 0.07,
            "require_zero_overflow": True,
            "stability_result": file_ref(stability_path),
            "tools": {
                "extract_run_metrics": file_ref(extractor),
                "calculate_stability": file_ref(calculator),
                "validate_sentinel_result": file_ref(validator),
            },
        }
        self.gate_contract = {
            **unsigned_contract,
            "contract_sha256": run_suite._canonical_json_sha256(unsigned_contract),
        }
        result_path = run_root / "sentinel-result.json"
        write_json(
            result_path,
            {
                "schema_version": "p02b-sf10-sentinel-result-v2",
                "state": "PASS",
                "fixture_only": False,
                "formal_gate_eligible": True,
                "downstream_release_eligible": True,
                "gate_contract": self.gate_contract,
                "stability": stability,
            },
        )
        write_json(
            self.lease,
            {
                "schema_version": "cidr-batch-lease-v2",
                "p02b": {
                    "result": file_ref(result_path),
                    "validator": file_ref(validator),
                    "gate_contract": self.gate_contract,
                    "gate_contract_sha256": self.gate_contract["contract_sha256"],
                },
            },
        )

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
            "gate_contract": self.gate_contract,
            "gate_contract_sha256": self.gate_contract["contract_sha256"],
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
        invalid = self.receipt()
        invalid["gate_contract"] = dict(invalid["gate_contract"])
        invalid["gate_contract"]["method"] = "legacy-cv"
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
