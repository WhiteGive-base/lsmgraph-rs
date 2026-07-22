#!/usr/bin/env python3

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from validate_resource_run import sha256_file, validate_batch_integrity_guard


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ref(path):
    path = path.resolve()
    return {
        "path": str(path),
        "kind": "file",
        "exists": True,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


class BatchIntegrityV2Test(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.guard = self.root / "integrity-guard"
        self.guard.mkdir()
        self.lease = self.root / "lease.json"
        self.gate = self.root / "batch_gate_v2.py"
        self.binary = self.root / "binary"
        for path, text in (
            (self.lease, "{}\n"),
            (self.gate, "# gate\n"),
            (self.binary, "binary\n"),
        ):
            path.write_text(text, encoding="utf-8")
        base = dt.datetime(2026, 7, 22, 0, 0, tzinfo=dt.timezone.utc)
        stamp = lambda seconds: (base + dt.timedelta(seconds=seconds)).isoformat().replace(
            "+00:00", "Z"
        )
        self.stamp = stamp
        write_json(self.guard / "READY.json", {"ready_at_utc": stamp(2)})
        write_json(
            self.guard / "status.json",
            {"ended_at_utc": stamp(10), "binary_sha256": sha256_file(self.binary)},
        )
        (self.guard / "integrity-samples.tsv").write_text(
            "sample\n1\n", encoding="utf-8"
        )
        write_json(
            self.root / "command-release.json",
            {
                "schema_version": "cidr-command-release-v2",
                "state": "RELEASED",
                "released_at_utc": stamp(3),
            },
        )
        self.manifest = {
            "repo": {"git_sha": "1" * 40},
            "batch_gate": {
                "protocol_version": "short-clean-window-v2",
                "consumer": "P20",
                "lease": ref(self.lease),
                "gate_tool": ref(self.gate),
                "anchor_binary": ref(self.binary),
                "integrity_guard_required": True,
            },
        }
        self.execution = {
            "started_at_utc": stamp(0),
            "command_release_at_utc": stamp(3),
            "command_ended_at_utc": stamp(9),
            "guard_exit_code": 0,
        }
        self.collector_ready = {"ready_at_utc": stamp(1)}

    def tearDown(self):
        self.temporary.cleanup()

    def admission(self):
        return {
            "state": "PASS",
            "lease_sha256": sha256_file(self.lease),
            "guard_dir": str(self.guard),
        }

    def test_dual_ready_first_last_coverage_passes(self):
        errors = []
        completed = SimpleNamespace(
            returncode=0, stdout=json.dumps(self.admission()), stderr=""
        )
        with mock.patch("validate_resource_run.subprocess.run", return_value=completed):
            result = validate_batch_integrity_guard(
                self.root,
                self.manifest,
                self.execution,
                self.collector_ready,
                errors,
            )
        self.assertEqual(errors, [])
        self.assertEqual(result["state"], "PASS")
        self.assertEqual(set(result["evidence"]), {"release", "ready", "status", "samples"})

    def test_release_before_guard_ready_fails_closed(self):
        write_json(self.guard / "READY.json", {"ready_at_utc": self.stamp(4)})
        errors = []
        completed = SimpleNamespace(
            returncode=0, stdout=json.dumps(self.admission()), stderr=""
        )
        with mock.patch("validate_resource_run.subprocess.run", return_value=completed):
            result = validate_batch_integrity_guard(
                self.root,
                self.manifest,
                self.execution,
                self.collector_ready,
                errors,
            )
        self.assertEqual(result["state"], "FAILED")
        self.assertTrue(any("boundary coverage" in error for error in errors))

    def test_legacy_run_rejects_undeclared_guard_artifacts(self):
        errors = []
        result = validate_batch_integrity_guard(
            self.root, {"repo": {"git_sha": "1" * 40}}, {}, self.collector_ready, errors
        )
        self.assertIsNone(result)
        self.assertTrue(any("undeclared batch-guard" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
